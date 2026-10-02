# Core templates

`truewire init <name> --template <template>` writes the hand-written half of a project: `src/<pkg>/core/` and the `[cores.*]` and `[python.cores.*]` tables of `truewire.toml` that wire it to the code `truewire generate python` emits. A template is a working core for one common API shape, not a stub: a fresh project built on any of them, once it has one endpoint spec, passes `truewire check` and `truewire generate python` (both refuse an empty `spec/endpoints`), and pyright accepts the core as written. Adapt the core to the API; the generated code never changes when you do, because the generator reads nothing from it (ADR 0011) and calls it only through the protocols in `truewire_core.contract`.

| Template | Transport | Auth | Envelope | Errors |
| --- | --- | --- | --- | --- |
| `bearer` (default) | HTTP | `Authorization: Bearer <api_key>` on non-public calls | none | `ApiError` on any non-2xx |
| `hmac` | HTTP | API key plus HMAC-SHA256 over timestamp, method, path and body, in headers | none | `AuthError`, `RateLimited`, `BadRequest`, `ApiError` by status |
| `jsonrpc` | HTTP, one URL, `POST` only | `Authorization: Bearer <api_key>` on non-public calls | `{jsonrpc, id, method, params}` out, `result` back, `error` raised | by JSON-RPC code, then by status |
| `ws` | HTTP as `bearer`, plus one WebSocket connection | as `bearer` | none | as `bearer`; `ApiError` on a subscribe error frame |

Every template shares one shape, described in the [core skill](../packages/truewire/src/truewire/resources/agents/skills/core/SKILL.md): `Transport` (how a request reaches the wire), `ClientBase` (the root class the generated `main.py` subclasses, with `new(...)` and the context manager) and `Endpoint` (the base every generated endpoint class subclasses, whose `request()` sends and validates). `meta.py` is generated from `[cores.default].meta`, which every template declares as `{ public: boolean }`: an endpoint whose `meta` is `{"public": true}` is sent without credentials. `truewire import openapi` sets it for every operation with no `security` requirement.

Every template's `new(...)` takes `proxy=`, a proxy URL for HTTP and, in `ws`, the socket too; left `None`, the runtime reads `HTTPS_PROXY` (packages clause P18). Keep the keyword when you rewrite `new(...)`: a caller in a sandbox cannot always set the environment.

`ClientBase` also declares `RATE = None` and `RETRY = False`, and `new(...)` builds its `HttpClient(proxy=proxy, rate=cls.RATE, retry=cls.RETRY)`. The generated root overrides both when `[policy]` sets `rate` or `retry` (packages clause P19), so the client paces and retries as the project declares. A caller who wants another pace subclasses the root and sets `RATE`. A core written before this ignores `[policy]` until its `new(...)` passes them on. TypeScript and Rust cores do the same with the root's `static readonly RATE`/`RETRY` (`new HttpClient({ rate: Root.RATE, retry: Root.RETRY })`) and associated consts (`HttpClient::default().with_rate(Root::RATE).with_retry(Root::RETRY)`); `examples/github` and `examples/kraken` show both, and an `http` option passed in replaces them.

## `bearer`

The plain HTTP core. `Transport.headers()` returns `{}` for a public call and a bearer header otherwise; `Transport.send()` fills `{name}` placeholders in the path from the request, sends the rest as the query string (or a JSON body with `Content-Type: application/json` for `POST`/`PUT`/`PATCH`), and raises `ApiError` on a non-2xx status. `Endpoint.request()` dumps the request through the generated request type in JSON mode, so a datetime is the same ISO string on the query as in a body, and validates the raw body against the generated response type.

What to change:

- Headers every call needs (media type, API version, `User-Agent`) go into `headers()` unconditionally; only auth is conditional.
- An API that wraps every response unwraps it in `send()` or `request()`, and its endpoints declare `envelope.payload` (authoring rule 6).
- Map statuses the API distinguishes onto `AuthError`, `RateLimited` and `BadRequest` from `truewire_core.exceptions`; the `hmac` template's `raise_for_status` is the shape to copy.

## `hmac`

The `bearer` core with request signing. Three pure functions carry the recipe:

- `signature_message(timestamp, method, path, body)` returns the bytes the signature covers: `timestamp + METHOD + path + body`, where `path` includes the query string.
- `sign(secret, message)` returns the hex HMAC-SHA256.
- `Transport.headers(method, path, body, public=...)` puts the key, the timestamp and the signature into `X-API-Key`, `X-Timestamp` and `X-Signature`, and raises `AuthError` for a non-public call on a client built without credentials.

