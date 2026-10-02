//! The Spot WebSocket v2 transport. Kraken correlates every reply -- trading-method
//! replies and subscribe/unsubscribe acks alike -- by the client's `req_id`, so one
//! connection serves both verbs: `request` for `trading_ws.*` and `ping`, `subscribe` for
//! every channel. The private connection differs from the public one only by the token it
//! merges into every outgoing `params`.

use std::time::Duration;

use async_trait::async_trait;
use truewire_core::serde_json::{json, Map, Value};
use truewire_core::ws::{
    Data, Dialect, Incoming, Outgoing, Socket, SocketOptions, SubscribeOptions,
};
use truewire_core::{
    CommandCall, CommandEndpoint, Error, Result, Stream, StreamEndpoint, SubscribeCall,
};

use super::auth::TokenCache;
use super::envelope::raise_error;

pub const SPOT_WS_URL: &str = "wss://ws.kraken.com/v2";
pub const SPOT_WS_AUTH_URL: &str = "wss://ws-auth.kraken.com/v2";

/// Commands whose reply is the whole frame rather than its `result`; neither declares an `envelope`.
pub const RAW_METHODS: &[&str] = &["ping", "batch_cancel"];

/// Kraken closes a connection idle for about a minute; an application-level ping keeps it open.
const PING_INTERVAL: Duration = Duration::from_secs(30);

/// Kraken's frames: `{method, params, req_id}` out, a `req_id`-bearing reply or a
/// `channel` push in.
#[derive(Debug, Clone, Copy, Default)]
pub struct KrakenDialect;

#[async_trait]
impl Dialect for KrakenDialect {
    /// `{"method": ..., "params": {...}}`, the `req_id` added on the way out.
    type Request = Value;
    type Reply = Value;
    type Notification = Value;
    type Params = Value;

    fn parse(&self, frame: Data) -> Result<Incoming<Value, Value>> {
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

    fn encode_request(&self, id: u64, request: &Value) -> Result<Data> {
        let mut frame = request.clone();
        frame["req_id"] = Value::from(id);
        Ok(Data::from_json(&frame))
    }

    fn subscribe(&self, channel: &str, params: Option<&Value>) -> Result<Outgoing<Value>> {
        Ok(Outgoing::Rpc(
            json!({"method": "subscribe", "params": with_channel(channel, params)}),
        ))
    }

    fn unsubscribe(&self, channel: &str, params: Option<&Value>) -> Result<Outgoing<Value>> {
        Ok(Outgoing::Rpc(
            json!({"method": "unsubscribe", "params": with_channel(channel, params)}),
        ))
    }

    fn check_ack(&self, channel: &str, reply: &Value) -> Result<()> {
        checked(channel, reply)
    }

    fn ping(&self) -> Option<Data> {
        Some(Data::from_json(&json!({"method": "ping"})))
    }
}

fn with_channel(channel: &str, params: Option<&Value>) -> Value {
    let mut out = Map::new();
    out.insert("channel".to_string(), Value::from(channel));
    if let Some(Value::Object(params)) = params {
        out.extend(params.clone());
    }
    Value::Object(out)
}

/// `reply`, unless it reports a failure. `success` is absent from a whole-frame reply with
/// nothing to fail on (`pong`).
fn checked(method: &str, reply: &Value) -> Result<()> {
    if reply.get("success") == Some(&Value::Bool(false)) {
        let error = reply
            .get("error")
            .and_then(Value::as_str)
            .map(str::to_string);
        return Err(raise_error(&[
            error.unwrap_or_else(|| format!("\"{method}\" failed"))
        ]));
    }
    Ok(())
}

/// The transport every `streams.*` and `trading_ws.*` endpoint calls.
pub struct SocketCore {
    socket: Socket<KrakenDialect>,
    token: Option<TokenCache>,
}

impl std::fmt::Debug for SocketCore {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("SocketCore")
            .field("socket", &self.socket)
            .field("authenticated", &self.token.is_some())
            .finish()
    }
}

impl SocketCore {
    /// A connection to `url`, sending the token `token` yields with every request when given.
    pub fn new(url: impl Into<String>, token: Option<TokenCache>) -> Self {
        let options = SocketOptions::new(url).ping_interval(PING_INTERVAL);
        Self {
            socket: Socket::new(KrakenDialect, options),
            token,
        }
    }

    /// Open the connection through the HTTP proxy at `proxy`, as a `CONNECT` tunnel; an
    /// `Error::Logic` when it is not an `http://` URL.
    pub fn with_proxy(mut self, proxy: &str) -> Result<Self> {
        self.socket = self.socket.with_proxy(proxy)?;
        Ok(self)
    }

    /// The underlying socket, for inspecting its connection.
    pub fn socket(&self) -> &Socket<KrakenDialect> {
        &self.socket
    }

    /// Close the connection; the next use opens it again.
    pub async fn close(&self) {
        self.socket.close().await;
    }

    async fn params(&self, params: Option<Value>) -> Result<Value> {
        let mut params = match params {
            Some(Value::Object(params)) => params,
            Some(other) => {
                return Err(Error::logic(format!(
                    "params must be an object, not {other}"
                )))
            }
            None => Map::new(),
        };
        if let Some(token) = &self.token {
            params.insert("token".to_string(), Value::from(token.get().await?));
        }
        Ok(Value::Object(params))
    }
}

#[async_trait]
impl CommandEndpoint for SocketCore {
    /// One method call: the reply's `result`, or the whole frame for `RAW_METHODS`.
    async fn request(&self, call: CommandCall<'_, ()>) -> Result<Value> {
        let params = self.params(call.request).await?;
        let request = json!({"method": call.path, "params": params});
        let reply = match call.options.timeout {
            Some(timeout) => tokio::time::timeout(timeout, self.socket.rpc_request(&request))
                .await
                .map_err(|_| {
                    Error::network(format!("\"{}\" timed out after {timeout:?}", call.path))
                })??,
            None => self.socket.rpc_request(&request).await?,
        };
        checked(call.path, &reply)?;
        if RAW_METHODS.contains(&call.path) {
            return Ok(reply);
        }
        Ok(reply.get("result").cloned().unwrap_or(Value::Null))
    }
}

#[async_trait]
impl StreamEndpoint for SocketCore {
    /// One channel subscription; its acknowledgement is the `reply`.
    async fn subscribe(&self, call: SubscribeCall<'_, ()>) -> Result<Stream<Value>> {
        let params = self.params(call.parameters).await?;
        self.socket
            .subscribe(call.channel, Some(params), SubscribeOptions::new())
            .await
    }
}
