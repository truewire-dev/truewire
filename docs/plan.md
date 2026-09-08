# The plan

The plan is what a backend renders. It is computed once from a project (the spec tree
under `spec/` plus `truewire.toml`) and holds every decision that used to be made inside
the Python generator while it was writing strings: which types a module defines and what
they are called, what the request looks like, what a method returns and whether that can
be `null`, and how a declared pagination is walked. It contains no rendered code and no
language name. The Python backend reads it; a TypeScript backend reads the same object.

```sh
truewire plan                    # one line per router and per endpoint
truewire plan --json             # the whole plan, camelCase keys, `null` fields omitted
```

```python
from truewire.plan.build import build_plan

plan = build_plan(Path('examples/github'))     # a frozen pydantic `PackagePlan`
plan.endpoint('issues.list').pagination.walker  # 'paginated'
```

The models live in `truewire.plan.model`, the type tree in `truewire.plan.types`, the
builder in `truewire.plan.build`. Every model is frozen; `PackagePlan.to_json()` is the
JSON `--json` prints.

## The JSON shape

```jsonc
{
  "name": "github",                       // [project].name
  "rootClass": "GitHub",                  // [python].name, else PascalCase of name
  "cores": {                              // one entry per symbolic core name
    "default": { "meta": { /* JSON Schema */ } },
    "streams": { "forward": ["market_client"], "params": { "network": { "type": "...", "required": true } },
                 "children": { "market_data": "market_client" } }
  },
  "schemas": {                            // shared types per scope
    "": { "Label": { "type": "record", ... } },        // spec/schemas.json
    "futures": { ... }                                   // spec/endpoints/futures/schemas.json
  },
  "routers": [
    { "path": [], "core": "root", "doc": { "description": "...", "upstream": "https://..." },
      "children": [ { "name": "repos", "kind": "router", "class": "Repos" } ] },
    { "path": ["repos"], "core": "default", "children": [ { "name": "get", "kind": "endpoint", "class": "Get" } ] }
  ],
  "endpoints": [ /* EndpointPlan, sorted by path */ ]
}
```

One endpoint:

```jsonc
{
  "path": ["issues", "list"],             // function path; the last segment is the method
  "kind": "rpc",                          // or "stream"
  "transports": ["http"],
  "wire": { "path": "/repos/{owner}/{repo}/issues", "method": "GET", "placeholders": ["owner", "repo"] },
  "core": "default",                      // nearest router.json's `core`
  "meta": { "public": true },
  "deprecated": false,
  "request": {
    "shape": "fields",                    // none | fields | union | array
    "type": "Request",                    // the `types` entry holding the whole request
    "fields": [                           // for `fields`: one per property, wire names
      { "wire": "owner", "required": true, "type": { "type": "scalar", "base": "string" },
        "description": "Account owner of the repository, case-insensitive." },
      { "wire": "per_page", "required": false, "type": { "type": "scalar", "base": "integer" }, "default": 30 }
    ],
    "needsCast": false                    // a bare Literal/Any alias, not a class
  },
  "response": {
    "wire": "Issues",                     // the wire body's type; a `wireTypes` entry when enveloped
    "payload": "Issues",                  // the returned type: a `types` entry or a shared one
    "selector": "",                       // envelope.payload
    "optional": false,                    // `X | null`
    "needsCast": false
  },
  "pagination": {
    "strategy": "page", "driver": "page", "driverRequired": false,
    "size": "per_page", "sizeDefault": 30, "start": 1,
    "done": { "kind": "short_page" }, "rows": null, "cursorFrom": null,
    "rowType": { "type": "ref", "id": "Issue" },
    "stateType": { "type": "scalar", "base": "integer" },
    "seedable": true,
    "walker": "paginated"                 // paginated | generator | none
  },
  "stream": null,                         // for kind: stream, see below
  "types": { "Request": { "type": "record", ... }, "Issue": { ... }, "Issues": { "type": "list", ... } },
  "wireTypes": {},                        // the frame's types, when `selector` is not empty
  "docs": { "description": "...", "url": "https://...", "notes": [] }
}
```

A stream endpoint's `request` is its parameters and `response.payload` its pushed
message; `stream` adds `channelParams` (parameters that are channel placeholders),
`connectOnly` (a connect-triggered push whose channel is its one parameter),
`directChannel` (the parameters are exactly the placeholders, so no parameters object is
built), `push`, `verb` and `replyPayload` as declared.

Types are the eight-node tree of `truewire.plan.types`: `scalar{base, format?}`, `ref{id}`,
`literal{values}`, `list{item}`, `tuple{items}`, `union{variants}`, `dict{key, value}`,
`record{id, fields}`. A `scalar` carries the wire base (`string`, `integer`, `number`,
`boolean`, `null`, `any`) and the spec `format` that narrows it (`decimal-string`,
`epoch-millis`, `date-time`, ...); which language type that becomes is the backend's
call. A `ref` names an entry of the same endpoint's `types` or of a shared scope.

