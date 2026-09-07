---
name: truewire-discover
description: Turn an API's documentation into an endpoint inventory a Truewire spec can be written from. Use when starting a new Truewire project from a docs URL, or when an existing project needs to know what upstream added.
---

# Discover: from a docs URL to an inventory

## Goal

A file, `spec/inventory.md`, listing every operation the API exposes that the project will
cover, with enough facts per row to write `endpoint.json` without reopening the docs page.
A human reads this file once and edits it; every later skill trusts it.

## Steps

1. **Find the reference, not the guide.** Locate the page that lists operations (often
   "API reference", "Endpoints", "Methods"). Note the base URL(s), the API version header or
   path segment, the authentication scheme, and the rate-limit rules. Put these at the top
   of the inventory under `## Transport`.
2. **Check for a machine-readable document.** If an OpenAPI 3.x document exists, download it
   into the project root and note its URL; `truewire import openapi <doc>` will seed the
   spec in the next skill, and this inventory then records what the document gets wrong.
   AsyncAPI, GraphQL or gRPC descriptors: note them; only gRPC unary has a path today.
3. **List operations.** One table row per operation, grouped the way the docs group them.
   Columns: group, name (what the method will be called, lowercase, short), transport
   (`http`, `ws`, `both`), method and path (or channel or RPC method name), auth (`public`,
   `key`, `signed`), pagination as documented, docs URL.
   For WebSocket APIs list streams separately: channel name, subscribe message shape,
   whether the server pushes on connect, unsubscribe shape.
4. **Mark what cannot be recorded.** Column `record`: `yes`, or the `unverified` reason that
   will apply (`missing_credentials`, `program_enrollment`, `unsafe`, `requires_state`).
   Anything that moves money or mutates state under a no-writes rule is `unsafe`.
5. **Note the traps.** A `## Notes` section with the facts a schema cannot express: the
   envelope shape (`{code, msg, data}`?), how errors arrive, which fields are strings on the
   wire (`"1.23"`, `"true"`), timestamp units, ids that are numbers in one place and strings
   in another. Every later skill reads this section first.

## Done when

- `spec/inventory.md` exists with `## Transport`, the operation table, streams (if any) and
  `## Notes`.
- Every row has a docs URL that resolves.
- A human has read it once. Do not start `spec` on an unreviewed inventory.

## Do not

- Do not write `endpoint.json` here.
- Do not guess a field's type from its name. If the docs do not say, write `unknown` in the
  notes; the recording will say.
