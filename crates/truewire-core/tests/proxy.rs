//! Pin packages clause P18: an explicit proxy carries HTTP and WebSocket traffic alike.
//!
//! A caller that cannot set the process environment (an agent sandbox, a library embedding
//! the client) must still be able to route both transports through a proxy. So the proxy
//! variables are cleared first, and a local proxy that records what it was asked to reach
//! is given to the client only through `with_proxy`.
//!
//! The proxy speaks the two forms an HTTP proxy is sent: `CONNECT host:port` (every
//! WebSocket, and HTTP to `https://`) and an absolute-form request line such as
//! `GET http://host:port/path` (what reqwest sends for plain `http://`). Upstream are a
//! plain HTTP server answering `/hello` and a WebSocket server speaking a subscribe dialect.

use std::sync::{Arc, Mutex, Once};
use std::time::Duration;

use futures::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio_tungstenite::tungstenite::Message;
use truewire_core::http::RequestOptions;
use truewire_core::ws::{Data, Dialect, Incoming, Outgoing, Socket, SocketOptions, SubscribeOptions};
use truewire_core::{Error, HttpClient, HttpClientOptions};

const PROXY_VARIABLES: [&str; 4] = ["HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY"];
static CLEAR: Once = Once::new();

/// Nothing in the environment names a proxy, in either case. Once, before any test reads it.
fn no_proxy_environment() {
    CLEAR.call_once(|| {
        for name in PROXY_VARIABLES {
            std::env::remove_var(name);
            std::env::remove_var(name.to_ascii_lowercase());
        }
    });
}

/// The bytes up to and including the blank line ending a request or reply head; empty at EOF.
async fn read_head(stream: &mut TcpStream) -> Vec<u8> {
    let mut head = Vec::new();
    let mut byte = [0u8];
    while !head.ends_with(b"\r\n\r\n") {
        match stream.read(&mut byte).await {
            Ok(1) => head.push(byte[0]),
            _ => return Vec::new(),
        }
    }
    head
}

// -- the proxy ------------------------------------------------------------------------

/// A local HTTP proxy that tunnels `CONNECT` and forwards absolute-form requests.
#[derive(Clone, Default)]
struct Proxy {
    url: String,
    /// `METHOD target` of every request line, in arrival order.
    seen: Arc<Mutex<Vec<String>>>,
    /// The `Proxy-Authorization` of each request line.
    auth: Arc<Mutex<Vec<Option<String>>>>,
}

impl Proxy {
    async fn start() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
        let proxy = Proxy {
            url: format!("http://{}", listener.local_addr().expect("addr")),
            ..Default::default()
        };
        let recorder = proxy.clone();
        tokio::spawn(async move {
            while let Ok((client, _)) = listener.accept().await {
                tokio::spawn(recorder.clone().handle(client));
            }
        });
        proxy
    }

    fn seen(&self) -> Vec<String> {
        self.seen.lock().expect("seen").clone()
    }

    fn auth(&self) -> Vec<Option<String>> {
        self.auth.lock().expect("auth").clone()
    }

    async fn handle(self, mut client: TcpStream) {
        let head = String::from_utf8_lossy(&read_head(&mut client).await).into_owned();
        let mut lines = head.split("\r\n");
        let mut line = lines.next().unwrap_or_default().split(' ');
        let (method, target) = (line.next().unwrap_or_default(), line.next().unwrap_or_default());
        self.seen.lock().expect("seen").push(format!("{method} {target}"));
        let auth = lines.find_map(|h| {
            h.to_ascii_lowercase()
                .starts_with("proxy-authorization:")
                .then(|| h[20..].trim().to_string())
        });
        self.auth.lock().expect("auth").push(auth);
        let mut upstream = if method == "CONNECT" {
            let Ok(upstream) = TcpStream::connect(target).await else {
                return;
            };
            let _ = client.write_all(b"HTTP/1.1 200 Connection established\r\n\r\n").await;
            upstream
        } else {
            let url = url::Url::parse(target).expect("absolute-form target");
            let authority = format!("{}:{}", url.host_str().expect("host"), url.port().expect("port"));
            let Ok(mut upstream) = TcpStream::connect(authority).await else {
                return;
            };
            let _ = upstream
                .write_all(head.replacen(target, url.path(), 1).as_bytes())
                .await;
            upstream
        };
        let _ = tokio::io::copy_bidirectional(&mut client, &mut upstream).await;
    }
}

