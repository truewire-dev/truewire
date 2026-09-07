---
name: truewire-core
description: Adapt a Truewire project's hand-written core (transport, auth, envelope, errors) to a specific API until one `truewire capture` succeeds against the live API. Use after `truewire-spec` and before recording examples.
---

# Core: the one place that knows how to reach the API

## Goal

`src/<pkg>/core/` handles the API's base URL, headers, signing, envelope unwrapping and
error mapping, so that every generated endpoint can call `self.request(...)` and get a
validated value back. Proof: one `truewire capture` of a public endpoint succeeds and its
recording passes `truewire check`.

## What `truewire init` gave you

A `core/__init__.py` with `Transport` (base URL, `HttpClient`, optional bearer token,
`headers()` and `send()`), `ClientBase` (the root client's `new(...)` and context manager)
and `Endpoint` (the base every generated class subclasses; `request()` sends and validates).
`core/types.py` maps the spec's timestamp formats to real `datetime`/`date` types. The
generated code never changes when you change the core.

## Steps

1. **Headers and versioning.** Put the API's media type, version header and a User-Agent in
   `Transport.headers()`. GitHub needs three headers; many APIs need none.
2. **Auth.** Extend `Transport` with what the API needs: an API key header, HMAC signing of
   the request (timestamp, nonce, body), a query-string key, a session token. Read
   credentials from `new(...)` keywords, which the caller fills from environment variables;
   never read `os.environ` inside the core and never hardcode a value.
   Every transport-injected request field (signature, nonce, timestamp) must be listed in
   the affected endpoints' `redacted` so the mock server ignores it.
3. **Envelope and errors.** If the API wraps responses (`{code, msg, data}`, JSON-RPC
   `{result, error}`), unwrap in `send()` or `request()` and raise `ApiError` with the
   API's own code and message on failure. Declare `envelope` on the endpoints so the spec
   describes what the core returns (authoring rule 6).
4. **`meta`.** The `Meta` TypedDict in the core and `[cores.<name>].meta` in
   `truewire.toml` agree on the per-endpoint facts the core reads (`public`, `signed`,
   a permission scope). `truewire check` validates every endpoint's `meta` against that
   schema.
5. **WebSocket.** For `stream` or `ws` endpoints, add a socket client in the core
   (`truewire_core.ws` has the primitives) and route it through `[python.cores]` children
   in `truewire.toml`; `examples/kraken/src/kraken/core` is a complete reference.
6. **Prove it.** `truewire generate python` (the generator imports the core), then
   `truewire capture <group.name> --request '{...}' --new base_url=<url> [--new
   api_key=$KEY]` on the simplest public endpoint. The recorded pair must pass `check`.

## Done when

- One `truewire capture` succeeds and `truewire check` accepts the pair.
- `grep -rn "os.environ\|sk_\|secret" src/<pkg>/core` finds nothing that is a value.
- Every injected request field is in `redacted` on the endpoints that carry it.

## Do not

- Do not put API-specific logic into generated files; they are overwritten.
- Do not swallow errors into `None`; raise `ApiError` with the wire message.
