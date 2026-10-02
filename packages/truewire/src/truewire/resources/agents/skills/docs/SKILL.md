---
name: truewire-docs
description: Write a Truewire project's README and usage docs so every code block type-checks against the generated client, verified by `truewire docs check` and `truewire docs lint`. Use after `truewire-implement`.
---

# Docs: a README whose examples cannot rot

## Goal

A `README.md` a new user can follow end to end: install, construct the client, call three
representative endpoints (one plain, one paginated, one stream if the API has one), run
against the mock. Every Python block in it is type-checked against the generated package by
`truewire docs check`, so a method rename or a changed field fails CI instead of a reader.

## How the gates read the file

- `truewire docs check` type-checks **each fenced `python` block on its own** (top-level
  `await` is wrapped in a coroutine). Every block imports what it uses; no block may rely
  on a `client` defined in an earlier block.
- `truewire docs lint` rejects prose written about the build process instead of for the
  reader. Words and phrases it bans: `unverified`, `codegen`, `truewire.toml`, any
  `spec/...` path, `AGENTS.md`/`CLAUDE.md`, "the monorepo", "source repository",
  "out of scope"/"scoped out", "doc-derived", "live-verified"/"live-captured",
  "confirmed live", "provenance", "not exercised", "this build/run/session", "what was
  built", "we could not"/"we were unable", "gate N". Write for the reader: "recorded" and
  "not recorded because ..." instead of verified/unverified; "the client" instead of "what
  was built". It also checks relative links and applies a word budget to
  `docs/how-to/`, `docs/index.md` and `docs/api-keys.md` (the README is uncapped).

## Steps

1. **Install and construct.** `pip install -e .` (the project's own `pyproject.toml`) and
   `<Name>.new(base_url=..., api_key=...)`, where `<Name>` is the class `truewire init`
   named (`[python].name`, exported from `src/<pkg>/__init__.py`). Show where credentials
   come from (an environment variable the caller reads) and that `validate=True` is the
   default.
2. **Three calls.** Real method names from `src/<pkg>/`, real field names from the
   recordings, real types in the comments (`Decimal`, `datetime`, `Literal[...]`). Show a
   `_paged` walk both as `async for page in ...` (one page's rows per iteration, empty
   pages skipped) and as `await ...` (every row, flattened). Annotate results as
   `Sequence[...]`, which is what the walker returns, not `list[...]`.
3. **Mock.** `truewire mock` prints a base URL; show the same call against it with the
   exact recorded arguments (the mock answers only a request that matches a recording,
   `page=1` included for a walk's first page).
4. **Coverage, honestly.** A short table: endpoints, recorded, not recorded and why. Copy
   the `Coverage (paired examples)` line from `truewire examples`; the `Public`/`Authed`
   split there is informational. Do not round up.
5. **Gates.** `truewire docs check` and `truewire docs lint`, both exit 0.

## Done when

- `truewire docs check` and `truewire docs lint` exit 0.
- The README's coverage table equals `truewire examples`' output.
- A reader can copy every block in order and it runs against the mock.

## Do not

- Do not paste a response body that was not recorded.
- Do not describe endpoints that were not recorded as if they were tested; say what is.
