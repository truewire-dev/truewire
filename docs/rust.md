# Rust

`truewire generate rust` renders a project's plan (`docs/plan.md`) as the modules of a Rust
crate with the same guarantees as the Python and TypeScript packages: typed requests and
responses, runtime validation with a raw escape hatch, and resumable pagination walkers.
The runtime it depends on is `truewire-core` (`crates/truewire-core`), the third after
`truewire-core` (Python) and `@truewire/core` (TypeScript). Both were designed from the
plan, the language-neutral IR every backend renders from, rather than by transliterating
either earlier runtime.

Status: the runtime exists and is tested (`cargo test`, `cargo clippy -- -D warnings` and
`cargo fmt --check` run in CI, job `runtime-rust`). The generator renders HTTP `rpc`
endpoints, the type tree, routers, the root and the `PaginatedResponse` walkers;
`examples/github` is generated, compiled and replayed against `truewire mock` in CI (job
`examples-rust`). Stream endpoints, WebSocket commands and composite cores are not rendered
yet and are reported as skipped, the way the TypeScript backend reported them before its
second pass. The crate is not on crates.io (the `truewire` crate name is reserved by
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
truewire generate rust             # writes src/github/**/*.rs and .truewire/rust-files.json
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
truewire-core = "0.1"
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
as an `Arc<dyn HttpEndpoint<Meta>>` (or `CommandEndpoint`/`StreamEndpoint`, once those are
rendered), the Rust half of `truewire_core.contract` and `contract.ts` (ADR 0011):

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

## Recording

`HttpClient::recording()` is the Rust form of `truewire_core.http.recording()`: every
exchange the client makes until the `Recording` is stopped or dropped, in order, at the
wire level (method, URL with query, headers, body sent; status, headers, body received),
before the core unwraps an envelope or maps an error. It is what a future `truewire
capture` reads through a generated Rust client. Recordings may overlap, and
`HttpClientOptions::on_exchange` is the permanent form.

## Streams

The runtime is built; the generator does not render `stream` endpoints yet (they are
reported as skipped). When it does, a `stream` endpoint's generated method calls
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

`examples/github/tests` is the pattern for a generated package. `common/mod.rs` spawns
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

- `stream` endpoints, `rpc` endpoints reached only over a WebSocket, and routers under a
  composite core (`children`/`forward` in `truewire.toml`) with everything beneath them:
  the runtime has `StreamEndpoint`, `CommandEndpoint` and `Socket` for them, and the
  generator reports each as skipped. `examples/kraken` therefore has no Rust package yet.
- `window` walks, `seek` walks with `overlap`, the `unchanged` terminator, and the walks
  the plan marks `walker: generator` (`offset`, and the shapes the resumable form does
  not cover): the plain method is generated and the walker reported as skipped.
- Publishing `truewire-core` to crates.io, and a `release/core-rust` workflow in the shape
  of `release-core-ts.yml`.
- `truewire docs check` for ```rust blocks and `truewire surface` for the snake_case rule.

Two gaps are the plan's rather than the backend's, recorded in `docs/plan.md`: every
integer renders as `i64` (the plan carries no width), and a union renders
`#[serde(untagged)]` (the plan carries no discriminator). Two runtime limitations a
reader should know: `rust_decimal` holds 28 significant digits, so a `decimal-string`
beyond that fails validation rather than losing precision silently, and a literal mixing
strings with numbers widens to `serde_json::Value`.
