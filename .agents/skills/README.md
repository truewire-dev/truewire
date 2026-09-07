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
whether the step is done; an agent never decides that for itself. The skills are written
from the process that built `examples/github` and `examples/kraken`; the first end-to-end
run by an agent loading only these files is roadmap item 8's remaining "done when".

Rules that hold across all six:

- The wire is the truth. When the docs and a recording disagree, the recording wins and the
  disagreement goes into the endpoint's `notes`.
- No invented values. An enum, a default, a page size, a timestamp format: each comes from
  the docs or from a recording, and the endpoint's `notes` say which.
- No secrets in the tree. Credentials come from environment variables the core reads;
  recordings are scrubbed with `truewire capture --scrub KEY`.
- An endpoint that cannot be called is declared `unverified` with a reason, never skipped.
