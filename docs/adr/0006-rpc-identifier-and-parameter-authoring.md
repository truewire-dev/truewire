# ADR 0006: An RPC operation has one identifier field, and request fields declare a role, not wire placement

- Status: accepted
- Date: 2026-08-13 (carried over 2026-09-06)

## Context

Two problems, found while reviewing the spec format, shared one root cause.

**A redundant identifier.** ADR 0005 gave `rpc` endpoints both `path` (for HTTP) and `channel` (for WS) so a dual-transport operation could declare them independently. A dual-transport client was then built: 218 endpoints, over 100 of them reachable over both transports. Checked against every one, `path == channel` with zero exceptions. The field introduced to let two transports diverge never diverged. Meanwhile the same field was being reused for WS-only commands to hold the command's event name, a reuse the format docs had to carry a defensive paragraph for.

**A positional-parameter workaround that was a live bug.** JSON-RPC's positional `params` has no OpenAPI location, so the format modeled it as a request body whose schema was a `prefixItems` tuple. That quietly changed what the body meant for those endpoints: not the literal POST body but the value of the frame's `params` field, a distinction stated only in prose. One client's core then wrapped the already-list-shaped value in one more list, so every positional call sent `params: [[a, b, c]]`. Its recorded examples stored the flat array. Coverage read 100% while the client sent a body the API rejected.

Both are the same question asked twice: what does a path-shaped string actually mean, and how does the format check it?

## Decision

1. An `rpc` endpoint has one identifier field, `path`, regardless of transport: a REST path, a JSON-RPC method name, or a WS command's event name. `method` (the HTTP verb) is optional; an API whose HTTP transport is uniformly POST may leave it unset and let the core default it. Streams keep `channel` exclusively.

2. A `{name}` placeholder in the identifier substitutes a request field of that name, and this is audited: every placeholder has a matching declared field, and every field the identifier names appears as a placeholder. The substitution already existed; this makes it a checked rule.

3. A request field is a normal, individually named argument, on any kind and any transport. Its place in the request schema states its role, not its literal wire serialization. Codegen and the client core decide how it reaches the wire: a URL query string for REST, a slot in a positional JSON-RPC `params` array in declaration order, a key in a WS command frame. Packing into a positional array becomes one explicit, testable step in the core rather than an assumption a generated method had to get right unaided. `eth_getBlockByNumber(blockNumber, includeTransactions)` is two request fields, in order.

4. A discriminated-union request stays a union: a request schema that is an `anyOf` of titled variants whose required fields differ (`LimitOrder` requires `price`, `MarketOrder` forbids it). Flat named fields cannot express "this whole call is exactly one of these five differently shaped things," and marking every variant-specific field optional lets a market order with a price type-check. Title the wrapper too, so the generated parameter has a name better than `body`.

5. The endpoint's generated function path is a validated type: dot-separated identifiers, checked at load time, instead of a bare string that fails with an opaque `AttributeError` during example replay.

## Consequences

Every dual-transport endpoint file dropped one redundant line. The positional-params prose rule was replaced by a checked one, and the double-wrap bug was fixed as part of the same change.

Left open: this does not declare how a request field is packed (positional array vs. named object). That is still API knowledge living in the core, the same way envelope unwrapping is. If a second API needs it stated rather than inferred, that is a follow-on.
