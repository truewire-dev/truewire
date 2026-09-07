# ADR 0009: Truewire is a standalone project; `truewire.toml` replaces the monorepo's per-client convention

- Status: accepted
- Date: 2026-09-06

## Context

Everything in this repository was extracted from a private monorepo that builds typed clients for a family of trading and blockchain APIs. In that monorepo the tool was not a product. It was `common/`: a spec format, 18 lint rules, a mock server, a Python generator and a runtime, wired to a `clients/<name>/` directory convention that the CLI assumed in 29 separate places. Export, release, registry, secrets and worktree commands, about 5,000 lines, existed only to publish that one family of clients.

An audit in September 2026 found that the reusable part was not an OpenAPI SDK generator. It was a toolchain for building typed, validated clients for APIs you do not control, verified against the wire. Five things in it had no equivalent in OpenAPI Generator, Speakeasy, Fern or hey-api: recorded examples as the source of truth with a mock that replays them over HTTP and WebSocket; a verified/unverified evidence taxonomy with a gate; pagination, envelopes, redaction and push declared as audited data; wire-fidelity formats so a price is a `Decimal` and a timestamp a `datetime`; and a pipeline an agent can drive from a docs URL to a checked client.

The market moved at the same time. The largest hosted SDK generator wound down its public product after an acquisition, leaving its users with generated code and no pipeline. The remaining independent is closed source. The open-source option generates fifty languages at low quality. Nobody serves consumers of third-party, badly specified, WebSocket-heavy APIs at all.

Two options were considered. Keep the tool inside the monorepo and publish only the clients it generates, which is what had been happening, and which makes the tool invisible and unownable by anyone else. Or extract it, give it a name, a project file and a license, and let the original monorepo become its first user.

## Decision

Truewire is a standalone open-source project with its own repository, name, license and roadmap.

- **Project file.** A `truewire.toml` at the project root replaces the `clients/<name>/` convention. It names the package, the spec directory, the output directory and the generator options. Every CLI command locates its inputs through it. There is no assumed directory layout above the project root. `truewire init` writes one.
- **Scope.** The spec format, checks, examples and mock server, the Python generator and the `truewire-core` runtime move over. The export, release, registry, secrets and worktree commands do not; they were specific to the original monorepo's publishing arrangement. An OpenAPI importer and `truewire init` are added, since a standalone tool cannot assume a spec already exists.
- **License.** Apache-2.0 for the toolchain, for the patent grant and enterprise comfort. MIT for the runtime, because it ships inside users' packages. CC0 for any community spec registry.
- **Runtime fork.** `truewire-core` forks from the original runtime and diverges freely. The original stays on PyPI, untouched, for the clients already published against it.
- **Business model.** Open core. Everything a single developer needs is free and self-hosted, forever. Revenue comes from spec-as-a-service (a docs URL in, a verified spec, mock and client out), later a hosted registry with managed regeneration, and eventually support.
- **How it is run.** The repository is the memory: `ROADMAP.md`, `docs/adr/` and GitHub Issues. A coding agent does most of the daily work; a human owns the domain, the org, the money and the launch.

## Consequences

The tool becomes usable outside the domain it was built in, and ownable by anyone who clones it. The original monorepo becomes a consumer of Truewire rather than its home, which is the right direction of dependency: a downstream user with 3,638 endpoints keeps the tool honest.

Harder: two codebases for a while. Fixes land in Truewire first and flow back; the original monorepo's copy is frozen except for backports until it switches over. `meta` still needs a hand-written core per API, and the generator still introspects that core at generation time, a Python-only trick that a second language will have to replace with something declared.

Left open: whether the 14 original specs seed the public registry. That is the original owner's decision. The spec format may also change in small ways before 1.0, and each change gets its own ADR.
