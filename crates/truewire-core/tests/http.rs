//! Pins `HttpClient`: requests are built from `query`/`json`/`headers`, a failure to reach
//! the server is a `NetworkError`, and every exchange can be recorded at the wire level with
//! the response body still readable by the caller. The server is a minimal HTTP/1.1
//! listener on a loopback port.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::json;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use truewire_core::http::{query_from, HttpClient, HttpClientOptions, RequestOptions};
use truewire_core::Error;

/// One request as the server saw it.
#[derive(Debug, Clone)]
struct Seen {
    method: String,
    target: String,
    headers: Vec<(String, String)>,
    body: Vec<u8>,
}

impl Seen {
    fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(n, _)| n.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.as_str())
    }
}

/// What the server answers: status, body, extra headers, and an optional delay first.
#[derive(Debug, Clone)]
struct Reply {
    status: u16,
    body: String,
    delay: Option<Duration>,
}

fn ok(body: serde_json::Value) -> Reply {
    Reply {
        status: 200,
        body: body.to_string(),
        delay: None,
    }
}

type Handler = Arc<dyn Fn(&Seen) -> Reply + Send + Sync>;

struct Server {
    base: String,
    seen: Arc<Mutex<Vec<Seen>>>,
}

impl Server {
    async fn start(handler: Handler) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
        let base = format!("http://{}", listener.local_addr().expect("addr"));
        let seen: Arc<Mutex<Vec<Seen>>> = Arc::new(Mutex::new(Vec::new()));
        let log = seen.clone();
        tokio::spawn(async move {
            loop {
                let Ok((mut stream, _)) = listener.accept().await else {
                    return;
                };
                let handler = handler.clone();
                let log = log.clone();
                tokio::spawn(async move {
                    let mut buffer = Vec::new();
                    let mut chunk = [0u8; 4096];
                    let header_end = loop {
                        let n = stream.read(&mut chunk).await.unwrap_or(0);
                        if n == 0 {
                            return;
                        }
                        buffer.extend_from_slice(&chunk[..n]);
                        if let Some(i) = buffer.windows(4).position(|w| w == b"\r\n\r\n") {
                            break i + 4;
                        }
                    };
                    let head = String::from_utf8_lossy(&buffer[..header_end]).to_string();
                    let mut lines = head.lines();
                    let request_line = lines.next().unwrap_or_default();
                    let mut parts = request_line.split(' ');
                    let method = parts.next().unwrap_or_default().to_string();
                    let target = parts.next().unwrap_or_default().to_string();
                    let headers: Vec<(String, String)> = lines
                        .filter_map(|l| l.split_once(':'))
                        .map(|(n, v)| (n.trim().to_string(), v.trim().to_string()))
                        .collect();
                    let length: usize = headers
                        .iter()
                        .find(|(n, _)| n.eq_ignore_ascii_case("content-length"))
                        .and_then(|(_, v)| v.parse().ok())
                        .unwrap_or(0);
                    while buffer.len() < header_end + length {
                        let n = stream.read(&mut chunk).await.unwrap_or(0);
                        if n == 0 {
                            break;
                        }
                        buffer.extend_from_slice(&chunk[..n]);
                    }
                    let seen = Seen {
                        method,
                        target,
                        headers,
                        body: buffer[header_end..].to_vec(),
                    };
                    let reply = handler(&seen);
                    log.lock().expect("seen").push(seen);
                    if let Some(delay) = reply.delay {
                        tokio::time::sleep(delay).await;
                    }
                    let response = format!(
                        "HTTP/1.1 {} X\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{}",
                        reply.status,
                        reply.body.len(),
                        reply.body
                    );
                    let _ = stream.write_all(response.as_bytes()).await;
                    let _ = stream.shutdown().await;
                });
            }
        });
        Self { base, seen }
    }

    fn url(&self, path: &str) -> String {
        format!("{}{}", self.base, path)
    }

    fn seen(&self) -> Vec<Seen> {
        self.seen.lock().expect("seen").clone()
    }
}

fn client() -> HttpClient {
    HttpClient::new(HttpClientOptions {
        client: Some(reqwest::Client::builder().no_proxy().build().expect("client")),
        ..Default::default()
    })
}

