//! Drive `Socket` through three dialects against an in-process WebSocket server speaking a
//! Kraken-like protocol: `req_id`-correlated replies, `channel` pushes, and (for the
//! serial dialect) an uncorrelated `event` acknowledgement. Pins request/reply
//! correlation, subscribe/push/unsubscribe, serial ordering, the lazy connection contract
//! and failure propagation.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use async_trait::async_trait;
use futures::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio::net::TcpListener;
use tokio::sync::mpsc;
use tokio_tungstenite::tungstenite::protocol::{frame::coding::CloseCode, CloseFrame};
use tokio_tungstenite::tungstenite::Message;
use truewire_core::ws::{Data, Dialect, Incoming, Link, Outgoing, Socket, SocketOptions, SubscribeOptions};
use truewire_core::{Error, Stream};

// -- the server -------------------------------------------------------------------------

enum Cmd {
    Send(String),
    Drop,
    Close(u16, String),
}

#[derive(Clone)]
struct Conn {
    received: Arc<Mutex<Vec<Value>>>,
    cmd: mpsc::UnboundedSender<Cmd>,
    open: Arc<Mutex<bool>>,
}

impl Conn {
    fn received(&self) -> Vec<Value> {
        self.received.lock().expect("received").clone()
    }

    fn reply(&self, message: Value) {
        let _ = self.cmd.send(Cmd::Send(message.to_string()));
    }

    fn drop_connection(&self) {
        let _ = self.cmd.send(Cmd::Drop);
    }

    fn close(&self, code: u16, reason: &str) {
        let _ = self.cmd.send(Cmd::Close(code, reason.to_string()));
    }

    fn is_open(&self) -> bool {
        *self.open.lock().expect("open")
    }
}

#[derive(Clone, Copy, Default)]
struct ServerOptions {
    /// Accept TCP, then close without a WebSocket handshake.
    refuse: bool,
    /// Accept TCP and never answer the handshake.
    hang: bool,
}

struct Server {
    url: String,
    conns: Arc<Mutex<Vec<Conn>>>,
}

/// A Kraken-like server: answers `method` calls, acks subscribe/unsubscribe, pushes on demand.
fn handle(request: &Value) -> Option<Value> {
    if let Some(event) = request.get("event").and_then(Value::as_str) {
        let channel = request.get("channel").cloned().unwrap_or(Value::Null);
        return Some(json!({"event": format!("{event}d"), "channel": channel, "ok": channel != "forbidden"}));
    }
    let base = json!({"req_id": request.get("req_id").cloned().unwrap_or(Value::Null), "method": request.get("method").cloned().unwrap_or(Value::Null)});
    let mut reply = base.as_object().expect("object").clone();
    let params = request.get("params").cloned().unwrap_or(json!({}));
    match request.get("method").and_then(Value::as_str) {
        Some("add") => {
            reply.insert("success".into(), json!(true));
            reply.insert(
                "result".into(),
                json!(params["a"].as_i64().unwrap_or(0) + params["b"].as_i64().unwrap_or(0)),
            );
        }
        Some("slow") => {
            reply.insert("success".into(), json!(true));
            reply.insert("result".into(), json!("slow"));
            reply.insert("__delay".into(), json!(15));
        }
        Some("silent") => return None,
        Some("fail") => {
            reply.insert("success".into(), json!(false));
            reply.insert("error".into(), json!("EGeneral:Invalid"));
        }
        Some("subscribe") => {
            let channel = params["channel"].as_str().unwrap_or_default().to_string();
            reply.insert("success".into(), json!(channel != "forbidden"));
            reply.insert("result".into(), json!({"channel": channel}));
            if channel == "forbidden" {
                reply.insert("error".into(), json!("ESubscription:Forbidden"));
            }
        }
        Some("unsubscribe") => {
            reply.insert("success".into(), json!(true));
            reply.insert("result".into(), json!({"channel": params["channel"]}));
        }
        _ => {
            reply.insert("success".into(), json!(false));
            reply.insert("error".into(), json!("Unknown method"));
        }
    }
    Some(Value::Object(reply))
}

