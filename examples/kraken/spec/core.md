# kraken Core

## Scope

This pass covers **Spot only** -- REST (`api.kraken.com/0`) and WebSocket v2
(`ws.kraken.com/v2` / `ws-auth.kraken.com/v2`). `client.toml`'s `[secrets]` only
provisions `KRAKEN_API_KEY`/`KRAKEN_PRIVATE_KEY`, which are Spot-specific keys --
Kraken Futures uses an entirely separate key pair and onboarding is Futures-specific,
so Futures, FIX, and Institutional (all mapped in `discovery.md` for completeness) are
out of scope until separate credentials exist. Spot WS v1 (legacy) and the dedicated L3
(`ws-l3.kraken.com/v2`) connection are also out of scope -- `discovery.md` already flags
v1 as "not the integration target for new work," and L3 is a narrow, recently-split-off
feature that doesn't change the shape below.

## Surfaces

- **Spot** (HTTP RPC): public market data (unsigned GET) and private account/trading/
  funding/Earn calls (signed POST), sharing one envelope and one `HttpRpcClient` --
  matching every other Typed client's "not a public/private split, credentials is just
  an optional field" pattern.
- **Streams** (WebSocket v2, stream-shaped): channel subscriptions only (`ticker`,
  `book`, `executions`, `balances`, ...) -- every subscribe/unsubscribe is answered by a
  `req_id`-correlated reply, then followed by pushes. Two physical connections back this
  surface: public (`ws.kraken.com/v2`, no auth) and private (`ws-auth.kraken.com/v2`,
  token auth) -- not two surfaces, since (mirroring Spot's own public/private split) the
  only difference between them is whether the transport was built with a token source.
- **TradingWs** (WebSocket v2, RPC-shaped): the WS trading methods (`add_order`,
  `cancel_order`, ...), over the same `req_id`-correlated reply protocol as Streams but
  request/reply only, no pushes -- `await`ed directly like an HTTP call, not iterated.
  Kept out of `Streams` on purpose: nesting a single-shot RPC call under a property whose
  every other member returns an iterable `StreamManager` misled callers (and one doc
  page) into treating it as a subscription. Reaches Kraken over the *same* private
  connection `Streams.private` uses -- `Kraken.new()` builds one `private_client` and
  hands it to both -- since Kraken puts both there; it is a separate top-level surface,
  not a separate socket.

Spot and Streams are not the same method namespace reachable over two transports (no
JSON-RPC-style shared surface here): REST is form/query REST, WS is a distinct
request/reply-plus-push protocol with different field names for the same concepts
(`pair` vs `symbol`, `volume` vs `order_qty`, `price` vs `limit_price`, ...). No
swappable-`Client` abstraction was needed.

## Transport

**Spot** -- single base URL, `https://api.kraken.com`. `Spot` composes `market_data`,
`account`, `trading`, `funding`, `earn` as `functools.cached_property` children, each an
`RpcEndpoint(client=self.client)` sharing the one `HttpRpcClient`.

**Streams** -- two base URLs, resolved once in `Kraken.new()`:
`wss://ws.kraken.com/v2` (public) and `wss://ws-auth.kraken.com/v2` (private). `Streams`
holds `market_client`/`private_client` directly (not a single `client` field) since
they're genuinely two connections; `market_data` is a `StreamEndpoint` over
`market_client`, `private` is a `StreamEndpoint` over `private_client`.

**TradingWs** -- one `WsRpcEndpoint`, backed by the *same* `private_client` instance
`Streams.private` uses. `Kraken.new()` builds `private_client` once and passes the same
`KrakenSocketClient` to both `Streams(private_client=...)` and
`TradingWs(client=private_client)` -- one physical socket, two independent top-level
Python surfaces over it. Safe because `KrakenSocketClient.__aenter__`/`__aexit__`
delegate to the underlying `Socket`, which connects lazily on first use and is
reference-safe to enter/exit from two owners (`typed_core.ws.socket.Socket.__aenter__`
takes ownership without connecting; `__aexit__` only closes if something actually
opened).

## Envelope

**Spot REST**: `{"error": [...], "result": {...}}`, uniform across every endpoint,
public or private. A non-empty `error` array means failure -- HTTP status stays 200 for
almost every logical failure. `core/envelope.py`'s `unwrap()` returns `result` directly
(unwraps the envelope); callers never see the wrapper.

**Streams**: `{"method", "req_id", "success", "result"|"error", "time_in", "time_out"}`
for every reply (trading calls and subscribe/unsubscribe acks alike); channel pushes are
separately shaped `{"channel", "type": "snapshot"|"update", "data": [...]}`. `data` is
an array (a subscription can cover several symbols/assets in one push), so each
endpoint method's result type is the *message* (`channel`/`type`/`data`), not a
flattened per-item type -- validating against a flattened type silently swallowed the
`data` wrapper during smoke testing; see Gotchas.

## Authentication

**Spot REST** -- per-request HMAC-SHA512, exactly as `discovery.md` describes:
`API-Sign = base64(HMAC-SHA512(base64_decode(private_key), path + SHA256(nonce + form_body)))`,
sent as the `API-Key`/`API-Sign` headers alongside a `nonce` that must be strictly
increasing. `core/auth.py`'s `NonceGenerator` guarantees that (current ms, bumped by one
on collision, under an `asyncio.Lock`) since nonce collisions from rapid concurrent
calls would otherwise silently break auth. `authed_request` url-encodes the body once
and signs *that exact string* -- not a re-encoding of the dict -- since Kraken hashes
the literal bytes sent.

