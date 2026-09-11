# Packages

One package per language, generated from the same spec, published to that language's own
registry. A caller in any of them writes the same call and gets the same validation.

## Parity, honestly

**P1.** Languages are not equal and the shape says so rather than pretending. Three tiers:

| Tier | What the backend provides | Languages today |
| --- | --- | --- |
| **1 — complete** | generated client, runtime validation, strict typing, mock-backed tests, checked doc snippets, published package, MCP server, conformance replay | Python |
| **2 — client** | everything above except the MCP server and conformance replay | TypeScript, Rust |
| **3 — planned** | nothing yet | Go |

**P2.** A project declares only the languages whose backend has reached tier 2. Declaring a
tier-3 language in `truewire.toml` is an error, not a to-do, because a declared backend
that cannot generate makes every gate in the project unsatisfiable.

**P3.** Tier 1 is not a goal for every language. The MCP server and the conformance replay
run in the toolchain's own language, and a caller in Rust is served by a Python-hosted
conformance report exactly as well. What every language owes is a client that is typed,
validated, tested, documented and published.

## Python

```
packages/python/
  src/<name>/
    __init__.py  __init__.pyi       re-exports the client
    api/                            GENERATED; deletable in full
      main.py                       the root client
      __init__.py  __init__.pyi
      <router>/
        <endpoint>.py               one endpoint, one file
        __init__.py  __init__.pyi   the router
    cores/                          HAND-WRITTEN
      root/  <surface>/
    mcp/                            generated, tier 1, optional
    py.typed
  test/
  typings/
  pyproject.toml
  pyrightconfig.json
  ruff.toml
  README.md  CHANGELOG.md  LICENSE
```

**P4.** `api/` holds generated code and nothing else. `truewire generate --delete` removes
the directory and rebuilds it, and the manifest is a safety net rather than the only thing
standing between a rename and an orphaned file.

**P5.** Cores are hand-written, so they live outside `api/`, in `cores/`, named for the
surface they serve. *(This differs from the first draft of the shape, which nested each
core inside the router it served. Co-locating reads better; a directory that can be
deleted wholesale is worth more. `truewire.toml` maps core names to import paths, so the
layout costs nothing either way, and [I4](README.md#the-invariants) decides it.)*

**P6.** A core provides, trimmed to what the API needs: `transport/` (one module per
transport), `endpoint/` (the `RpcEndpoint` and `StreamEndpoint` mixins that generated
endpoints subclass), `auth.py`, `envelope.py`, `exceptions.py`, `types.py`, and `base.py`
on the root core only. The mixins own the lifecycle (`__aenter__`, `__aexit__`), the
request methods, and the typed `Meta` that the spec and `[cores.<name>].meta` agree on.

**P7.** Every `__init__.py` with a sibling `__init__.pyi` is three lines of
`lazy_loader.attach_stub`, and the `.pyi` carries the real imports and `__all__`. Importing
the package costs one module, not two hundred, and the type checker still sees everything.

**P8.** The lazy-loader discipline is required of generated code and optional in a core. A
generator cannot get the two halves out of step; a person editing a core by hand can, and
the cost of that mistake is worse than the import time it saves.

**P9.** `typings/lazy_loader/__init__.pyi` is written by `truewire init` from a toolchain
resource. It is not copied between projects by hand.

**P10.** Strict pyright and ruff, configured per package, both run by `truewire lint`. A
generated file that does not type-check is a generator bug, and is fixed in the generator.

**P11.** No generated per-package CLI. `truewire call` covers development, and a shipped
CLI is a second public surface to version, document and support for a use no caller has
asked for. *(withdrawn from the first draft, where it appeared with a question mark.)*

## TypeScript

**P12.** The same tree with the language's names: `src/api/` generated, `src/cores/`
hand-written, `test/` against the mock, `package.json`, `tsconfig`, ESM only, published to
npm. Strict mode, no `any` in generated output, and every exported symbol typed from the
spec rather than inferred.

**P13.** Validation is the runtime's, not the caller's: the generated client validates
responses the same way the Python one does, and the same recording that passes in Python
passes here.

## Rust

**P14.** `src/api/` generated, `src/cores/` hand-written, `tests/` against the mock,
published to crates.io. Async, one runtime-agnostic core crate, errors as an enum per
endpoint outcome rather than a stringly-typed catch-all.

## Go

**P15.** Not started. When it starts, it starts from the same tree and the same gates, and
it does not get a dialect of its own: a backend whose output needs rules the other three do
not have is a backend that has misunderstood the spec.

## Publishing

**P16.** Every package is published from CI by trusted publishing, triggered by merging the
release PR that `truewire-release` opened. No token is held by a person, and no package is
published from a laptop.

**P17.** Versions move together across languages within a project: one spec change, one
version, four releases. A language whose package did not change is still released, because
a caller comparing two languages at the same version number must be comparing the same
spec.
