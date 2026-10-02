# The shape

What a finished Truewire project looks like, and what the toolchain must provide to get
there. This is the **target**, not a description of the code as it stands. Where the two
differ, `truewire score` prints the gap, and the gap is the work.

Everything else in `docs/` explains *how* a part works. This tree says *what must be true*.
Read it first; read the rest when you need the detail.

## What Truewire is for

An API owner publishes documentation and, at best, one client library in one language,
written once and left to rot. Callers write their own, discover the undocumented fields by
crashing in production, and find out that an enum gained a value when an order fails.

Truewire's offer is three artefacts and one service:

1. **The spec** — one directory per endpoint, carrying request and response schemas and at
   least one recorded live request/response pair. This is the only artefact that is not
   regenerable, and everything else is a function of it.
2. **The clients** — generated from the spec in every supported language, validating at
   runtime, typed strictly, documented, and published to the language's own registry.
3. **The report** — a nightly replay of every recorded request against the live API, which
   says whether the API still behaves as documented, and dates every deviation
   ([ADR 0012](../adr/0012-conformance-runs.md)).

The artefacts are free. The service is the product.

## The invariants

Five rules decide every contested question below. When a design choice is not obviously
determined by anything else, it is determined by these.

- **I1 — The wire is the truth.** When documentation and a recording disagree, the
  recording wins, and the disagreement is written into the endpoint's `notes`.
- **I2 — Nothing is invented.** An enum value, a default, a page size, a timestamp format:
  each comes from the docs or from a recording, and the endpoint says which. An endpoint
  that cannot be called is declared `unverified` with a reason from the closed set, never
  quietly skipped.
- **I3 — Every step ends in an exit code.** No agent and no human decides that a step is
  done. A command decides. A rule in this document that no command checks is marked
  *(unchecked)* and is a claim, not a gate.
- **I4 — Generated code is disposable; the spec and the core are not.** Anything a machine
  can rewrite lives in a directory that can be deleted and rebuilt. Anything hand-written
  lives outside it. The boundary is a directory boundary, not a comment.
- **I5 — No secret enters the tree.** Credentials reach the client through constructor
  arguments the caller supplies from the environment. The core never reads a file, a
  recording never contains a live value, and `[secrets].required` names the variables so
  that a leak is a failed check rather than an incident.

## The two shapes

| Document | What it fixes |
| --- | --- |
| [workspace.md](workspace.md) | A repository that uses Truewire: the tree, and what every entry is for. |
| [agents.md](agents.md) | The skills and rules that an agent loads to produce one, and how they get there. |
| [packages.md](packages.md) | What a generated package looks like per language, and what parity between languages means. |
| [toolchain.md](toolchain.md) | The `truewire` package and its CLI: the commands, their contracts, and the module map. |
| [score.md](score.md) | The scorecard: the definition of *finished*, one row per checkable claim. |

## Clause ids

Every normative clause carries an id: `W` for workspace, `A` for agents, `P` for packages,
`T` for toolchain, `S` for score. A failing check names the id it failed, so that a report
points at a line in this document rather than at a feeling. Ids are stable; a clause that
is withdrawn keeps its number and is marked withdrawn.

## Status of this document

Draft. The shape it describes is agreed at the level of the tree and the invariants; the
clauses marked *(open)* are the ones still being argued, and each says what the argument
is. Nothing here is implemented by virtue of being written down: `truewire score` against
the three existing projects — `bluesky`, `weather-gov`, `kraken` — is the measure of how
much of it is real.