**Streams** -- token auth, not per-message signing. The private connection is built
with a `TokenCache` (`core/auth.py`, shared shape with other Typed clients' token-cache
transports) whose `fetch` callback is the Spot HTTP client's `get_ws_token`
(`POST /private/GetWebSocketsToken`, itself HMAC-signed like any other private REST
call). The cached token is merged into *every* outgoing `params` on that connection --
subscribe requests and RPC calls alike -- inside `SocketConnection.rpc_send`, since
Kraken attaches it the same way (`params.token`) in both cases. The public connection is
built with no token source and simply never attaches one.

Both `KRAKEN_API_KEY`/`KRAKEN_PRIVATE_KEY` resolve once, in `Kraken.new()`, and are
shared by both transports (`core/auth.resolve_credentials`).

## Errors

Kraken's `error` array entries follow `<Category>:<Description>` (`EAPI`, `EAuth`,
`EAccount`, `EGeneral`, `ETrade`, `EFunding`, `EOrder`, `EService`, `EBM`). There is no
machine-readable full code list; `core/envelope.py`'s mapping is built from Kraken's own
[categorized error reference](https://docs.kraken.com/exchange/guides/general/errors):

| Category (default) | typed_core exception |
|---|---|
| `EAPI`, `EAuth`, `EAccount` | `AuthError` |
| `EGeneral`, `ETrade`, `EFunding` | `BadRequest` |
| `EOrder`, `EService`, `EBM` | `ApiError` |

...overridden by substring match (cuts across categories, per Kraken's own docs):

| Substring | Exception |
|---|---|
| `Rate limit exceeded`, `Too many requests`, `Orders limit exceeded`, `Domain rate limit exceeded`, `Scheduled orders limit exceeded` | `RateLimited` |
| `Invalid key`, `Invalid signature`, `Invalid nonce`, `Permission denied`, `Temporary lockout` | `AuthError` |
| `Invalid price`, `Tick size check failed`, `Order minimum not met`, `Cost minimum not met` | `BadRequest` |

Verified live: an `AddOrder` call sized below the pair's cost minimum came back as
`EOrder:Cost minimum not met` and raised `BadRequest` as designed (see Gotchas).

Streams' `error` field is a single string (not an array) but follows the same
`<Category>:...` convention, so `transport/ws.py` reuses `raise_error` by wrapping it in
a one-element list.

HTTP-status-level failures (rare -- mostly edge/proxy issues, since Kraken answers 200
for logical errors) map the same as every other Typed client: 401/403 -> `AuthError`,
429 -> `RateLimited`, other 4xx -> `BadRequest`, other non-2xx -> `ApiError`.

## WebSocket

`typed_core.ws.StreamsRpc` is the primitive: Kraken correlates *every* reply
(subscribe/unsubscribe acks included) by the same client-supplied `req_id`, so
`request_subscription`/`request_unsubscription` are implemented as plain `rpc_request`
calls with `method: "subscribe"`/`"unsubscribe"` -- no separate ack-matching logic
needed, unlike a venue with no `req_id` (which would need `SerialReplies` instead).

One connection class, `KrakenSocketClient` (`core/transport/ws.py`), backs both the
public and private sockets; both `StreamClient.subscribe` (channel subscriptions) and
`WsRpcClient.request` (trading methods) are implemented on it, since Kraken's private
connection genuinely serves both. `endpoint/stream.py` drops the generic template's
`authed_subscribe` -- Kraken's private channels take no separate authed call, just a
`token` merged into the same `subscribe` params, so which connection you're on (not a
per-call flag) is what determines auth.

## Gotchas / Gaps (for review)

- **New endpoint-base file** (`core/endpoint/ws_rpc.py`): the generic template only
  anticipates `endpoint/rpc.py` (HTTP) and `endpoint/stream.py` (subscribe). Kraken's WS
  trading RPCs (`add_order` et al.) don't fit either shape cleanly -- they're
  request/reply like HTTP's `RpcEndpoint`, but `method`+`params` rather than
  `path`+`params`/`data`, and always-private with no public counterpart. Added a third,
  minimal `WsRpcEndpoint`/`WsRpcClient` pair rather than force-fitting. Worth a second
  look: is this the right cut, or should trading calls live as methods directly on
  `KrakenSocketClient`'s owning endpoint instead of a separate base class?
- **`endpoint/stream.py` lost `authed_subscribe`**: adapted away since Kraken's
  auth-requiredness lives at the connection level, not the call level (see WebSocket
  section above). Flagging in case a future non-Spot surface needs the split back.
- **`data` is an array, not a flattened item**: `ticker`/`balances` endpoint methods
  return the whole `{channel, type, data: [...]}` message, not a per-item stream. This
  was the one thing smoke testing actually caught -- an earlier version validated
  against the flattened item type directly and every live message failed pydantic
  validation (`data` present, expected fields missing) until the wrapper type was
  fixed. Worth checking this reads naturally at the call site once more subsections are
  filled in -- `async for msg in stream: ... msg['data']` is a bit more ceremony than a
  flattened item would be.
- **AddOrder's parameter surface is intentionally partial.** Kraken's real `AddOrder` /
  WS `add_order` bodies are large (`triggers`, `conditional`, `oflags`, `timeinforce`,
  `leverage`, `stptype`, ...); the PoC only wires `pair`/`type`/`ordertype`/`volume`/
  `price`/`validate` (REST) and `symbol`/`side`/`order_type`/`order_qty`/`limit_price`/
  `validate` (WS) to prove the shape end-to-end. Full parameter coverage is codegen's
  job, not this pass's.
- **`DepositMethod`'s hyphenated fields are dropped**, not aliased: `gen-address`,
  `fee-percentage`, `address-setup-fee` aren't valid Python identifiers and pydantic
  `TypedDict` field aliasing wasn't worth reaching for in a PoC type. They still arrive
  in the raw dict (the shared `TypedDict` base tolerates extra fields), just untyped.
  Real codegen output should alias them properly.
- **`EarnStrategiesResult` is minimal** (`items` only, each item typed loosely against
  `id`/`asset`/`can_allocate`/`can_deallocate`); the real response nests `lock_type`,
  `auto_compound`, `apr_estimate`, `allocation_fee` as tagged unions, which is real
  codegen work, not hand-written PoC work.
- **Nothing here was placed as a real order.** Every `AddOrder`/`add_order` smoke test
  used `validate=True` (Kraken validates without ever reaching the matching engine).
  The account's real balance (~10 USDC) was read live via `Balance`/`balances`, but no
  live trading was exercised, per the "tiny" trade policy warranting explicit
  confirmation before spending it for real.
- **L3 order book** (`ws-l3.kraken.com/v2`, and its HTTP fallback `/private/Level3`) is
  entirely unaddressed -- out of scope per discovery, called out again here since it's
  the one *documented* Spot feature this pass skips outright rather than just leaving a
  parameter gap.

## Smoke Test

All eight PoC endpoints were exercised live against the real account
(`clients/kraken/.env`, `KRAKEN_API_KEY`/`KRAKEN_PRIVATE_KEY`):

- HTTP: `spot.market_data.ticker`, `spot.account.balance`, `spot.funding.deposit_methods`,
  `spot.earn.strategies`, `spot.trading.add_order` (`validate=True`)
- WebSocket: `streams.market_data.ticker` (subscribe + one message), `streams.private.balances`
  (subscribe + one message, token auth end-to-end), `trading_ws.add_order`
  (`validate=True`)

Every call round-tripped through real HMAC signing / token exchange, envelope
unwrapping, and pydantic validation successfully; the one live error encountered
(`EOrder:Cost minimum not met`, from an under-sized test order) mapped to `BadRequest`
as designed.
