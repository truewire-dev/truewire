# Rust

`truewire generate rust` renders a project's plan (`docs/plan.md`) as the modules of a Rust
crate with the same guarantees as the Python and TypeScript packages: typed requests and
responses, runtime validation with a raw escape hatch, and resumable pagination walkers.
The runtime it depends on is `truewire-core` (`crates/truewire-core`), the third after
`truewire-core` (Python) and `@truewire/core` (TypeScript). Both were designed from the
plan, the language-neutral IR every backend renders from, rather than by transliterating
either earlier runtime.

Status: the runtime exists and is tested (`cargo test`, `cargo clippy -- -D warnings` and
`cargo fmt --check` run in CI, job `runtime-rust`). The generator renders `rpc` endpoints
over HTTP, over a WebSocket and over both, `stream` endpoints (with a typed subscription
`reply`), composite cores, the type tree, routers, the root, `dispatch.rs`, and every
`PaginatedResponse` walker (`page`, `offset`, `token`, `seek`), and unary `grpc` endpoints
over `prost` stubs (see "gRPC endpoints" below). `examples/github` (HTTP,
paging) and `examples/kraken` (REST with HMAC-SHA512 signing, WebSocket commands and
streams over two sockets, a composite root) are generated, compiled and replayed against
`truewire mock` in CI (job `examples-rust`), with `truewire-testing` as their harness.
The crate is not on crates.io (the `truewire` crate name is reserved by
`crates/truewire`, and `truewire-core` will be published beside it). See the end of this
page for what is left.

## Using it

```toml
# truewire.toml
[rust]
package = "github"    # the modules live at src/github/, lib.rs at their top
src = "src"
name = "GitHub"       # the root struct
```

```sh
truewire generate rust             # writes src/github/**/*.rs and .truewire/codegen/rust.json
truewire generate rust --check     # CI: every owned file exists and is what the plan renders
```

Then write a `Cargo.toml` whose library is the generated crate root, and the `core`
module the root declares:

```toml
[lib]
path = "src/github/lib.rs"

[dependencies]
async-trait = "0.1"
serde = { version = "1", features = ["derive"] }
truewire-core = "0.2"
```

```rust
use github::core::CoreOptions;
use github::repos::get::Request;
use github::{CallOptions, GitHub};

let client = GitHub::new(CoreOptions::default());
let repo = client.repos.get(Request { owner: "truewire-dev".into(), repo: "truewire".into(), ..Default::default() }, CallOptions::default()).await?;
repo.created_at                                    // a TimestampIso, deref to DateTime<Utc>
let raw = client.repos.get_raw(request, CallOptions::default()).await?;   // the serde_json::Value as it came
let commits = client.repos.list_commits_paged(request, CallOptions::default()).await?;   // every row
```

`examples/github` is the reference: its `truewire.toml`, `Cargo.toml`,
`src/github/core/mod.rs` and `tests/` are what a project copies.

## What is generated

One file per spec node, beside the Python and TypeScript packages when all are declared:

| file | contents |
| --- | --- |
| `types/mod.rs`, `types/<scope>.rs` | the shared `schemas.json` types, one module per scope |
| `<router>/<endpoint>.rs` | the endpoint's `Request`, its response types, the enums hoisted out of them, and a struct with the method, its `_raw` twin and the walker |
| `<router>/mod.rs` | a router struct delegating to its endpoints and holding its child routers as `pub` fields |
| `client.rs` | the root struct, `GitHub::from_core(core)` |
| `meta.rs` | one struct per `[cores.<name>]` with a `meta` schema (`DefaultMeta`) |
| `lib.rs` | the crate root: `pub mod` for every module above and for the hand-written `core`, `pub use client::GitHub` and `CallOptions` |