fn client_with_timeout(timeout: Duration) -> HttpClient {
    HttpClient::new(HttpClientOptions {
        client: Some(reqwest::Client::builder().no_proxy().build().expect("client")),
        timeout: Some(timeout),
        on_exchange: None,
    })
}

#[tokio::test]
async fn appends_query_parameters_skipping_null_and_uppercases_the_method() {
    let server = Server::start(Arc::new(|_| ok(json!({})))).await;
    let query = query_from([
        ("symbol".to_string(), json!("BTC/USD")),
        ("limit".to_string(), json!(10)),
        ("open".to_string(), json!(true)),
        ("since".to_string(), json!(null)),
    ]);
    let response = client()
        .request("get", &server.url("/orders?page=1"), RequestOptions::new().query(query))
        .await
        .expect("reply");
    assert_eq!(response.status, 200);
    assert!(response.is_success());
    let seen = server.seen();
    assert_eq!(seen[0].method, "GET");
    assert_eq!(seen[0].target, "/orders?page=1&symbol=BTC%2FUSD&limit=10&open=true");
}

#[tokio::test]
async fn sends_json_as_an_application_json_body_keeping_an_explicit_content_type() {
    let server = Server::start(Arc::new(|_| ok(json!({})))).await;
    let client = client();
    client
        .request(
            "POST",
            &server.url("/orders"),
            RequestOptions::new().json(json!({"symbol": "BTC/USD", "qty": "1.5"})),
        )
        .await
        .expect("reply");
    client
        .request(
            "POST",
            &server.url("/orders"),
            RequestOptions::new()
                .json(json!({}))
                .header("content-type", "application/vnd.api+json"),
        )
        .await
        .expect("reply");
    let seen = server.seen();
    assert_eq!(seen[0].header("content-type"), Some("application/json"));
    assert_eq!(
        serde_json::from_slice::<serde_json::Value>(&seen[0].body).expect("json"),
        json!({"symbol": "BTC/USD", "qty": "1.5"})
    );
    assert_eq!(seen[1].header("content-type"), Some("application/vnd.api+json"));
}

#[tokio::test]
async fn sends_a_raw_body_and_headers_as_given() {
    let server = Server::start(Arc::new(|_| ok(json!({})))).await;
    client()
        .request(
            "PUT",
            &server.url("/x"),
            RequestOptions::new().body("a=1").header("x-api-key", "k"),
        )
        .await
        .expect("reply");
    let seen = server.seen();
    assert_eq!(seen[0].method, "PUT");
    assert_eq!(seen[0].body, b"a=1");
    assert_eq!(seen[0].header("x-api-key"), Some("k"));
}

#[tokio::test]
async fn returns_the_reply_whatever_its_status() {
    let server = Server::start(Arc::new(|_| Reply {
        status: 429,
        body: "nope".into(),
        delay: None,
    }))
    .await;
    let response = client()
        .request("GET", &server.url("/x"), RequestOptions::new())
        .await
        .expect("reply");
    assert_eq!(response.status, 429);
    assert!(!response.is_success());
    assert_eq!(response.text(), "nope");
    assert!(response.json().is_err());
    assert_eq!(response.header("content-type"), Some("application/json"));
}

#[tokio::test]
async fn a_connection_failure_is_a_network_error_naming_the_request() {
    // A port nothing listens on.
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
    let url = format!("http://{}/x", listener.local_addr().expect("addr"));
    drop(listener);
    let err = client()
        .request("get", &url, RequestOptions::new())
        .await
        .expect_err("refused");
    assert!(err.is_network(), "{err}");
    assert_eq!(err.message(), format!("Error sending request to GET {url}"));
    assert!(std::error::Error::source(&err).is_some());
}

#[tokio::test]
async fn a_timeout_is_a_network_error() {
    let server = Server::start(Arc::new(|_| Reply {
        status: 200,
        body: "{}".into(),
        delay: Some(Duration::from_millis(300)),
    }))
    .await;
    let err = client_with_timeout(Duration::from_millis(20))
        .request("GET", &server.url("/slow"), RequestOptions::new())
        .await
        .expect_err("times out");
    assert!(err.is_network(), "{err}");
    let err = client()
        .request(
            "GET",
            &server.url("/slow"),
            RequestOptions::new().timeout(Duration::from_millis(20)),
        )
        .await
        .expect_err("times out");
    assert!(err.message().starts_with("Error sending request"), "{err}");
}

