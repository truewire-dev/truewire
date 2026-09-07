# ADR 0004: Envelope extraction is declared per endpoint, and examples store the raw wire body

- Status: accepted
- Date: 2026-08-08 (carried over 2026-09-06)

## Context

Many APIs wrap every response in an envelope: `{retCode, retMsg, result}`, `{success, data}`, JSON-RPC's `{jsonrpc, id, result}`. The client core unwraps it and hands the caller the payload. Two questions follow: what does a recorded example store, the wire frame or the unwrapped payload, and where is the unwrapping declared so that checks and the mock server can read it?

The first design stored the unwrapped payload and reconstructed the envelope at mock-serve time from a per-client template. That worked until it did not: the template fabricated per-call fields it could not know, and the mock server grew a hardcoded JSON-RPC branch to cope.

The second draft moved to one envelope declaration per client. That answered the wrong question. Being uniform across transports for one operation says nothing about being uniform across operations for one client, and a real client already contradicted it: 9 of its 41 endpoints were wire JSON-RPC and unwrapped `result`, the other 32 were plain REST that returned the whole frame. Applying a client-wide extraction to all 41 broke 21 previously-passing tests before the mistake was caught.

## Decision

`envelope` is an optional field on the endpoint, a sibling of `spec` and `pagination`, for the same reason those are siblings: it is generation and validation metadata, and the request schema has nothing to say about it. There is no client-level file and no client-wide default. Every endpoint states its own envelope, or states none.

It holds two independent dotted paths:

- `payload`: the path from the raw wire frame to the value the client core hands the caller. Read by `truewire check` and any other static reader of a recorded response. Never read by the mock server, because the live client under test runs its own core and does its own unwrapping exactly as it would against the real API.
- `correlate`: how a request-supplied correlation value (a JSON-RPC `id`, mostly) is threaded into a served response. Read only by the mock server. Absent when the envelope carries nothing request-echoed.

For an endpoint that declares `envelope`, a recorded response's `payload` is the literal wire response. For one that does not, it is the whole frame the caller receives, exactly as before. The mock server collapses to one path for any `rpc` endpoint on any transport: match the request, look up the example, serve its raw payload, apply `correlate` if declared. The template reconstruction and the hardcoded JSON-RPC branch are gone.

A WS-transport RPC endpoint that declares `envelope` extracts exactly like an HTTP one when its reply example is validated. A WS command whose core never unwraps anything declares no `envelope` and its reply schema describes the whole frame.

**Stream verbs.** A stream endpoint's `envelope` also declares `channel`, the path into the subscribe frame that carries the channel identity, and `verb`, the path that carries subscribe-vs-unsubscribe intent plus the two literal values it takes (`{"path": "op", "subscribe": "subscribe", "unsubscribe": "unsubscribe"}`). Before `verb` existed the mock server inferred intent from up to four undeclared signals, including a blind recursive scan of the subscribe payload for the string `"subscribe"`. That guess was right until an API's dialect did not match it, at which point the mock crashed on unsubscribe. An absent declaration defaulting to a guess instead of failing loudly is the exact anti-pattern `channel` already existed to end, one field over.

## Consequences

`truewire check` validates a stored example against the shape the core actually returns, by extracting first, rather than validating a template's guess. The mock server is transport- and envelope-agnostic. A mixed client (some endpoints enveloped, some not) is expressible, which the client-level design could not do.

`truewire check` warns on a stream endpoint with no `verb`. Every stream has subscribe and unsubscribe semantics, so there is no "maybe this doesn't apply" case; it is a warning rather than an error only while existing specs migrate.

Left open: an envelope field that is not the payload and varies per call (a server timestamp, a request-cost counter) is still whatever the recording captured. Storing the raw wire body makes that visible as recorded data instead of hiding it behind a template, but it does not make it live.