Names Truewire invents are `snake_case`/`PascalCase` of the function segment
(`list_commits`, `list_commits_paged`, struct `ListCommits`). Rust has one convention per
kind of identifier and `rustc` warns on every departure, so the wire's names are not kept
verbatim as TypeScript keeps them: a field is `snake_case` of the wire name and carries
`#[serde(rename = "...")]` with the wire name whenever the two differ (`htmlUrl`,
`X-Rate`, a keyword such as `type` becoming `type_`), so the struct still dumps to the
wire object and a recorded `request.json` decodes into it as it stands. The output is
printed by the generator itself; no formatter runs over it, but the printer reproduces
`rustfmt`'s decisions (import order and packing, struct-literal, attribute and chain
widths, signature breaking) so `cargo fmt --check` passes on what it writes.

A record is a `serde` struct with the newtype for every narrowed scalar, an `Option` per
optional key, and a flattened map so an undocumented field never breaks a client (the
same rule the Python `TypedDict` and the TypeScript `object` codec follow):

```rust
#[derive(Debug, Clone, PartialEq, Default, Serialize, Deserialize)]
pub struct Label {
    pub id: i64,
    pub name: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    #[serde(with = "truewire_core::validation::double_option")]
    pub description: Option<Option<String>>,
    pub created_at: TimestampIso,
    /// Keys the spec does not document, kept as they came.
    #[serde(flatten)]
    pub extra: serde_json::Map<String, serde_json::Value>,
}
```

`Default` is derived when every required field has one (a timestamp has no meaningful
zero), so a request is `Request { owner, repo, ..Default::default() }`. A `literal` of
strings is an `enum` with `#[serde(rename)]`s; a literal of numbers or booleans widens to
its scalar (`serde` renames only strings) with the values in its doc comment. A `union` is
an `#[serde(untagged)]` enum whose variants are tried in order, a `list` a `Vec`, a `tuple`
a Rust tuple, a `dict` a `HashMap<String, _>`, a nullable type an `Option`, a field both
optional and nullable an `Option<Option<T>>` with `double_option`. Rust needs a name for
every enum and the plan names only the types it lists, so an inline literal or union is
hoisted into a sibling type named after its position (`Issue.state_reason` becomes
`IssueStateReason`; the non-null half of an alias `Response = A | B | null` becomes
`ResponseValue`). A field whose type reaches back to its own record is boxed. A `scalar`
is rendered by base and format: `string`/`integer`/`number`/`boolean`/`any` to
`String`/`i64`/`f64`/`bool`/`serde_json::Value`, and each `format` in
`truewire_core::types` to its newtype. Every newtype derefs to the value inside and
converts `From` it, so a request built from a real `DateTime<Utc>` is `.into()` away from
its field; that is the Rust form of the converters' pass-through of an already-parsed
value, which Python's converters gained in core 0.2.1.

A generated method dumps the request, hands the core a call, and decodes the reply:

```rust
pub async fn get(&self, request: Request, options: CallOptions) -> Result<Repository> {
    let raw = self.get_raw(request, options).await?;
    decode(raw)
}

/// `get` without validation: the wire body as it came.
pub async fn get_raw(&self, request: Request, options: CallOptions) -> Result<serde_json::Value> {
    let meta = DefaultMeta { public: Some(true) };
    let call = HttpCall {
        method: Some("GET"),
        path: "/repos/{owner}/{repo}",
        request: Some(dump(&request)?),
        meta: &meta,
        options,
    };
    self.core.request(call).await
}
```

`validate: false` is a second method rather than a flag because Rust has no overload
returning a different type: the raw method returns the `serde_json::Value` the core
returned, and the typed method is `decode` on top of it. A method whose endpoint returns
nothing has no `_raw` twin. A request field the spec fixes to one value (a required
single-value `enum`, wire dispatch plumbing) is left out of `Request` and inserted into
the dumped object by the method. Every failure in `decode` is a `ValidationError` whose
`path()` is the JSON pointer of the offending value (`/bids/1/1`) and whose message is
`serde`'s own (`invalid type: integer \`4\`, expected a string at /bids/1/1`), the same
shape the TypeScript codecs report.

## The core contract

