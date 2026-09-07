---
name: truewire-docs
description: Write a Truewire project's README and usage docs so every code block type-checks against the generated client, verified by `truewire docs check`. Use after `truewire-implement`.
---

# Docs: a README whose examples cannot rot

## Goal

A `README.md` a new user can follow end to end: install, construct the client, call three
representative endpoints (one plain, one paginated, one stream if the API has one), run
against the mock. Every Python block in it is type-checked against the generated package by
`truewire docs check`, so a method rename or a changed field fails CI instead of a reader.

## Steps

1. **Install and construct.** `pip install -e .` (the project's own `pyproject.toml`) and
   `Client.new(base_url=..., api_key=...)`. Show where credentials come from (environment
   variables the caller reads) and that `validate=True` is the default.
2. **Three calls.** Real method names from `src/<pkg>/`, real field names from the
   recordings, real types in the comments (`Decimal`, `datetime`, `Literal[...]`). Show a
   `_paged` walk both as `async for page in ...` and as `await ...`.
3. **Mock.** `truewire mock` and the same call with `base_url` pointed at the printed URL.
4. **Coverage, honestly.** A short table: endpoints, verified, unverified by reason. Copy
   the numbers from `truewire examples`; do not round up.
5. **Gate.** `truewire docs check` type-checks every fenced Python block against the
   package; `truewire docs lint` catches broken relative links. Both exit 0.

## Done when

- `truewire docs check` and `truewire docs lint` exit 0.
- The README's coverage table equals `truewire examples`' output.
- A reader can copy every block in order and it runs against the mock.

## Do not

- Do not paste a response body that was not recorded.
- Do not describe endpoints that are `unverified` as if they were tested; say what is.
