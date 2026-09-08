# Truewire

Turn any API, documented or not, REST or WebSocket, into a typed, validated client you can trust.

**Typed clients, true to the wire.**

## Why

SDK generators assume two things: that you own the API, and that you have a good OpenAPI document. Most of the APIs people actually integrate are neither. They belong to someone else. Their docs are prose, examples and a few tables. Half of them push data over WebSocket, where OpenAPI has nothing to say. The pagination scheme is described in a sentence, if at all. Prices arrive as strings, timestamps as milliseconds, booleans as `"true"`.

We start from the wire, not the document, because the wire is what your code actually talks to. You record real request and response pairs against the live API. You declare what a schema cannot express, pagination, envelopes, stream subscriptions, redacted fields, as data next to the endpoint. The generator turns that into an async, typed, runtime-validated client. A mock server replays the recorded examples over HTTP and WebSocket so tests never touch the network. We gate every step: an endpoint is verified by a recorded example, or it states why it is not.

## Quickstart

```bash
pip install truewire
truewire init petstore                # --template hmac | jsonrpc | ws for a signed, JSON-RPC or WebSocket core
cd petstore
truewire import openapi spec.yaml     # optional: seed spec/ from an OpenAPI 3.0/3.1 document
truewire check                        # lint the spec: titles, enums, formats, pagination, envelopes
truewire generate python              # write the typed client into src/petstore
pip install -e .                      # the project ships its own pyproject.toml
truewire capture pets.get_pet --request '{"petId": 42}'   # record one live call as an example pair
truewire mock                         # serve every recorded example over HTTP and WS on localhost
```

Use the generated client:

```python
from petstore import Petstore

async with Petstore.new() as client:
  pet = await client.pets.get_pet(pet_id=42)
  print(pet['name'], pet['status'])   # typed: str, Literal['available', 'pending', 'sold']
  print(pet['created_at'])            # a real datetime, parsed from the wire
```

An endpoint with a declared `pagination` block gets a `_paged` variant. It walks pages for you and works both as an iterator and as an awaitable:

```python
async with Petstore.new() as client:
  async for page in client.pets.list_pets_paged(limit=100):
    ...                                               # one page at a time
  pets = await client.pets.list_pets_paged(limit=100)  # or every row, flattened
```

Point tests at the mock server and replay the same examples the spec was verified against:

```python
async with Petstore.new(base_url='http://127.0.0.1:8321') as client:
  pet = await client.pets.get_pet(pet_id=42)   # served from spec/endpoints/pets/get_pet/examples/
```

