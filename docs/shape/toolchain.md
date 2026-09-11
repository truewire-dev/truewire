# The toolchain

The `truewire` package: one Python distribution, one CLI, installed with `pip install
truewire` or run with `uvx truewire`.

**T1.** The toolchain is written in Python and stays there. It generates four languages; it
does not need to be written in them. Python is where the spec model, the mock, the
validator and the MCP server already are, and a rewrite buys nothing a caller can see.

## The commands

Every command reads `truewire.toml`, exits non-zero on failure, and prints a result a
person can act on.

| Command | Contract | Status |
| --- | --- | --- |
| `init [name]` | Write the workspace skeleton: `.truewire/`, `.gitignore`, `packages/`, `spec/`, `docs/`, `.agents/`, `AGENTS.md`, `truewire.toml`. Idempotent. | shipped |
| `check` | Every `endpoint.json`, router and recording satisfies the authoring rules. | shipped |
| `capture <endpoint>` | Call one endpoint live through the project's own client and write the pair. | shipped |
| `generate [language]` | Render the spec into each declared backend. `--check` reports staleness; `--delete` removes orphans. | shipped |
| `examples` | Coverage of recordings, `--require-verified` to demand one per endpoint. | shipped |
| `surface` | Every endpoint in the spec is reachable as a method on the generated client. | shipped |
| `standards` | Docstrings, duplicate schemas, leaked secrets, router coverage. | shipped |
| `docs check` | Every code block type-checks against the generated package. | shipped |
| `mock` | Serve the recordings over real HTTP and WebSocket. | shipped |
| `mcp` | Serve a generated client's endpoints to an agent that calls the API. | shipped |
| `import openapi` | Turn an OpenAPI document into a spec tree. | shipped |
| **`call <language> <endpoint> [args]`** | Instantiate the client from `truewire.toml` and `.env` and make one call. A subscription prints the reply, then each message, until `Ctrl+C` unsubscribes cleanly. | target |
| **`test [language]`** | Run each declared package's own suite against `truewire mock`, without the caller knowing pytest from vitest from cargo. | target |
| **`lint [language]`** | Run each declared package's formatter, linter and type checker, configured from the package, reported uniformly. | target |
| **`score`** | The scorecard: every row in [score.md](score.md), one table, one exit code. | target |
| **`conform`** | The nightly replay and its report ([ADR 0012](../adr/0012-conformance-runs.md)). | target |
| **`agents update` / `agents check`** | Refresh the vendored skills and rules; fail on silent drift. | target |

**T2.** `generate` keeps its name. The first draft of the shape called it `codegen`, which
matches the subsystem and the manifest directory, but the command has shipped since 0.1.0
and is named in every skill, every ADR and every README. The rename would cost a
deprecation cycle and buy consistency with a noun. `codegen` stays the name of the
subsystem and of `.truewire/codegen/`.

**T3.** `lint` is a Truewire command, not a Justfile. A project should not require a task
runner on top of the toolchain that is already installed, and an agent should not have to
learn four toolchains' invocations to satisfy one gate. The per-language tools are the real
ones — ruff, pyright, eslint, tsc, clippy, rustfmt — configured in the package and invoked
by the toolchain.

**T4.** `test` and `lint` take an optional language and default to every declared one. The
exit code is the worst of them, and the output names which language failed.

## The package

```
src/truewire/
  schemas/        pydantic models for everything with a schema
    spec/           endpoint.json, router.json, examples
    docs/           docs.yml
    config/         truewire.toml
  workspace/      one handle on the current project
  codegen/        the IR and one backend per language
  mock/           the replay server, one module per transport
  call/           instantiate a client and make one call
  test/           run the declared packages' suites
  score/          the scorecard
  conform/        the nightly replay
  standards/      the lint rules that are not a language's own
  mcp/            serve a generated client to an agent
  cli/            one module per command, no logic of its own
  resources/      templates and files that init writes
```

**T5.** `cli/` contains argument parsing and output formatting and nothing else. Every
command's behaviour is importable without the CLI, because the conformance runner, the
tests and any future runner all need it without a subprocess.

**T6.** `Workspace` is the single way to reach anything in a project:

```python
workspace = Workspace.load('truewire.toml')
workspace.spec              # walk routers and endpoints
workspace.codegen           # settings and, per language, the manifest
workspace.secrets           # the names, never the values
workspace.packages          # the declared backends and their roots
```

Nothing else parses `truewire.toml`, and nothing else joins a path from it.

**T7.** Every artefact with a schema has one published at `truewire.dev/schemas/`, and the
pydantic model in `schemas/` is the source those are generated from. A file the toolchain
reads that an editor cannot complete is a file people will get wrong.

## Settled questions

These were open when the shape was first drafted. They are settled here so that they stop
being re-argued; each can be reopened by evidence, not by preference.

**T8. Secrets** are environment variables named by `[secrets].required`, read by the
caller, passed to the constructor. No vault, no keyring, no encrypted file in the repo. The
toolchain's job is to make a leak *detectable*, not to manage custody.

**T9. Schemas** are served from the site and cached under `.truewire/cache/`, never
vendored.

**T10. Skills** are vendored per project and refreshed by a command
([A10](agents.md#vendoring-and-drift)); not a plugin, for now.

**T11. Docs** for a project are published at `<name>.truewire.dev`, built from `docs/` and
`docs.yml` by the site, with the reference section generated from the spec.

**T12. The CLI's own tests** run in the repository's CI on every push, and the gates a
project runs are the gates the toolchain's CI runs on its own examples. A gate that the
toolchain cannot pass on its own example projects is not shipped.
