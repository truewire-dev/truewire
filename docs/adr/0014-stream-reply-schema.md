# ADR 0014: A stream declares its subscription reply schema separately from its push payload

- Status: accepted
- Date: 2026-09-08
- Carried over: 2026-09-14, from the system Truewire was extracted from (its ADR 0022)

## Context

A `kind: 'stream'` endpoint in the request/response shape (design §8) carried two schemas:
`parameters` for the subscribe call and `payload` for each pushed message. The legacy
`openapi` shape had a third slot, `responses.reply`, and the migration dropped it: no
mechanized client's `subscribe()` did anything with the acknowledgement beyond handing it
back, so nothing seemed lost. `truewire_core.util.Stream` kept an `Any`-defaulted
`SubscriptionReply` type argument that no generated method ever filled.

dydx showed what was lost. Its Indexer acks every channel with a real value — `v4_orderbook`
with the full book as `{price, size}` objects, `v4_markets` with every market, the two
subaccount channels with the whole subaccount — and pushes a different shape afterwards
(`[price, size]` tuples, deltas). Its hand-written `StreamsMixin.subscribe` had one
`response_type` and applied it to both frames, so entering an order-book stream raised
`ValidationError` before the first push (SDK PoC report, 2026-09-06, live on testnet), and
three channels validated their ack silently to `{}`. The interim fix stopped validating the
ack at all, returning it raw into the `Any` slot. The seven `*ReplyContents` types sat
generated in `indexer/schemas.py`, unreferenced, because the spec had nowhere to name them.

The alternatives were a per-client `meta` convention (a core-private fact codegen would
have to be taught per venue) or a union `payload` covering both frames (which types every
push as possibly-a-snapshot and every ack as possibly-a-delta — wrong on both sides).

## Decision

`StreamEndpointSpec` gains an optional fourth new-shape field, `reply`: a JSON Schema for the
value the core hands back as `Stream.reply`, after `envelope.reply_payload` (or `payload`)
extraction, exactly as `payload` describes a pushed message after `envelope.payload`
extraction. It is the new-shape counterpart of the legacy `openapi.responses.reply`.

- `truewire check` validates every recorded `<id>.reply.json` against it, extracting
  through the envelope first. The authoring audit sees it as the `reply` response.
- The Python generator renders it as the second type argument of the generated method's
  `StreamManager[Payload, Reply, Any]` and passes it to the resolved core as `reply_type=`,
  beside `response_type=`. A bare `$ref` imports the shared type, as `payload` does.
- A stream that declares no `reply` generates byte-for-byte what it did before: no
  `reply_type=`, `Any` in the reply slot. Verified by regenerating every stream-carrying
  client and diffing nothing.
- A core is expected to validate the ack through `reply_type` and never through
  `response_type`, and vice versa. dydx's `StreamsMixin.subscribe` does; its seven channels
  declare `reply` pointing at their `*ReplyContents` schemas.

## Consequences

- `Stream.reply` is a real type on any channel that declares one, so an SDK consumer can
  seed a local book from the ack without a cast. The generated `validate` flag now governs
  the ack as well as the pushes on such a channel.
- Declaring `reply` on a stream whose core's `subscribe()` has no `reply_type` parameter
  fails pyright at generation time. That is deliberate: the core is the one place that
  knows how the ack is unwrapped, so adopting the field is a per-core step, not something
  codegen can do for a venue on its own.
- Left open: every other mechanized client's cores still take no `reply_type`, and their
  streams declare no `reply`. Where a venue's ack carries a value worth typing (deribit's
  `result` list of subscribed channels, say), adopting it is one spec field plus one
  parameter on that core's `subscribe()`; the audit does not require it.