The full CLI is `truewire init | import | capture | check | examples | surface | generate | mock | mcp | standards | docs`. Run `truewire --help` for each command. `truewire import registry github` starts a project from a spec in the [registry](https://github.com/truewire-dev/registry) instead of a document.

Hand the same endpoints to an agent as MCP tools, one per endpoint, answered through the generated client:

```bash
pip install 'truewire[mcp]'
truewire mcp --project petstore --new base_url=https://petstore.example.com/v1
```

## What's in the box

| Piece | What it does |
| --- | --- |
| Spec format | One directory per endpoint: `endpoint.json` (JSON Schema 2020-12 request and response), `upstream.md`, and `examples/`. Declared blocks for `pagination`, `envelope`, `push`, `redacted`, `unverified`, `meta`. Not OpenAPI, but imports from it. |
| Checks | `truewire check` runs 19 lint rules over the spec (titles, enums, timestamp formats, positional rows, unions, descriptions, pagination references, envelope selectors, stream verbs). A response schema describes the wire body as recorded; `envelope.payload` selects what the generated method returns (ADR 0010). `truewire examples --require-verified` fails when an endpoint has neither a recorded example nor a stated reason. `truewire surface` fails when a spec'd endpoint has no reachable method. |
| Examples and mock server | Recorded request/response pairs (`truewire capture` records a live call through your own client and core, so the pair carries the real headers, signing and envelope) and WebSocket captures, replayed by `truewire mock` over real HTTP and WS: subscribe/unsubscribe lifecycle, push-on-connect, push-after-RPC, correlation ids, declared redaction, and a 409 when two examples match one request. |
| Python generator and `truewire-core` | `truewire generate python` emits async endpoint methods with typed `TypedDict` responses, `validate` and `transport` keywords, `_paged` walkers, and router classes with docstrings. Your hand-written core (transport, signing, envelope, errors) is declared, not introspected: `truewire.toml` says how routers compose it, `truewire_core.contract` says what it provides, and the generator never imports your package (ADR 0011). `truewire-core` is the small MIT runtime: HTTP, WebSocket streams and RPC, validation, paging, timestamp types, errors, the core contract. |
| Plan | `truewire plan --json` prints the language-neutral plan the generators render from: types as a tree, request fields, the returned type, stream facts and every pagination decision, per endpoint ([docs/plan.md](docs/plan.md)). A second backend reads it instead of the spec. |
| Standards | `truewire standards` runs the checks that guard a client's public surface: docstring shape, duplicate schemas, secret placeholders in examples, router coverage, no `__call__` classes. |
| Docs | `truewire docs check` type-checks every code block in your README and docs against the generated package, so an example that no longer compiles fails CI. |
| Examples | `examples/kraken`: 75 endpoints over REST and WebSocket, hand-written core, 63 replay tests. `examples/github`: the GitHub REST API captured live, page-walked, 14 tests. Both kept green in CI. |
| Agent-native | Every gate is a CLI command with a plain result (`check`, `examples --require-verified`, `surface`, `standards`, `docs check`), and `capture`, `mock` and `mcp` need no human in the loop, so a coding agent can take a docs URL and drive a project to a verified spec, a mock, a client and checked docs. Six skill files for that workflow (discover, spec, core, implement, docs, review) live in [`.agents/skills/`](.agents/skills/README.md). |

## How it compares

A cell reads `?` where we have not verified the claim ourselves. We publish what we checked, not what we guessed. Send corrections and we will fix the table.

| | Truewire | OpenAPI Generator | Speakeasy | Fern | hey-api |
| --- | --- | --- | --- | --- | --- |
| Open source | Yes (Apache-2.0, MIT runtime) | Yes (Apache-2.0) | No (closed generator) | Partial (?) | Yes (MIT) |
| Self-hosted | Yes | Yes | CLI runs locally, generator is hosted (?) | ? | Yes |
| Runtime validation of responses | Yes, default on, per-call override | Varies by generator | Yes | ? | Yes (via Zod/Valibot plugins) |
| WebSocket streams | Yes (subscribe, push, RPC over WS) | No | ? | Partial (?) | No |
| JSON-RPC | Yes (over HTTP and WS) | No | ? | ? | No |
| Declared pagination with generated walkers | Yes, 5 strategies | No | Yes | Yes | ? |
| Mock server from recorded examples | Yes, HTTP and WS | No | HTTP only (?) | ? | No |
| Verified-coverage gate | Yes | No | No | No | No |
| Docs type-checking | Yes | No | No | No | No |
| MCP server | Yes (`truewire mcp`) | No | Yes (Gram) | ? | ? |
| TypeScript | In the repository, not on npm yet | Yes | Yes | Yes | Yes |
| Python | Yes | Yes | Yes | Yes | Experimental |

We build Truewire for API consumers first: people integrating an API they do not control. If you own your API and have a clean OpenAPI document, any tool above will serve you, and `truewire import openapi` reads your document too.

## Status

Alpha. Python is the shipped target; a TypeScript generator and runtime (`@truewire/core`, `packages/core-ts`) are in the repository and prove the same spec through the same mock on `examples/github`, but are not published to npm yet (see [docs/typescript.md](docs/typescript.md)). We extracted it from a private system that generates 14 production API clients covering 3,638 endpoints (3,272 request/reply, 316 streams, 50 gRPC) with 2,404 recorded HTTP example pairs and 384 WebSocket captures. Those 14 clients serve exchange and blockchain APIs in production, where the wire is the only reliable documentation.

We will change the spec format in small ways before 1.0, and we record each change in `docs/adr/`. We ship when the gate is green, and we publish the gate: every item in [ROADMAP.md](ROADMAP.md) ends with a "done when" line.

## About

Truewire is a spinoff of the internal tooling behind [Tribulnation](https://github.com/tribulnation)'s typed exchange clients, founded by [Marcel Claramunt](https://claramunt.eu) ([@marcelclaramunt](https://x.com/marcelclaramunt)), who advises the project and is its public face. An AI operator runs the day-to-day engineering, the docs and the roadmap. Marcel decides on anything public, financial or legal. We say this plainly: the commit history shows it, and who writes a tool is a fair question to ask of anything you depend on. We hold the code to one bar, the one in [docs/standards.md](docs/standards.md), whoever wrote it.

Questions, bugs and spec corrections: open an issue, or write to hello@truewire.dev.

## Documentation

- [Concepts](docs/concepts.md): the five ideas behind the tool.
- [Spec authoring](docs/spec/authoring.md): the rules `truewire check` enforces.
- [The plan](docs/plan.md): what a backend renders from, and its JSON shape.
- [TypeScript](docs/typescript.md): the second backend, the core contract, and codecs.
- [Generated code](docs/generated.md): what `truewire generate` writes, quoted from `examples/github`, and what stays hand-written.
- [Core templates](docs/cores.md): the four cores `truewire init --template` writes (`bearer`, `hmac`, `jsonrpc`, `ws`), what each does and what to change.
- [Architecture decisions](docs/adr/README.md): why things are the way they are.
- [Contributing](CONTRIBUTING.md): dev setup, adding a check, adding a pagination strategy.

## License

Apache-2.0 for the toolchain (`truewire`). MIT for the runtime (`truewire-core`), since it ships inside your package. See [LICENSE](LICENSE).
