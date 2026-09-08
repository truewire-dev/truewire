//! WebSocket transport over `tokio-tungstenite`: one lazily opened connection carrying
//! request/reply calls correlated by id, channel subscriptions consumed as streams, and
//! acknowledgements matched by arrival order, in whatever mix an API's dialect needs.
//!
//! The Python and TypeScript runtimes offer this as a class hierarchy (`Socket`, `Rpc`,
//! `Streams`, `StreamsRpc`, `SerialReplies`) a core subclasses. Here the runtime owns every
//! piece of connection and correlation state in one [`Socket`], and the core supplies the
//! one thing the runtime cannot know, the wire dialect, as an implementation of
//! [`Dialect`]: how a frame is read into an identified reply, a channel push or a serial
//! acknowledgement, and how a request, a subscribe or an unsubscribe is written.
//!
//! - [`Socket::rpc_request`]: send a request under a fresh id and wait for its reply
//!   (`Rpc.rpcRequest`).
//! - [`Socket::subscribe`]: subscribe to a channel and get a [`Stream`] of its pushes,
//!   with the acknowledging reply and an `unsubscribe()` (`Streams.subscribe`).
//! - [`Socket::serial_request`]: send a frame and take the very next acknowledgement, one
//!   request at a time (`SerialReplies.request`).
//! - [`Socket::wait`]: race any future against the connection's fate, so a dropped
//!   connection fails a caller instead of hanging it.
//!
//! The connection opens on first use and closes on [`Socket::close`]; a connection the
//! peer dropped is reopened on the next use, with every request and subscription that was
//! on it failed with a `NetworkError`.

mod dialect;
mod socket;
mod stream;

pub use dialect::{Dialect, Incoming, Outgoing};
pub use socket::{Data, Link, Socket, SocketOptions, SubscribeOptions};
pub use stream::Stream;