// -- the upstream ---------------------------------------------------------------------

/// A plain HTTP server answering every request with `hello`; its `host:port`.
async fn http_upstream() -> String {
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
    let host = listener.local_addr().expect("addr").to_string();
    tokio::spawn(async move {
        while let Ok((mut stream, _)) = listener.accept().await {
            tokio::spawn(async move {
                while !read_head(&mut stream).await.is_empty() {
                    let reply = "HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 5\r\n\r\nhello";
                    if stream.write_all(reply.as_bytes()).await.is_err() {
                        return;
                    }
                }
            });
        }
    });
    host
}

/// A WebSocket server that acks each subscribe and pushes two frames on its channel; its
/// `host:port`.
async fn ws_upstream() -> String {
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
    let host = listener.local_addr().expect("addr").to_string();
    tokio::spawn(async move {
        while let Ok((stream, _)) = listener.accept().await {
            tokio::spawn(async move {
                let Ok(mut ws) = tokio_tungstenite::accept_async(stream).await else {
                    return;
                };
                while let Some(Ok(Message::Text(text))) = ws.next().await {
                    let frame: Value = serde_json::from_str(&text).expect("json");
                    let channel = frame["channel"].clone();
                    let event = frame["event"].as_str().expect("event");
                    let mut out = vec![json!({"event": format!("{event}d"), "channel": channel})];
                    if event == "subscribe" {
                        out.extend((0..2).map(|n| json!({"channel": channel, "n": n})));
                    }
                    for frame in out {
                        if ws.send(Message::text(frame.to_string())).await.is_err() {
                            return;
                        }
                    }
                }
            });
        }
    });
    host
}

/// The dialect `ws_upstream` speaks: acks by arrival order, pushes by channel.
struct Feed;

impl Dialect for Feed {
    type Request = Value;
    type Reply = Value;
    type Notification = Value;
    type Params = Value;

    fn parse(&self, frame: Data) -> Result<Incoming<Value, Value>, Error> {
        let frame = frame.json()?;
        if frame.get("event").is_some() {
            return Ok(Incoming::Serial(frame));
        }
        let channel = frame["channel"].as_str().unwrap_or_default().to_string();
        Ok(Incoming::Push {
            channel,
            notification: frame,
        })
    }

    fn encode_request(&self, _id: u64, _request: &Value) -> Result<Data, Error> {
        Err(Error::logic("no correlated requests here"))
    }

    fn subscribe(&self, channel: &str, _params: Option<&Value>) -> Result<Outgoing<Value>, Error> {
        Ok(Outgoing::Serial(Data::from_json(
            &json!({"event": "subscribe", "channel": channel}),
        )))
    }

    fn unsubscribe(&self, channel: &str, _params: Option<&Value>) -> Result<Outgoing<Value>, Error> {
        Ok(Outgoing::Serial(Data::from_json(
            &json!({"event": "unsubscribe", "channel": channel}),
        )))
    }
}

// -- the tests ------------------------------------------------------------------------

fn http(proxy: &str) -> HttpClient {
    HttpClient::new(HttpClientOptions::default().with_proxy(proxy).expect("proxy"))
}

fn feed(url: &str, proxy: &str) -> Socket<Feed> {
    let options = SocketOptions::new(url).timeout(Duration::from_secs(5));
    Socket::new(Feed, options).with_proxy(proxy).expect("proxy")
}

async fn hello(http: &HttpClient, url: &str) -> Result<String, Error> {
    Ok(http.request("GET", url, RequestOptions::new()).await?.text())
}

async fn first_push(feed: &Socket<Feed>, channel: &str) -> Result<Value, Error> {
    let mut stream = feed.subscribe(channel, None, SubscribeOptions::new()).await?;
    let push = stream.next().await.expect("a push")?;
    stream.unsubscribe().await?;
    feed.close().await;
    Ok(push)
}

