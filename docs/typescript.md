# TypeScript

`truewire generate typescript` renders a project's plan (`docs/plan.md`) as an ESM
TypeScript package with the same guarantees as the Python one: typed requests and
responses, runtime validation on by default with a per-call override, and generated
pagination walkers. The runtime it depends on is `@truewire/core` (`packages/core-ts`).

Status: in the repository, not on npm. `@truewire/core` is linked with a relative `file:`
dependency until it is published. HTTP `rpc` endpoints are generated; stream endpoints
(WebSocket subscriptions, `examples/kraken`) are not yet, and neither are composite cores
(`forward`/`params`/`children` in `truewire.toml`). See the end of this page for the list.

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
`package.json`, `tsconfig.json` and `test/` are what a project copies.

## What is generated

One file per spec node, beside the Python package when both are declared:

| file | contents |
| --- | --- |
| `types/index.ts`, `types/<scope>.ts` | the shared `schemas.json` types, one module per scope |
| `<router>/<endpoint>.ts` | the endpoint's `Request`, its response types, their codecs, and a class with the method |
| `<router>/index.ts` | a router class delegating to its endpoints and holding its child routers |
| `main.ts` | the root class, `new GitHub(core)` |
| `meta.ts` | one interface per `[cores.<name>]` with a `meta` schema (`DefaultMeta`) |
| `index.ts` | re-exports the root class, the shared types and `meta.ts` |

Names Truewire invents are camelCase/PascalCase (`list_commits` becomes `listCommits`,
`listCommitsPaged`, class `ListCommits`); names the API invented stay verbatim
(`per_page`, `html_url`), so the request object *is* the wire object and a recorded
`request.json` is a valid argument as it stands. Every method takes the request as its
first parameter and an options object as its second: `{ validate?: boolean; signal?:
AbortSignal }`. `validate: false` returns the raw `JSON.parse` value cast to the response
type, as in Python. The output is printed by the generator itself (two-space indent, sorted
imports, JSDoc from the spec's descriptions); no formatter runs over it.

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
shape under the project's name. `CommandEndpoint<Meta>` (a WebSocket command: `request({
path, ... })`) and `StreamEndpoint<Meta>` (`subscribe({ channel, parameters, ... })`
returning a `Subscription`) are declared for the shapes the backend will emit next.

The hand-written core is an object satisfying that interface by shape. `examples/github`'s
does four things in `request`: `requestCodec.dump(request)` to get the wire values, fill
the `{placeholders}` and send the rest as the query (or as a JSON body for POST/PUT/PATCH)
through `HttpClient`, map a non-2xx reply to `ApiError`/`AuthError`/`BadRequest`/
`RateLimited`, and `parseJson(responseCodec, text)` when validation is on. Envelope
unwrapping (`envelope.payload`), signing and headers are the core's business, as in Python;
the plan's `meta` tells it what each endpoint needs.

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
`truewire mock --http-port 0` and provides its base URL to every test through
`inject('httpBaseUrl')`; `replay.test.ts` walks `spec/endpoints/**/examples/*.request.json`
and calls, for each, the method its function path names with the recorded request
(validation on, so the codec accepts the recorded response); `paging.test.ts` walks the same
recorded multi-page captures as `test/test_paging.py`; `codecs.test.ts` round-trips recorded
bodies through `parse` and `dump` without the mock. CI (`examples-ts`) builds
`@truewire/core`, installs the example, runs `truewire generate typescript --check`, `tsc
--noEmit` and `vitest run`.

After a change to `packages/core-ts`, rebuild it (`yarn build`) and reinstall the example
(`yarn install --force`): a `file:` dependency is copied at install time.

## Not generated yet

- Stream endpoints (`kind: stream`): reported as skipped. The WebSocket runtime
  (`@truewire/core/ws`) and `StreamEndpoint` exist; the emitter and `examples/kraken`'s
  core are the next step, and what proves the WebSocket half of roadmap item 10.
- Composite cores: a core declaring `forward`, `params` or `children` in
  `[python.cores.<name>]` is skipped (`examples/kraken`'s `root`/`streams`).
- `window` walks, `seek` walks with `overlap`, and the `unchanged` terminator: the plain
  method is generated with a note; no walker.
- A `rpc` endpoint with both `http` and `ws` transports is generated for HTTP only.
- `truewire docs check` for ```ts blocks, and `truewire surface` for the camelCase rule.
- Publishing `@truewire/core` (and a `@truewire/testing` with the replay helpers) to npm.
