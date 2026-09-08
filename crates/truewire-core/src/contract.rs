//! The contract between a hand-written core and the code `truewire generate rust` will
//! emit: the Rust half of `truewire_core.contract` and `@truewire/core`'s `contract.ts`.
//!
//! A generated endpoint struct holds its core as an `Arc<dyn HttpEndpoint<Meta>>` (or
//! `CommandEndpoint`/`StreamEndpoint`) and calls exactly one verb on it; a generated router
//! hands the same core to every child. The generator reads nothing else from a core and
//! never imports the project's own `core` module: the traits below are what a core
//! implements, and the compiler checks the two against each other where the client is
//! constructed (ADR 0011).
//!
//! `Meta` is the per-endpoint `meta` shape the core's `[cores.<name>]` entry declares in
//! `truewire.toml`, rendered by the generator into `<package>/meta.rs`; a core with no
//! schema receives `()`.
//!
//! What the plan carries, and where it goes:
//!
//! - `wire.method`/`wire.path` (an HTTP endpoint) or `wire.path` (a WebSocket command's
//!   method name) or `wire.channel` (a stream): on the call, verbatim, placeholders unfilled.
//! - The request: dumped through `serde` into a wire [`Value`] before the call, so the
//!   core sees wire names and wire forms (a `TimestampMillis` is already an integer) and
//!   fills `{name}` placeholders from it. `None` when the endpoint declares no request.
//! - The reply: the core returns the wire body as a [`Value`], after unwrapping its
//!   envelope (`envelope.payload`) and mapping errors. Generated code then either
//!   [`decode`](crate::validation::decode)s it into the response type or, for
//!   `validate: false`, hands the `Value` back as it came. That is why the traits are not
//!   generic in the response type and why a core never validates: `validate` is a decision
//!   the generated method takes after the call, not a flag the core reads.
//!
//! Verbs:
//!
//! - [`HttpEndpoint::request`]: one HTTP call.
//! - [`CommandEndpoint::request`]: one WebSocket command/reply call.
//! - [`StreamEndpoint::subscribe`]: one channel subscription, returning a
//!   [`Stream`] of pushed payloads the caller iterates and unsubscribes.
//!
//! There is no `ClientRoot` or `Composite` trait: a Rust root is a struct the generator
//! writes with one field per transport the `truewire.toml` core declares, and the
//! hand-written code builds it (`Client::new(core)`); nothing needs a `new(...)` protocol.

use std::time::Duration;

use async_trait::async_trait;
use serde_json::Value;

use crate::errors::Result;
use crate::ws::Stream;

/// Options every generated method takes as its last parameter.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct CallOptions {
    /// Time before this call is abandoned with a `NetworkError`; the core's default when `None`.
    pub timeout: Option<Duration>,
}

impl CallOptions {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn timeout(mut self, timeout: Duration) -> Self {
        self.timeout = Some(timeout);
        self
    }
}

/// One HTTP call. `{name}` placeholders in `path` are filled from `request`.
#[derive(Debug, Clone)]
pub struct HttpCall<'a, Meta> {
    /// The wire HTTP method; `None` when the spec leaves it to the core (a uniformly POST JSON-RPC API).
    pub method: Option<&'a str>,
    /// The wire path template.
    pub path: &'a str,
    /// The dumped request: an object keyed by the wire's own parameter names, a union
    /// member, an array, or `None` when the endpoint declares no request.
    pub request: Option<Value>,
    /// The endpoint's declared `meta`, in the shape its core's schema states.
    pub meta: &'a Meta,
    pub options: CallOptions,
}

/// One WebSocket command; `path` is the wire method name.
#[derive(Debug, Clone)]
pub struct CommandCall<'a, Meta> {
    pub path: &'a str,
    pub request: Option<Value>,
    pub meta: &'a Meta,
    pub options: CallOptions,
}

/// One channel subscription; `{name}` placeholders in `channel` are filled from `parameters`.
#[derive(Debug, Clone)]
pub struct SubscribeCall<'a, Meta> {
    pub channel: &'a str,
    /// The dumped parameters object, or `None` for a direct-channel or connect-only
    /// stream, where the generated method filled the channel itself.
    pub parameters: Option<Value>,
    pub meta: &'a Meta,
    pub options: CallOptions,
}

/// Base of a generated `rpc` endpoint reached over HTTP.
#[async_trait]
pub trait HttpEndpoint<Meta = ()>: Send + Sync {
    /// Send one call and return the wire body the method's return type describes, its
    /// envelope unwrapped and its errors mapped.
    async fn request(&self, call: HttpCall<'_, Meta>) -> Result<Value>;
}

/// Base of a generated `rpc` endpoint reached over a WebSocket connection.
#[async_trait]
pub trait CommandEndpoint<Meta = ()>: Send + Sync {
    /// Send one command and return its reply.
    async fn request(&self, call: CommandCall<'_, Meta>) -> Result<Value>;
}

/// Base of a generated `stream` endpoint.
#[async_trait]
pub trait StreamEndpoint<Meta = ()>: Send + Sync {
    /// Subscribe to `channel` with `parameters`; each pushed payload is one item.
    async fn subscribe(&self, call: SubscribeCall<'_, Meta>) -> Result<Stream<Value>>;
}
