# Changelog

## Unreleased

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