The generated code never imports the project's core. Each generated struct holds its core
as an `Arc<dyn HttpEndpoint<Meta>>` (or `CommandEndpoint`/`StreamEndpoint`, or `GrpcEndpoint` for a gRPC endpoint, whose contract carries encoded
messages; see "gRPC endpoints"), the Rust half of `truewire_core.contract` and `contract.ts`
(ADR 0011):

```rust
pub struct HttpCall<'a, Meta> {
    pub method: Option<&'a str>,       // the wire HTTP method; None when the spec leaves it to the core
    pub path: &'a str,                 // the wire path template; {name} placeholders are filled from request
    pub request: Option<Value>,        // the dumped request (wire keys, wire forms), or None
    pub meta: &'a Meta,                // the endpoint's declared meta, in the shape the core's schema states
    pub options: CallOptions,          // { timeout: Option<Duration> }
}

#[async_trait]
pub trait HttpEndpoint<Meta = ()>: Send + Sync {
    async fn request(&self, call: HttpCall<'_, Meta>) -> Result<Value>;
}

#[async_trait]
pub trait CommandEndpoint<Meta = ()>: Send + Sync {
    async fn request(&self, call: CommandCall<'_, Meta>) -> Result<Value>;   // path is the wire method name
}

#[async_trait]
pub trait StreamEndpoint<Meta = ()>: Send + Sync {
    async fn subscribe(&self, call: SubscribeCall<'_, Meta>) -> Result<Stream<Value>>;
}
```

Two decisions differ from the other runtimes, both forced by what the plan carries and
what Rust can type:

- **The request reaches the core already dumped, and the reply leaves it undecoded.** A
  core sees `serde_json::Value`s on both sides: the wire keys and wire forms of the
  request (a `TimestampMillis` is already an integer), and the wire body of the reply,
  envelope unwrapped and errors mapped. Generated code decodes it, or hands it back raw.
  A core therefore never validates and never reads a `validate` flag; that decision is the
  generated method's, after the call. In Python and TypeScript the core receives the
  request type or codec and validates itself; in Rust that would make every trait generic
  in two types the core does not care about, and `dyn` dispatch impossible.
- **There is no `ClientRoot` or `Composite` trait.** The root is a struct the generator
  writes, and the hand-written code builds it: `GitHub::from_core(core)`. A router whose
  endpoints all hold the same contract hands a clone of one `Arc<dyn HttpEndpoint<Meta>>`
  to every child; one whose subtree needs several `meta` shapes is generic in the core,
  `from_core<C>(core: C) where C: HttpEndpoint<DefaultMeta> + HttpEndpoint<FuturesMeta> +
  'static`, and the `Arc<C>` it wraps coerces to each child's trait object. Nothing needs
  a `new(...)` protocol; `params` are what the hand-written core takes when it is built.
- **The root's constructor is `from_core`, not `new`, and it takes the core by value.**
  Both halves are about the call site. `new` is left free for the hand-written core to
  define as an inherent impl on the generated type -- the Rust answer to the base class
  `[python.cores.root]` names -- so a client reads `GitHub::new(CoreOptions::default())`
  rather than `GitHub::new(Arc::new(Core::new(CoreOptions::default())))`. And the `Arc` is
  the generator's storage decision, not the caller's, so the root wraps what it is given;
  `truewire-core` implements the endpoint traits for `Arc<T>`, so a core already shared
  between two clients satisfies the same bound and goes through the same door. A composite
  root takes one parameter per declared field and is named `from_cores`.

A hand-written HTTP core does four things in `request`, as `examples/github`'s
`src/github/core/mod.rs` does: fill the `{placeholders}` from the request object and send
the rest as the query (or as a JSON body for POST/PUT/PATCH) through `HttpClient`, add its
headers and signing, map a non-2xx reply to `Error::Api` with the status and decoded body
(`Error::auth(msg).with_status(401).with_body(body)`; `ApiKind::{BadRequest, Auth,
RateLimited, Api}`), and return `response.json()?` (or the `envelope.payload` inside it).
`http::query_from` renders a dumped request object as query pairs the way the example
cores do (`null` skipped, a nested value as JSON text). The crate root declares `pub mod
core;` for it, the one module the generator does not write, as the Python package's
`[python.cores.<name>].base` names the class it does not write.

