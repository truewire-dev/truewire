# Changelog

## 0.10.1 (2026-09-11)

- `truewire --version` (and `-V`) prints the installed toolchain version. It was the first command a newcomer ran after `pip install truewire`, and it was an error.

## 0.10.0 (2026-09-09)

**Breaking, Rust backend only.** The generated root struct's constructor is renamed and
takes its core by value. Regenerate, and change the one line that builds the client.

- **The generated root is constructed with `from_core`, not `new`.** The root is the one
  struct a caller ever constructs, and it had the good name, so a hand-written core had
  nowhere to put a better one -- two inherent `new`s on one type collide. `new` is now
  left free, and a core defines it as an inherent impl on the generated type, in the same
  crate: the Rust answer to the base class `[python.cores.root]` names in the Python
  backend. A composite root is `from_cores`, one parameter per declared field, as before.
- **It takes `impl HttpEndpoint<Meta> + 'static` rather than `Arc<dyn ...>`.** The `Arc`
  is the generator's storage decision, not the caller's, so the root wraps what it is
  given. `truewire-core` 0.1.1 implements the endpoint traits for `Arc<T>`, so a core
  already shared between clients still fits the same parameter.

Together those turn the first line of every Rust client from

```rust
let client = GitHub::new(Arc::new(Core::new(CoreOptions::default())));
```

into

```rust
let client = GitHub::new(CoreOptions::default());
```

with the core supplying that `new`. `examples/github` shows the impl; `docs/rust.md` says
why it is shaped this way.

## 0.9.1 (2026-09-09)

Both findings come from building the weather.gov showcase, which is the point of building
one.

- **An enum member spelled like its own record broke the generated Python.** A record's own
  name is quoted inside its class body, because generated code cannot use `from __future__
  import annotations` and a recursive field would otherwise raise `NameError` at import.
  That substitution was textual and unconditional, so it rewrote string literals too: a
  record `Alert` whose own `messageType` is `Literal['Alert', 'Update', 'Cancel']` came out
  as `Literal[''Alert'', ...]` -- a syntax error in `schemas.py`, so nothing downstream ran
  at all. `api.weather.gov` really is shaped that way. The substitution now skips the string
  literals already in a rendered type expression, which also makes it idempotent, which it
  was not.
- **Rule 18 now refuses a router group that collides with a shared schema.** A `forecast/`
  group beside a `Forecast` in `schemas.json` renders one name for two things, because the
  module composing the group also imports the shared types. `truewire check` passed it,
  Python generated and ran, and `tsc` then refused it (`TS2440`/`TS2395`) -- one backend's
  silence is not evidence a name is free. It is refused at the gate now, in either
  direction, with both renames named in the message.

## 0.9.0 (2026-09-09)

- **The Rust backend renders a composite root, and stream endpoints.** A router whose core
  hands different children different transports had no Rust rendering, and skipping the
  root took every descendant with it: a client that speaks both HTTP and a WebSocket -- the
  combination Truewire exists for -- produced its types and nothing else. A composite `new`
  now takes one parameter per declared field (`Bluesky::new(client, socket)`), handing each
  child the field `[cores.<name>] children` maps it to; a child that is itself composite
  takes its own fields, so they pass straight through. `kind: "stream"` endpoints render as
  `subscribe`, typed and `_raw`, over the runtime's `StreamEndpoint`. The Bluesky showcase
  went from 3 generated files to 22, compiles with warnings as errors, and replays its
  recordings over a real WebSocket.
- **A skipped Rust router said so; the eleven endpoints it took with it did not.** Modules
  that rendered and were then dropped as unreachable are now reported, with their names, so
  the size of a hole is visible rather than inferred from a file count.
- **`_size` was bound for every Rust walk and read only by two of them.** Every
  cursor-paged endpoint carried two dead lines and a `unused variable: size` warning -- six
  on a clean build of one client. It is bound where it is read.
- **`truewire mock` can serve a browser.** It sent no `Access-Control-Allow-Origin` and
  answered `OPTIONS` with 501, so a generated client running in a browser could not be
  tested against it at all: the preflight failed before the request was made. It now sends
  the headers and answers the preflight.
- The core-shape computation that decides what a router's constructor takes moved to
  `truewire.codegen.shapes`, shared by the TypeScript and Rust backends rather than
  duplicated. A subtree a backend rendered nothing for no longer contributes a field, which
  used to render as `Arc<dyn >`.

## 0.8.2 (2026-09-08)

