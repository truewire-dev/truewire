---
name: truewire-discover
description: Turn an API's documentation into an endpoint inventory a Truewire spec can be written from. Use when starting a new Truewire project from a docs URL, or when an existing project needs to know what upstream added.
---

# Discover: from a docs URL to an inventory

## Goal

A file, `spec/inventory.md`, listing every operation the API exposes that the project will
cover, with enough facts per row to write `endpoint.json` without reopening the docs page.
A human reads this file once and edits it; every later skill trusts it. When no human is
in the loop, say so at the top of the file and continue; the CLI gates decide the rest.

## Steps

1. **Find the reference, not the guide.** Locate the page that lists operations (often
   "API reference", "Endpoints", "Methods"). Note the base URL(s), the API version header or
   path segment, the required headers (media type, User-Agent), the authentication scheme,
   the shape of the API's own tokens (a prefix like `ghp_`, a length) so recordings can be
   grepped for leaks later, and the rate-limit rules. Put these at the top of the inventory
   under `## Transport`.
   If the docs host is unreachable, write the inventory from what you know, mark every
   such row `source: memory` and say so at the top; the recordings will confirm or
   correct it, and `notes` on each endpoint must say the docs were not read.
2. **Check for a machine-readable document.** If an OpenAPI 3.x document exists and the
   whole surface is in scope, download it into the project root and note its URL;
   `truewire import openapi <doc>` seeds the spec in the next skill, and this inventory
   then records what the document gets wrong. If only a subset is in scope, or the
   document is huge (GitHub's is tens of megabytes), do not import: write the endpoints by
   hand. AsyncAPI, GraphQL or gRPC descriptors: note them; only gRPC unary has a path today.
3. **List operations.** One table row per operation. The `group` and `name` columns are the
   directory path `spec` will create, `spec/endpoints/<group>/<name>/`, and that path is
   the client's attribute tree: `pulls/list/` becomes `client.pulls.list(...)` and the
   capture path `pulls.list`. A group with one endpoint is fine.
   Columns: group, name, transport (`http`, `ws`, `both`), method and path (or channel or
   RPC method name), auth (`public`, `key`, `signed`), pagination, docs URL.
   For pagination write which *body-level* fact ends the walk: a total, a cursor field, a
   short page, an empty page. An API whose "next" lives only in a `Link` header (GitHub)
   still ends by a short or empty page in the body; probe it with `per_page=2` on a small
   collection before declaring it.
   For WebSocket APIs list streams separately: channel name, subscribe message shape,
   whether the server pushes on connect, unsubscribe shape.
4. **Mark what cannot be recorded.** Column `record`: `yes`, or the `unverified` reason
   that will apply. The closed set is `missing_credentials`, `program_enrollment`,
   `unsafe`, `requires_state`, `runtime_error`, `not_captured` (defined in
   `truewire.spec.endpoint.Unverified`). Anything that moves money or mutates state under
   a no-writes rule is `unsafe`.
5. **Note the traps.** A `## Notes` section with the facts a schema cannot express: the
   envelope shape (`{code, msg, data}`?), how errors arrive and which statuses mean auth
   or rate limit, which fields are strings on the wire (`"1.23"`, `"true"`), timestamp
   units, ids that are numbers in one place and strings in another. Every later skill
   reads this section first.

## Done when

- `spec/inventory.md` exists with `## Transport`, the operation table, streams (if any) and
  `## Notes`.
- Every row has a docs URL, resolved if the docs host is reachable.
- A human has read it once, or the top of the file says nobody has.

## Do not

- Do not write `endpoint.json` here.
- Do not guess a field's type from its name. If the docs do not say, write `unknown` in the
  notes; the recording will say.