`meta` is the generated `<package>/meta.rs` struct for the core's `[cores.<name>].meta`
schema (`DefaultMeta { public: Option<bool> }` in the examples, a property required by the
schema being a plain field), or `()` for a core with no schema. Its properties map narrowly:
scalars, an `enum` of one scalar kind to that scalar, arrays of those, and
`serde_json::Value` for anything else.

## gRPC endpoints

A `kind: grpc` endpoint (ADR 0017) is rendered over the `prost` messages `truewire protos
rust` builds from `spec/proto/`. The command runs `buf generate` with `protoc-gen-prost`
(prost 0.14) over the stripped tree and writes `<package>/protos/`: one `<proto
package>.rs` per package, `descriptors.binpb` (the tree's `FileDescriptorSet`) and a
`mod.rs` nesting one module per package segment around each file's `include!`, the way
`prost-build` names them, with `FILE_DESCRIPTOR_SET`. The stubs are checked in like the
generated modules; the crate needs no `build.rs` and no `protoc`. `truewire protos rust
--check` compares them with a fresh build.

```sh
cargo install protoc-gen-prost --version 0.5.0 --locked   # beside buf
truewire protos rust       # src/<package>/protos/
truewire generate rust     # the endpoints, and `pub mod protos;` in lib.rs
```

```toml
[dependencies]
prost = "0.14"
prost-types = "0.14"     # when a message uses a well-known type
truewire-core = { version = "0.2", features = ["grpc"] }
```

Each endpoint module aliases its messages and calls one verb on its core:

```rust
pub const METHOD: &str = "/cosmos.bank.v1beta1.Query/AllBalances";
pub type Request = crate::protos::cosmos::bank::v1beta1::QueryAllBalancesRequest;
pub type Response = crate::protos::cosmos::bank::v1beta1::QueryAllBalancesResponse;
pub type Row = crate::protos::cosmos::base::v1beta1::Coin;

pub async fn all_balances(&self, request: Request, options: CallOptions) -> Result<Response> {
    let call = GrpcCall { method: METHOD, request: request.encode_to_vec(), meta: &(), options };
    let reply = self.core.invoke(call).await?;
    decode_message("cosmos.bank.v1beta1.QueryAllBalancesResponse", &reply)
}

pub fn all_balances_paged(&self, request: Request, options: CallOptions) -> PaginatedResponse<Row, Vec<u8>>;
```

The contract is the one trait that does not speak `Value`: `GrpcEndpoint<Meta>::invoke(GrpcCall
{ method, request: Vec<u8>, meta, options }) -> Result<Vec<u8>>`. Carrying encoded messages
keeps it object-safe for every message type and free of `prost`, and the generated method
does the typed half (`grpc_codec.rs` holds the one decode helper, a `ValidationError` when a
reply does not decode). `truewire_core::grpc::GrpcClient` (feature `grpc`, `tonic`)
implements it for every `Meta`, so a gRPC-only client is `GrpcDemo::from_core(GrpcClient::new(url)?)`;
it maps statuses onto the error taxonomy (`NOT_FOUND` and `INVALID_ARGUMENT` are bad
requests, `UNAVAILABLE` a network error, and so on). There is no `_raw` twin. Walks clone
the request, set the driver path (`request.pagination.get_or_insert_with(Default::default).key
= state`) and read rows, cursor and total straight off the fields; a `token` walk's state is
the cursor's Rust type (`Vec<u8>` for Cosmos `next_key`), a `page` walk's the index's
(`u64`). `dispatch.rs` gains `call_grpc(function, request: &[u8], options) -> Result<Vec<u8>>`,
which decodes the request into the method's `Request` and encodes what it returned.

Names follow `prost-build` (`heck`): a message is `UpperCamelCase` (`QueryBTCRequest` is
`QueryBtcRequest`), a nested type sits in a module named after its parent
(`search_response::Hit`), a field is `snake_case` with keywords raw (`r#type`), a singular
message field and a proto3 `optional` scalar are `Option`s, an enum field an `i32`, and
the well-known types come from `prost-types`.

Testing: `truewire-testing` with feature `grpc` has `GrpcMock`, an in-process gRPC server
over `tonic` answering every recorded gRPC example (a request equal to a recorded one, as
a message, gets its response; anything else `NOT_FOUND`; a method with no recording
`UNIMPLEMENTED`), `grpc_examples`, and `replay_grpc`, which encodes each recorded request,
calls the generated `call_grpc` and compares the reply with the recording as messages. A
recorded `google.protobuf.Any` is read in proto JSON's `@type` form or betterproto's
`type_url`/`value` form; one whose `@type` names a message `spec/proto/` does not declare
cannot be encoded, so that example is left out of the mock and reported skipped:

```rust
let protos = Arc::new(Protos::from_descriptor_set(dydx::protos::FILE_DESCRIPTOR_SET)?);
let mock = GrpcMock::start(env!("CARGO_MANIFEST_DIR"), protos.clone()).await?;
let client = GrpcDemo::from_core(GrpcClient::new(mock.url())?);
let examples = grpc_examples(env!("CARGO_MANIFEST_DIR"));
let client = &client;
replay_grpc(&examples, &protos, |function, request| async move {
    client.call_grpc(&function, &request, CallOptions::default()).await
})
.await
.assert_passed();
```

`packages/truewire/test/test_codegen_rust_grpc.py` does exactly that for the `grpc_client`
fixture, plus both walks, after `cargo fmt --check` and `cargo clippy -- -D warnings`; it
skips without cargo, `buf` or `protoc-gen-prost`.

## Protobuf sources

A project with `spec/proto/**/*.proto` gets `<package>/protos.rs`: `SOURCES`, each file's
path under `spec/proto/` beside its text (ADR 0016). A hand-written core compiles them once
with `truewire_core::proto::Protos::from_sources(SOURCES)` (feature `proto`; imports resolve
among the sources, and `google/protobuf/*.proto` resolve too), decodes a binary frame with
`decode(envelope, bytes)` and narrows it with `narrow(&frame, field)`. A project with gRPC
endpoints gets no `protos.rs`: its `protos` module is the `prost` one above, and a core
compiles the same messages with `Protos::from_descriptor_set(protos::FILE_DESCRIPTOR_SET)`.

A router whose endpoints are all hand-written (mexc's protobuf-framed spot streams) is still
rendered, holding the core those endpoints would hold as `pub(crate) core`, so the inherent
`impl` written beside it has a struct and a transport.

## Recording

`HttpClient::recording()` is the Rust form of `truewire_core.http.recording()`: every
exchange the client makes until the `Recording` is stopped or dropped, in order, at the
wire level (method, URL with query, headers, body sent; status, headers, body received),
before the core unwraps an envelope or maps an error. It is what a future `truewire
capture` reads through a generated Rust client. Recordings may overlap, and
`HttpClientOptions::on_exchange` is the permanent form.

## Proxy

Packages clause P18: a proxy is given explicitly, since a sandbox or a library caller
cannot always set `HTTPS_PROXY`. The generated client takes its core ready-made
(`from_core`), so the core is where the proxy goes, one URL for both transports:

```rust
let options = HttpClientOptions::default().with_proxy("http://127.0.0.1:3128")?;
let http = HttpClient::new(options);
let socket = Socket::new(dialect, SocketOptions::new(url)).with_proxy("http://127.0.0.1:3128")?;
```

`with_proxy` on `HttpClientOptions` builds the `reqwest` client (`http://` or `https://`
proxy; an explicit one ignores the environment, `NO_PROXY` included). On `Socket` every
connection is a `CONNECT` tunnel through an `http://` proxy, `ws://` and `wss://` alike,
credentials from the URL's userinfo. `""` means no proxy. Without one, HTTP reads
`HTTPS_PROXY`/`HTTP_PROXY`/`ALL_PROXY`/`NO_PROXY` as `reqwest` always did; the socket
reads nothing. The `github` and `kraken` example cores take `CoreOptions::proxy`, and
`try_new` returns a bad one as an error where `new` panics.

## WebSocket commands and dual transports

An `rpc` endpoint whose `transports` is `["ws"]` renders like an HTTP one, holding an
`Arc<dyn CommandEndpoint<Meta>>` and handing it a `CommandCall { path, request, meta,
options }` whose `path` is the wire method name. One declaring `["http", "ws"]` holds a
core satisfying both and picks the transport per call from `CallOptions::transport`
(`Transport::Http` or `Transport::Ws`), defaulting to the one its spec lists first -- the
Rust form of the Python backend's `transport=` keyword:

```rust
let time = client.public.get_time(CallOptions::default().transport(Transport::Ws)).await?;
```

A trait object names one trait, so a core that must be several at once (a dual-transport
endpoint, a router holding commands beside streams, a composite field handed to both) is
held as a combined trait the generator declares in `contract.rs`, with a blanket impl:
`pub trait CommandStreamEndpoint: CommandEndpoint + StreamEndpoint {}`. A core
implementing the parts implements the whole, and the value upcasts to each part where a
child holds only one.

## Calling by function path

`dispatch.rs` adds `call`, `call_raw`, `subscribe` and `subscribe_raw` to the root: every
generated method by its dotted function path, with a wire request in and a wire value out
(`call` decodes the request into its `Request`, calls the typed method and dumps the
result; `call_raw` calls the `_raw` twin). It is the lookup Python gets from `getattr` and
TypeScript from indexing the client, written out because Rust has no reflection, and it is
what `truewire-testing`'s replay and anything else driving a client from data use. An
unknown function is a `LogicError`.

## Streams

A `stream` endpoint's generated method calls
`subscribe` with the channel template, the dumped parameters (or `None` for a
direct-channel or connect-only stream, where the method filled the template itself, as in
the other backends) and the endpoint's `meta`, and maps the core's `Stream<Value>` into
its message type:

```rust
pub async fn ticker(&self, parameters: Parameters, options: CallOptions) -> Result<Stream<TickerMessage>> {
    let stream = self.core.subscribe(SubscribeCall { channel: "ticker", parameters: Some(dump(&parameters)?), meta: &(), options }).await?;
    Ok(stream.map(decode))
}
```

A stream whose parameters only fill its channel template (`tickers.{symbol}`, no
parameters object on the wire) takes a generated `Parameters` struct of those fields; the
method dumps it, fills each placeholder with the field's query text (a string verbatim, a
number as its digits) and subscribes with `parameters: None`, as the TypeScript and Go
methods do.

A stream declaring a `reply` schema (ADR 0014) returns `Stream<Message, Reply>`: the typed
method adds `.map_reply(decode)`, so `stream.reply` is the acknowledgement's own type.

`Stream<N>` is a `futures::Stream<Item = Result<N>>` with the acknowledging `reply` and an
`unsubscribe()`; a dropped connection yields one `NetworkError` item and ends it. There is
no lazy `Subscription` and no `await using`: `subscribe` is an `async fn`, and a value that
is dropped without `unsubscribe()` forgets the subscription locally without sending a
frame.

The hand-written WebSocket core is a `Dialect` given to a `Socket`. Where `@truewire/core`
has `Socket`/`Rpc`/`Streams`/`StreamsRpc`/`SerialReplies` to subclass, the Rust runtime
owns all connection and correlation state in `Socket` and asks the core only for the wire
dialect: `parse(frame)` into `Incoming::{Reply{id, reply}, Push{channel, notification},
Serial(reply), Ignore}`, `encode_request(id, request)`, `subscribe`/`unsubscribe` returning
`Outgoing::{Rpc(request), Serial(frame), Nothing}` (acknowledged by the correlated reply,
by the next serial acknowledgement, or by nothing), `check_ack` to refuse a subscription,
`ping` for an application-level ping frame, and `on_open` for a handshake on every fresh
connection (a `Link` to send on before the connection is current). Kraken's `req_id`
dialect is `Outgoing::Rpc` everywhere; the `ws` template's uncorrelated `subscribed` acks
are `Incoming::Serial` and `Outgoing::Serial`. `Socket` then serves `rpc_request`,
`subscribe` (with `SubscribeOptions::request_channel`/`message_key` for one wire channel
feeding several local ones), `serial_request`, `wait` and `close`; the connection opens on
first use, and one the peer dropped fails every caller on it and is reopened by the next
use. `crates/truewire-core/tests/ws.rs` holds three dialects (`Rpc`-only, `Streams` with
serial acks, `StreamsRpc` with a handshake) driven against an in-process server.

## Pagination

`<method>_paged` is rendered from the plan's pagination decisions, as in TypeScript:

- `walker: paginated` (a `page` walk ended by `short_page`, `empty` or `total`; a `token`
  walk ended by `absent_cursor`; a plain `seek` walk): a plain method returning
  `PaginatedResponse<Row, State>`: `.await` (every row, flattened; also `all()`), `rows()`
  (one non-empty page at a time), `pages()` (every page with the state before and after
  it, for checkpointing), `resume(state)` and `via(invoker)`. Its `next(state)` is pure in
  `state`, so a page can be retried and a walk resumed. The request type is the endpoint's
  `Request` without the driver parameter (`ListPagedRequest`), unless the cursor is
  required and seeds the walk, in which case it is `Request` itself.
- A `token` walk over a union payload (one page shape per product category) matches the
  payload's variant, reads its rows and its cursor there (a variant without the cursor ends
  the walk), and yields the union of the variants' row types as one untagged
  `<Walker>PagedRequestRow` enum, or the shared row type when every variant has the same one.