impl Server {
    async fn start(options: ServerOptions) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
        let url = format!("ws://{}", listener.local_addr().expect("addr"));
        let conns: Arc<Mutex<Vec<Conn>>> = Arc::new(Mutex::new(Vec::new()));
        let registry = conns.clone();
        tokio::spawn(async move {
            loop {
                let Ok((stream, _)) = listener.accept().await else {
                    return;
                };
                if options.refuse {
                    drop(stream);
                    continue;
                }
                if options.hang {
                    tokio::spawn(async move {
                        let _keep = stream;
                        tokio::time::sleep(Duration::from_secs(60)).await;
                    });
                    continue;
                }
                let Ok(ws) = tokio_tungstenite::accept_async(stream).await else {
                    continue;
                };
                let (cmd_tx, mut cmd_rx) = mpsc::unbounded_channel();
                let conn = Conn {
                    received: Arc::new(Mutex::new(Vec::new())),
                    cmd: cmd_tx,
                    open: Arc::new(Mutex::new(true)),
                };
                registry.lock().expect("conns").push(conn.clone());
                tokio::spawn(async move {
                    let (mut sink, mut source) = ws.split();
                    loop {
                        tokio::select! {
                            message = source.next() => {
                                let Some(Ok(message)) = message else { break };
                                match message {
                                    Message::Text(text) => {
                                        let request: Value = serde_json::from_str(text.as_str()).expect("json");
                                        conn.received.lock().expect("received").push(request.clone());
                                        if let Some(reply) = handle(&request) {
                                            if let Some(delay) = reply.get("__delay").and_then(Value::as_u64) {
                                                let cmd = conn.cmd.clone();
                                                tokio::spawn(async move {
                                                    tokio::time::sleep(Duration::from_millis(delay)).await;
                                                    let _ = cmd.send(Cmd::Send(reply.to_string()));
                                                });
                                            } else if sink.send(Message::text(reply.to_string())).await.is_err() {
                                                break;
                                            }
                                        }
                                    }
                                    Message::Ping(payload) => {
                                        conn.received.lock().expect("received").push(json!({"__ping": payload.len()}));
                                        let _ = sink.send(Message::Pong(payload)).await;
                                    }
                                    Message::Close(_) => break,
                                    _ => {}
                                }
                            }
                            cmd = cmd_rx.recv() => {
                                match cmd {
                                    Some(Cmd::Send(text)) => {
                                        if sink.send(Message::text(text)).await.is_err() { break }
                                    }
                                    Some(Cmd::Drop) | None => break,
                                    Some(Cmd::Close(code, reason)) => {
                                        let _ = sink.send(Message::Close(Some(CloseFrame { code: CloseCode::from(code), reason: reason.into() }))).await;
                                        break;
                                    }
                                }
                            }
                        }
                    }
                    *conn.open.lock().expect("open") = false;
                });
            }
        });
        Self { url, conns }
    }

    fn sockets(&self) -> Vec<Conn> {
        self.conns.lock().expect("conns").clone()
    }

    fn last(&self) -> Conn {
        self.sockets().last().cloned().expect("a connection")
    }

    /// Push one JSON message to every open connection.
    fn push(&self, message: Value) {
        for conn in self.sockets() {
            if conn.is_open() {
                conn.reply(message.clone());
            }
        }
    }

    async fn settle(&self) {
        tokio::time::sleep(Duration::from_millis(20)).await;
    }

    /// Wait until the last connection is closed on the server side, or fail after a second.
    async fn closed(&self) {
        for _ in 0..100 {
            if !self.last().is_open() {
                return;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        panic!("the last connection is still open");
    }
}

fn options(server: &Server) -> SocketOptions {
    SocketOptions::new(&server.url).timeout(Duration::from_millis(500))
}

fn sent_methods(conn: &Conn) -> Vec<String> {
    conn.received()
        .iter()
        .filter_map(|m| m.get("method").and_then(Value::as_str).map(str::to_string))
        .collect()
}

async fn take<N: Send + 'static, R: Send + Unpin + 'static>(stream: &mut Stream<N, R>, n: usize) -> Vec<N> {
    let mut out = Vec::new();
    while out.len() < n {
        match stream.next().await {
            Some(Ok(item)) => out.push(item),
            Some(Err(e)) => panic!("stream failed: {e}"),
            None => break,
        }
    }
    out
}

// -- Rpc: a dialect correlating replies by req_id ---------------------------------------

#[derive(Debug, Clone, PartialEq, serde::Serialize)]
struct Request {
    method: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    params: Option<Value>,
}

fn call(method: &str, params: Option<Value>) -> Request {
    Request {
        method: method.into(),
        params,
    }
}

struct Calc;

#[async_trait]
impl Dialect for Calc {
    type Request = Request;
    type Reply = Value;
    type Notification = Value;
    type Params = Value;

    fn parse(&self, frame: Data) -> Result<Incoming<Value, Value>, Error> {
        let frame = frame.json()?;
        match frame.get("req_id").and_then(Value::as_u64) {
            Some(id) => Ok(Incoming::Reply { id, reply: frame }),
            None => Ok(Incoming::Ignore),
        }
    }

    fn encode_request(&self, id: u64, request: &Request) -> Result<Data, Error> {
        let mut frame = truewire_core::dump(request)?;
        frame["req_id"] = json!(id);
        Ok(Data::from_json(&frame))
    }

    fn subscribe(&self, _channel: &str, _params: Option<&Value>) -> Result<Outgoing<Request>, Error> {
        Err(Error::logic("no subscriptions here"))
    }

    fn unsubscribe(&self, _channel: &str, _params: Option<&Value>) -> Result<Outgoing<Request>, Error> {
        Err(Error::logic("no subscriptions here"))
    }
}

#[tokio::test]
async fn rpc_correlates_concurrent_requests_by_id_out_of_order() {
    let server = Server::start(ServerOptions::default()).await;
    let rpc = Socket::new(Calc, options(&server));
    let (slow_call, add_call) = (call("slow", None), call("add", Some(json!({"a": 1, "b": 2}))));
    let (slow, sum) = tokio::join!(rpc.rpc_request(&slow_call), rpc.rpc_request(&add_call));
    assert_eq!(slow.expect("slow")["result"], "slow");
    assert_eq!(sum.expect("sum")["result"], 3);
    let ids: Vec<u64> = server
        .last()
        .received()
        .iter()
        .map(|m| m["req_id"].as_u64().expect("id"))
        .collect();
    assert_eq!(ids, [0, 1]);
    assert_eq!(rpc.open().await.expect("open").pending_replies(), 0);
    assert_eq!(rpc.next_id(), 2);
    rpc.close().await;
}

#[tokio::test]
async fn rpc_opens_the_connection_lazily_once_on_the_first_request() {
    let server = Server::start(ServerOptions::default()).await;
    let rpc = Socket::new(Calc, options(&server));
    assert!(server.sockets().is_empty());
    assert!(!rpc.is_open().await);
    let (one, two) = (
        call("add", Some(json!({"a": 1, "b": 1}))),
        call("add", Some(json!({"a": 2, "b": 2}))),
    );
    let (a, b) = tokio::join!(rpc.rpc_request(&one), rpc.rpc_request(&two));
    assert_eq!(a.expect("a")["result"], 2);
    assert_eq!(b.expect("b")["result"], 4);
    assert_eq!(server.sockets().len(), 1);
    assert!(rpc.is_open().await);
    rpc.close().await;
    server.closed().await;
    assert!(!rpc.is_open().await);
}

#[tokio::test]
async fn rpc_ignores_a_reply_with_an_unknown_id_and_frames_that_are_not_replies() {
    let server = Server::start(ServerOptions::default()).await;
    let rpc = Socket::new(Calc, options(&server));
    rpc.open().await.expect("open");
    server
        .last()
        .reply(json!({"req_id": 99, "method": "x", "success": true}));
    server.last().reply(json!({"channel": "heartbeat"}));
    server.settle().await;
    assert_eq!(
        rpc.rpc_request(&call("add", Some(json!({"a": 2, "b": 3}))))
            .await
            .expect("sum")["result"],
        5
    );
    rpc.close().await;
}

#[tokio::test]
async fn rpc_a_dropped_connection_fails_pending_requests_and_the_next_request_reconnects() {
    let server = Server::start(ServerOptions::default()).await;
    let rpc = Socket::new(Calc, options(&server));
    let pending = tokio::spawn({
        let rpc = Arc::new(rpc);
        let inner = rpc.clone();
        async move { (inner.rpc_request(&call("silent", None)).await, rpc) }
    });
    server.settle().await;
    server.last().drop_connection();
    let (result, rpc) = pending.await.expect("task");
    let err = result.expect_err("dropped");
    assert!(err.is_network(), "{err}");
    assert_eq!(
        rpc.rpc_request(&call("add", Some(json!({"a": 1, "b": 1}))))
            .await
            .expect("sum")["result"],
        2
    );
    assert_eq!(server.sockets().len(), 2);
    rpc.close().await;
}

#[tokio::test]
async fn rpc_close_fails_pending_requests_and_clears_the_reply_table() {
    let server = Server::start(ServerOptions::default()).await;
    let rpc = Arc::new(Socket::new(Calc, options(&server)));
    let pending = tokio::spawn({
        let rpc = rpc.clone();
        async move { rpc.rpc_request(&call("silent", None)).await }
    });
    server.settle().await;
    let link = rpc.open().await.expect("open");
    assert_eq!(link.pending_replies(), 1);
    rpc.close().await;
    let err = pending.await.expect("task").expect_err("closed");
    assert_eq!(err.to_string(), "NetworkError: Connection closed");
    assert_eq!(link.pending_replies(), 0);
}

#[tokio::test]
async fn rpc_a_refused_connection_is_a_network_error_and_the_next_open_retries() {
    let server = Server::start(ServerOptions {
        refuse: true,
        ..Default::default()
    })
    .await;
    let rpc = Socket::new(Calc, options(&server));
    let err = rpc.rpc_request(&call("add", None)).await.expect_err("refused");
    assert!(err.is_network(), "{err}");
    assert!(err.message().starts_with("Failed to connect to ws://"), "{err}");
    assert!(!rpc.is_open().await);
}

#[tokio::test]
async fn rpc_a_connection_that_never_opens_times_out_into_a_network_error() {
    let server = Server::start(ServerOptions {
        hang: true,
        ..Default::default()
    })
    .await;
    let rpc = Socket::new(Calc, SocketOptions::new(&server.url).timeout(Duration::from_millis(30)));
    let err = rpc.open().await.expect_err("hangs");
    assert!(err.message().starts_with("Failed to connect to"), "{err}");
}

#[tokio::test]
async fn rpc_a_server_close_frame_fails_pending_waits_with_its_code_and_reason() {
    let server = Server::start(ServerOptions::default()).await;
    let rpc = Arc::new(Socket::new(Calc, options(&server)));
    let link = rpc.open().await.expect("open");
    let waiting = tokio::spawn({
        let link = link.clone();
        async move { link.wait(std::future::pending::<()>()).await }
    });
    server.settle().await;
    server.last().close(1008, "gone");
    let err = waiting.await.expect("task").expect_err("closed");
    assert_eq!(err.to_string(), "NetworkError: Connection closed (1008 gone)");
    assert!(link.is_closed());
    let sum = rpc
        .rpc_request(&call("add", Some(json!({"a": 1, "b": 1}))))
        .await
        .expect("reconnects");
    assert_eq!(sum["result"], 2);
    rpc.close().await;
}

#[tokio::test]
async fn rpc_a_frame_the_dialect_rejects_fails_the_connection() {
    let server = Server::start(ServerOptions::default()).await;
    let rpc = Socket::new(Calc, options(&server));
    let link = rpc.open().await.expect("open");
    let _ = server.last().cmd.send(Cmd::Send("{not json".into()));
    server.settle().await;
    assert!(link.is_closed());
    let err = link.wait(std::future::pending::<()>()).await.expect_err("failed");
    assert!(err.is_validation(), "{err}");
    server.closed().await;
    rpc.close().await;
}

#[tokio::test]
async fn rpc_pings_on_the_interval_with_a_protocol_ping_when_the_dialect_has_none() {
    let server = Server::start(ServerOptions::default()).await;
    let rpc = Socket::new(Calc, options(&server).ping_interval(Duration::from_millis(10)));
    rpc.open().await.expect("open");
    tokio::time::sleep(Duration::from_millis(60)).await;
    let pings = server
        .last()
        .received()
        .iter()
        .filter(|m| m.get("__ping").is_some())
        .count();
    assert!(pings >= 2, "{pings}");
    rpc.close().await;
}

// -- Streams + serial: acks carry no id, matched by arrival order -----------------------

struct Feed;

#[async_trait]
impl Dialect for Feed {
    type Request = Request;
    type Reply = Value;
    type Notification = Value;
    type Params = Value;

    fn parse(&self, frame: Data) -> Result<Incoming<Value, Value>, Error> {
        let frame = frame.json()?;
        if frame.get("event").is_some() {
            return Ok(Incoming::Serial(frame));
        }
        match frame.get("channel").and_then(Value::as_str) {
            Some(channel) => Ok(Incoming::Push {
                channel: channel.to_string(),
                notification: frame,
            }),
            None => Ok(Incoming::Ignore),
        }
    }

    fn encode_request(&self, _id: u64, _request: &Request) -> Result<Data, Error> {
        Err(Error::logic("no correlated requests here"))
    }

    fn subscribe(&self, channel: &str, params: Option<&Value>) -> Result<Outgoing<Request>, Error> {
        let mut frame = json!({"event": "subscribe", "channel": channel});
        if let Some(Value::Object(params)) = params {
            frame.as_object_mut().expect("object").extend(params.clone());
        }
        Ok(Outgoing::Serial(Data::from_json(&frame)))
    }

    fn unsubscribe(&self, channel: &str, params: Option<&Value>) -> Result<Outgoing<Request>, Error> {
        let mut frame = json!({"event": "unsubscribe", "channel": channel});
        if let Some(Value::Object(params)) = params {
            frame.as_object_mut().expect("object").extend(params.clone());
        }
        Ok(Outgoing::Serial(Data::from_json(&frame)))
    }

    fn check_ack(&self, channel: &str, reply: &Value) -> Result<(), Error> {
        if reply["ok"] == false {
            return Err(Error::api(format!("subscribe {channel:?} refused")));
        }
        Ok(())
    }

    fn ping(&self) -> Option<Data> {
        Some(Data::from_json(&json!({"event": "ping"})))
    }
}

#[tokio::test]
async fn streams_subscribe_yields_pushes_in_order_until_unsubscribed() {
    let server = Server::start(ServerOptions::default()).await;
    let feed = Socket::new(Feed, options(&server));
    let mut stream = feed
        .subscribe("ticker", Some(json!({"symbol": "BTC/USD"})), SubscribeOptions::new())
        .await
        .expect("subscribed");
    assert_eq!(
        stream.reply,
        Some(json!({"event": "subscribed", "channel": "ticker", "ok": true}))
    );
    assert_eq!(
        server.last().received(),
        [json!({"event": "subscribe", "channel": "ticker", "symbol": "BTC/USD"})]
    );
    let link = feed.open().await.expect("open");
    assert_eq!(link.subscribed_channels(), ["ticker"]);

    server.push(json!({"channel": "ticker", "data": 1}));
    server.push(json!({"channel": "ticker", "data": 2}));
    server.push(json!({"channel": "book", "data": "ignored"}));
    server.push(json!({"channel": "ticker", "data": 3}));
    let received = take(&mut stream, 3).await;
    assert_eq!(
        received.iter().map(|p| p["data"].clone()).collect::<Vec<_>>(),
        [json!(1), json!(2), json!(3)]
    );

    assert_eq!(
        stream.unsubscribe().await.expect("unsubscribed"),
        Some(json!({"event": "unsubscribed", "channel": "ticker", "ok": true}))
    );
    assert!(link.subscribed_channels().is_empty());
    assert_eq!(stream.unsubscribe().await.expect("again"), None);
    assert_eq!(server.last().received().len(), 2);
    // The stream ends once unsubscribed.
    assert!(stream.next().await.is_none());
    feed.close().await;
}

#[tokio::test]
async fn streams_message_key_and_request_channel_route_one_wire_channel_to_several_local_ones() {
    let server = Server::start(ServerOptions::default()).await;
    let feed = Socket::new(Feed, options(&server));
    let key = |push: &Value| format!("ticker:{}", push["symbol"].as_str().unwrap_or_default());
    let mut btc = feed
        .subscribe(
            "ticker:BTC/USD",
            Some(json!({"symbol": "BTC/USD"})),
            SubscribeOptions::new().request_channel("ticker").message_key(key),
        )
        .await
        .expect("btc");
    let mut eth = feed
        .subscribe(
            "ticker:ETH/USD",
            Some(json!({"symbol": "ETH/USD"})),
            SubscribeOptions::new().request_channel("ticker").message_key(key),
        )
        .await
        .expect("eth");
    let channels: Vec<Value> = server.last().received().iter().map(|m| m["channel"].clone()).collect();
    assert_eq!(channels, [json!("ticker"), json!("ticker")]);
    server.push(json!({"channel": "ticker", "symbol": "ETH/USD", "data": "e"}));
    server.push(json!({"channel": "ticker", "symbol": "BTC/USD", "data": "b"}));
    assert_eq!(take(&mut btc, 1).await[0]["data"], "b");
    assert_eq!(take(&mut eth, 1).await[0]["data"], "e");
    feed.close().await;
}

#[tokio::test]
async fn streams_subscribing_twice_to_one_local_channel_is_a_logic_error() {
    let server = Server::start(ServerOptions::default()).await;
    let feed = Socket::new(Feed, options(&server));
    let _first = feed
        .subscribe("ticker", None, SubscribeOptions::new())
        .await
        .expect("first");
    let err = feed
        .subscribe("ticker", None, SubscribeOptions::new())
        .await
        .expect_err("second");
    assert!(err.is_logic(), "{err}");
    feed.close().await;
}

#[tokio::test]
async fn streams_a_refused_subscription_leaves_no_channel_behind() {
    let server = Server::start(ServerOptions::default()).await;
    let feed = Socket::new(Feed, options(&server));
    let err = feed
        .subscribe("forbidden", None, SubscribeOptions::new())
        .await
        .expect_err("refused");
    assert!(err.message().contains("refused"), "{err}");
    assert!(feed.open().await.expect("open").subscribed_channels().is_empty());
    assert!(feed
        .subscribe("forbidden", None, SubscribeOptions::new())
        .await
        .is_err());
    feed.close().await;
}

#[tokio::test]
async fn streams_map_and_filter() {
    let server = Server::start(ServerOptions::default()).await;
    let feed = Socket::new(Feed, options(&server));
    let stream = feed
        .subscribe("ticker", None, SubscribeOptions::new())
        .await
        .expect("subscribed");
    let reply = stream.reply.clone();
    let mut doubled = stream
        .map(|p: Value| Ok(p["data"].as_i64().unwrap_or(0)))
        .filter(|n| n % 2 == 0)
        .map(|n| Ok(n * 2));
    for i in 1..=4 {
        server.push(json!({"channel": "ticker", "data": i}));
    }
    assert_eq!(take(&mut doubled, 2).await, [4, 8]);
    assert_eq!(doubled.reply, reply);
    doubled.unsubscribe().await.expect("unsubscribed");
    assert!(feed.open().await.expect("open").subscribed_channels().is_empty());
    feed.close().await;
}

#[tokio::test]
async fn streams_a_dropped_connection_ends_iteration_with_a_network_error_and_forgets_the_channel() {
    let server = Server::start(ServerOptions::default()).await;
    let feed = Socket::new(Feed, options(&server));
    let mut stream = feed
        .subscribe("ticker", None, SubscribeOptions::new())
        .await
        .expect("subscribed");
    let link = feed.open().await.expect("open");
    server.last().drop_connection();
    let item = stream.next().await.expect("an item");
    assert!(item.is_err_and(|e| e.is_network()));
    assert!(stream.next().await.is_none());
    assert!(link.subscribed_channels().is_empty());
    feed.close().await;
}

#[tokio::test]
async fn streams_close_clears_every_subscription() {
    let server = Server::start(ServerOptions::default()).await;
    let feed = Socket::new(Feed, options(&server));
    let _a = feed.subscribe("a", None, SubscribeOptions::new()).await.expect("a");
    let _b = feed.subscribe("b", None, SubscribeOptions::new()).await.expect("b");
    let link = feed.open().await.expect("open");
    assert_eq!(link.subscribed_channels().len(), 2);
    feed.close().await;
    assert!(link.subscribed_channels().is_empty());
    assert!(!feed.is_open().await);
}

#[tokio::test]
async fn streams_the_dialect_s_ping_frame_is_sent_on_the_interval() {
    let server = Server::start(ServerOptions::default()).await;
    let feed = Socket::new(Feed, options(&server).ping_interval(Duration::from_millis(10)));
    feed.open().await.expect("open");
    tokio::time::sleep(Duration::from_millis(60)).await;
    let pings = server.last().received().iter().filter(|m| m["event"] == "ping").count();
    assert!(pings >= 2, "{pings}");
    feed.close().await;
}

// -- SerialReplies ----------------------------------------------------------------------

#[tokio::test]
async fn serial_serializes_concurrent_requests_and_matches_replies_by_arrival_order() {
    let server = Server::start(ServerOptions::default()).await;
    let feed = Arc::new(Socket::new(Feed, options(&server)));
    let link = feed.open().await.expect("open");
    let a = tokio::spawn({
        let link = link.clone();
        async move {
            link.serial_request(Data::from_json(&json!({"event": "subscribe", "channel": "a"})))
                .await
        }
    });
    let b = tokio::spawn({
        let link = link.clone();
        async move {
            link.serial_request(Data::from_json(&json!({"event": "subscribe", "channel": "b"})))
                .await
        }
    });
    let (a, b) = tokio::join!(a, b);
    let (a, b) = (a.expect("task").expect("a"), b.expect("task").expect("b"));
    assert_eq!(a["channel"], "a");
    assert_eq!(b["channel"], "b");
    let sent: Vec<Value> = server.last().received().iter().map(|m| m["channel"].clone()).collect();
    assert_eq!(sent.len(), 2);
    feed.close().await;
}

#[tokio::test]
async fn serial_a_failed_reply_wait_releases_the_lock() {
    let server = Server::start(ServerOptions::default()).await;
    let feed = Arc::new(Socket::new(Feed, options(&server)));
    let pending = tokio::spawn({
        let feed = feed.clone();
        async move { feed.serial_request(Data::from_json(&json!({"quiet": true}))).await }
    });
    server.settle().await;
    server.last().drop_connection();
    let err = pending.await.expect("task").expect_err("dropped");
    assert!(err.is_network(), "{err}");
    let reply = feed
        .serial_request(Data::from_json(&json!({"event": "subscribe", "channel": "c"})))
        .await
        .expect("next connection serves");
    assert_eq!(reply["channel"], "c");
    feed.close().await;
}

// -- StreamsRpc: subscribe acks and method replies all correlated by req_id --------------

struct Spot;

#[async_trait]
impl Dialect for Spot {
    type Request = Request;
    type Reply = Value;
    type Notification = Value;
    type Params = Value;

    fn parse(&self, frame: Data) -> Result<Incoming<Value, Value>, Error> {
        let frame = frame.json()?;
        if let Some(id) = frame.get("req_id").and_then(Value::as_u64) {
            return Ok(Incoming::Reply { id, reply: frame });
        }
        match frame.get("channel").and_then(Value::as_str) {
            Some(channel) => Ok(Incoming::Push {
                channel: channel.to_string(),
                notification: frame,
            }),
            None => Ok(Incoming::Ignore),
        }
    }

    fn encode_request(&self, id: u64, request: &Request) -> Result<Data, Error> {
        let mut frame = truewire_core::dump(request)?;
        frame["req_id"] = json!(id);
        Ok(Data::from_json(&frame))
    }

    fn subscribe(&self, channel: &str, params: Option<&Value>) -> Result<Outgoing<Request>, Error> {
        let mut p = json!({"channel": channel});
        if let Some(Value::Object(params)) = params {
            p.as_object_mut().expect("object").extend(params.clone());
        }
        Ok(Outgoing::Rpc(call("subscribe", Some(p))))
    }

    fn unsubscribe(&self, channel: &str, params: Option<&Value>) -> Result<Outgoing<Request>, Error> {
        let mut p = json!({"channel": channel});
        if let Some(Value::Object(params)) = params {
            p.as_object_mut().expect("object").extend(params.clone());
        }
        Ok(Outgoing::Rpc(call("unsubscribe", Some(p))))
    }

    fn check_ack(&self, _channel: &str, reply: &Value) -> Result<(), Error> {
        if reply["success"] == false {
            return Err(Error::api(reply["error"].as_str().unwrap_or("failed").to_string()));
        }
        Ok(())
    }
}

#[tokio::test]
async fn streams_rpc_serves_rpc_calls_and_subscriptions_on_one_connection() {
    let server = Server::start(ServerOptions::default()).await;
    let spot = Socket::new(Spot, options(&server));
    let mut stream = spot
        .subscribe("ticker", Some(json!({"symbol": "BTC/USD"})), SubscribeOptions::new())
        .await
        .expect("subscribed");
    assert_eq!(
        stream.reply.as_ref().expect("reply")["result"],
        json!({"channel": "ticker"})
    );
    let sum = spot
        .rpc_request(&call("add", Some(json!({"a": 20, "b": 22}))))
        .await
        .expect("sum");
    assert_eq!(sum["result"], 42);
    server.push(json!({"channel": "ticker", "data": "tick"}));
    assert_eq!(take(&mut stream, 1).await[0]["data"], "tick");
    let ack = stream.unsubscribe().await.expect("unsubscribed").expect("a reply");
    assert_eq!(ack["result"], json!({"channel": "ticker"}));
    assert_eq!(server.sockets().len(), 1);
    assert_eq!(sent_methods(&server.last()), ["subscribe", "add", "unsubscribe"]);
    spot.close().await;
}

#[tokio::test]
async fn streams_rpc_a_refused_subscription_rejects_and_is_cleaned_up() {
    let server = Server::start(ServerOptions::default()).await;
    let spot = Socket::new(Spot, options(&server));
    let err = spot
        .subscribe("forbidden", None, SubscribeOptions::new())
        .await
        .expect_err("refused");
    assert_eq!(err.to_string(), "ApiError: ESubscription:Forbidden");
    let link = spot.open().await.expect("open");
    assert!(link.subscribed_channels().is_empty());
    assert_eq!(link.pending_replies(), 0);
    spot.close().await;
}

#[tokio::test]
async fn streams_rpc_close_clears_subscriptions_and_replies() {
    let server = Server::start(ServerOptions::default()).await;
    let spot = Arc::new(Socket::new(Spot, options(&server)));
    let _stream = spot
        .subscribe("ticker", None, SubscribeOptions::new())
        .await
        .expect("subscribed");
    let pending = tokio::spawn({
        let spot = spot.clone();
        async move { spot.rpc_request(&call("silent", None)).await }
    });
    server.settle().await;
    let link = spot.open().await.expect("open");
    spot.close().await;
    let err = pending.await.expect("task").expect_err("closed");
    assert_eq!(err.to_string(), "NetworkError: Connection closed");
    assert!(link.subscribed_channels().is_empty());
    assert_eq!(link.pending_replies(), 0);
    server.closed().await;
}

/// `Spot` with an authentication handshake on every fresh connection.
struct Authed {
    handshake: Mutex<Option<Value>>,
}

#[async_trait]
impl Dialect for Authed {
    type Request = Request;
    type Reply = Value;
    type Notification = Value;
    type Params = Value;

    fn parse(&self, frame: Data) -> Result<Incoming<Value, Value>, Error> {
        Spot.parse(frame)
    }

    fn encode_request(&self, id: u64, request: &Request) -> Result<Data, Error> {
        Spot.encode_request(id, request)
    }

    fn subscribe(&self, channel: &str, params: Option<&Value>) -> Result<Outgoing<Request>, Error> {
        Spot.subscribe(channel, params)
    }

    fn unsubscribe(&self, channel: &str, params: Option<&Value>) -> Result<Outgoing<Request>, Error> {
        Spot.unsubscribe(channel, params)
    }

    async fn on_open(&self, link: &Link<Self>) -> Result<(), Error> {
        let reply = link.rpc_request(&call("add", Some(json!({"a": 1, "b": 1})))).await?;
        if reply["success"] != true {
            return Err(Error::auth("handshake refused"));
        }
        *self.handshake.lock().expect("handshake") = Some(reply);
        Ok(())
    }
}

#[tokio::test]
async fn streams_rpc_on_open_handshakes_before_the_connection_serves_callers() {
    let server = Server::start(ServerOptions::default()).await;
    let spot = Socket::new(
        Authed {
            handshake: Mutex::new(None),
        },
        options(&server),
    );
    let sum = spot
        .rpc_request(&call("add", Some(json!({"a": 2, "b": 2}))))
        .await
        .expect("sum");
    assert_eq!(sum["result"], 4);
    assert_eq!(
        spot.dialect()
            .handshake
            .lock()
            .expect("handshake")
            .as_ref()
            .expect("done")["result"],
        2
    );
    assert_eq!(server.sockets().len(), 1);
    assert_eq!(sent_methods(&server.last()), ["add", "add"]);
    spot.close().await;
}

/// A handshake that fails: the open fails and no connection is current.
struct Unauthed;

#[async_trait]
impl Dialect for Unauthed {
    type Request = Request;
    type Reply = Value;
    type Notification = Value;
    type Params = Value;

    fn parse(&self, frame: Data) -> Result<Incoming<Value, Value>, Error> {
        Spot.parse(frame)
    }

    fn encode_request(&self, id: u64, request: &Request) -> Result<Data, Error> {
        Spot.encode_request(id, request)
    }

    fn subscribe(&self, channel: &str, params: Option<&Value>) -> Result<Outgoing<Request>, Error> {
        Spot.subscribe(channel, params)
    }

    fn unsubscribe(&self, channel: &str, params: Option<&Value>) -> Result<Outgoing<Request>, Error> {
        Spot.unsubscribe(channel, params)
    }

    async fn on_open(&self, link: &Link<Self>) -> Result<(), Error> {
        let reply = link.rpc_request(&call("fail", None)).await?;
        Err(Error::auth(reply["error"].as_str().unwrap_or("refused").to_string()))
    }
}

#[tokio::test]
async fn streams_rpc_a_failed_handshake_fails_the_open() {
    let server = Server::start(ServerOptions::default()).await;
    let spot = Socket::new(Unauthed, options(&server));
    let err = spot.rpc_request(&call("add", None)).await.expect_err("handshake fails");
    assert!(err.is_network(), "{err}");
    assert!(err.message().starts_with("Failed to connect"), "{err}");
    assert!(!spot.is_open().await);
    server.closed().await;
}
