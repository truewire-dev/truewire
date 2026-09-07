# ADR 0007: Redaction is declared per endpoint, and example matching is uniqueness-checked

- Status: accepted
- Date: 2026-08-14 (carried over 2026-09-06)

## Context

The mock server had two hardcoded functions that grew one more branch per API as each new client's auth-injection shape needed a special case: a query-string API key for one, an HMAC signature plus timestamp for another, `signature`/`timestamp`/`recvWindow` for a third, a monotonic `nonce` for a fourth. Every one of these is injected by the transport at send time, never a real caller parameter, never present in a recorded example, so the mock had to ignore it during comparison somehow.

"Declare the auth shape more precisely, then derive the ignore-list from it" was considered and rejected. It is the same whack-a-mole one level down: a new auth shape still means a new branch, just inside a schema instead of inside the mock.

The deeper problem is not an auth problem. Any request field a recorded example fundamentally cannot capture needs the same treatment, and inferring "which fields are like this" from an auth declaration can never cover a case with no relationship to authentication. A live WS session token attached to JSON-RPC `params` by the transport is one such case. A nonce is another.

A second, independent problem surfaced at the same time: matching was first-match-wins. Given several candidate examples for one route, the mock returned the first structural match, silently. Three endpoints in one corpus each recorded two examples whose requests were byte-identical at two points in a stateful narrative (empty vs. populated) but whose responses differed. The mock always served the first, and the second example's response was never exercised by any test. That gap was invisible until matching checked for it.

## Decision

**Declared redaction.** `redacted: list[str]` is an optional field on the endpoint, a sibling of `spec`, `pagination` and `envelope`. It names request keys the mock server ignores when comparing a real call against a recorded one: a query item, a body key, a JSON-RPC `params` entry, a WS frame key. Flat names, not dotted paths: a redacted name has nothing to route to, because the comparison already knows which substructure it is looking at.

Declared per endpoint, not per project, for the same reason `envelope` is: not every endpoint on a client is signed, and a mixed surface states that plainly instead of a client-wide default picking one answer for both.

`redacted` is never read when an example is replayed through the real client. A redacted key that is also a genuine parameter still needs its own recorded value. Redaction only widens what the mock accepts when comparing; it never substitutes a fabricated value into a real call.

**Uniqueness-checked matching.** Matching moves from "the first passing candidate wins" to "exactly one passing candidate succeeds." Zero candidates is the existing unexpected-parameters failure (HTTP 422). More than one is a new ambiguous-match failure: HTTP 409, or an `ambiguous_parameters` error frame over WS, carrying the tied candidates so a human can resolve them. 409 rather than 422 because an ambiguous match is never the caller's fault.

## Consequences

A new API with a new transport-injected field declares `redacted` on the endpoints that carry it and needs no change to the mock server. A duplicate or overlapping example corpus fails loudly instead of silently mis-serving one example's response to a request that should have resolved to another.

The fix for an ambiguous pair is a spec-content fix, not a matcher fix: collapse the pair to the single more informative example. There is no request-level signal to recover when the only variable was external account state.

Known, accepted limitation: on a WS dialect with no declared `verb` (ADR 0004), a genuine resubscribe to an already-active channel is indistinguishable from an unsubscribe by the active-membership fallback. Declaring `verb` resolves it.