- A page size or a page `total` the wire sends as a string is parsed; a size that is not a
  number is unknown, and a total that is not one never ends the walk.
- The terminator checks are the runtime's, not the emitter's: `exhausted(rows, size)` for
  `short_page`/`empty`, `total_reached(...)` for a `page` walk ended by `total`,
  `cursor_or_done(cursor)` for `absent_cursor` (a zero-valued cursor is absent, as the
  plan's `has_zero_value` says), and `TotalSeen` for the rule that a missing `total`, or
  one that disagrees with an earlier page of the same walk, is a `LogicError`.

The `page`-strategy body of GitHub's `issues.list` walker reads:

```rust
pub fn list_paged(&self, request: ListPagedRequest, options: CallOptions) -> PaginatedResponse<Issue, i64> {
    let endpoint = self.clone();
    let size = request.per_page.unwrap_or(30);
    let size = Some(size as usize);
    let next = move |page: i64| {
        let endpoint = endpoint.clone();
        let request = request.clone();
        let options = options.clone();
        async move {
            let request = request.at(Some(page));
            let response = endpoint.list(request, options).await?;
            let rows = response;
            if exhausted(rows.len(), size) {
                return Ok((rows, None));
            }
            Ok((rows, Some(page + 1)))
        }
    };
    PaginatedResponse::new(1, next)
}
```

`at(page)` is a private method of `ListPagedRequest` building the single-call `Request`
for one page. Every read of the response (rows, a cursor, a `total`) is rendered through
the type tree, so a nullable page or an optional cursor is read through `Option` rather
than assumed present.

## Seek and offset walks

A `seek` walk (ADR 0013) returns `PaginatedResponse<Row, SeekState<Key, Row>>`. Its request
is the single call's own `Request` (both bounds kept); `init` is the caller's moving bound,
each page is requested from the state's `pos` (and, with a `span`, up to the span edge as
the far bound) with an integer page size clamped once to at least 2 and at most the
schema's `maximum` (a page must hold one new row beside the one it re-reads; the clamped
size is also the cap), and the page is folded back through `truewire_core::Seek`, which
owns the whole algorithm: re-served boundary rows deduplicated by key (`unique`) or by
content, the moving bound set to a full page's extreme key, span chunks never past the
caller's far bound, and a `LogicError` for a full page stuck on one key or a carried row
the venue stopped serving. `Key` is the moving bound's type (an integer, a float, a string
id compared by equality, `IntegerString`, or any timestamp newtype); a row's key is read
through its wire form, so a numeral-string field walks a numeric bound. An `offset` walk's
state is the offset, stepped by the rows each page held. A `total` terminator only decides
when to stop: a missing or moving total is not an error, and no state is kept outside
`next`'s argument.

