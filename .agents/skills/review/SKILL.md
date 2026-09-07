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

1. **Run every gate, in this order, from a clean venv.** Record the exact output line of
   each:
   `truewire check`, `truewire examples --require-verified`, `truewire surface`,
   `truewire standards`, `truewire generate python --check` (no drift between spec and
   generated code), `PYTHONPATH=src pytest -q`, `pyright`, `truewire docs check`,
   `truewire docs lint`.
2. **Read the recordings, not only the schemas.** Open five `examples/*.response.json` at
   random. Does each field's schema type match what is on the wire? Are timestamps declared
   with the format the value actually has? Is anything credential-shaped present without
   an obvious placeholder?
3. **Read `notes`.** Every judgement call must cite its source (docs URL, recording id).
   An `enum` or `default` with no source is a finding.
4. **Read the `unverified` declarations.** Each reason must be one nobody could have
   removed from here: `missing_credentials` with the tier named, `unsafe` with the effect
   named, `requires_state` with the state named. "Did not get to it" is not a reason.
5. **Pagination.** For every `_paged` method, one test walks a recorded multi-page sequence
   to its terminator. A single-page recording proves nothing about the walk.
6. **Standards.** `docs/standards.md` lists the rules `truewire standards` enforces and the
   ones that are review-only; go through the review-only ones by hand and say so.
7. **Write it down.** `REVIEW.md`: a table of gate → result, a list of findings with file
   paths, and a verdict: deliver, or fix these first.

## Done when

- Every gate in step 1 exits 0, and the review says so with the output line.
- Zero findings open, or each open finding has an owner and a reason it can ship.

## Do not

- Do not soften a red gate into "known issue". Red blocks delivery.
- Do not review from the docs; review from the recordings and the code.
