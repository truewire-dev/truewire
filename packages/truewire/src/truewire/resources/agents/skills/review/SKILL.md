---
name: truewire-review
description: Review a finished Truewire project against docs/standards.md and every CLI gate before it is delivered, released or merged. Use as the last step of a spec-service delivery or before a pull request that adds endpoints.
---

# Review: the last gate before anyone else sees it

## Goal

A written review, `REVIEW.md` in the project (or the pull request body), listing each
check with its result and each judgement call with its evidence. A reviewer who has never
seen the API can read it and trust the numbers.

## Steps

1. **Run every gate, in this order.** Use the venv the project's `pyrightconfig.json`
   points at; a fresh venv with only `truewire` installed is better when you can make
   one, because it catches an import that only worked thanks to a dev dependency. Record
   the exact final output line of each:
   `truewire check`, `truewire examples --require-verified`, `truewire surface`,
   `truewire standards`, `truewire standards --only links` (warning-severity: exits 0
   even when every link is unreachable, so read its warnings), `truewire generate python
   --check`, `PYTHONPATH=src pytest -q`, `pyright` (the project's own config, which must
   include `test/` for S17 to be checked), `truewire docs check`, `truewire docs lint`.
2. **Read the recordings, not only the schemas.** Open five `examples/*.response.json`
   (all of them when there are fewer). Does each field's schema type match what is on the
   wire? Are timestamps declared with the format the value actually has? Grep every
   recording for the API's own token shapes, as listed in the inventory's `## Transport`
   (GitHub: `gh[pous]_`), and for anything credential-shaped without an obvious
   placeholder.
3. **Read `notes`.** Every judgement call must cite its source (docs URL, recording id).
   An `enum` or `default` with no source is a finding; "docs were unreachable, written
   from memory" is acceptable only when a recording confirms the field.
4. **Read the `unverified` declarations.** Each reason must be one nobody could have
   removed from here: `missing_credentials` with the tier named, `unsafe` with the effect
   named, `requires_state` with the state named. "Did not get to it" is not a reason.
5. **Pagination.** For every `_paged` method, one test walks a recorded multi-page sequence
   to its terminator. A single-page recording proves nothing about the walk.
6. **Review-only standards.** `truewire standards` enforces the mechanical rules; these
   are checked by reading: S4 (`py.typed` shipped), S9 (errors mapped to `AuthError`,
   `RateLimited`, `BadRequest` where the API distinguishes them), S10, S11, S12, S17
   (a `typing_usage.py` under `test/` exercising the public types), S18, S19, S23, S24,
   S27, S28. `docs/standards.md` has each rule's text; tick them one by one.
7. **Fix, then re-run.** The reviewer fixes what the review finds when the fix is local
   (a missing error mapping, a wrong format, a note without a source) and re-runs every
   gate from step 1. A finding that changes the spec's shape or the core's design goes
   back to the `spec` or `core` skill with the finding written down.
8. **Write it down.** `REVIEW.md`: a table of gate → result with the output line, the
   recordings read, the notes audit, the review-only standards table, findings with file
   paths, and a verdict: deliver, or fix these first.

## Done when

- Every gate in step 1 exits 0, and the review says so with the output line.
- Zero findings open, or each open finding has an owner and a reason it can ship
  (an unreachable docs host is such a reason; a red gate never is).

## Do not

- Do not soften a red gate into "known issue". Red blocks delivery.
- Do not review from the docs; review from the recordings and the code.
