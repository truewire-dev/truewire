<!-- vendored from truewire 0.10.1 sha256:e21783789c40eb67; `truewire agents update` rewrites this file -->
# Agent skills

Six skills that take a coding agent from a docs URL to a project that passes every Truewire
gate. Each is a `SKILL.md` an agent loads on demand (the format Claude Code, Cursor and
Codex read). They are written for any API: REST, JSON-RPC, WebSocket, with or without an
OpenAPI document, with or without credentials.

| Skill | Input | Output | Gate |
| --- | --- | --- | --- |
| `discover` | a docs URL | an endpoint inventory (`spec/inventory.md`) | reviewed by a human once |
| `spec` | the inventory, the docs | `endpoint.json` per endpoint, routers, shared schemas | `truewire check` |
| `core` | the API's auth and envelope | `src/<pkg>/core/` | one `truewire capture` succeeds |
| `implement` | a spec that checks | recorded examples, a generated client, tests | `examples --require-verified`, `surface`, tests against `mock` |
| `docs` | a generated client | README with checked code blocks | `truewire docs check` |
| `review` | a finished project | a review against `docs/standards.md` | `truewire standards` |

The order is the order of the table. Every step ends with a command whose exit code says
whether the step is done; an agent never decides that for itself. The skills were written
from the process that built `examples/github` and `examples/kraken`, then proven by an
agent loading only these files on three GitHub endpoints not in the repository; its
findings shaped the current text.

Rules that hold across all six:

- The wire is the truth. When the docs and a recording disagree, the recording wins and the
  disagreement goes into the endpoint's `notes`.
- No invented values. An enum, a default, a page size, a timestamp format: each comes from
  the docs or from a recording, and the endpoint's `notes` say which.
- No secrets in the tree. Credentials come from environment variables the core reads;
  recordings are scrubbed with `truewire capture --scrub KEY`.
- An endpoint that cannot be called is declared `unverified` with a reason, never skipped.

## Vendored copies

These files ship inside the `truewire` package. `truewire init` copies them into a project's
`.agents/skills/`, so an agent reads them with no network and no install step. Each copy
carries a line naming the toolchain version that wrote it and a digest of what it wrote,
such as `<!-- vendored from truewire 0.11.0 sha256:...; ... -->`, right after a skill's
front matter.

`truewire agents update` rewrites every copy from the installed toolchain.
`truewire agents check` exits 1 when a copy differs from the installed one, naming the
file, and `truewire standards` runs it. A copy whose text matches passes even if its
version line names an older toolchain. The digest tells a copy edited since it was written
from one an older toolchain wrote differently: `check` calls the first `edited`, the second
`stale`.

To keep a deliberate change, for example to hold a skill at an older version or to fit it to
one API, add this line anywhere in the file, on a line of its own and not indented:

    <!-- truewire: local edit -->

`agents check` then accepts the file, and `agents update` leaves it alone unless given
`--force`. A file under `.agents/` that the toolchain did not write is never touched, and
a copy the toolchain no longer ships is removed only if nobody changed it; delete the
version line of an edited one to make it the project's own.
