# Roadmap

Priority order. An item moves down this list only when a real user need pushes something else above it. Each item ends with a "done when" line so that progress is checkable, not a feeling.

Status words: **exists** (working in the system Truewire was extracted from, needs decoupling), **new** (not built yet).

## P0: a standalone tool that works end to end

### 1. Spec format, `truewire check`, `truewire examples`

Status: shipped in 0.1.0 (2026-09-07). The directory-per-endpoint format, its 18 lint rules and the paired-example coverage report read from a `truewire.toml` project root; nothing assumes the original monorepo's layout.

Done when: `truewire check` and `truewire examples --require-verified` run against a fresh `truewire init` project with zero references to any path convention other than `truewire.toml`, and the existing rule tests pass unchanged.

### 2. Mock server from recorded examples (HTTP and WebSocket)

Status: shipped in 0.1.0 (2026-09-07). The mock replays recorded examples over real HTTP and WS: subscribe and unsubscribe lifecycle, push-on-connect, push-after-RPC, binary frames, correlation ids, declared redaction, and a 409 when more than one example matches a request.

Done when: `truewire mock` serves a project's examples from the CLI with a printed base URL, a generated client's test suite passes against it for both transports, and the ambiguity and unexpected-parameter responses are documented.

### 3. Python generator and `truewire-core` runtime

Status: shipped in 0.1.0 (2026-09-07); `truewire-core` is on PyPI. The generator emits request/reply and stream endpoints, `validate` and `transport` keywords, `_paged` walkers (awaitable `PaginatedResponse` for token, seek, page-with-total and page-until-short shapes), composed router classes and docstrings. Proven on `examples/kraken` (recorded WebSocket-heavy) and `examples/github` (captured live, paginated REST).

Done when: `truewire generate python` produces a package that imports, type-checks under pyright strict, and passes a mock-backed test suite, for one imported OpenAPI project and one recorded WebSocket-heavy project. `truewire-core` is on PyPI.

### 4. `truewire import openapi`

Status: shipped in 0.1.0 (2026-09-07). Reads an OpenAPI 3.0 or 3.1 document and writes one endpoint directory per operation, mapping `parameters` and `requestBody` to the request schema, the 2xx response to the response schema, and any `examples` or `example` entries to recorded example pairs, declared `not_captured` where the document had none.

Done when: the Swagger Petstore document imports, `truewire check` reports zero errors on the result, and the imported examples replay through the mock server against the generated client.

### 5. `truewire init` and the `truewire.toml` project layout

Status: shipped in 0.1.0 (2026-09-07). One project file at the root names the package, the spec directory, the output directory and the generator options; `truewire init` also writes a `pyproject.toml` so the generated client installs with `pip install -e .`. See ADR 0009.

Done when: `truewire init <name>` produces a project that passes `check`, `generate` and `mock` with no edits, and every CLI command locates its inputs through `truewire.toml`.

## P1: the things that make it worth adopting

### 6. Registry of specs

Status: new. A separate repository, one directory per API, CC0-licensed, so a consumer can `truewire import registry <api>` and start from a verified spec rather than from a docs page. Seeded with the 14 clients' specs if their owner agrees.

Done when: the registry repository exists with a documented layout, at least ten specs pass `truewire check` in CI there, and the import command pulls one by name.

### 7. MCP server from any spec (`truewire mcp`)

Status: shipped (2026-09-07). Every endpoint already has a name, a description, a typed request schema and a typed response. That is most of an MCP tool definition. `truewire mcp --project <dir> --new base_url=...` serves a project's `rpc` endpoints as MCP tools over stdio, backed by the generated client; `--list` prints the tools. Credentials go through the root client's own `.new(...)` keywords.

Done when: `truewire mcp` exposes a project's request/reply endpoints as tools, an MCP client can call one end to end, and authenticated endpoints are gated by the same credential resolution the client uses.

### 8. Agent skills, generalized

Status: exists, needs generalizing. The staged skills (discover, core, spec, implement, docs, review, release) were written for one family of APIs. They need to lose that vocabulary and work from a docs URL for any API.

Done when: an agent given only a public docs URL and the skills produces a project that passes `check`, `examples --require-verified` (with honest `unverified` declarations), `generate` and a mock-backed test run, on an API outside the original domain.

### 9. Server-sent events

Status: new. AI APIs stream responses over SSE. The spec needs a way to declare an SSE endpoint, the runtime needs a reader, the generator needs to emit an async iterator, and the mock needs to replay recorded event sequences.

Done when: one SSE endpoint is spec'd, recorded, generated and replayed, with the same verified-coverage gate the other kinds have.

## P2: second language, better docs

### 10. TypeScript generator and runtime

Status: new, large. The spec format is language-neutral. The generator is not. This is a second backend and a second runtime, with the same guarantees: typed responses, runtime validation on by default, generated pagination walkers, WebSocket streams.

Done when: a project generates a TypeScript package that type-checks, validates at runtime, and passes the same mock-backed tests as the Python package generated from the same spec.

### 11. Docs site generator

Status: new. Each endpoint already has a description, an upstream link, a request schema, a response schema and real recorded examples. A docs generator renders that as a site where every example is a real recording, not a hand-written guess.

Done when: `truewire docs build` produces a static site for a project, every code block on it passes `truewire docs check`, and the recorded examples shown match the ones the mock serves.

## P3: hosted

### 12. Truewire Cloud

Status: new. A hosted registry with private specs, managed regeneration and publishing (a GitHub App: spec change, pull request, package release), and hosted mock endpoints for CI. Free for public specs.

Done when: a team can push a spec change and receive a reviewed pull request with the regenerated package, and CI can point at a hosted mock URL instead of running one locally.

### 13. Go generator, richer gRPC, AsyncAPI import

Status: new. A third language, gRPC beyond unary calls, and an importer for AsyncAPI documents where they exist.

Done when: each lands with the same end-to-end proof the Python and TypeScript generators have.

## Non-goals (for now)

- **Java, C#, PHP, Ruby generators.** Not until there is demand with money behind it. The spec format is language-neutral, so nothing blocks a contributor, but the maintainers will not build or support these yet.
- **Server stubs.** Truewire generates clients. If you own the API, other tools generate your server.
- **API gateway or proxy.** The mock server replays recorded examples for tests. It is not a runtime proxy and will not become one.
- **GraphQL.** Different wire model, different tooling ecosystem, no overlap with the problems Truewire solves.
