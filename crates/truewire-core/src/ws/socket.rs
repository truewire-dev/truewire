//! The connection and its correlation state.

use std::borrow::Cow;
use std::collections::HashMap;
use std::future::Future;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, Weak};
use std::time::Duration;

use futures::stream::{SplitSink, SplitStream};
use futures::{SinkExt, StreamExt};
use tokio::net::TcpStream;
use tokio::sync::{mpsc, oneshot, watch};
use tokio::task::JoinHandle;
use tokio_tungstenite::tungstenite::protocol::CloseFrame;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::{MaybeTlsStream, WebSocketStream};

use super::dialect::{Dialect, Incoming, Outgoing};
use super::stream::Stream;
use crate::errors::{Error, Result};

/// One WebSocket frame's payload: text or binary.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Data {
    Text(String),
    Binary(Vec<u8>),
}

impl Data {
    /// The payload as text (lossily for binary).
    pub fn as_text(&self) -> Cow<'_, str> {
        match self {
            Self::Text(s) => Cow::Borrowed(s),
            Self::Binary(b) => String::from_utf8_lossy(b),
        }
    }

    /// The payload as bytes.
    pub fn as_bytes(&self) -> &[u8] {
        match self {
            Self::Text(s) => s.as_bytes(),
            Self::Binary(b) => b,
        }
    }

    /// The payload decoded as JSON; a syntax error is a `ValidationError`.
    pub fn json(&self) -> Result<serde_json::Value> {
        crate::validation::parse_slice(self.as_bytes())
    }

    /// A text frame holding `value` as JSON.
    pub fn from_json(value: &serde_json::Value) -> Self {
        Self::Text(value.to_string())
    }

    fn into_message(self) -> Message {
        match self {
            Self::Text(s) => Message::text(s),
            Self::Binary(b) => Message::binary(b),
        }
    }
}

impl From<String> for Data {
    fn from(value: String) -> Self {
        Self::Text(value)
    }
}

impl From<&str> for Data {
    fn from(value: &str) -> Self {
        Self::Text(value.to_string())
    }
}

impl From<Vec<u8>> for Data {
    fn from(value: Vec<u8>) -> Self {
        Self::Binary(value)
    }
}

impl From<serde_json::Value> for Data {
    fn from(value: serde_json::Value) -> Self {
        Self::from_json(&value)
    }
}

/// How a [`Socket`] connects.
#[derive(Debug, Clone)]
pub struct SocketOptions {
    pub url: String,
    /// Time allowed to open (and to close) the connection. Default 10 s.
    pub timeout: Duration,
    /// Interval between pings, when set: the dialect's [`ping`](Dialect::ping) frame, or a
    /// protocol-level ping when it has none. No pinging by default.
    pub ping_interval: Option<Duration>,
}

impl SocketOptions {
    pub fn new(url: impl Into<String>) -> Self {
        Self {
            url: url.into(),
            timeout: Duration::from_secs(10),
            ping_interval: None,
        }
    }

    pub fn timeout(mut self, timeout: Duration) -> Self {
        self.timeout = timeout;
        self
    }

    pub fn ping_interval(mut self, interval: Duration) -> Self {
        self.ping_interval = Some(interval);
        self
    }
}

/// Options of one subscription.
#[derive(Clone, Default)]
pub struct SubscribeOptions<N> {
    /// Channel identifier to use for the subscription request, if different from the local one.
    pub request_channel: Option<String>,
    /// Derive the local channel identifier from an incoming notification; the channel itself matches otherwise.
    pub message_key: Option<MessageKey<N>>,
}

/// A function deriving the local channel of a notification.
pub type MessageKey<N> = Arc<dyn Fn(&N) -> String + Send + Sync>;

impl<N> SubscribeOptions<N> {
    pub fn new() -> Self {
        Self {
            request_channel: None,
            message_key: None,
        }
    }

    pub fn request_channel(mut self, channel: impl Into<String>) -> Self {
        self.request_channel = Some(channel.into());
        self
    }

    pub fn message_key(mut self, key: impl Fn(&N) -> String + Send + Sync + 'static) -> Self {
        self.message_key = Some(Arc::new(key));
        self
    }
}

