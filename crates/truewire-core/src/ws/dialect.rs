//! The wire dialect a core supplies to a [`Socket`](super::Socket).

use async_trait::async_trait;

use super::socket::{Data, Link};
use crate::errors::Result;

/// What one incoming frame turned out to be.
#[derive(Debug, Clone, PartialEq)]
pub enum Incoming<Reply, Notification> {
    /// The reply to the request sent under `id` ([`Socket::rpc_request`](super::Socket::rpc_request)).
    Reply { id: u64, reply: Reply },
    /// A push on `channel`, routed to the subscription holding it.
    Push {
        channel: String,
        notification: Notification,
    },
    /// An acknowledgement with no correlation id, matched to the oldest pending
    /// [`Socket::serial_request`](super::Socket::serial_request) by arrival order.
    Serial(Reply),
    /// A frame that is none of these (a heartbeat, a status frame): dropped.
    Ignore,
}

/// How a subscribe or unsubscribe request travels and how its acknowledgement comes back.
#[derive(Debug, Clone, PartialEq)]
pub enum Outgoing<Request> {
    /// As a correlated request: the reply under its id acknowledges it.
    Rpc(Request),
    /// As a raw frame: the next [`Incoming::Serial`] acknowledges it.
    Serial(Data),
    /// Nothing is sent: the connection itself is the subscription (a `push` stream), or the
    /// protocol needs no unsubscribe frame. The acknowledgement is `None`.
    Nothing,
}

/// The wire dialect of one WebSocket API: frames in, frames out.
///
/// Every method is pure over its arguments; the [`Socket`](super::Socket) owns the
/// connection and all correlation state. A core that needs to attach something to every
/// request (a session token) does it in [`encode_request`](Self::encode_request), or in
/// its own wrapper around `rpc_request`.
#[async_trait]
pub trait Dialect: Send + Sync + 'static {
    /// What [`encode_request`](Self::encode_request) takes.
    type Request: Send + Sync + 'static;
    /// What an [`Incoming::Reply`]/[`Incoming::Serial`] carries.
    type Reply: Send + Sync + Clone + 'static;
    /// What an [`Incoming::Push`] carries.
    type Notification: Send + Sync + 'static;
    /// What [`subscribe`](Self::subscribe) and [`unsubscribe`](Self::unsubscribe) take.
    type Params: Send + Sync + 'static;

    /// Read one frame. Returning an error fails the connection; return
    /// [`Incoming::Ignore`] for a frame the protocol does not care about.
    fn parse(&self, frame: Data) -> Result<Incoming<Self::Reply, Self::Notification>>;

    /// Write `request` tagged with `id` the way the protocol correlates replies.
    fn encode_request(&self, id: u64, request: &Self::Request) -> Result<Data>;

    /// The subscribe request for `channel` with `params`.
    fn subscribe(&self, channel: &str, params: Option<&Self::Params>) -> Result<Outgoing<Self::Request>>;

    /// The unsubscribe request for `channel` with `params`.
    fn unsubscribe(&self, channel: &str, params: Option<&Self::Params>) -> Result<Outgoing<Self::Request>>;

    /// Check the reply that acknowledged a subscribe or unsubscribe; an error here rejects
    /// the subscription (a refused channel). Accepts every reply by default.
    fn check_ack(&self, _channel: &str, _reply: &Self::Reply) -> Result<()> {
        Ok(())
    }

    /// The frame to send every [`SocketOptions::ping_interval`](super::SocketOptions::ping_interval);
    /// a protocol-level ping when `None` (the default).
    fn ping(&self) -> Option<Data> {
        None
    }

    /// A handshake to run on a freshly opened connection before it serves callers (an
    /// authentication exchange). `link` sends frames and awaits correlated replies on that
    /// connection; an error here fails the open.
    async fn on_open(&self, _link: &Link<Self>) -> Result<()>
    where
        Self: Sized,
    {
        Ok(())
    }
}
