# ADR 0003: Response validation is a tolerant `TypedDict` base plus a `validator[T]` wrapper

- Status: accepted
- Date: 2026-08-07 (carried over 2026-09-06)

## Context

Six independently built clients converged on the same validation shape without copying it from one template: a `TypedDict` base decorated `@pydantic.with_config({'extra': 'allow'})` that every generated wire-shape type uses instead of a bare `typing_extensions.TypedDict`, plus `validator[T]`, a lazily built `pydantic.TypeAdapter[T]` wrapper that catches `pydantic.ValidationError` and re-raises it as the runtime's own `ValidationError`.

The pattern was reconsidered when one client's generated code was found building a bare `TypeAdapter` at module scope instead. On inspection that was a codegen inconsistency in one file, not evidence the wrapper is unnecessary. The core's envelope unwrapping and API error-code mapping already live in one place; folding pydantic's error translation into that same method would conflate two distinct failures (a malformed API response vs. a schema mismatch) into one.

`extra='allow'` is not pydantic's default (that is `'ignore'`, which drops unknown fields silently), and `'forbid'` was never used by any of the six. The choice is deliberate.

## Decision

`truewire-core` ships this pattern, and generated clients use it:

- a `TypedDict` base configured `extra='allow'`, used by every generated wire-shape `TypedDict`;
- `validator[T]`, whose `.python()`, `.json()` and `.dump()` are the one place `pydantic.ValidationError` becomes `truewire_core.ValidationError`;
- envelope and error-code mapping stay a separate method in the client core, and never duplicate the wrapper's translation.

`extra='allow'` specifically:

- `'forbid'` turns any undocumented field the API adds to a live payload into a hard failure on every validated call, with no client-side control over when the API ships the change or when the spec gets regenerated to catch up.
- `'ignore'` avoids the crash but silently drops the field from the returned dict. Data that was on the wire disappears with no signal, which a client that calls itself response-validated should not do quietly.
- `'allow'` keeps declared fields typed and validated and lets undeclared ones ride along. A typed caller sees nothing extra; a caller inspecting the dict still gets the field. Promoting it to a declared field later changes nothing about what was actually returned in the meantime.

## Consequences

The client survives additive drift, a new field on a response, without a broken validated call in production. The two failure modes, API-level errors and schema-validation errors, each have exactly one place they are mapped.

Left open: `extra='allow'` only protects against additive drift. An API that changes a field's meaning, or restructures a response entirely, still validates cleanly as long as the declared subset happens to type-check. Nothing today logs that extra fields arrived on a call for a human to fold into the next spec revision.