`Transport.send()` builds the query string itself so the signed path and the sent path are the same bytes. `Transport.timestamp` is the clock, a field a test replaces to sign a known value. `ClientBase.new(base_url=..., api_key=..., api_secret=...)` takes the credentials; the caller reads them from environment variables, the core never does.

What to change:

- The order, separators and casing in `signature_message`; the digest or the encoding (base64, say) in `sign`; the header names at the top of the module.
- Where the injected values travel. The template puts them in headers, which a recorded example never holds, so nothing is declared in the spec. An API that wants the timestamp, a nonce or the signature as a query or body field gets it added in the per-attempt auth hook, and every endpoint that carries it lists the field under `redacted` in its `endpoint.json`: a recorded example then never pins a value that changes on every call, and `truewire mock` ignores the field when matching a request. `truewire capture` records the parameters the call was made with, never the wire body, so an injected field is absent from the example either way; `redacted` is what tells the mock to ignore it on the wire. A field nested out of a flat name's reach, like a login frame's `args[0].sign`, is named by path under `match.ignore` instead (ADR 0018).

## `jsonrpc`

JSON-RPC 2.0 over HTTP. Every call is one `POST` to `base_url` carrying `{"jsonrpc": "2.0", "id": <counter>, "method": <path>, "params": <request>}`. In the spec, an endpoint's `path` is the JSON-RPC method name with no leading slash (`getBalance`), its `method` is `POST`, its response schema describes the whole reply frame, and it declares `envelope: {"payload": "result"}` so the generated method returns `result` (authoring rule 6). Adding `"correlate": "id"` to the envelope makes `truewire mock` echo the request id into the served frame, which the core checks.

The core:

- `build_request(id, method, params)` builds the frame; `params` is the request's named parameters as a dict, or absent for a call without any.
- `unwrap(frame, id=..., method=...)` returns `result`, raises `ApiError` on a frame that is not an object, answers another id, or carries neither `result` nor `error`, and hands an `error` member to `raise_error`.
- `raise_error(method, error)` maps `error.code` through three tables: `INVALID_REQUEST_CODES` (JSON-RPC's own parse, invalid-request, method-not-found and invalid-params codes) to `BadRequest`, `AUTH_CODES` to `AuthError`, `RATE_LIMIT_CODES` to `RateLimited`, anything else to `ApiError`. The message carries the method, the API's message, the code and the first 200 characters of `data`.
- `Transport.call(method, params, public=...)` posts, maps a non-2xx status through `raise_for_status`, decodes the frame and unwraps it. `Endpoint.request()` serializes the generated request through its validator (so declared formats apply), calls, and validates `result` against the generated response type. It accepts the HTTP `method` because the `HttpEndpoint` contract passes one, and never reads it.

What to change:

- `AUTH_CODES` and `RATE_LIMIT_CODES`: put the API's own codes there. The defaults are placeholders, one uncommon code each.
- Positional parameters: return a list from `build_request`, in the order the endpoint's `request` schema declares its properties. Declare that packing on the endpoint as `envelope.positional`, one entry per wire slot: a property name, `{"spread": "transactions"}` (an array property's elements, one slot each) or `{"fold": ["pageKey", "maxCount"]}` (one object of those present). Trailing absent slots are dropped. `truewire mock` builds the expected `params` from the recorded request that way and compares element by element; without `positional` it expects `[request]`.
- An error shape that is not `{code, message, data}`: change `raise_error`.
- Batch requests, a method that lives on its own URL, an API-key query parameter: `Transport.call` and `Transport.headers`.

## `ws`

The `bearer` HTTP core plus a WebSocket client, for an API with both. Two files:

- `core/__init__.py` holds `Transport`, `Endpoint`, a `ClientBase` with two fields, `client` (HTTP) and `socket`, and `StreamEndpoint`, the base for every generated `stream` endpoint. `StreamEndpoint.subscribe(channel, parameters, ...)` serializes the parameters through their validator, fills `{name}` placeholders in the channel from them, and hands the rest to the socket as the subscribe frame's fields.
- `core/ws.py` holds `Connection`, a `truewire_core.ws.Streams` subclass with the `SerialReplies` mixin, and `SocketClient`, which owns one connection and the validation default. `ClientBase.new(base_url=..., ws_url=...)` builds both; the socket opens on the first subscription and closes with the client.

