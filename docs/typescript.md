# TypeScript

`truewire generate typescript` renders a project's plan (`docs/plan.md`) as an ESM
TypeScript package with the same guarantees as the Python one: typed requests and
responses, runtime validation on by default with a per-call override, and generated
pagination walkers. The runtime it depends on is `@truewire/core` (`packages/core-ts`).

Status: `@truewire/core` publishes to npm from `packages/core-ts` (a merged
`release/core-ts` pull request, see [Releasing](../CONTRIBUTING.md#releasing)); a generated
project depends on it as an ordinary `package.json` dependency. `examples/github` and `examples/kraken` in
this repository link it with a relative `file:` dependency instead, so the examples always
test the runtime at the same commit. `rpc` endpoints over HTTP, over a WebSocket and over
both, `stream` endpoints (with a typed subscription `reply` when declared), every
pagination walk including `seek`, and composite cores (`forward`/`children` in
`truewire.toml`) are generated;
`examples/github` is the HTTP case and `examples/kraken` the WebSocket and composite one.
See the end of this page for what is still left out.

## Using it

```toml
# truewire.toml
[typescript]
package = "github"    # the package lives at src/github/
src = "src"
name = "GitHub"       # the root class
```

```sh
truewire generate typescript             # writes src/github/**/*.ts and .truewire/codegen/typescript.json
truewire generate typescript --check     # CI: every owned file exists and is what the plan renders
```

Then write `src/github/core/index.ts` (below), add `@truewire/core` to `package.json`, and
use the client:

```ts
import { GitHub } from 'github'
import { Core } from 'github/core'

const client = new GitHub(new Core({ token: process.env.GITHUB_TOKEN }))

const repo = await client.repos.get({ owner: 'truewire-dev', repo: 'truewire' })
repo.created_at                                    // a Date
const commits = await client.repos.listCommitsPaged({ owner: 'truewire-dev', repo: 'truewire', per_page: 50 })
for await (const page of client.issues.listPaged({ owner: 'truewire-dev', repo: 'truewire', state: 'all' })) ...
const raw = await client.repos.get({ owner: 'truewire-dev', repo: 'truewire' }, { validate: false })
```

`examples/github` is the reference: its `truewire.toml`, `src/github/core/index.ts`,
`package.json`, `tsconfig.json` and `test/` are what a project copies. `examples/kraken` is
the same shape for an API with a REST half and a WebSocket half behind one root:

```ts
import { Kraken } from 'kraken'
import { Core } from 'kraken/core'

const client = new Kraken(new Core({ credentials: { apiKey, privateKey } }))

const time = await client.spot.marketData.time()
await using ticker = client.streams.marketData.ticker({ symbol: ['BTC/USD'] })
for await (const message of ticker) message.data[0].timestamp   // a Date
const order = await client.tradingWs.addOrder({ symbol: 'BTC/USD', side: 'buy', order_type: 'market', order_qty: 1 })
```

## What is generated

One file per spec node, beside the Python package when both are declared:

| file | contents |
| --- | --- |
| `types/index.ts`, `types/<scope>.ts` | the shared `schemas.json` types, one module per scope |
| `<router>/<endpoint>.ts` | the endpoint's `Request` (a stream's `Parameters`), its response or message types, their codecs, and a class with the method |
| `<router>/index.ts` | a router class delegating to its endpoints and holding its child routers; under a composite core, the `<Class>Core` fields interface it takes |
| `main.ts` | the root class, `new GitHub(core)`; `new Kraken({ spot_client, market_client, private_client })` for a composite root |
| `meta.ts` | one interface per `[cores.<name>]` with a `meta` schema (`DefaultMeta`) |
| `index.ts` | re-exports the root class, the shared types and `meta.ts` |

Names Truewire invents are camelCase/PascalCase (`list_commits` becomes `listCommits`,
`listCommitsPaged`, class `ListCommits`); names the API invented stay verbatim
(`per_page`, `html_url`), so the request object *is* the wire object and a recorded
`request.json` is a valid argument as it stands. Every method takes the request as its
first parameter and an options object as its second: `{ validate?: boolean; signal?:
AbortSignal }`. `validate: false` returns the raw `JSON.parse` value, typed `unknown`: each
method is declared twice, an overload for `{ validate: false }` returning `unknown` and one
for every other call returning the declared type, as Python's `@overload`s return `Any` and
the record ([docs/generated.md](generated.md#validatefalse-returns-the-raw-body)). The output
is printed by the generator itself (two-space indent, sorted imports, JSDoc from the spec's
descriptions); no formatter runs over it.

A type is an `interface` (or a `type` alias) and, beside it, a codec of the same name:

```ts
export interface Label {
  id: number
  name: string
  description?: string | null
}

export const Label: Codec<Label> = t.object({
  id: t.integer,
  name: t.string,
  description: t.optional(t.nullable(t.string)),
})
```

`Codec<Label>` on the constant is what makes `tsc` prove the codec and the interface agree;
the two cannot drift. `parse` turns a decoded wire value into the typed one (`decimal-string`
into the branded `Decimal`, every timestamp format into a `Date` behind its alias, `date`
into `DateIso`, `integer-string` into a `bigint`, `boolean-string` into a `boolean`) and names the
JSON pointer of the first failure in a `ValidationError`; `dump` renders it back to the wire.

Numbers keep their precision. `parseJson` (and `parseJsonText`, which a hand-written core
uses for replies it unwraps itself) reads an integer literal beyond `Number.MAX_SAFE_INTEGER`
as a `bigint` instead of rounding it, and `dumpJson`/`stringifyJson` write a `bigint` back as
bare digits. `t.integer` rejects such a value by name rather than hand back a wrong `number`:
declare the field `integer-string` (a `bigint`) when a venue sends one. A bare JSON integer that can outgrow a double (deribit's int64
`starbase_order_id`) declares `format: int64` instead: `t.int64` holds it as `number | bigint`,
a `number` while exact and a `bigint` beyond it, and dumps it back as bare digits. An `epoch-micros`,
`epoch-nanos` or long RFC 3339 fraction with digits below the millisecond parses to a
`PreciseDate`, a `Date` that also keeps `subMillisecondNanos` (`epochNanoseconds(date)` is the
exact instant), and dumps back with every digit; a whole-millisecond value stays a plain
`Date`. An RFC 3339 fraction is written in groups of three, as many as the value needs (`02Z`,
`.733340Z`), as the Rust and Go runtimes write it. Python keeps microseconds and rounds nanoseconds; TypeScript keeps nanoseconds.
Objects keep keys they were not told about, unions try their variants in order, tuples are
`readonly`. The combinators (`t.object`, `t.array`, `t.tuple`, `t.union`, `t.literal`,
`t.record`, `t.nullable`, `t.optional`, `t.lazy`, the formats) are the whole validator: no
schema interpretation, no dependency, no `eval`.

## The core contract

The generated code never imports the project's core. Each generated class takes its core as
a constructor argument typed by `@truewire/core/contract`, the TypeScript half of
`truewire_core.contract`:

```ts
interface CallOptions { validate?: boolean; signal?: AbortSignal }

interface HttpCall<Req, Res, Meta> extends CallOptions {
  method: string | undefined     // the wire HTTP method; undefined when the spec leaves it to the core
  path: string                   // the wire path template; {name} placeholders are filled from request
  request: Req | undefined       // the generated Request value (wire keys), or undefined
  requestCodec: Codec<Req> | undefined
  responseCodec: Codec<Res> | undefined
  meta: Meta                     // the endpoint's declared meta, in the shape the core's schema states
}

interface HttpEndpoint<Meta = Record<string, never>> {
  request<Req, Res>(call: HttpCall<Req, Res, Meta>): Promise<Res>
}
```

A generated endpoint under the `default` core is `class Get { constructor(readonly core:
HttpEndpoint<DefaultMeta>) {} }` and calls `this.core.request({ method: 'GET', path:
'/repos/{owner}/{repo}', request, requestCodec: Request, responseCodec: Repository, meta: {
public: true }, ...options })`. A router's constructor takes the intersection of its
endpoints' core types and hands the same object to every child; the root class is the same
shape under the project's name. An `rpc` endpoint whose transport is `ws` takes a
`CommandEndpoint<Meta>` and calls `request({ path, ... })`, `path` being the wire method
name; a `stream` endpoint takes a `StreamEndpoint<Meta>` and calls `subscribe`:

```ts
interface SubscribeCall<Params, Message, Meta> extends CallOptions {
  channel: string                          // the wire channel template; {name} placeholders are filled from parameters
  parameters: Params | undefined           // the generated Parameters value (wire keys), or undefined
  parametersCodec: Codec<Params> | undefined
  messageCodec: Codec<Message> | undefined // codec of each pushed message
  meta: Meta
}

interface StreamEndpoint<Meta = Record<string, never>> {
  subscribe<Params, Message>(call: SubscribeCall<Params, Message, Meta>): Subscription<Message>
}
```

An `rpc` endpoint declaring both `http` and `ws` transports takes a `DualEndpoint<Meta>`,
whose one `request` receives a `TransportCall` (an `HttpCall` plus `transport: 'http' |
'ws'`). Its methods take `TransportOptions` (`CallOptions` plus `transport?`), and the
generated call passes `transport: options?.transport ?? '<first declared>'`, the TypeScript
half of Python's `transport=` keyword; the core routes the call to its HTTP client or its
socket, `path` being the HTTP path or the RPC method name (ADR 0006):

```ts
const pet = await client.pets.getPet({ petId: 42 })                     // over HTTP, declared first
const same = await client.pets.getPet({ petId: 42 }, { transport: 'ws' })
```

`packages/testing-ts/test/fixture` is a JSON-RPC API over both transports with such a core.

The hand-written core is an object satisfying that interface by shape. `examples/github`'s
does four things in `request`: `requestCodec.dump(request)` to get the wire values, fill
the `{placeholders}` and send the rest as the query (or as a JSON body for POST/PUT/PATCH)
through `HttpClient`, map a non-2xx reply to `ApiError`/`AuthError`/`BadRequest`/
`RateLimited`, and `parseJson(responseCodec, text)` when validation is on. Envelope
unwrapping (`envelope.payload`), signing and headers are the core's business, as in Python;
the plan's `meta` tells it what each endpoint needs.

The core is also where a proxy goes (packages clause P18), since the generated client
takes its core ready-made: `new HttpClient({ proxy })` and a socket's `{ url, proxy }`,
one HTTP(S) proxy URL for both transports. HTTP sends `http://` as an absolute-form
request and tunnels `https://`; every WebSocket is a `CONNECT` tunnel. It runs on Node
over undici's `ProxyAgent`, an optional peer dependency (`npm install undici`) loaded on
first use; in a browser, or beside `fetch`/`createWebSocket`, `proxy` is a `LogicError`
at construction rather than silently ignored. Without it nothing changes: the global
`fetch` and `WebSocket`, which on Node read no proxy variable by default. The `github` and
`kraken` example cores take `proxy` in their `CoreOptions`.

## Streams

A `stream` endpoint's class has one method, named after the endpoint, whose parameter is
the endpoint's `Parameters` interface (the subscribe frame's own fields, verbatim) and whose
return value is the core's `Subscription<Message>`:

```ts
export class Ticker {
  constructor(readonly core: StreamEndpoint) {}

  ticker(parameters: Parameters, options: CallOptions & { validate: false }): Subscription<unknown>
  ticker(parameters: Parameters, options?: CallOptions): Subscription<TickerMessage>
  ticker(parameters: Parameters, options?: CallOptions): Subscription<TickerMessage> {
    return this.core.subscribe({
      channel: 'ticker',
      parameters,
      parametersCodec: Parameters,
      messageCodec: TickerMessage,
      meta: {},
      ...options,
    })
  }
}
```

A stream that declares its subscription `reply` (ADR 0014) takes a
`ReplyStreamEndpoint<Meta>` instead, returns `Subscription<Message, Reply>`, and hands the
core `replyCodec: Reply` beside `messageCodec`: the core parses the acknowledgement through
it (unless `validate` is off, when both the reply and the pushes stay raw) and returns it as
the stream's typed `reply`. A core that only satisfies `StreamEndpoint` does not
type-check against such a class, so adopting a declared reply is a per-core step, as in
Python. A stream without `reply` renders exactly as before.

The call is not async: `Subscription` (`@truewire/core`) subscribes when awaited, iterated
or opened, and is `AsyncDisposable`, so `await using stream = client.streams.marketData.ticker(...)`
unsubscribes on scope exit; awaiting it gives the `Stream`, whose `reply` is the ack and
whose `unsubscribe()` ends it. Each pushed message is what `messageCodec.parse` makes of the
frame -- `validate: false` yields the frame as it came, typed `unknown`, the same overload
rule as a call. What the method hands the core is what the Python backend hands
`self.subscribe(...)` ([docs/plan.md](plan.md)): the channel template, the parameters object
and its codec for the general shape; for a direct-channel stream (the parameters are exactly
the channel's placeholders, `/ticker/{symbol}`) or a connect-only one (the channel *is* the
one parameter), the module declares the `Parameters` interface itself, fills the template
from it -- `` channel: `/ticker/${parameters.symbol}` `` -- and passes no parameters
object, as the Python method fills the channel from its own locals. The hand-written core
fills `{name}` placeholders from `parameters` otherwise, as `docs/cores.md`'s `ws` template
does, and validates each push against `messageCodec` unless told not to;
`examples/kraken/src/kraken/core/socket.ts` is one over `ws.StreamsRpc`, serving `request`
and `subscribe` on the same connection.

## Composite cores

A router whose core declares `children` or `forward` in `truewire.toml` (Kraken's `root`
and `streams`, [docs/truewire-toml.md](truewire-toml.md)) is built from more than one
transport. In Python that is a base class with a field per transport and a `new()`; in
TypeScript it is a *fields object*, and the generated router exports the interface it takes:

```ts
export interface KrakenCore {
  market_client: CommandEndpoint & StreamEndpoint
  private_client: CommandEndpoint & StreamEndpoint
  spot_client: HttpEndpoint<SpotMeta>
}

export class Kraken {
  constructor(readonly core: KrakenCore) {
    this.spot = new Spot(core.spot_client)
    this.streams = new Streams(core)
    this.tradingWs = new TradingWs(core.private_client)
  }
}
```

The fields are the ones the declarations name, verbatim, each typed by the intersection of
what the endpoints reached through it need. A child endpoint or plain router receives the
field its `children` entry maps it to (`client` when unmapped); a child that is itself a
composite receives the whole object, since `forward` says its fields are the parent's
same-named ones (`Streams` reads `market_client` and `private_client` off the object
`Kraken` was given). `params` has no rendering: the hand-written core takes its parameters
when it is built, and generated code never builds a core. The core is still never imported
(ADR 0011): `examples/kraken/src/kraken/core/index.ts`'s `Core` declares `implements
KrakenCore` and holds the three transports, and the root's interface is re-exported from
`index.ts` for it.

## Hand-written methods

An endpoint whose spec declares `surface: {kind: "handwritten"}` is not rendered, as in
Python: the project writes the method, and `[typescript.extras."<router node>"]` in
`truewire.toml` folds its class into the generated router:

```toml
[[typescript.extras."spot.account"]]
file = "retrieve_export"      # src/kraken/spot/account/retrieve_export.ts
class = "RetrieveExport"
methods = ["retrieveExport"]
# replaces = "get_candles"    # stand in for a generated child (the class extends it)
# field = "spot_client"       # under a composite router: the field it is built from
```

A hand-written endpoint still counts toward its router's core: the router holds the
contract the endpoint would have (`StreamEndpoint<SpotStreamsEndpointMeta>` for mexc's
protobuf-framed spot streams), and a composite parent hands it the mapped field, so a router
whose endpoints are all hand-written is built from a real transport rather than `undefined`.

The router imports the class, builds it from the core it already holds (the whole core, or
`field`, `client` by default, under a composite), and exposes each listed method as a
`readonly` property typed `RetrieveExport['retrieveExport']`, bound to the instance, so its
overloads reach the caller unchanged. An entry that `replaces` a generated child is built
in that child's place, and the router keeps delegating the generated methods to it. A
method name the router already has, a `replaces` naming no generated child, and a node that
is no router are refused before anything is written. The class takes what its router
holds; when it needs more of the core (kraken's `retrieveExport` needs a signed call whose
body is bytes), it checks for it at run time. `examples/kraken`'s `retrieve_export.ts` is
the reference.

`truewire surface --language typescript` asks the same question as the Python gate, of the
TypeScript package: each in-scope spec is *generated* (its module declares the camelCase
method), *hand-written* (an extras entry at the symbol's router node lists the symbol's
file and camelCased name, and the file declares it), or *absent*; anything else is a gap,
and a generated `rpc` method whose implementation takes no `options` is reported like a
missing `validate`.

## Pagination

`<method>Paged` is rendered from the plan's pagination decisions:

- `walker: paginated` (a `page` walk ended by `short_page`, `empty` or `total`; a `token`
  walk ended by `absent_cursor`): a plain method returning
  `PaginatedResponse<Row, State>`, awaitable (every row, flattened) and async-iterable (one
  page at a time), with `pages()`, `resume(state)` and `via(invoker)`. Its `next(state)` is
  pure in `state`, so a page can be retried and a walk resumed. The request type is the
  endpoint's `Request` without the driver parameter (`ListPagedRequest = Omit<Request,
  'page'>`), unless the cursor is required and seeds the walk.
- `walker: generator` (`offset`, and `page`/`token` shapes the resumable form does not
  cover): `async *<method>Paged` yielding every page's response.
- A `total` terminator is checked on every page: a missing total, or one that disagrees
  with an earlier page of the same walk, throws `LogicError`.
- `seek` (ADR 0013): a plain method returning `PaginatedResponse<Row, SeekState<Row, Key>>`,
  rendered as one call to `@truewire/core`'s `seek`. The walker takes the whole `Request`
  (both bounds: the moving one, the bound the venue anchors truncation to, seeds the walk;
  the far one caps it), plus the `span` keyword when one is declared. The runtime moves the
  bound to the extreme cursor key of each full page, carries the rows sharing that key in
  its state `[pos, carried]` and drops them when re-served (by key for a `unique` cursor,
  by content otherwise), and ends on a short page when a cap resolves (the caller's size,
  else the declared `cap` or the size's schema default). Keys compare by the bound's type:
  a timestamp format parses a raw row value through its converter, a numeric bound
  compares numerically, anything else by equality. The state carries rows, so the
  `validate: false` overload's `PaginatedResponse<unknown, SeekState<unknown, Key>>` and the
  declared one are unrelated types; only the implementation signature is their union. A
  caller's numeric size is clamped once, before `seek`, to `min(max(size, 2), maximum)` (a
  page must hold one new row beside the one it re-reads), and that `size` is both the cap
  and what every request sends; an omitted size stays unset, and a size sent as a string
  is left alone.

```ts
const trades = await client.account.fillsPaged({ start: new Date('2026-09-01'), end: new Date(), limit: 500 })
for await (const page of client.market.candlesPaged({ symbol: 'BTC-USD', start, end })) ...
```

## Testing

`@truewire/testing` (`packages/testing-ts`) is the TypeScript half of `truewire.testing`:

- `mockSetup({ project })` (`@truewire/testing/vitest`) is a vitest `globalSetup` that
  spawns `truewire mock --http-port 0 --ws-port 0 --json` (`TRUEWIRE_BIN`, else the nearest
  `.venv/bin/truewire`, else `truewire` on `PATH`) and provides `httpBaseUrl` and `wsUrl`
  to every test through `inject`. The project declares those two keys on vitest's
  `ProvidedContext` beside its setup; `startMock` is the same without vitest. With
  `--json` the mock's first stdout line is `{"event": "ready", "http": <url>, "ws": <url> |
  null}`, the contract any foreign test runner reads; the example-discovery rules are the
  ones documented at the top of `packages/testing-ts/src/examples.ts`.
- `httpExamples`, `wsExamples` and `endpointRecords` discover recordings with the pairing
  rules of `truewire.spec.repo`, including a WebSocket example synthesized from each HTTP
  one of a dual-transport `rpc` endpoint.
- `describeReplay({ test: { describe, it }, projectRoot, packageDir, withClient,
  importModule: url => import(url) })` replays every HTTP example (validated, then with
  `validate: false`, comparing shapes), every WebSocket command, and every channel
  subscription with a recorded push (its first message read); a dual-transport endpoint
  is replayed over each transport. Each recording is parsed through its endpoint module's
  `Request` (a stream's `Parameters`) codec first, and one the typed value cannot render
  back exactly is skipped; so is an endpoint with a declared `surface`. The runner and the
  module loader are passed in because a `file:` copy of the package carries its own
  `node_modules`: suites registered on a second vitest never run, and an `import()` of a
  generated `.ts` module made from under `node_modules` is not transformed.

`examples/github/test` and `examples/kraken/test` use it. In github, `replay.test.ts` is one
`describeHttpReplay` call, and `paging.test.ts` walks the same recorded multi-page captures as
`test/test_paging.py`; `codecs.test.ts` round-trips recorded bodies through `parse` and
`dump` without the mock. `examples/kraken/test` adds the WebSocket half: `streams.test.ts`
subscribes to a public and a private channel and calls the trading methods against the
mock, the way `test/test_streams.py` does, and `codecs.test.ts` decodes every recorded
`*.messages.json` capture through its message codec. Its `replay.test.ts` is one
`describeReplay` call over HTTP and WebSocket recordings alike, leaving out the one channel
(`streams.private.executions`) with no recorded subscribe ack for the mock to answer
Kraken's `req_id`-correlated subscribe with. CI (`examples-ts`) builds `@truewire/core` and
`@truewire/testing`, installs each example, runs `truewire generate typescript --check`,
`tsc --noEmit` and `vitest run`; `testing-ts` runs the package's own tests against its
fixture.

After a change to `packages/core-ts` or `packages/testing-ts`, rebuild it (`yarn build`) and
reinstall the example (`yarn install --force`): a `file:` dependency is copied at install
time.

## Protobuf WebSocket frames

A project whose pushes are Protocol Buffers keeps its `.proto` files under `spec/proto/`
(ADR 0016). The generator writes them verbatim into `<package>/proto.ts` as
`PROTO_SOURCES`; the core compiles them once with `@truewire/core/protobuf` (add
`protobufjs` to the package's dependencies) and decodes binary frames as ProtoJSON:

```ts
import { isBinary, ProtoFrames, type Frame } from '@truewire/core/protobuf'
import { PROTO_SOURCES } from '../proto.js'

const frames = ProtoFrames.compile(PROTO_SOURCES, 'PushDataV3ApiWrapper')

// in a `Streams<Frame>` core
parseMsg(msg: Data) {
  if (!isBinary(msg)) return this.onJson(msg)
  const frame = frames.decode(msg)
  return { channel: frame.string('channel')!, notification: frame }
}

// in the endpoint core: narrow to the endpoint's `meta.proto_field`
subscribe(call) {
  const field = call.meta.proto_field
  return this.client.subscribe(call.channel).filter(f => f.has(field)).map(f => f.field(field))
}
```

`truewire mock` replays `<id>.messages.protobuf.json` frames as binary messages, and
`WsExample.frames` holds them decoded from base64.

## gRPC endpoints

A `kind: grpc` endpoint (ADR 0017) is rendered over protobuf-es stubs built from
`spec/proto/` into `<package>/protos/`:

```sh
truewire protos typescript     # the stubs (--check compares them)
truewire generate typescript   # the endpoint modules, which import them
```

A module exports `Request` (`MessageInitShape` of the input), `Response` and the stub's
`method`, and calls `GrpcEndpoint<Meta>.unary`; `GrpcClient` from `@truewire/core/grpc` is
the core. The project depends on `@bufbuild/protobuf`, `@connectrpc/connect` and
`@connectrpc/connect-node`. `@truewire/testing/grpc` has `startGrpcMock` and
`describeGrpcReplay`.

Building the stubs needs `buf` and the language's protoc plugin. Nothing else in Truewire
needs them: `truewire plan`, `check` and the generated code's callers do not. Install them
user-locally (the directory must be on `PATH`; `truewire protos` also finds them in a
`node_modules/.bin` above the project, or through `TRUEWIRE_BUF`, `TRUEWIRE_PROTOC_GEN_ES`
and `TRUEWIRE_PROTOC_GEN_GO`):

```sh
npm install -g --prefix ~/.local @bufbuild/buf@1.73.0 @bufbuild/protoc-gen-es@2.15.0
GOBIN=~/.local/bin go install google.golang.org/protobuf/cmd/protoc-gen-go@v1.36.12
```

The toolchain tests that build stubs (`test_grpc_proto.py`, `test_codegen_go_grpc.py`) skip
when a tool is missing, so run them with the tools installed before trusting a green run.

## Not generated yet

- `truewire docs check` for ```ts blocks.
- Streaming gRPC methods. Unary `grpc` endpoints are generated over protobuf-es stubs
  (`truewire protos typescript`), called through `@truewire/core/grpc` and replayed with
  `@truewire/testing/grpc` (ADR 0017).
- Typed protobuf stream messages: a protobuf-framed stream is served by a hand-written core
  (see "Protobuf WebSocket frames" below).
- A `seek` walk whose row type the plan cannot name: the plain method is generated with a
  note; no walker.

`@truewire/core` itself is published: merging a `release/core-ts` pull request into `main`
runs `.github/workflows/release-core-ts.yml`, which tests and builds `packages/core-ts`,
runs `npm publish --provenance`, tags `core-ts-v<version>` and creates the GitHub release.
Generated code depends on the published package as a normal dependency; only this
repository's own example links it by path.