type WsStream = WebSocketStream<MaybeTlsStream<TcpStream>>;

/// One live connection: what a [`Socket`] holds while open, and what a dialect's
/// [`on_open`](Dialect::on_open) handshake is given before the connection is current.
pub struct Link<D: Dialect> {
    dialect: Arc<D>,
    url: String,
    sink: tokio::sync::Mutex<SplitSink<WsStream, Message>>,
    counter: Arc<AtomicU64>,
    replies: Mutex<HashMap<u64, oneshot::Sender<D::Reply>>>,
    subscriptions: Mutex<HashMap<String, mpsc::UnboundedSender<D::Notification>>>,
    message_keys: Mutex<HashMap<String, MessageKey<D::Notification>>>,
    serial_tx: mpsc::UnboundedSender<D::Reply>,
    serial_rx: tokio::sync::Mutex<mpsc::UnboundedReceiver<D::Reply>>,
    serial_lock: tokio::sync::Mutex<()>,
    closed: watch::Sender<Option<Error>>,
    tasks: Mutex<Vec<JoinHandle<()>>>,
}

impl<D: Dialect> std::fmt::Debug for Link<D> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Link")
            .field("url", &self.url)
            .field("closed", &self.is_closed())
            .finish_non_exhaustive()
    }
}

impl<D: Dialect> Drop for Link<D> {
    fn drop(&mut self) {
        for task in self.tasks.lock().expect("tasks lock").drain(..) {
            task.abort();
        }
    }
}

impl<D: Dialect> Link<D> {
    /// The dialect this connection speaks.
    pub fn dialect(&self) -> &D {
        &self.dialect
    }

    /// Whether the connection has failed or been closed.
    pub fn is_closed(&self) -> bool {
        self.closed.borrow().is_some()
    }

    /// How many requests are waiting for a reply.
    pub fn pending_replies(&self) -> usize {
        self.replies.lock().expect("replies lock").len()
    }

    /// The local channels currently subscribed.
    pub fn subscribed_channels(&self) -> Vec<String> {
        self.subscriptions
            .lock()
            .expect("subscriptions lock")
            .keys()
            .cloned()
            .collect()
    }

    /// Write one frame.
    pub async fn send(&self, data: Data) -> Result<()> {
        if let Some(error) = self.closed.borrow().clone() {
            return Err(error);
        }
        let mut sink = self.sink.lock().await;
        sink.send(data.into_message())
            .await
            .map_err(|e| Error::network(format!("Error sending to {}", self.url)).with_source(e))
    }

    /// Send `request` under a fresh id and wait for its reply.
    pub async fn rpc_request(&self, request: &D::Request) -> Result<D::Reply> {
        let id = self.counter.fetch_add(1, Ordering::SeqCst);
        let frame = self.dialect.encode_request(id, request)?;
        let (tx, rx) = oneshot::channel();
        self.replies.lock().expect("replies lock").insert(id, tx);
        let result = async {
            self.send(frame).await?;
            self.wait(rx).await?.map_err(|_| self.closed_error())
        }
        .await;
        self.replies.lock().expect("replies lock").remove(&id);
        result
    }

    /// Send `frame` and take the next [`Incoming::Serial`] acknowledgement, one request at a time.
    pub async fn serial_request(&self, frame: Data) -> Result<D::Reply> {
        let _guard = self.serial_lock.lock().await;
        self.send(frame).await?;
        let mut rx = self.serial_rx.lock().await;
        self.wait(rx.recv()).await?.ok_or_else(|| self.closed_error())
    }

    /// Wait for `future`, failing instead if the connection fails first.
    pub async fn wait<T>(&self, future: impl Future<Output = T>) -> Result<T> {
        let mut closed = self.closed.subscribe();
        if let Some(error) = closed.borrow_and_update().clone() {
            return Err(error);
        }
        tokio::select! {
            value = future => Ok(value),
            _ = closed.changed() => Err(self.closed_error()),
        }
    }

    fn closed_error(&self) -> Error {
        self.closed
            .borrow()
            .clone()
            .unwrap_or_else(|| Error::network("Connection closed"))
    }

