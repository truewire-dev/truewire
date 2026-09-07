# ADR 0011: The generator reads a declared core contract and never imports the target package

- Status: accepted
- Date: 2026-09-07

## Context

A Truewire project has two halves: a spec tree the toolchain owns, and a hand-written `core/` package the project owns, holding the transport, signing, envelope unwrapping and error mapping the generated endpoint classes call through `self.request(...)` and `self.subscribe(...)`. The contract between the two was implicit. The generator emitted a call shape (`request(request, *, method, path, meta, validate, request_type, response_type)`), the `init` template and the example cores happened to implement it, and only pyright ever checked that a given core did.

One mechanism went further and imported the project's package while generating it. A composite router whose children need more than the parent's single `client` field (Kraken's `Streams`, built from two sockets; a subtree scoped to a network chosen by the caller) was composed by importing the child's base class, reading its `.new()` signature with `inspect`, matching parameter names against the parent's dataclass fields, and rendering the rest as caller parameters with types resolved through `get_type_hints`. That is why `truewire init` wrote a placeholder `main.py`: the package's `__init__.py` imports the generated root class, so a brand-new project's first generation would fail on an import of a file the generator had not written yet. It is also why a `TypeAliasType` was required for a `Literal` alias to survive introspection, and why `sys.path` had to be patched and unpatched around every router.

Two more pieces of the contract lived only in hand-written code. Every core carried a `class Meta(TypedDict)` written by hand to match the `[cores.<name>].meta` JSON Schema in `truewire.toml`, with nothing keeping the two in step. And every project carried a `core/types.py` mapping the six spec timestamp formats to `Annotated` aliases, written by `init` and identical in every project, because the generator imported `TimestampMillis` and friends from the project's own core.

The architecture review of 2026-09-07 found this coupling to be the one thing that blocks a second target language. A TypeScript backend cannot introspect a Python class, and a registry or docs site cannot read a contract that exists only as the behaviour of one project's code. The review rejected the alternative of a declarative core (auth shape, body encoding and envelope as data): Kraken's signing scheme and per-path body encodings are the target domain, not the exception, and a second language would then need an interpreter for that data as well. The smallest change that unblocks a second language is to publish the contract as data and delete the import.

## Decision

The generator reads the core contract from `truewire.toml` and the runtime. It never imports the package it is generating.

- **Composition is declared.** `[python.cores.<name>]` gains two keys. `forward` lists the `new()` keywords the composing class passes from its own same-named fields (`forward = ["market_client"]` renders `Streams.new(self.private_client, market_client=self.market_client)`). `params` maps the `new()` keywords a caller supplies to their types (`params = { network = "pkg.core:Network" }` renders `def token(self, *, network: Network) -> Token`; the table form `{ type = ..., required = false }` renders an optional). A core declaring either is built through `new(client, *, ...)`; one declaring neither is built as `Child(client=self.<field>)`. When the composing class's own core is the child's core, `params` are forwarded from `self` instead of exposed, which is what lets a parameterized subtree compose its descendants without asking for `network` at every level. The introspection code, the placeholder `main.py` and the `sys.path` patching are deleted.
- **The verbs are published.** `truewire_core.contract` ships `Protocol`s for what a base provides: `HttpEndpoint` (`request` with `method`), `CommandEndpoint` (`request` without it), `StreamEndpoint` (`subscribe`), `ClientRoot` (`new`, `__aenter__`, `__aexit__`) and `Composite` (`new(client, *, ...)`). The generator emits against these shapes; a core can check itself against them.
- **`Meta` is generated.** `truewire generate` writes `<package>/meta.py` with one `TypedDict` per `[cores.<name>]` that declares a `meta` schema, named `<Name>Meta`. A core annotates its `meta` parameter with that class; generated calls keep passing a plain dict literal, matched structurally. `truewire init` writes the first `meta.py` from the same renderer, so the core template imports it before the project ever generates.
- **Timestamp aliases move to the runtime.** `truewire_core.types` exports `TimestampSeconds`, `TimestampMillis`, `TimestampMicros`, `TimestampNanos`, `TimestampIso` and `DateIso`, and the converter instances behind them. Generated code imports them from there. `init` keeps writing `core/types.py` as a re-export so callers that import it from the project keep working, and so a project has an obvious place for an alias the runtime does not ship.
- **Gate.** A test generates a project with the target package refused from `sys.path` and asserts the plan is complete.

## Consequences

The contract is data. A TypeScript backend needs the same two `truewire.toml` keys and its own protocols; it needs nothing about any project's Python. A fresh project generates on the first run with nothing seeded. `Meta` cannot drift from its schema. The per-project `types.py` boilerplate is gone, and a runtime fix to a converter reaches every project without regeneration.

Harder: a project that composed a child through `.new()` has to say so in `truewire.toml`, where it used to be inferred; the generator reports a child it cannot build rather than guessing. A `params` type must be importable by name (`module:Name` or a builtin); an inline `Literal[...]` is not expressible, and the alias the previous mechanism required anyway is the answer. The generated `meta.py` is one more generated file in the package.

Left open: `auth` as a reserved endpoint field beside `meta` (review item 4), which this ADR does not add; the gRPC backend, which still imports generated proto stubs and has no example project; and the runtime version pin, which generated packages now need at `>=0.2.0` for `truewire_core.types`.