## Testing

`crates/truewire-core/tests` is at the granularity of `packages/core-ts/test`: `errors.rs`,
`validation.rs` (a generated-shaped struct through parse, dump, every format, every
container, every failure path), `times.rs` (the converters), `http.rs` (a loopback HTTP/1.1
listener: query, JSON and raw bodies, statuses, network failures, timeouts, recording),
`paging.rs` (the resumable contract and the terminator helpers) and `ws.rs` (three
dialects against an in-process `tokio-tungstenite` server: correlation out of order, lazy
open, drop and reconnect, close, refused and hanging connections, close frames, dialect
errors, pings, subscribe/push/unsubscribe, `message_key` routing, refused subscriptions,
`map`/`filter`, serial ordering, and an `on_open` handshake). No test touches the network.

`crates/truewire-testing` is the harness for a generated package: `Mock::start(project)`
runs `truewire mock` on free ports and reads the HTTP and WebSocket URLs it announces
(`TRUEWIRE_BIN`, else the nearest `.venv/bin/truewire`, else `PATH`); `http_examples` and
`ws_examples` discover every recording with its function path; `replay` runs a check over
all of them and fails once, naming every failure; `Replayed::compare` checks the typed
value dumps back to the raw one. `examples/kraken/tests/replay.rs` is a whole replay test
in thirty lines, through `dispatch.rs`.