    /// Fail the connection with `error`, once: every pending reply and subscription is
    /// released. Returns whether this call was the one that failed it.
    pub fn fail(&self, error: Error) -> bool {
        let first = self.closed.send_if_modified(|closed| {
            if closed.is_some() {
                false
            } else {
                *closed = Some(error);
                true
            }
        });
        if first {
            self.replies.lock().expect("replies lock").clear();
            self.subscriptions.lock().expect("subscriptions lock").clear();
        }
        first
    }

    async fn close_sink(&self) {
        let mut sink = self.sink.lock().await;
        let _ = sink.send(Message::Close(None)).await;
        let _ = sink.close().await;
    }

    fn dispatch(&self, incoming: Incoming<D::Reply, D::Notification>) {
        match incoming {
            Incoming::Reply { id, reply } => {
                if let Some(tx) = self.replies.lock().expect("replies lock").remove(&id) {
                    let _ = tx.send(reply);
                }
            }
            Incoming::Push { channel, notification } => {
                let key = self.message_keys.lock().expect("keys lock").get(&channel).cloned();
                let local = match key {
                    Some(key) => key(&notification),
                    None => channel,
                };
                if let Some(tx) = self.subscriptions.lock().expect("subscriptions lock").get(&local) {
                    let _ = tx.send(notification);
                }
            }
            Incoming::Serial(reply) => {
                let _ = self.serial_tx.send(reply);
            }
            Incoming::Ignore => {}
        }
    }

    async fn perform(&self, outgoing: Outgoing<D::Request>) -> Result<Option<D::Reply>> {
        match outgoing {
            Outgoing::Rpc(request) => self.rpc_request(&request).await.map(Some),
            Outgoing::Serial(frame) => self.serial_request(frame).await.map(Some),
            Outgoing::Nothing => Ok(None),
        }
    }

    async fn subscribe(
        self: &Arc<Self>,
        channel: &str,
        params: Option<D::Params>,
        options: SubscribeOptions<D::Notification>,
    ) -> Result<Stream<D::Notification, D::Reply>> {
        let request_channel = options.request_channel.unwrap_or_else(|| channel.to_string());
        if let Some(key) = options.message_key {
            self.message_keys
                .lock()
                .expect("keys lock")
                .insert(request_channel.clone(), key);
        }
        let (tx, rx) = mpsc::unbounded_channel();
        {
            let mut subscriptions = self.subscriptions.lock().expect("subscriptions lock");
            if subscriptions.contains_key(channel) {
                return Err(Error::logic(format!("Already subscribed to channel {channel:?}")));
            }
            subscriptions.insert(channel.to_string(), tx);
        }
        let subscribed = async {
            let outgoing = self.dialect.subscribe(&request_channel, params.as_ref())?;
            let reply = self.perform(outgoing).await?;
            if let Some(reply) = &reply {
                self.dialect.check_ack(&request_channel, reply)?;
            }
            Ok::<_, Error>(reply)
        }
        .await;
        let reply = match subscribed {
            Ok(reply) => reply,
            Err(e) => {
                self.subscriptions.lock().expect("subscriptions lock").remove(channel);
                return Err(e);
            }
        };

        let link = Arc::clone(self);
        let items = futures::stream::unfold(
            (rx, link.closed.subscribe(), false),
            |(mut rx, mut closed, done)| async move {
                if done {
                    return None;
                }
                let already = closed.borrow_and_update().clone();
                if let Some(error) = already {
                    return Some((Err(error), (rx, closed, true)));
                }
                tokio::select! {
                    next = rx.recv() => match next {
                        Some(notification) => Some((Ok(notification), (rx, closed, false))),
                        // The sender is gone: an unsubscribe (the stream simply ends) or a failed
                        // connection (the failure is the last item).
                        None => {
                            let error = closed.borrow().clone();
                            error.map(|error| (Err(error), (rx, closed, true)))
                        }
                    },
                    _ = closed.changed() => {
                        let error = closed.borrow().clone().unwrap_or_else(|| Error::network("Connection closed"));
                        Some((Err(error), (rx, closed, true)))
                    }
                }
            },
        )
        .boxed();

        let local = channel.to_string();
        let unsubscribe = move || async move {
            let outgoing = link.dialect.unsubscribe(&request_channel, params.as_ref())?;
            let reply = link.perform(outgoing).await;
            link.subscriptions.lock().expect("subscriptions lock").remove(&local);
            reply
        };
        Ok(Stream::new(reply, items, unsubscribe))
    }
}