The wiring in `truewire.toml`:

```toml
[cores.streams]                  # stream endpoints read nothing per call: no meta schema

[python.cores.root]
base = "feed.core:ClientBase"
children = { streams = "socket" }   # the streams composite is built on the root's socket field

[python.cores.streams]
base = "feed.core:StreamEndpoint"
```

`init` also writes `spec/endpoints/streams/router.json` naming the `streams` core, so every `stream` endpoint placed under `spec/endpoints/streams/` subclasses `StreamEndpoint` and reaches the socket, while every other group keeps the HTTP transport. `--ws-url` sets the socket URL baked into `new()`; it defaults to `--base-url` with a `ws`/`wss` scheme.

`Connection` speaks the subscribe dialect `truewire mock` serves for an endpoint that records only `parameters`: it sends `{"type": "subscribe", "channel": ..., ...params}` and `{"type": "unsubscribe", ...}`, treats a frame whose `type` is `subscribed`, `unsubscribed`, `ack` or `error` as the reply to the most recent request (acks carry no correlation id, so `SerialReplies` pairs them by arrival order), and routes every other frame carrying `channel` to the subscription named by `subscription_key(channel, frame)`: the channel, plus `:<id>` when the subscription was keyed by an `id` parameter. Each pushed frame is validated against the generated payload type unless `validate=False`.

What to change:

- The two frames in `request_subscription` and `request_unsubscription`, `ACK_TYPES`, and the error check, to the API's own dialect. Stream endpoints then declare `envelope.verb` (and `envelope.channel` when the frame does not carry a top-level `channel`) so the mock recognizes the frames.
- `subscription_key`: the one place that decides which local subscription a pushed frame belongs to.
- `Connection.ping`: a protocol-level ping every 30 seconds by default; an API with its own heartbeat frame sends it here.
- An API that correlates acks by a request id, or that carries request/reply methods over the same socket, is a `truewire_core.ws.StreamsRpc`; `examples/kraken/src/kraken/core` is a complete reference for that shape, including token authentication and two connections behind one root.
- A private connection: build a second `SocketClient` in `ClientBase.new`, add a field for it, and route a second group to it with another `children` entry.

## Proving a core

The core skill's gate applies to every template: `truewire generate python` (with a `pyrightconfig.json` in the project so it type-checks), then one `truewire capture` of the simplest public endpoint whose recording passes `truewire check`. The toolchain's own tests run every template through `init`, `check` and `generate` with pyright, call `bearer`, `hmac` and `jsonrpc` cores through `truewire mock`, check that `bearer`, `hmac` and `ws` send a datetime query parameter as ISO and a JSON body with its `Content-Type`, and drive the `ws` core through a subscribe, push and unsubscribe round trip (`packages/truewire/test/test_init_templates.py`).

A core that sends more than one request per call -- minting a token, refreshing an expired one, fetching a ticket, retrying after a 401 -- needs nothing special to capture: `truewire capture` records the exchange whose method and path (or JSON-RPC method name) the endpoint declares, names the ones it skipped, and refuses rather than record anything when none of them matches. A token endpoint's own response therefore never lands in another endpoint's example (authoring rule 6).

Signing must run after pacing and on every retry. The HMAC template passes an
`httpx.Auth` hook to `HttpClient.request(auth=...)`; HTTPX invokes it inside each attempt.
For a body nonce, the auth hook builds a new `httpx.Request` with the new body and matching
content length (as Kraken does). TypeScript offers `RequestOptions.prepare(request)`,
which returns the signed request; Rust offers `request_prepared(method, url, prepare)`, whose callback builds fresh
`RequestOptions` per attempt and returns them as a `Result`. The existing Rust
`RequestOptions` fields are unchanged. Do not pre-sign before calling the runtime:
queued requests and retries would otherwise reuse stale timestamps and nonces.

Connection failures during a followed redirect cannot prove the origin did nothing.
Python therefore suppresses connection retries when redirects are enabled (including
an underlying HTTPX client's default). Fetch and reqwest follow redirects by default,
so TypeScript and Rust only retry connection failures for safe HTTP methods (GET,
HEAD, OPTIONS, TRACE); TypeScript also allows them when a prepared request disables
redirects. A POST's direct 429/503 response is still retryable. Responses identified as
coming through a redirect are returned without restarting the original request.
