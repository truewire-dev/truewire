---
name: truewire-spec
description: Write or extend a Truewire spec (endpoint.json per endpoint, routers, shared schemas) from an endpoint inventory and the API docs, until `truewire check` passes. Use after `truewire-discover`, or when adding endpoints to an existing project.
---

# Spec: from the inventory to a tree that checks

## Goal

`spec/endpoints/**/endpoint.json` for every row of `spec/inventory.md`, `router.json` for
every group, shared shapes in `spec/schemas.json`, and `truewire check` reporting zero
errors and zero violations. Read `docs/spec/authoring.md` once before starting; every rule
there has a reason and `check` enforces most of them.

## Steps

1. **Start the project.** `truewire init <name> --base-url <url>` if there is no
   `truewire.toml`. If the inventory found an OpenAPI document: `truewire import openapi
   <doc>`; then treat the imported tree as a draft, not a result. The importer declares
   `unverified: not_captured` on every endpoint the document gave no example for.
2. **Routers first.** `spec/endpoints/router.json` names the root core; each group
   directory gets a `router.json` with `description`, `upstream` and `core` (the core its
   endpoints share, usually `default`). A directory is a group or a leaf, never both.
3. **One endpoint at a time.** For each inventory row write `endpoint.json`:
   - `docs`: the row's URL. `meta`: what the core needs (`{"public": true}` or the
     project's own keys, declared in `truewire.toml`).
   - `spec.kind` `rpc` (request/reply, over `http`, `ws` or both) or `stream`; `path`
     with `{name}` placeholders for path parameters, `method` for HTTP.
   - `request`: a titled object; one property per parameter with a `description`; path
     placeholders and required parameters in `required`.
   - `response`: exactly what the core hands back. If the core unwraps an envelope, declare
     `envelope` and describe the unwrapped value. Title every object, describe every
     property, use `anyOf` for unions and nullables, `enum` only for documented closed sets.
   - Wire formats: `decimal-string`, `integer-string`, `boolean-string`, `epoch-seconds`,
     `epoch-millis`, `epoch-micros`, `epoch-nanos`, `date-time`, `date`. The inventory's
     notes say which; a recording confirms.
   - `pagination`: a declared block (`page`, `token`, `offset`, `window`, `seek`) naming
     real request parameters and how the walk ends. Never inferred from parameter names.
   - `redacted`: request keys the transport injects (signatures, nonces) so the mock ignores
     them.
   - `unverified`: reason and detail, for every row the inventory marked unrecordable.
   - `notes`: every judgement call, with its source.
4. **Share what repeats.** A shape used by two or more endpoints goes into
   `spec/schemas.json` and is referenced as `{"$ref": "Name"}`.
5. **Run the gate after each group.** `truewire check` (or `truewire check --path
   spec/endpoints/<group>` for one group). Fix every error and every violation; a warning
   is a decision to record in `notes`, not to ignore.

## Done when

- `truewire check` exits 0 with zero errors and zero violations for the whole project.
- Every inventory row has an endpoint directory, or a line in `spec/inventory.md` saying
  why it is excluded.
- No description is empty, no enum is invented, no default is guessed.

## Do not

- Do not copy an OpenAPI schema's `additionalProperties: false` blindly; the recording
  decides what the API actually sends.
- Do not describe error responses; errors belong to the core.
- Do not put a value you have not seen in the docs or on the wire into an `enum`.