#[tokio::test]
async fn a_bad_method_or_url_is_a_logic_error() {
    assert!(matches!(
        client().request("G ET", "http://x", RequestOptions::new()).await,
        Err(Error::Logic(_))
    ));
    assert!(matches!(
        client().request("GET", "not a url", RequestOptions::new()).await,
        Err(Error::Logic(_))
    ));
}

#[tokio::test]
async fn recording_records_every_exchange_in_order_at_the_wire_level() {
    let n = Arc::new(Mutex::new(0));
    let server = Server::start(Arc::new(move |_| {
        let mut n = n.lock().expect("n");
        *n += 1;
        ok(json!({"n": *n}))
    }))
    .await;
    let client = client();
    let rec = client.recording();
    let first = client
        .request("GET", &server.url("/a"), RequestOptions::new())
        .await
        .expect("reply");
    client
        .request("POST", &server.url("/b"), RequestOptions::new().json(json!({"x": 1})))
        .await
        .expect("reply");
    rec.stop();
    client
        .request("GET", &server.url("/c"), RequestOptions::new())
        .await
        .expect("reply");

    let exchanges = rec.exchanges();
    assert_eq!(rec.len(), 2);
    assert_eq!(
        exchanges
            .iter()
            .map(|x| x.request.url.path().to_string())
            .collect::<Vec<_>>(),
        ["/a", "/b"]
    );
    assert_eq!(first.json().expect("json"), json!({"n": 1}));
    assert_eq!(exchanges[0].response.json().expect("json"), json!({"n": 1}));
    assert_eq!(exchanges[1].request.method, reqwest::Method::POST);
    assert_eq!(
        serde_json::from_slice::<serde_json::Value>(exchanges[1].request.body.as_ref().expect("body")).expect("json"),
        json!({"x": 1})
    );
    assert_eq!(exchanges[1].response.status, 200);
    assert_eq!(rec.last().expect("last").request.url.path(), "/b");
}

#[tokio::test]
async fn dropping_a_recording_stops_it() {
    let server = Server::start(Arc::new(|_| ok(json!({})))).await;
    let client = client();
    let exchanges;
    {
        let rec = client.recording();
        client
            .request("GET", &server.url("/a"), RequestOptions::new())
            .await
            .expect("reply");
        exchanges = rec.exchanges();
    }
    client
        .request("GET", &server.url("/b"), RequestOptions::new())
        .await
        .expect("reply");
    assert_eq!(exchanges.len(), 1);
}

#[tokio::test]
async fn on_exchange_is_a_permanent_hook_and_recordings_may_overlap() {
    let server = Server::start(Arc::new(|_| ok(json!({})))).await;
    let seen = Arc::new(Mutex::new(Vec::new()));
    let log = seen.clone();
    let client = HttpClient::new(HttpClientOptions {
        client: Some(reqwest::Client::builder().no_proxy().build().expect("client")),
        timeout: None,
        on_exchange: Some(Arc::new(move |x| {
            log.lock().expect("log").push(x.request.url.path().to_string())
        })),
    });
    let a = client.recording();
    client
        .request("GET", &server.url("/1"), RequestOptions::new())
        .await
        .expect("reply");
    let b = client.recording();
    client
        .request("GET", &server.url("/2"), RequestOptions::new())
        .await
        .expect("reply");
    a.stop();
    client
        .request("GET", &server.url("/3"), RequestOptions::new())
        .await
        .expect("reply");
    b.stop();
    assert_eq!(seen.lock().expect("log").len(), 3);
    assert_eq!(a.len(), 2);
    assert_eq!(b.len(), 2);
}

#[tokio::test]
async fn records_nothing_when_the_request_fails() {
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
    let url = format!("http://{}/x", listener.local_addr().expect("addr"));
    drop(listener);
    let client = client();
    let rec = client.recording();
    assert!(client.request("GET", &url, RequestOptions::new()).await.is_err());
    assert!(rec.is_empty());
}