/// A WebSocket client speaking one [`Dialect`]: lazy connection, request/reply
/// correlation, channel subscriptions, serial acknowledgements, an optional periodic ping
/// and error propagation via [`wait`](Self::wait).
///
/// The connection opens on first use and closes on [`close`](Self::close); many concurrent
/// calls are fine. A connection the peer closed fails every call and subscription on it
/// with a `NetworkError` and is reopened by the next use.
pub struct Socket<D: Dialect> {
    dialect: Arc<D>,
    options: SocketOptions,
    counter: Arc<AtomicU64>,
    state: tokio::sync::Mutex<Option<Arc<Link<D>>>>,
}

impl<D: Dialect> std::fmt::Debug for Socket<D> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Socket")
            .field("url", &self.options.url)
            .finish_non_exhaustive()
    }
}

impl<D: Dialect> Socket<D> {
    pub fn new(dialect: D, options: SocketOptions) -> Self {
        Self {
            dialect: Arc::new(dialect),
            options,
            counter: Arc::new(AtomicU64::new(0)),
            state: tokio::sync::Mutex::new(None),
        }
    }

    /// The dialect this socket speaks.
    pub fn dialect(&self) -> &D {
        &self.dialect
    }

    pub fn options(&self) -> &SocketOptions {
        &self.options
    }

    /// The id the next request will be sent under.
    pub fn next_id(&self) -> u64 {
        self.counter.load(Ordering::SeqCst)
    }

    /// Whether a connection is open.
    pub async fn is_open(&self) -> bool {
        self.state.lock().await.as_ref().is_some_and(|link| !link.is_closed())
    }

    /// The current connection, opening one first if none exists yet (or the last one is gone).
    pub async fn open(&self) -> Result<Arc<Link<D>>> {
        let mut state = self.state.lock().await;
        if let Some(link) = state.as_ref() {
            if !link.is_closed() {
                return Ok(Arc::clone(link));
            }
        }
        let link = self.connect().await?;
        *state = Some(Arc::clone(&link));
        Ok(link)
    }

    async fn connect(&self) -> Result<Arc<Link<D>>> {
        let url = &self.options.url;
        let failed = |e: Error| Error::network(format!("Failed to connect to {url}")).with_source(e);
        let connecting = tokio_tungstenite::connect_async(url.as_str());
        let (ws, _) = match tokio::time::timeout(self.options.timeout, connecting).await {
            Ok(Ok(connected)) => connected,
            Ok(Err(e)) => return Err(Error::network(format!("Failed to connect to {url}")).with_source(e)),
            Err(_) => {
                return Err(failed(Error::network(format!(
                    "timed out after {:?}",
                    self.options.timeout
                ))))
            }
        };
        let (sink, source) = ws.split();
        let (closed, _) = watch::channel(None);
        let (serial_tx, serial_rx) = mpsc::unbounded_channel();
        let link = Arc::new(Link {
            dialect: Arc::clone(&self.dialect),
            url: url.clone(),
            sink: tokio::sync::Mutex::new(sink),
            counter: Arc::clone(&self.counter),
            replies: Mutex::new(HashMap::new()),
            subscriptions: Mutex::new(HashMap::new()),
            message_keys: Mutex::new(HashMap::new()),
            serial_tx,
            serial_rx: tokio::sync::Mutex::new(serial_rx),
            serial_lock: tokio::sync::Mutex::new(()),
            closed,
            tasks: Mutex::new(Vec::new()),
        });
        let reader = tokio::spawn(read_loop(Arc::downgrade(&link), source));
        link.tasks.lock().expect("tasks lock").push(reader);
        if let Some(interval) = self.options.ping_interval {
            let pinger = tokio::spawn(ping_loop(Arc::downgrade(&link), interval));
            link.tasks.lock().expect("tasks lock").push(pinger);
        }
        if let Err(e) = self.dialect.on_open(&link).await {
            link.fail(e.clone());
            link.close_sink().await;
            return Err(failed(e));
        }
        Ok(link)
    }