#[tokio::test]
async fn an_http_request_goes_through_the_given_proxy() {
    no_proxy_environment();
    let (proxy, upstream) = (Proxy::start().await, http_upstream().await);
    let text = hello(&http(&proxy.url), &format!("http://{upstream}/hello")).await;
    assert_eq!(text.expect("hello"), "hello");
    assert_eq!(proxy.seen(), [format!("GET http://{upstream}/hello")]);
}

#[tokio::test]
async fn a_websocket_subscription_goes_through_the_given_proxy_as_a_connect_tunnel() {
    no_proxy_environment();
    let (proxy, upstream) = (Proxy::start().await, ws_upstream().await);
    let push = first_push(&feed(&format!("ws://{upstream}/"), &proxy.url), "ticker").await;
    assert_eq!(push.expect("push"), json!({"channel": "ticker", "n": 0}));
    assert_eq!(proxy.seen(), [format!("CONNECT {upstream}")]);
}

/// The P18 check: one proxy URL, both transports, no proxy in the environment.
#[tokio::test]
async fn one_proxy_sees_both_transports() {
    no_proxy_environment();
    let (proxy, http_host, ws_host) = (Proxy::start().await, http_upstream().await, ws_upstream().await);
    let (client, socket) = (http(&proxy.url), feed(&format!("ws://{ws_host}/"), &proxy.url));
    assert_eq!(
        hello(&client, &format!("http://{http_host}/hello"))
            .await
            .expect("hello"),
        "hello"
    );
    let push = first_push(&socket, "trades").await.expect("push");
    assert_eq!(push, json!({"channel": "trades", "n": 0}));
    let mut seen = proxy.seen();
    seen.sort();
    assert_eq!(
        seen,
        [format!("CONNECT {ws_host}"), format!("GET http://{http_host}/hello")]
    );
    assert_eq!(socket.proxy().map(|u| u.to_string()), Some(format!("{}/", proxy.url)));
}

/// The upstreams speak no TLS, so both fail after the tunnel: what matters is that the
/// proxy was asked for it. The HTTP one waits for a request head that a TLS hello never
/// completes, hence the timeout.
#[cfg(any(feature = "rustls-tls", feature = "native-tls"))]
#[tokio::test]
async fn https_and_wss_are_tunnelled_with_connect() {
    no_proxy_environment();
    let (proxy, http_host, ws_host) = (Proxy::start().await, http_upstream().await, ws_upstream().await);
    let options = RequestOptions::new().timeout(Duration::from_secs(1));
    let error = http(&proxy.url)
        .request("GET", &format!("https://{http_host}/hello"), options)
        .await
        .unwrap_err();
    assert!(error.is_network(), "{error:?}");
    let error = feed(&format!("wss://{ws_host}/"), &proxy.url).open().await.unwrap_err();
    assert!(error.is_network(), "{error:?}");
    assert_eq!(
        proxy.seen(),
        [format!("CONNECT {http_host}"), format!("CONNECT {ws_host}")]
    );
}

#[tokio::test]
async fn the_credentials_in_the_proxy_url_are_sent_on_both_transports() {
    no_proxy_environment();
    let (proxy, http_host, ws_host) = (Proxy::start().await, http_upstream().await, ws_upstream().await);
    let with_credentials = proxy.url.replace("http://", "http://user:pa%20ss@");
    hello(&http(&with_credentials), &format!("http://{http_host}/hello"))
        .await
        .expect("hello");
    let socket = feed(&format!("ws://{ws_host}/"), &with_credentials);
    let mut redacted = socket.proxy().expect("proxy");
    assert_eq!(redacted.as_str(), format!("{}/", proxy.url));
    assert_eq!(redacted.username(), "");
    assert_eq!(redacted.password(), None);
    for shown in [redacted.to_string(), format!("{redacted:?}"), format!("{socket:?}")] {
        assert!(
            !shown.contains("user:") && !shown.contains("\"user\""),
            "username shown: {shown}"
        );
        assert!(!shown.contains("pa%20ss"), "encoded password shown: {shown}");
        assert!(!shown.contains("pa ss"), "password shown: {shown}");
    }
    // Inspecting or changing the returned copy must preserve connection credentials.
    redacted.set_port(Some(1)).expect("port");
    assert_eq!(socket.proxy().expect("proxy").as_str(), format!("{}/", proxy.url));
    first_push(&socket, "ticker").await.expect("push");
    let basic = Some("Basic dXNlcjpwYSBzcw==".to_string()); // base64("user:pa ss")
    assert_eq!(proxy.auth(), [basic.clone(), basic]);
}