### Pagination

`walker` is the decision the whole block leads to:

- `paginated`: the endpoint has rows (`rowType`) and a state the walker can seed
  (`seedable`): the backend exposes pages and a resumable state
  (`PaginatedResponse[row, state]` in Python).
- `generator`: a plain async iterator of responses, for a declaration the resumable shape
  does not cover (`offset`, `window`, `seek` with `overlap`, or rows the tree cannot name).
- `none`: the declaration cannot be walked at all (an `offset` walk ending on an item
  count with no rows to count and no page size to step by).

`stateType` is the driver parameter's type without its `null`; `seedable` is true unless
the strategy is `token`/`seek` and the cursor has neither a zero value (`''`, `0`) nor is
required on the single call. `sizeDefault` is the size property's own `default`.

## How the Python backend uses it

`truewire generate` builds the plan once and attaches it to the generator.
`rpc_endpoint` and `stream_endpoint` read four decisions off it that they used to
re-derive by parsing their own rendered output: the cursor's type for the walker's seed
(`stateType`), whether the walker can be seeded (`seedable`), whether the returned type is
nullable so every read on it is guarded (`response.optional`), and whether a request or
response type needs `cast(type, ...)` (`needsCast`). A generator built outside the CLI
(a test) plans the one endpoint it is asked for on demand. The stream-shape predicates
(direct channel, connect-only) and the driver-parameter rule are the plan's functions,
called from both sides.

## How a second backend consumes it

`truewire.codegen.typescript` is that backend (`docs/typescript.md`); it reads the plan as
described here and needed nothing added to it.

Walk `endpoints`. For each: define the entries of `types` (and `wireTypes` when present)
in its own language, rendering `scalar` by base and format; build the method's parameters
from `request.fields` (or one parameter of `request.type` for a `union`/`array`); return
`response.payload`; pass `wire`, `meta` and `core` to the hand-written core (ADR 0011);
render a walker per `pagination.walker` from `driver`, `size`, `done`, `rows`,
`cursorFrom`, `rowType` and `stateType`. Walk `routers` to compose classes: `children`
gives each child's attribute name, kind and class, `core` which declared base composes
it, and `cores[core]` how (`forward`, `params`, `children`). `schemas` gives every shared
scope's types, keyed by the directory that owns them. Identifiers Truewire invents
(`class`, `rootClass`) are PascalCase and language-neutral; identifiers the API invented
(`fields[].wire`, `driver`, `size`) are verbatim.

The tests in `packages/truewire/test/test_plan.py` pin the GitHub example's plan as
`test/fixtures/plans/github.json`, so a change to the shape is a visible diff.

## Not yet on the plan

- The Python walkers' bodies are still string emitters (`paged_method`,
  `paged_response_method` and their dispatch targets); they read the plan's decisions
  but render from the header they are handed. The row type the Python backend puts in a
  signature is still resolved by `paged_response_rows_type` over rendered definitions;
  the plan's `rowType` comes from the type tree, and the tests check the two agree on
  both examples.
- Python-side naming: which request fields are positional (`_flat_request_kwargs`),
  identifier sanitising (`safe_identifier`), and an endpoint class renamed to dodge an
  imported name (`Any`, `Literal`, ...) rather than a type in its module.
- gRPC endpoints and OpenAPI-shaped (not yet migrated) endpoints are not planned; the
  Python backend keeps its own path for both.
- `auth` is reserved (architecture review, item 4) and always `null`: no spec field
  declares it yet.
- **Integer width and signedness.** `scalar{base: "integer"}` carries no range and no
  narrower format, so a backend has to render every integer as its language's widest
  signed type. A schema's `minimum: 0`, a `format: int32`, or an id larger than a signed
  64-bit value cannot be expressed here, so Rust renders `i64` throughout. Python does not
  notice (its `int` is unbounded) and TypeScript has the same blind spot behind
  `Number.MAX_SAFE_INTEGER`. A width format, or `minimum`/`maximum` on the scalar node,
  would close it. Found while writing the Rust runtime.
- **Union discriminators.** `union{variants}` lists its variants in order and nothing
  else, so a backend must try them in order. TypeScript's codec keeps the inner path and
  message when none matches; Rust's `#[serde(untagged)]` reports only that nothing matched,
  at the union's own path. An OpenAPI `discriminator` is dropped by `truewire import
  openapi` and has no node here; with one, a backend could render a tagged union and give
  an exact error. Found while writing the Rust runtime.