    /// Wait for `future`, failing instead if the connection fails first (opening one if needed).
    pub async fn wait<T>(&self, future: impl Future<Output = T>) -> Result<T> {
        self.open().await?.wait(future).await
    }

    /// Send `request` under a fresh id and wait for its reply.
    pub async fn rpc_request(&self, request: &D::Request) -> Result<D::Reply> {
        self.open().await?.rpc_request(request).await
    }

    /// Send `frame` and take the next serial acknowledgement.
    pub async fn serial_request(&self, frame: Data) -> Result<D::Reply> {
        self.open().await?.serial_request(frame).await
    }

    /// Write one frame.
    pub async fn send(&self, frame: Data) -> Result<()> {
        self.open().await?.send(frame).await
    }

    /// Subscribe to `channel`, the local channel identifier; `options.request_channel`
    /// names the channel in the request when it differs, `options.message_key` derives the
    /// local identifier from each notification when the channel alone cannot. One
    /// subscription per local channel at a time.
    pub async fn subscribe(
        &self,
        channel: &str,
        params: Option<D::Params>,
        options: SubscribeOptions<D::Notification>,
    ) -> Result<Stream<D::Notification, D::Reply>> {
        self.open().await?.subscribe(channel, params, options).await
    }

    /// Close the connection if one was opened; do nothing when none ever was. Every
    /// pending call fails with `NetworkError("Connection closed")`.
    pub async fn close(&self) {
        let link = self.state.lock().await.take();
        if let Some(link) = link {
            link.fail(Error::network("Connection closed"));
            let _ = tokio::time::timeout(self.options.timeout, link.close_sink()).await;
            for task in link.tasks.lock().expect("tasks lock").drain(..) {
                task.abort();
            }
        }
    }
}

async fn read_loop<D: Dialect>(link: Weak<Link<D>>, mut source: SplitStream<WsStream>) {
    loop {
        let message = source.next().await;
        let Some(link) = link.upgrade() else { return };
        let data = match message {
            Some(Ok(Message::Text(text))) => Data::Text(text.to_string()),
            Some(Ok(Message::Binary(bytes))) => Data::Binary(bytes.to_vec()),
            Some(Ok(Message::Ping(payload))) => {
                let mut sink = link.sink.lock().await;
                let _ = sink.send(Message::Pong(payload)).await;
                continue;
            }
            Some(Ok(Message::Pong(_))) | Some(Ok(Message::Frame(_))) => continue,
            Some(Ok(Message::Close(frame))) => {
                link.fail(Error::network(closed_message(frame.as_ref())));
                return;
            }
            Some(Err(e)) => {
                link.fail(Error::network("WebSocket error").with_source(e));
                return;
            }
            None => {
                link.fail(Error::network("Connection closed"));
                return;
            }
        };
        match link.dialect.parse(data) {
            Ok(incoming) => link.dispatch(incoming),
            Err(e) => {
                if link.fail(e) {
                    link.close_sink().await;
                }
                return;
            }
        }
    }
}

async fn ping_loop<D: Dialect>(link: Weak<Link<D>>, interval: Duration) {
    let mut ticker = tokio::time::interval(interval);
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    ticker.tick().await;
    loop {
        ticker.tick().await;
        let Some(link) = link.upgrade() else { return };
        if link.is_closed() {
            return;
        }
        let result = match link.dialect.ping() {
            Some(frame) => link.send(frame).await,
            None => {
                let mut sink = link.sink.lock().await;
                sink.send(Message::Ping(Vec::new().into()))
                    .await
                    .map_err(|e| Error::network("Error sending ping").with_source(e))
            }
        };
        if let Err(e) = result {
            link.fail(e);
            return;
        }
    }
}

fn closed_message(frame: Option<&CloseFrame>) -> String {
    match frame {
        Some(frame) => format!("Connection closed ({} {})", u16::from(frame.code), frame.reason)
            .trim_end()
            .to_string(),
        None => "Connection closed".to_string(),
    }
}