#[tokio::test]
async fn without_a_proxy_or_with_an_empty_one_nothing_reaches_it() {
    no_proxy_environment();
    let (proxy, http_host, ws_host) = (Proxy::start().await, http_upstream().await, ws_upstream().await);
    for client in [HttpClient::default(), http("")] {
        hello(&client, &format!("http://{http_host}/hello"))
            .await
            .expect("hello");
    }
    let options = SocketOptions::new(format!("ws://{ws_host}/"));
    for socket in [
        Socket::new(Feed, options.clone()),
        feed(&format!("ws://{ws_host}/"), ""),
    ] {
        assert!(socket.proxy().is_none());
        first_push(&socket, "ticker").await.expect("push");
    }
    assert_eq!(proxy.seen(), Vec::<String>::new());
}

#[tokio::test]
async fn an_unreachable_proxy_fails_both_transports() {
    no_proxy_environment();
    let (http_host, ws_host) = (http_upstream().await, ws_upstream().await);
    let closed = TcpListener::bind("127.0.0.1:0").await.expect("bind");
    let dead = format!("http://{}", closed.local_addr().expect("addr"));
    drop(closed);
    let error = hello(&http(&dead), &format!("http://{http_host}/hello"))
        .await
        .unwrap_err();
    assert!(error.is_network(), "{error:?}");
    let error = feed(&format!("ws://{ws_host}/"), &dead).open().await.unwrap_err();
    assert!(error.is_network(), "{error:?}");
}

#[tokio::test]
async fn a_proxy_that_refuses_connect_is_a_network_error_naming_its_answer() {
    no_proxy_environment();
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
    let url = format!("http://{}", listener.local_addr().expect("addr"));
    tokio::spawn(async move {
        let (mut client, _) = listener.accept().await.expect("accept");
        read_head(&mut client).await;
        let _ = client
            .write_all(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
            .await;
    });
    let error = feed("wss://ws.example.invalid/v2", &url).open().await.unwrap_err();
    assert!(error.is_network(), "{error:?}");
    let chain = format!("{error:?}");
    assert!(chain.contains("407 Proxy Authentication Required"), "{chain}");
}

#[test]
fn misconfiguration_is_a_logic_error_that_never_repeats_the_url() {
    let prebuilt = HttpClientOptions {
        client: Some(reqwest::Client::new()),
        ..Default::default()
    };
    let error = prebuilt.with_proxy("http://127.0.0.1:3128").err().expect("refused");
    assert_eq!(error.code(), "logic");
    assert!(
        error.to_string().contains("pass `client` or a proxy, not both"),
        "{error}"
    );

    let not_a_url = "http://user:secret-password@proxy:port";
    let socks = "socks5://user:secret-password@127.0.0.1:1080";
    for (bad, http_message, ws_message) in [
        (not_a_url, "is not a URL", "is not a URL"),
        (
            socks,
            "must be an http:// or https:// URL, not socks5://",
            "must be an http:// URL, not socks5://",
        ),
    ] {
        let http_error = HttpClientOptions::default().with_proxy(bad).err().expect("refused");
        let ws_error = Socket::new(Feed, SocketOptions::new("ws://x.invalid/"))
            .with_proxy(bad)
            .unwrap_err();
        for (error, message) in [(http_error, http_message), (ws_error, ws_message)] {
            assert_eq!(error.code(), "logic");
            assert!(error.to_string().contains(message), "{error}");
            assert!(!format!("{error} {error:?}").contains("secret-password"), "{error:?}");
        }
    }
}