- **`truewire capture` recorded the wrong exchange, and it could be a credential.** It
  wrote `exchanges[-1]`, the *last* request that went through `truewire_core.http.HttpClient`
  during the call, rather than the one belonging to the endpoint being captured. A
  hand-written core legitimately sends more than one request around an endpoint call --
  minting an OAuth token, refreshing an expired one, fetching a WebSocket ticket, retrying
  after a 401 -- and whenever one of those landed last, *its* response body was written to
  `spec/endpoints/**/examples/<id>.response.json`, then committed and published. A token
  endpoint's 200 body is an access token, so a capture through such a core could publish a
  live credential to a public repository, which is exactly what `docs/spec/authoring.md`
  rule 6 exists to prevent. `truewire check` catches it only when the foreign body happens
  to violate the endpoint's response schema, and the file is written before the check runs
  either way, so the recording is on disk regardless. **Anyone who captured through a core
  that makes more than one request should re-read every recorded response it wrote, and
  rotate any credential that appears in one** -- a value published in a git history stays
  published after the file is deleted.
  `capture` now records the exchange the endpoint declares: its method plus its `path`
  filled from the call's own parameters, matched against the tail of the wire URL path
  (so a base URL's own prefix and any query string are ignored), or, for a JSON-RPC-shaped
  endpoint whose `path` is a method name, that name read off the posted frame at the
  envelope's selector. No match records nothing and fails, naming the endpoint, the route
  it expected and every request the core actually made -- method and path only, since a
  header, a request body and a response body are where a credential would be, and an
  `ApiError` message is withheld for the same reason. Several matches are all this
  endpoint's own attempts (a retry re-sending the same call), so the last stands and the
  output says how many matched. Every capture now reports which exchange it recorded and
  which it skipped; there is no flag that restores the old behaviour.
- `truewire_core.http.recording()` (and its TypeScript and Rust twins) document that the
  list holds everything the core sent and that an exchange is identified by its request,
  never by its position -- the assumption that produced the bug above was taught in their
  own examples.
- `truewire.spec.rpc_selector`: the rule naming an RPC frame's operation (`envelope.selector`,
  or JSON-RPC's `method`), moved out of `truewire.mock` so the mock server and `capture`
  read one canonical definition.

## 0.8.1 (2026-09-08)

- **A router group may no longer claim a class name that is already taken**
  (`docs/spec/authoring.md` rule 18). A client whose declared `name` matches one of its
  router groups generated code that did not build: Rust's `client.rs` imported
  `crate::weather::Weather` and then declared its own `Weather`, so the root struct
  contained itself (`E0255`, `E0072`, `E0391`), and TypeScript's `main.ts` did the same
  by bare import (`TS2440`, `TS2395`). Python raised nothing at all -- the class shadowed
  the import, so `client.weather` returned another root client and every endpoint under
  the group became unreachable. The same shape one level down (a group whose class name
  is its parent group's) and sideways (`list-orders` and `list_orders`, which both render
  `ListOrders`) is refused with it. `truewire check` reports it as an `error` naming the
  client, the group and the `router.json` it comes from, and `truewire generate python`,
  `typescript` and `rust` refuse the same condition before writing a file. The rename is
  never made for you: the root class name and every group attribute are the client's
  public surface.

## 0.8.0 (2026-09-08)

- **`truewire generate rust`.** The third backend, reading the plan into the modules of
  a crate over `truewire-core`: one `serde` struct per record (`snake_case` fields with
  `#[serde(rename)]` carrying the wire name, `Option` and `double_option`, a flattened
  `extra` map, cycles boxed), an enum per string literal and per untagged union (inline
  ones hoisted and named after their position), the runtime's newtype per format, one
  struct per HTTP `rpc` endpoint holding an `Arc<dyn HttpEndpoint<Meta>>` with the typed
  method, its `_raw` twin and a `<method>_paged` walker over `PaginatedResponse` for the
  `page`, `token` and plain `seek` strategies, routers delegating to their endpoints, the
  root, `meta.rs` and `lib.rs`. `[rust]` in `truewire.toml` (`package`, `src`, `name`)
  declares it, under the same manifest discipline as the other backends
  (`.truewire/rust-files.json`, `--check`, `--delete`). The output is printed to satisfy
  `cargo fmt --check` without running a formatter. Stream endpoints, WebSocket commands,
  composite cores, `window`/`seek`-with-`overlap`/`unchanged` walks and generator-shaped
  walks are reported as skipped for now (`docs/rust.md`).
- **`examples/github` generates Rust** beside its Python and TypeScript packages: a
  hand-written core over `HttpClient`, a replay test proving every recorded example
  decodes and dumps back to the wire body unchanged, and the paging walks. CI's
  `examples-rust` job runs `generate rust --check`, `cargo fmt --check`, `cargo clippy`
  and `cargo test`.

## 0.7.0 (2026-09-08)

- The toolchain and `truewire init` now require `truewire-core>=0.2.1`, the release whose
  converters accept an already-parsed `date`/`datetime`; a generated request carrying a
  real `date` fails to validate against 0.2.0.
- **`truewire generate typescript` renders stream endpoints.** A `kind: stream` endpoint
  is a class over `StreamEndpoint<Meta>` whose method returns the core's
  `Subscription<Message>` (`Subscription<unknown>` under `validate: false`), handing the
  core what the Python backend hands `self.subscribe(...)`: the channel template with the
  `Parameters` object and its codec, or the channel filled from the parameters for a
  direct-channel or connect-only stream. `@truewire/core` exports `Subscription` from its
  root for it.
- **`truewire generate typescript` composes cores.** A router under a core declaring
  `children` or `forward` takes a fields object, exported as the `<Class>Core` interface,
  and hands each child its declared field, or the whole object to a composite child;
  `params` needs no rendering, since generated code never builds a core (ADR 0011). The
  groups this used to skip (`examples/kraken`'s `root` and `streams`) are generated. The
  one remaining skip is the `ws` half of an `rpc` endpoint declaring both transports.
- **`examples/kraken` generates TypeScript** beside its Python package: a hand-written
  core in three transports (REST with HMAC-SHA512 signing and a nonce, two WebSocket v2
  connections over `ws.StreamsRpc`), and a vitest suite against `truewire mock` mirroring
  the Python one. CI's `examples-ts` job runs both examples through `generate typescript
  --check`, `tsc` and `vitest`.
- **Recursive schemas render instead of crashing.** A schema may reference itself --
  directly, through an array's `items`, or around a cycle of several schemas -- as long
  as one schema on the cycle is a record (`docs/spec/authoring.md` rule 17). Two records
  referencing each other used to pass `truewire check` and then raise
  `CircularDependencyError` from `generation_order`; the order now collapses each cycle
  before sorting, and the reference that points forward is emitted quoted. A cycle where
  *no* schema is a record cannot be expressed at all: it used to pass `truewire check`
  and then either die in generation with a bare `RecursionError` or emit an alias naming
  itself (`Node = dict[str, Node]`, a `NameError` on import). It is now one
  `schema-cycle` violation naming the schemas on it, and generation refuses the same
  shape with a `SchemaCycleError` if the gate is skipped. `LocalResolver`'s unreachable
  `Cycle detected` guard is gone -- its mapping never held a reference for it to loop on.
- **`truewire capture` drops the stale `unverified` block.** The pair it writes is the
  evidence the declaration said was missing (ADR 0001), and leaving the block in place
  failed `truewire examples` on the next run. `capture` now removes it from
  `endpoint.json`, every other key kept in place, and says so. The `examples` failure for
  a stale declaration names the endpoints in the message itself instead of asking for
  `--verbose`.
- **`truewire generate python --check` compares content.** It reported only manifest
  ownership and file existence, so an owned file whose body was stale passed; it now
  renders the plan the way `generate` writes it (banner, Ruff formatting) and lists every
  owned file that differs as `out of date`, exiting non-zero, the way the TypeScript path
  already did.
- **`truewire init .`** writes the project into the current directory and names the package
  after it (`open-meteo` -> `open_meteo`); so does `truewire init <name>` run inside an
  empty directory named `<name>` (a `.git` or `.venv` there does not count). Anywhere else
  `truewire init <name>` still creates `./<name>`. An existing `.gitignore` gains the
  lines `init` writes instead of being replaced.
- **`generate --check` works without a manifest.** `.truewire/` is gitignored, so on a
  fresh clone `--check` failed with `missing manifest` and CI could never run it. Both
  backends now take the plan as the owned file list when `.truewire/<language>-files.json`
  is absent -- the plan names every file `generate` would write -- and check existence
  and content the same way, saying that the plan stood in; the one difference only a
  manifest can show (a file an earlier plan owned and this one does not) waits for the
  next `generate`. CI runs `truewire generate python --check` on both examples.

## 0.6.0 (2026-09-08)

- **`truewire init --template bearer|hmac|jsonrpc|ws`** (`docs/cores.md`): a hand-written
  core skeleton for the common API shapes. `bearer` (the default) is REST with a bearer
  token, a `public` meta flag and JSON errors; `hmac` signs requests with a nonce and the
  `[cores.signed]` meta; `jsonrpc` is JSON-RPC 2.0 over HTTP with the envelope unwrapped in
  the core; `ws` is a WebSocket core with a `streams` group (`--ws-url` sets the socket
  URL). Every template satisfies `truewire_core.contract`, passes `check` and `generate
  python` on a fresh project and is pyright-clean.
- **Docs**: `docs/generated.md` (what `truewire generate` writes, Python and TypeScript
  quoted verbatim from `examples/github`, and what stays hand-written), `docs/cores.md`
  and `docs/agents.md` (what to hand a coding agent, the CLI gates in order, `capture`,
  `mcp` versus the CLI plus skills, and three prompts to paste).
- **`validate=False` is typed as the raw body it returns.** A generated method's return
  type is the parsed record, which is only true when the reply was validated. Python:
  every request/reply method and every `_paged` walker now carries two `@overload`
  stubs -- `validate: Literal[False]` returns `Any` (`PaginatedResponse[Any, ...]`/
  `AsyncIterator[Any]` for a walker), `validate: bool | None = None` returns the
  declared type -- and the implementation keeps its header. TypeScript: two overload
  signatures per method and per router delegate, `options: CallOptions & { validate:
  false }` returning `unknown` first, then the declared one. Runtime behaviour is
  unchanged; `Function` gains `overloads`, the Python emitter `validate_overloads`, the
  TypeScript emitter `emit_signatures`/`raw_returns`. The `validate` docstring says what
  `False` returns. Both examples regenerated; `examples/github/test/typing_usage.{py,ts}`
  assert the types under pyright and `tsc`.
- **`truewire generate typescript`** (`truewire.codegen.typescript`, `docs/typescript.md`):
  a second backend, reading the same plan as the Python one and writing an ESM package:
  one `interface` plus a `Codec<T>` value per type (built from `@truewire/core`'s
  combinators, so `tsc` proves the two agree), one class per `rpc` endpoint with the method
  and a `<method>Paged` walker (`PaginatedResponse` for `walker: paginated`, an async
  generator otherwise), router classes that delegate, a root class taking the hand-written
  core, `meta.ts` and `index.ts`. Output is printed deterministically; no formatter runs.
  The generated code imports nothing from the project's core: it is typed by the
  `@truewire/core` contract (`HttpEndpoint<Meta>`) and the core satisfies it by shape.
- `truewire.toml` gains a `[typescript]` section (`package`, `src`, `name`). The manifest
  discipline is the Python one (`.truewire/typescript-files.json`, `--check`, `--delete`);
  `--check` also reports an owned file whose content differs from the plan.
- Stream endpoints, composite cores (`forward`/`params`/`children`), `window` walks and
  `seek`+`overlap` walks are reported as skipped by the TypeScript backend, not rendered.
- `examples/github` carries a TypeScript package beside its Python one: a hand-written
  `core/index.ts`, the generated code, and a vitest suite (replay of every recorded example,
  the two paging walks, codec round-trips) against `truewire mock`, run in CI.

## 0.5.0 (2026-09-08)

- **The plan** (`truewire.plan`, `docs/plan.md`): a frozen, JSON-serialisable model of
  every decision a backend renders -- package, cores, shared type scopes, routers, and
  per endpoint the wire location, request shape and fields, the returned type and its
  nullability, stream facts and the pagination decisions (`driver`, `size`, `done`,
  `rowType`, `stateType`, `seedable`, `walker`) -- computed once from a project by
  `truewire.plan.build.build_plan`. Types are the language-neutral tree with a
  `Scalar{base, format}` node; no Python name appears in the plan.
- `truewire generate python` builds the plan once and the Python backend reads its four
  formerly string-derived decisions from it (the cursor's seed type, seedability, response
  nullability, `cast(type, ...)`) instead of re-parsing rendered definitions. Generated
  output is byte-identical for both examples.
- `truewire plan [--project DIR] [--json]`: prints the plan as a one-line-per-endpoint
  summary, or as JSON for a second backend, a docs site or a test.

- **The generator reads a declared core contract and never imports the target package**
  (ADR 0011). Composing a child through `.new()` is declared in `truewire.toml`:
  `[python.cores.<name>]` gains `forward` (keywords passed from the composing class's own
  fields) and `params` (keywords a caller supplies, with their types). The `.new()`
  introspection, the `sys.path` patching and `truewire init`'s placeholder `main.py` are
  gone; a new project generates on its first run with nothing seeded. A test generates the
  fixture project with the target package refused from `sys.path`.
- `truewire generate python` writes `<package>/meta.py`: one `TypedDict` per
  `[cores.<name>]` that declares a `meta` schema, named `<Name>Meta`. A core annotates
  its `meta` parameter with it instead of hand-writing the class. `truewire init` writes
  the first one from the same renderer.
- Generated code imports the timestamp aliases (`TimestampMillis`, ...) and their
  converters from `truewire_core.types` instead of the project's `core`. `truewire init`
  writes `core/types.py` as a re-export, and pins `truewire-core>=0.2.0,<0.3`.
- Both example projects updated: cores import `Meta` from the generated module,
  `examples/kraken` declares `forward = ["market_client"]` for `streams`.

## 0.4.0 (2026-09-07)

- **A response schema describes the wire body; `envelope.payload` selects the returned
  value** (ADR 0010, authoring rule 6). Before, the schema described the value the core
  returned after unwrapping and `truewire check` extracted `envelope.payload` from a
  recording before validating. Now the schema describes the whole recorded frame, the
  checker validates the recording against it as-is, and the generator types the method's
  return value from the schema at `envelope.payload`. The method still returns the
  unwrapped value; cores, the mock server, `truewire capture` and `envelope.correlate` are
  unchanged. Pagination paths stay relative to what the method returns. `truewire import
  openapi` keeps writing the document's response schema, which was always the wire body.
  A new `envelope` lint rule (error) requires `envelope.payload` to name a property of the
  response schema. Stream `payload` schemas are unchanged.
- `truewire migrate`: rewrites a spec written under the previous rule. For every rpc
  endpoint declaring `envelope.payload` whose response schema does not carry that path,
  it wraps the schema into the frame its recordings show (`<Title>Frame`, other keys
  inferred and described as wire fields, the old schema under the payload key). It
  refuses an endpoint with no recording; `--template <function>` names a recorded
  endpoint whose frame stands in. A second run changes nothing.
- `examples/kraken` migrated with the command: 64 endpoints, generated source unchanged.

## 0.3.0 (2026-09-07)

- `truewire import registry <name>`: start a project from a spec in
  [truewire-dev/registry](https://github.com/truewire-dev/registry): copies the spec tree,
  merges the `[cores]` sections it needs into `truewire.toml`, runs `check`, and names any
  core the project still lacks a `[python.cores]` entry for. `--registry` accepts a git URL
  or a local checkout.
- Agent skills under `.agents/skills/` (discover, spec, core, implement, docs, review),
  proven by an agent run on endpoints not in the repository and revised from its findings.
- A `page` walk ended by `short_page` measures against the size parameter's documented
  `default` when the caller omits the size, instead of ending only on an empty page.
- `truewire examples` counts an endpoint with `meta.public: true` as public.
- `capture --help` shows a paginated walk recorded with the page index explicit.

## 0.2.0 (2026-09-07)

- `truewire capture <function> --request '{...}'`: call one endpoint against the live API
  through the project's own generated client and core, and record the pair under the
  endpoint's `examples/`; `--scrub KEY` replaces a secret-bearing response field with a
  placeholder, and the pair is checked against the spec afterwards. Requires
  `truewire-core>=0.1.1`.
- A `page` walk ended by a short or empty page (the `page`/`per_page` shape most REST
  APIs use) now generates the awaitable `PaginatedResponse` wrapper, like token, seek
  and page-with-total walks.
- `truewire check` reports a `$ref` that resolves to nothing as a violation instead of
  crashing.
- `examples/github`: a second real project, captured live from the GitHub REST API.

## 0.1.0 (2026-09-07)

First release. `truewire init | import openapi | check | examples | surface | generate python |
mock | mcp | standards | docs`: a directory-per-endpoint spec format with an 18-rule linter,
recorded-example coverage with honest `unverified` reasons, an HTTP and WebSocket mock server
that replays recorded examples, a Python generator with runtime validation and pagination
walkers, an OpenAPI 3.0/3.1 importer, and an MCP server that exposes a project's endpoints
as tools. `init` writes a `pyproject.toml` so the generated client installs with
`pip install -e .`.
