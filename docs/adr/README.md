# Architecture Decision Records

A durable record of decisions that are hard to reverse, cost real evidence to reach, or trade one guarantee for another. Not a changelog: `git log` already has that. Not a place to restate what the code or a doc page already makes obvious. Write one when a decision would otherwise survive only in a commit message or a pull request thread that might itself get lost.

One file per decision, numbered, never edited in place. A changed decision gets a new ADR that supersedes the old one, and the old one's `Status` line is updated to say so.

Most of these were carried over from the system Truewire was extracted from. The reasoning is kept; the names and the war stories are trimmed to what still applies.

| # | Title | Status |
| --- | --- | --- |
| [0001](0001-endpoint-outcome-taxonomy.md) | Every documented endpoint resolves to verified, unverified with a reason, or excluded | accepted |
| [0002](0002-declared-pagination.md) | Pagination is declared as an audited discriminated union, not inferred | accepted |
| [0003](0003-tolerant-validation-wrapper.md) | Response validation is a tolerant `TypedDict` base plus a `validator[T]` wrapper | accepted |
| [0004](0004-declared-envelope-extraction.md) | Envelope extraction is declared per endpoint, and examples store the raw wire body | accepted |
| [0005](0005-rpc-stream-kind-and-transports.md) | `spec.kind` is `rpc` or `stream`; transport is a separate `transports` list | accepted, amended by 0006 |
| [0006](0006-rpc-identifier-and-parameter-authoring.md) | An RPC operation has one identifier field, and request fields declare a role, not wire placement | accepted |
| [0007](0007-declared-redaction.md) | Redaction is declared per endpoint, and example matching is uniqueness-checked | accepted |
| [0008](0008-whole-body-dump.md) | A request body serializes through `validator(Type).dump()`, not per-field conversion | accepted |
| [0009](0009-standalone-project-and-truewire-toml.md) | Truewire is a standalone project; `truewire.toml` replaces the monorepo's per-client convention | accepted |

New entry: copy `0000-template.md`, number it next, add a row above.
