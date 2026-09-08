# TypeScript

`truewire generate typescript` renders a project's plan (`docs/plan.md`) as an ESM
TypeScript package with the same guarantees as the Python one: typed requests and
responses, runtime validation on by default with a per-call override, and generated
pagination walkers. The runtime it depends on is `@truewire/core` (`packages/core-ts`).

Status: `@truewire/core` publishes to npm from `packages/core-ts` (a merged
`release/core-ts` pull request, see [Releasing](../CONTRIBUTING.md#releasing)); a generated
project depends on it as an ordinary `package.json` dependency. `examples/github` and `examples/kraken` in
this repository link it with a relative `file:` dependency instead, so the examples always
test the runtime at the same commit. `rpc` endpoints over HTTP and over a WebSocket, `stream`
endpoints and composite cores (`forward`/`children` in `truewire.toml`) are generated;
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
truewire generate typescript             # writes src/github/**/*.ts and .truewire/typescript-files.json
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
into `DateIso`, `integer-string`/`boolean-string` into `number`/`boolean`) and names the
JSON pointer of the first failure in a `ValidationError`; `dump` renders it back to the wire.
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

The hand-written core is an object satisfying that interface by shape. `examples/github`'s
does four things in `request`: `requestCodec.dump(request)` to get the wire values, fill
the `{placeholders}` and send the rest as the query (or as a JSON body for POST/PUT/PATCH)
through `HttpClient`, map a non-2xx reply to `ApiError`/`AuthError`/`BadRequest`/
`RateLimited`, and `parseJson(responseCodec, text)` when validation is on. Envelope
unwrapping (`envelope.payload`), signing and headers are the core's business, as in Python;
the plan's `meta` tells it what each endpoint needs.

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

## Pagination

`<method>Paged` is rendered from the plan's pagination decisions:

- `walker: paginated` (a `page` walk ended by `short_page`, `empty` or `total`; a `token`
  walk ended by `absent_cursor`; a plain `seek` walk): a plain method returning
  `PaginatedResponse<Row, State>`, awaitable (every row, flattened) and async-iterable (one
  page at a time), with `pages()`, `resume(state)` and `via(invoker)`. Its `next(state)` is
  pure in `state`, so a page can be retried and a walk resumed. The request type is the
  endpoint's `Request` without the driver parameter (`ListPagedRequest = Omit<Request,
  'page'>`), unless the cursor is required and seeds the walk.
- `walker: generator` (`offset`, and `page`/`token` shapes the resumable form does not
  cover): `async *<method>Paged` yielding every page's response.
- A `total` terminator is checked on every page: a missing total, or one that disagrees
  with an earlier page of the same walk, throws `LogicError`.

## Testing

`examples/github/test` is the pattern. `setup.ts` is a vitest `globalSetup` that spawns
`truewire mock --http-port 0 --ws-port 0` and provides its URLs to every test through
`inject('httpBaseUrl')` (and `inject('wsUrl')`); `replay.test.ts` walks
`spec/endpoints/**/examples/*.request.json` and calls, for each, the method its function
path names with the recorded request (validation on, so the codec accepts the recorded
response); `paging.test.ts` walks the same recorded multi-page captures as
`test/test_paging.py`; `codecs.test.ts` round-trips recorded bodies through `parse` and
`dump` without the mock. `examples/kraken/test` adds the WebSocket half: `streams.test.ts`
subscribes to a public and a private channel and calls the trading methods against the
mock, the way `test/test_streams.py` does, and `codecs.test.ts` decodes every recorded
`*.messages.json` capture through its message codec. Its replay parses each recording
through the endpoint's `Request` codec first, since a recording holds wire values and the
method takes typed ones. CI (`examples-ts`) builds `@truewire/core`, installs each example,
runs `truewire generate typescript --check`, `tsc --noEmit` and `vitest run`.

After a change to `packages/core-ts`, rebuild it (`yarn build`) and reinstall the example
(`yarn install --force`): a `file:` dependency is copied at install time.

## Not generated yet

- `window` walks, `seek` walks with `overlap`, and the `unchanged` terminator: the plain
  method is generated with a note; no walker.
- A `rpc` endpoint with both `http` and `ws` transports is generated for HTTP only, and
  reported as skipped for the `ws` half.
- `truewire docs check` for ```ts blocks, and `truewire surface` for the camelCase rule.
- A `@truewire/testing` package with the replay helpers (`examples/github/test` is
  hand-written for now).

`@truewire/core` itself is published: merging a `release/core-ts` pull request into `main`
runs `.github/workflows/release-core-ts.yml`, which tests and builds `packages/core-ts`,
runs `npm publish --provenance`, tags `core-ts-v<version>` and creates the GitHub release.
Generated code depends on the published package as a normal dependency; only this
repository's own example links it by path.
