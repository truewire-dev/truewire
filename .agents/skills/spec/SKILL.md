---
name: truewire-spec
description: Write or extend a Truewire spec (endpoint.json per endpoint, routers, shared schemas) from an endpoint inventory and the API docs, until `truewire check` passes. Use after `truewire-discover`, or when adding endpoints to an existing project.
---

# Spec: from the inventory to a tree that checks

## Goal

`spec/endpoints/**/endpoint.json` for every row of `spec/inventory.md`, `router.json` for
every group, shared shapes in `spec/schemas.json`, and `truewire check` reporting zero
errors and zero violations. Read `docs/spec/authoring.md` once; for a plain JSON REST API
the rules that matter are 0, 1, 2, 3, 5, 7, 8, 14 and 16, and the rest are for wire
formats and windows you may never meet.

## The shape of one endpoint

Every key an `endpoint.json` can carry, so you never have to read the loader:

```jsonc
{
  "docs": "https://docs.example.com/reference/pulls#list",
  "meta": {"public": true},                 // what the core reads; schema in truewire.toml [cores.<name>].meta
  "spec": {
    "kind": "rpc", "transports": ["http"], "path": "/repos/{owner}/{repo}/pulls", "method": "GET",
    "description": "List pull requests, newest first.",
    "request": {"title": "ListPullsRequest", "type": "object", "required": ["owner", "repo"],
      "properties": {
        "owner": {"type": "string", "description": "Account owner."},
        "repo": {"type": "string", "description": "Repository name."},
        "state": {"type": "string", "enum": ["open", "closed", "all"], "default": "open", "description": "Which pulls."},
        "per_page": {"type": "integer", "default": 30, "minimum": 1, "maximum": 100, "description": "Rows per page."},
        "page": {"type": "integer", "default": 1, "minimum": 1, "description": "Page index, from 1."}}},
    "response": {"title": "Pulls", "type": "array", "description": "One page.", "items": {"$ref": "PullRequest", "description": "One pull request."}}
  },
  "pagination": {"strategy": "page", "index": {"parameter": "page", "start": 1},
                 "size": {"parameter": "per_page"}, "done": {"kind": "short_page"}},
  "envelope": {"payload": "data"},          // only when the core unwraps a wrapper the API sends around `Pulls` ({"data": [...], ...}):
                                            // `response` then describes that whole frame and this path selects what the method returns (rule 6). Omit otherwise
  "redacted": ["signature", "nonce"],       // request keys the transport injects; omit when none
  "unverified": {"reason": "requires_state", "detail": "needs an open pull request; none exist"},
  "notes": ["`state` enum from the docs page above.", "Docs were unreachable; schema from prior knowledge, confirmed by the page1 recording."]
}
```

`notes` is a list of strings. `unverified.reason` is one of `missing_credentials`,
`program_enrollment`, `unsafe`, `requires_state`, `runtime_error`, `not_captured`. An
array response needs no title on the array's items beyond the `$ref` or an inline titled
object. Response fields you do not declare pass through unchecked: declare what a caller
reads, and say in `notes` that the schema is a subset.

## Steps

1. **Start the project.** `truewire init <name> --base-url <url>` if there is no
   `truewire.toml`. If the inventory chose to import an OpenAPI document: `truewire import
   openapi <doc>`; then treat the imported tree as a draft, not a result (the importer
   declares `unverified: not_captured` where the document had no example). To start from a
   registry spec instead: `truewire import registry <name>`.
2. **Routers.** `truewire init` wrote `spec/endpoints/router.json` with `"core": "root"`.
   Each group directory gets its own `router.json` with `description`, `upstream` and
   `"core": "default"` (the core `init` declared) unless you added a core in
   `truewire.toml`. A directory is a group or a leaf, never both.
3. **Directory is the API.** `spec/endpoints/<group>/<name>/endpoint.json` becomes
   `client.<group>.<name>(...)`, the capture path `<group>.<name>`, and the test id. Choose
   names you want to type.
4. **One endpoint at a time**, following the shape above. Wire formats when the value is
   not what its JSON type says: `decimal-string`, `integer-string`, `boolean-string`,
   `epoch-seconds`, `epoch-millis`, `epoch-micros`, `epoch-nanos`, `date-time`, `date`.
   Unions and nullables are `anyOf`. `enum` only for documented closed sets. Every object
   titled, every property described.
5. **Pagination, declared.** For `page`/`per_page` APIs with no total in the body (GitHub
   and most REST APIs): `short_page` ends the walk on the first page shorter than
   `per_page`; give `per_page` its documented `default` so a caller who omits it still
   gets a walk that stops on a short page instead of only on an empty one. `empty` is the
   alternative when the API pads pages. `token`, `offset`, `window`, `seek` are in rule 8.
6. **Share what repeats.** A shape used by two or more endpoints goes into
   `spec/schemas.json` and is referenced as `{"$ref": "Name"}`.
7. **Run the gate after each group.** `truewire check` (or `truewire check --path
   spec/endpoints/<group>`). Fix every error and every violation; a warning is a decision
   to record in `notes`, not to ignore.

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
- Do not write `upstream.md` files unless the project already uses them; they are optional.
