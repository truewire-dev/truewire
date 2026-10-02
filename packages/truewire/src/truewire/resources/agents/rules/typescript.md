# TypeScript rules

These rules cover the package `[typescript]` declares: the client `truewire generate
typescript` writes under `<src>/<package>/`, the hand-written core in `<package>/core/`,
`[typescript.extras]` classes, and `test/`. Each rule names the tool that enforces it, or
says (review) when a reviewer checks it by reading the code.

## Tools

- `truewire lint typescript` runs `tsc -p tsconfig.json --noEmit` in the package. It runs
  `eslint .` only if the package has an eslint config. Truewire ships none, so no eslint
  rule applies unless the project adds one. There is no formatter step.
- `tsconfig.json` is the one `docs/typescript.md` in the toolchain repository says to
  copy, from its `examples/github`: `strict`, `NodeNext` module and resolution,
  `verbatimModuleSyntax`, `isolatedModules`, `noImplicitOverride`,
  `noFallthroughCasesInSwitch`, and `include` covering `src/**/*.ts` and `test/**/*.ts`.
  Never loosen an option to make `tsc` pass. (review)
- `truewire generate typescript --check` fails when a generated file differs from what the
  plan renders, so never run a formatter (prettier, `eslint --fix`) over generated files.
- `truewire test typescript` runs the package's `test` script (`vitest run`) with the
  lockfile's package manager, and fails a run in which no test passed.

## Modules

- ESM only: `package.json` has `"type": "module"` and `"sideEffects": false`. (review)
- Relative imports end in `.js` (`'../meta.js'`). (tsc, `NodeNext`)
- A name used only as a type is imported with `import type` or `type X`. (review; under
  `verbatimModuleSyntax` tsc rejects a value import only of a type-only export, while a
  class used only as a type still compiles as a value import)
- An `import()` of a variable carries `/* webpackIgnore: true */ /* @vite-ignore */`.
  Without them, webpack replaces the import with an empty context. (review)

## Naming

- Names Truewire invents are camelCase for methods and PascalCase for classes:
  `list_commits` becomes `listCommits`, class `ListCommits`. Names the API invented stay
  verbatim (`per_page`), so a request object is the wire object. (review)
- A hand-written method is the camelCased name of its spec's `surface` symbol. Its
  `[typescript.extras]` entry lists it, and the file the entry names declares it.
  (`truewire surface --language typescript`)
- `<package>/core/index.ts` exports `Core`, built as `new Core(options)`, and `export
  interface CoreOptions {` with one `name?: type` per line and no `extends`. `baseUrl` is
  the field `--base-url` sets. `truewire call typescript` reads these from the source and
  fails if they are missing. (`truewire call`)

## Typing

- Generated output has no `any`: a schema `any` is `unknown`. Hand-written code also types
  a value of unknown shape as `unknown` and narrows it before use. (review)
- Hand-written exported functions and methods declare their return types. (review)
- A generated file that fails `tsc` is a generator bug. Report it. Do not edit
  the file and do not silence `tsc`. (review)
- A hand-written codec uses the combinator the generator renders for the same schema:
  `t.integerString` for `integer-string`, `t.int64` for `int64`, and a `*Float` twin such
  as `t.epochSecondsFloat` for a `type: number` epoch. (review)

## Numbers and time

- Read wire JSON with `parseJson(codec, text)` or `parseJsonText`, and write it with
  `stringifyJson` or `dumpJson`. `JSON.parse` rounds integers past 2^53, and
  `JSON.stringify` throws on a `bigint`. (review: grep `JSON\.`)
- A core gets wire values only from `call.requestCodec.dump(call.request)`, which writes
  `bigint`, `Decimal` and timestamps in their wire form. It never reads the typed request
  directly. (review)
- A `bigint` becomes text through `String(value)` or `stringifyJson`, never through
  `Number(value)`. (review)
- A timestamp with sub-millisecond digits is a `PreciseDate`. Its exact instant is
  `epochNanoseconds(date)`, not `getTime()`. Tests compare `epochNanoseconds(actual)` with
  a `bigint` literal, because `toEqual` on two dates ignores the nanoseconds.
  (review)

## The core

- `request` honours `call.validate ?? this.validate`. When it is on, return
  `responseCodec.parse(...)`. When it is off, return the raw parsed value. (review)
- Pass `call.signal` to every HTTP request the core sends through `HttpClient`. WebSocket
  RPC (`rpcRequest`) takes no signal today. (review)
- Sign inside `RequestOptions.prepare`: the nonce, the timestamp and the signature.
  `prepare` runs after pacing and again on every retry. Never sign before calling
  `http.request`. (review)
- Credentials arrive through `CoreOptions`. The core never reads `process.env`. (review)

## Errors

- An error the hand-written core creates is an `@truewire/core` error class, never a bare
  `Error` or a string:
  - A failure the API returned is an `ApiError`, or its `BadRequest`, `AuthError` or
    `RateLimited` subclass, built with `{ status, body }`.
  - A reply in the wrong shape is a `ValidationError`.
  - A missing credential is an `AuthError`, thrown before anything is sent.
  - A misused client, or a core missing a capability, is a `LogicError`.
  A caller's abort reason propagates as it is: do not wrap cancellation. The generated
  seek walkers' `TypeError` for misuse stays. (review)
- No credential appears in an error's message, its `cause` or a public field: no token,
  and no URL with userinfo. Node's invalid-URL error carries its whole input, so never
  pass it on as `cause`. (review)
- `[policy].refuse` cannot name an endpoint whose method is hand-written, whether by
  `surface: handwritten` or by an extras entry that `replaces` it. (`truewire
  generate typescript`)

## Dependencies

- `@truewire/core` is the one runtime dependency. Add an optional peer only for the
  feature that needs it: `undici` for `proxy`, `protobufjs` for protobuf frames, and
  `@bufbuild/protobuf` with `@connectrpc/*` for gRPC. `engines.node` is `>=22`. (review)
- `vitest`, `typescript` and `@types/node` are devDependencies. (review)

## Tests

- Tests reach the API only through `truewire mock`, started from the vitest `globalSetup`
  on a free port, with the client built on its `baseUrl`; never the live API. `mockSetup`
  from `@truewire/testing/vitest` does this, but that package is unpublished, so only
  packages inside the toolchain repository can depend on it. (review)
- Every `<method>Paged` walker has a test that walks several recorded pages. (review)
- `test/typing_usage.ts` pins public return types with `expectTypeOf`, including the `{
  validate: false }` overload, and `tsc` checks it through `include`. (tsc)
