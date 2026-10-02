# Agents

Truewire is built to be driven by a coding agent, and the agent's instructions ship with
the toolchain rather than living in a prompt. Eight skills, one per step, each ending in a
command whose exit code decides whether the step is done ([I3](README.md#the-invariants)).

## The skills

| Skill | Input | Output | Gate |
| --- | --- | --- | --- |
| `truewire-discovery` | a docs URL | an endpoint inventory and the surfaces it splits into | a human reads it once |
| `truewire-core` | the API's auth, envelope and errors | `core/` per surface, hand-written | one `truewire capture` succeeds |
| `truewire-spec` | the inventory and the docs | `endpoint.json`, routers, shared schemas, recordings | `truewire check`, `truewire examples --require-verified` |
| `truewire-impl` | a spec that checks | generated packages, cores refined, tests | `truewire generate --check`, `truewire test`, `truewire lint`, `truewire surface` |
| `truewire-docs` | generated packages | `docs/` and each package README | `truewire docs check` |
| `truewire-review` | a finished project | a report against the standards | `truewire standards`, and the report |
| `truewire-release` | the diff since the last release | release notes, a version bump, a PR | the PR's own CI; merge publishes |
| `truewire-pipeline` | a docs URL and nothing else | all of the above | `truewire score` is full |

**A1.** The order of the table is the order of the work, with one exception:
`truewire-core` precedes `truewire-spec`, because a spec written before a single successful
call is a spec written from documentation, and documentation is not the wire
([I1](README.md#the-invariants)).

**A2.** Each skill states its gate, and stops at it. A skill never declares its own step
finished, and never proceeds past a red gate by narrowing what it claimed to do.

**A3.** `truewire-pipeline` delegates; it does not implement. It runs the seven skills in
order, then enters a hardening loop: `truewire-review` until two consecutive passes produce
no blocking finding **and** `truewire score` is full. "No issues found" is not a
termination condition, because a reviewer worth running always finds something; "no
*blocking* issues in two passes" is.

**A4.** A skill that needs a decision a person must make — a credential tier, a paid
account, whether an endpoint may be called at all — writes the question into the project's
`NOTES.md` and continues with everything that does not depend on the answer.

## The rules

**A5.** `.agents/rules/<language>.md` is the style a generated package and its
hand-written core must satisfy: naming, typing strictness, error handling, what the
language's linter is configured to enforce. One file per backend: `python.md`,
`typescript.md`, `rust.md`, `go.md`.

**A6.** Rules are enforced by a linter wherever a linter can express them. A rule that no
tool checks belongs in the file only if a reviewer can apply it consistently; a rule that
is neither checkable nor consistently applicable is deleted, not written down.

## Wiring

**A7.** `AGENTS.md` at the project root is the entrypoint: what this project is, what
Truewire is, which skills exist, which rules apply, and the command list. It is written for
an agent that has never seen the repository and has no memory of any previous session.

**A8.** `CLAUDE.md` contains exactly one line, `@AGENTS.md`. Any other agent runner that
wants its own filename gets the same one-line redirect. There is one document, and the
filenames are aliases.

**A9.** `.claude/skills/` and `.claude/rules/` are symlinks into `.agents/`. The canonical
location is `.agents/`, because it is the one that Claude Code, Cursor and Codex all read
without being told.

## Vendoring and drift

**A10.** Skills and rules are **vendored copies** in each project, not a plugin and not a
submodule. An agent working in `bit2me/` reads the files that are in `bit2me/`, with no
network and no install step, and a project can hold a skill at an older version
deliberately.

**A11.** `truewire agents update` rewrites the vendored copies from the installed
toolchain. Each file carries the toolchain version that produced it, and `truewire agents
check` fails when a vendored copy has drifted from the installed one without being marked
as a deliberate local edit. This is the same discipline as `generate --check`: a copy is
allowed, a silent copy is not.

**A12.** *(open)* Publishing the skills as a Claude Code plugin as well would make the
first install one command instead of a clone. It is not done, because a plugin is one
runner's format and the vendored copy is every runner's, and doing both means two things to
keep in step. It is worth revisiting once a stranger has actually tried to start a project
from scratch.