`examples/github/tests` is the older pattern for a generated package. `common/mod.rs` spawns
`truewire mock --http-port 0 --ws-port 0` and builds the client against its URL;
`replay.rs` walks `spec/endpoints/**/examples/*.request.json`, decodes each recorded
request into its `Request` struct, calls the method and its `_raw` twin, and checks the
typed value dumps back to exactly the body the wire sent; `paging.rs` walks the same
recorded multi-page captures as `test/test_paging.py`. CI (`examples-rust`) runs
`truewire generate rust --check`, `cargo fmt --check`, `cargo clippy --all-targets -- -D
warnings` and `cargo test`. The backend's own tests (`packages/truewire/test/
test_codegen_rust.py`) render the fixture client and, when `rustfmt` is installed, prove
every file passes `rustfmt --check`.

## Not generated yet

- Walks the resumable form does not cover (a `page` or `token` walk the plan marks
  `walker: generator`): the plain method is generated and the walker reported as skipped.
- Streaming `grpc` methods, and a gRPC walk driven through a `oneof` member or ended by
  anything but `absent_cursor`/`empty` (token) or `total`/`short_page`/`empty` (page):
  the method is generated and the walker reported as skipped.
- Publishing `truewire-core` to crates.io, and a `release/core-rust` workflow in the shape
  of `release-core-ts.yml`.
- `truewire docs check` for ```rust blocks and `truewire surface` for the snake_case rule.

Two gaps are the plan's rather than the backend's, recorded in `docs/plan.md`: every
integer renders as `i64` (the plan carries no width), and a union renders
`#[serde(untagged)]` (the plan carries no discriminator). Two runtime limitations a
reader should know: a literal mixing strings with numbers widens to `serde_json::Value`,
and a `DecimalString` (the exact wire text over a `bigdecimal::BigDecimal`, any number of
digits) is `Clone` but not `Copy`.
