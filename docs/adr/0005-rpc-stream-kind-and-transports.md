# ADR 0005: `spec.kind` is `rpc` or `stream`; transport is a separate `transports` list

- Status: accepted, amended in part by [0006](0006-rpc-identifier-and-parameter-authoring.md)
- Date: 2026-08-08 (carried over 2026-09-06)

## Context

An earlier `spec.kind: 'http' | 'ws' | 'grpc'` conflated two different things: the transport a request travels over, and the call pattern the operation follows. `kind: 'http'` covered both plain REST and JSON-RPC over HTTP, distinguished only by whether the path started with `/`. `kind: 'ws'` covered both a one-shot request/reply sent as a WS frame and an open-ended push subscription, distinguished only by whether a message schema was declared. Neither distinction was a type. Both were read out of shape at runtime by whatever code happened to need them.

The conflation stopped being cosmetic when a concrete case needed both patterns to compose: a JSON-RPC API whose methods are reachable over both HTTP and WebSocket, with the same method name, the same parameters and the same envelope. Under the old `kind`, that operation needed two full endpoint files with no declared relationship between them.

## Decision

`spec.kind` is a two-way discriminated union:

- **`rpc`**: single request, single reply, over one or more declared `transports: ['http' | 'ws', ...]`.
- **`stream`**: a subscription pushing messages repeatedly. WS-only. It has no `transports` field to set, so a stream over HTTP is not a rejected value, it is a shape that cannot be constructed.

Reclassifying every existing spec is lossless and mechanical:

| Before | `kind` | `transports` |
| --- | --- | --- |
| REST or JSON-RPC over HTTP | `rpc` | `['http']` |
| WS command with a reply and no push | `rpc` | `['ws']` |
| WS subscription with pushed messages | `stream` | (none; implicitly WS) |
| Dual-transport JSON-RPC | `rpc` | `['http', 'ws']` |

The generator renders a `transport: Literal[...]` keyword on any `rpc` method with more than one declared transport, defaulting to the first listed. A single-transport method gets no such keyword.

gRPC was removed from the union at the time, as fully unused code, and re-added later against a real shape (`kind: 'grpc'`, unary only) once a client needed it.

## Consequences

Call pattern and transport are independent axes. An invalid combination is either unconstructable by the type or caught at load time, instead of surfacing later as a routing bug in the mock server.

Splitting `rpc` transports apart meant the mock server's WS handler could receive an `rpc` call over the same connection as subscription traffic. Dispatching by sniffing whether a frame "looks like JSON-RPC" would have reintroduced the inferred behavior this decision exists to remove, so the WS handler matches an RPC call by declared method name, the same comparator the HTTP side uses, before consulting the subscription matcher.

Amended by ADR 0006: the separate `channel` field this decision gave `rpc` endpoints for their WS identifier turned out never to differ from `path` and was removed.
