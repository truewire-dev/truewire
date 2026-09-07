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

`core/__init__.py`: `Transport` (base URL, `HttpClient`, optional token, `headers()`,
`send()`), `ClientBase` (the root client's `new(...)`, named after `[python].name`, and
the context manager) and `Endpoint` (the base every generated class subclasses;
`request()` sends and validates). `core/types.py` maps the spec's timestamp formats to
real `datetime`/`date` types. Generated code never changes when you change the core.

## Steps

1. **Headers on every call.** Media type, API version and User-Agent go on every request,
   public or not; only auth is conditional. The template's `headers()` returns `{}` for
   public calls: change it. GitHub, for instance, wants `Accept:
   application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28` and a `User-Agent`.
   The inventory's `## Transport` section lists them.
2. **Auth.** Extend `Transport` with what the API needs: an API key header, HMAC signing
   (timestamp, nonce, body), a query-string key, a session token. Read credentials from
   `new(...)` keywords, which the caller fills from environment variables; never read
   `os.environ` inside the core and never hardcode a value. Every transport-injected
   request field (signature, nonce, timestamp) goes into the affected endpoints'
   `redacted`; when the transport injects only headers, there is nothing to declare.
3. **Errors, mapped.** Raise `ApiError` with the API's own message; where the API
   distinguishes them, map 401/403 to `AuthError`, 429 (and rate-limit 403s) to
   `RateLimited`, 400/422 to `BadRequest`, all from `truewire_core.exceptions`
   (standards S9). Never swallow an error into `None`.
4. **Envelope.** If the API wraps responses (`{code, msg, data}`, JSON-RPC `{result,
   error}`), unwrap in `send()` or `request()` and declare `envelope.payload` on the
   endpoints. The response schema still describes the whole wire frame; the path selects
   the value the generated method returns (authoring rule 6, ADR 0010).
5. **`meta`.** The `Meta` TypedDict in the core and `[cores.<name>].meta` in
   `truewire.toml` agree on the per-endpoint facts the core reads (`public`, `signed`, a
   scope). `truewire check` validates every endpoint's `meta` against that schema.
6. **WebSocket.** For `stream` or `ws` endpoints, add a socket client in the core
   (`truewire_core.ws` has the primitives) and route it through `[python.cores]` children
   in `truewire.toml`; `examples/kraken/src/kraken/core` in the toolchain repository is a
   complete reference.
7. **Pyright config, now.** `truewire generate python` runs pyright when the project has
   `pyrightconfig.json`. Write it before generating, with `test/` created (empty is fine)
   so it does not warn:
   ```json
   {"include": ["src", "test"], "extraPaths": ["src"],
    "venvPath": ".", "venv": ".venv", "typeCheckingMode": "standard"}
   ```
   `venvPath`/`venv` point at whichever venv has `truewire-core` installed; inside the
   toolchain repository that is `"venvPath": "../..", "venv": ".venv"`.
8. **Prove it.** `truewire generate python`, then `truewire capture <group.name>
   --request '{...}'` on the simplest public endpoint. `--new base_url=...` only when
   overriding the default `init --base-url` baked into `new()`; `--new api_key=$KEY` when
   the endpoint needs one. The recorded pair must pass `check`.

## Done when

- One `truewire capture` succeeds and `truewire check` accepts the pair.
- `grep -rn "os.environ" src/<pkg>/core` finds nothing, and no literal key or token.
- Every injected request field is in `redacted` on the endpoints that carry it.

## Do not

- Do not put API-specific logic into generated files; they are overwritten.
- Do not swallow errors into `None`; raise `ApiError` (or its subclasses) with the wire
  message.
