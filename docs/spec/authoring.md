# Spec Authoring

Rules for `spec/schemas.json` and every `spec/endpoints/**/endpoint.json`. `truewire check` enforces most of them; each rule says whether it does, and at what severity. An `error` fails the check. A `warning` is a heuristic that can be wrong, so it reports but never fails.

## Endpoint inventory

`spec/inventory.json` records the upstream documentation's endpoint list for the
[S1 coverage score](../shape/score.md). It sits beside `schemas.json`; endpoint specs
live under `spec/endpoints/`, with `router.json` and scoped `schemas.json` in their groups.
Discovery notes may remain in `spec/inventory.md`, but the score reads only the JSON file.

```json
{
  "source": "https://www.weather.gov/documentation/services-web-api",
  "approved": {"by": "Reviewer name", "date": "2026-10-01"},
  "endpoints": [
    {"method": "GET", "path": "/stations/{stationId}/observations", "endpoint": "stations.get_observations"},
    {"method": "GET", "path": "/radar/queues/{host}", "excluded": "operator-only, undocumented response"},
    {"method": "GET", "path": "/alerts"}
  ]
}
```

Use `"approved": null` until a person has confirmed the inventory is the full upstream
list; then record their name and review date (`YYYY-MM-DD`). Approval can precede a
complete spec. Each entry requires `method` and `path`, and may carry either `endpoint`
or `excluded`, but never both, with no other fields. An entry with neither is documented
but not yet specified; keep it in the inventory so it counts in the denominator.
An endpoint id is its directory path under
`spec/endpoints/`, joined with dots: `stations/get_observations/endpoint.json` becomes
`stations.get_observations`, regardless of any function override. An exclusion is a
nonblank, one-line reason. `source` is the upstream documentation URL.

Each `(method, path)` pair must be unique, regardless of whether its entry is specified,
excluded or unspecified. Method comparison is case-insensitive (`get` and `GET` are the
same); paths are compared exactly, including case. Duplicate errors name both the later
entry's index and the first matching index. Different operations may map to the same
spec endpoint id.

`truewire check` validates this file's shape when present, using the packaged
[JSON Schema](../../packages/truewire/src/truewire/resources/inventory.schema.json)
and the loader's duplicate-operation check.
It permits null approval and bare entries and does not require an inventory.
`truewire score` requires an approved inventory and fails every unknown endpoint reference
or unspecified entry. It reports the resolved/total ratio, including explicit exclusions
as resolved: the example above fails with `2/3 endpoints; 1 unspecified`. An unapproved
inventory shows the same ratio plus `inventory not approved`. Every entry must resolve
and the inventory must be approved for S1 to pass with `N/N endpoints`.
Spec endpoints absent from the inventory are warnings, not failures. `score --verbose`
prints every unspecified operation's method and path, and all coverage errors when a
long list is abbreviated in the table. The inventory
follows `[spec].dir` when the project uses a custom spec directory.

## 0. Endpoint shape

Each `endpoint.json` carries one self-contained operation: a `spec` with `kind`, an identifier, a titled `request` object schema (each property is one named parameter) and a titled `response` schema. Keep 2xx responses only; errors belong to the client core. Inline any schema used once. `$ref` into `spec/schemas.json` is for genuinely shared shapes.

```jsonc
{
  "docs": "https://example.com/api/reference/pets/get",
  "spec": {
    "kind": "rpc", "transports": ["http"], "path": "/pets/{petId}", "method": "GET",
    "description": "Fetch one pet by id.",
    "request": {"title": "GetPetRequest", "type": "object", "required": ["petId"],
      "properties": {"petId": {"type": "integer", "description": "Pet id."}}},
    "response": {"title": "Pet", "type": "object", "properties": {"...": "..."}}
  }
}
```

Keep `function` segments short, when you set one at all. Prefer `wallet.deposit.history` over a segment copied from the upstream heading when the surrounding router already supplies context.

**A request property states a parameter's role, not its wire placement (ADR 0006).** A normal, individually named parameter is a property of `request` whether the endpoint is REST, JSON-RPC or a WS command. Codegen and the client core decide how it reaches the wire: a URL query string, a slot in a positional JSON-RPC `params` array in declaration order, a key in a WS frame. A `{name}` placeholder in `path` substitutes the request property of that name, and `truewire check` verifies the two agree both ways.

**A discriminated-union request is an `anyOf` of titled variants, and the wrapper is titled too.** Flat properties cannot express a payload whose required fields differ by variant (a limit order requires `price`, a market order forbids it). Rule 1 already titles each variant; codegen also needs a name for the generated method's parameter that holds one of them, and it has nothing to derive that from except the wrapper's own title. Without one it falls back to `body`, which is never wrong, but `order_request` reads better at the call site.

```jsonc
// WRONG: variants titled, wrapper isn't; the parameter can only be called `body`
{"description": "Order to place.", "anyOf": [
  {"title": "LimitOrderRequest", "type": "object", "properties": {"...": "..."}},
  {"title": "MarketOrderRequest", "type": "object", "properties": {"...": "..."}}]}
// RIGHT: the parameter renders as `order_request`
{"title": "OrderRequest", "description": "Order to place.", "anyOf": [
  {"title": "LimitOrderRequest", "type": "object", "properties": {"...": "..."}},
  {"title": "MarketOrderRequest", "type": "object", "properties": {"...": "..."}}]}
```

## 1. Title every object schema

Every named schema in `schemas.json` and every inline schema with a `properties` map carries a `title`: PascalCase, semantic, idiomatic. `title` is the generated type name. Without it the generator derives one from the schema's position in the document, and you get `Response200`.

```jsonc
// WRONG
{"type": "object", "properties": {"price": {"type": "string"}}}
// RIGHT
{"title": "SpotTrade", "type": "object", "properties": {"price": {"type": "string"}}}
```

A schema with only `additionalProperties` is a map, not a record, and needs no title. A `$ref` needs no inline title. An empty-object schema (`type: 'object'`, no properties, no `additionalProperties`) needs a title for the same reason a non-empty one does; `truewire check` reports this as the separate `title-empty-object` rule, `warning`-severity.

Enforcement: `truewire check`, `error` (`title`), `warning` (`title-empty-object`).

## 2. Closed sets use `enum`

Any value with a documented finite set of values declares `enum`, not a bare `string` or `integer`. Prefer a single-value `enum` over `const`. `enum` becomes `Literal[...]`; a closed set stated only in prose becomes `str`.

```jsonc
// WRONG
{"type": "string", "description": "Order side: BUY or SELL."}
// RIGHT
{"type": "string", "description": "Order side.", "enum": ["BUY", "SELL"]}
```

**"Documented" is load-bearing. Never invent an enum.** The check fires on field names that usually denote a closed set (`type`, `state`, `side`, `status`), so it overshoots. When the API names such a field but never publishes its values, a bare `string` is the correct spec: a guessed `enum` becomes a `Literal` that rejects valid responses at runtime, which is worse than an imprecise type.

Enforcement: `truewire check`, `warning`.

## 3. Timestamps carry their real wire format

Any field that is a Unix timestamp on the wire declares one of `format: 'epoch-seconds'`, `'epoch-millis'`, `'epoch-micros'`, `'epoch-nanos'`, or, for an RFC 3339 string, `format: 'date-time'`. A plain calendar date declares `format: 'date'`. Never a bare `string`/`integer` with the shape only in prose.

Each declared format renders to a distinct, correctly converting type (`TimestampMillis`, `TimestampIso`, all `datetime`; `DateIso`, a `date`) instead of a bare `str`/`int` with no conversion at all. This applies identically to a request parameter, a request body property and a response field. A request-side timestamp with no declared format is sent to the wire wrong; that was a live bug on one client's order endpoint before the check existed.

```jsonc
// WRONG
{"startTime": {"type": "integer", "description": "Start time, Unix ms."}}
// RIGHT
{"startTime": {"type": "integer", "format": "epoch-millis", "description": "Start time, Unix ms."}}
```

Every declared format renders to an alias `truewire_core.types` ships (`TimestampSeconds`, `TimestampMillis`, `TimestampMicros`, `TimestampNanos`, `TimestampIso`, `DateIso`); a format outside this list is refused at generation.

Enforcement: `truewire check`, `warning` (`timestamp-format`). A scalar field whose name ends, after splitting at case boundaries, in `time`, `timestamp`, `date`, `datetime`, `ts`, `since` or `at` and declares no format is reported. Only the last word is matched, so `timeInForce` is never mistaken for a timestamp. Like rule 2, a name is a lower bound on the real gap, not proof. Two clients read `0 errors` while declaring a format on 0 of 218 and 117 of 310 endpoints respectively before this check existed.

## 4. Positional rows use `prefixItems`, bounded by `minItems`/`maxItems`

An array whose entries are fixed-length heterogeneous rows declares `prefixItems`, one titled and described entry per position, plus `minItems` and `maxItems` set to the row length. Never `"items": {}`. The bounds are not optional: in JSON Schema 2020-12 `prefixItems` alone permits both short and long rows, so a row that loses a column still validates against its own examples.

```jsonc
// WRONG
{"type": "array", "items": {}, "description": "Kline row: open time, open, high, ..."}
// RIGHT
{"title": "Candle", "type": "array", "minItems": 6, "maxItems": 6, "prefixItems": [
  {"title": "openTime", "type": "integer", "format": "epoch-millis", "description": "Open time."},
  {"title": "open", "type": "string", "format": "decimal-string", "description": "Open price."}]}
```

Enforcement: `truewire check`, `error`.

## 5. Unions are `anyOf`, and only `anyOf`

The type parser raises on `oneOf` and `allOf`. Write `anyOf` directly. A nullable record is the same rule, and it is not obvious: `{"type": ["object", "null"]}` routes through the multi-type path and fails.

```jsonc
// WRONG
{"type": ["object", "null"], "properties": {"...": {}}}
// RIGHT
{"anyOf": [{"title": "PreListingInfo", "type": "object", "properties": {}}, {"type": "null"}]}
```

Enforcement: `truewire check`, `error`.

## 6. Schemas describe the wire body

A response schema, and the examples recorded against it, describe the body exactly as the API sent it: the whole frame, envelope included. Where the core unwraps an envelope, the endpoint declares `envelope.payload` (ADR 0004, ADR 0010), a dotted path into that schema naming the value the generated method returns; the generator derives the method's return type from the schema at that path. Where the core returns the frame, there is no `envelope`. Nothing else can hold: `truewire check` validates a recording against this schema and a recording is the wire, `truewire capture` writes the wire, and `truewire import openapi` copies a document that describes the wire too.

```jsonc
// The API sends {"error": [], "result": {...}}; the core returns `result`.
{"spec": {"...": "...",
  "response": {"title": "BalanceFrame", "type": "object", "required": ["result"],
    "description": "Wire frame.",
    "properties": {
      "error": {"type": "array", "items": {"type": "string"}, "description": "Wire envelope field; see the core."},
      "result": {"title": "Balance", "type": "object", "description": "Balance by asset.",
        "additionalProperties": {"type": "string", "format": "decimal-string"}}}}},
 "envelope": {"payload": "result"}}
// The generated method returns `Balance`. `BalanceFrame` is never rendered.
```

**Unwrap in the core by default.** An API that wraps every response (`{retCode, retMsg, result}`, `{success, data}`, JSON-RPC `{jsonrpc, id, result}`) has exactly one envelope, and a caller should never index past it. Unwrap in the core, declare `envelope.payload`, and let the schema keep the wrapper as it is on the wire. The wrapper's fields are wire facts the core reads (an error array, a request id); describe them briefly ("Wire envelope field; see the core."), since nothing generates from them. Errors belong to the core, and an envelope is how errors arrive.

**Keep the envelope when it carries something the caller needs.** An API that puts its page counts beside `data` rather than inside it returns the frame: no `envelope`, and its `pagination` block reads `"done": {"kind": "total", "path": "data.totalPage"}`.

**Pagination paths are relative to what the method returns.** `rows`, `done.path` and `cursor.from` resolve inside the schema at `envelope.payload`, never from the frame root, because the generated walk reads them off the value the core handed back.

**Be consistent with the core, not with the venue.** Envelope is declared per endpoint, never per project, because a real client can be two cores' worth of behavior: 9 JSON-RPC endpoints that unwrap `result` and 32 REST endpoints that do not, under one package. A project-level default could only pick one answer.

**A response field that is a genuine secret or PII gets an obviously fake, shape-preserving placeholder, never the real value.** An API key's own secret in a recorded response is a literal `"REDACTED_CLIENT_SECRET"` string: obviously fake, right shape. This needs no mechanism; a response body is never compared by the mock server, only replayed. `truewire standards` flags a credential-shaped response field with no obvious fake marker.

**A recording holds the response to the endpoint's own request, and nothing the core sent around it.** A hand-written core mints a token, refreshes one, fetches a ticket, retries after a 401; none of those responses describes this endpoint, and one of them is a credential. `truewire capture` picks the exchange whose method and path (or JSON-RPC method name) are the ones the endpoint declares, reports what it skipped, and refuses rather than guess when nothing matches -- it recorded whichever exchange happened to be last until the fix in `packages/truewire/CHANGELOG.md`, so a recording captured through a multi-request core before that is worth re-reading against this rule, and any credential found in one is worth rotating rather than only deleting. Writing a recording by hand is the same rule: the body is the reply to the request the endpoint's `path` and `method` name.

A spec written under the previous form of this rule, where the schema described the unwrapped value, is rewritten by `truewire migrate`: it wraps each such schema into the frame its recordings show.

Enforcement: `truewire check`, `error` (`envelope`): `envelope.payload` names a property of the response schema. A `$ref` or `anyOf` on the path is undecidable from the endpoint alone and is not flagged.

## 7. Describe everything that becomes a docstring

`description` is required on the operation, every request property, the response, and every response object property. Each one lands in generated source: the operation description is the method docstring, a request property description is its `Args:` bullet, a response property description is the `TypedDict` field docstring. Schema-level descriptions do not substitute for property-level ones.

Enforcement: `truewire check`, `error`.

## 8. Pagination is declared, not inferred

An endpoint the API paginates carries a `pagination` block beside `spec`, tagged by `strategy` (`page`, `token`, `offset`, `seek`), naming request parameters by their spec names. See ADR 0002 and ADR 0013.

```jsonc
{"pagination": {"strategy": "page",
  "index": {"parameter": "page", "start": 1}, "size": {"parameter": "pageSize"},
  "done": {"kind": "total", "path": "data.totalPage", "counts": "pages"}}}
{"pagination": {"strategy": "token",
  "cursor": {"parameter": "cursor", "from": "nextPageCursor"}, "size": {"parameter": "limit"},
  "done": {"kind": "absent_cursor", "rows": "list"}}}
{"pagination": {"strategy": "offset",
  "offset": {"parameter": "ofs"}, "size": {"parameter": "limit"},
  "done": {"kind": "total", "path": "count", "counts": "items", "rows": "trades"}}}
{"pagination": {"strategy": "seek",
  "cursor": {"field": "[-1][0]", "unique": true},
  "bound": {"start": "start", "end": "end"}, "anchor": "end",
  "size": {"parameter": "limit"}, "rows": "list"}}
```

**`page`, `offset` and `token` declare a terminator.** `page` and `offset` take `total`, `short_page` or `empty`; `token` takes `absent_cursor` or `empty`. A `total` states what it counts (`pages` or `items`); an item count needs a declared `size` to convert. `page` does not require a total: `short_page` works, and needs a `size` to be short relative to.

**A `total` only ever decides an earlier stop.** An empty page ends every `page`/`offset` walk whatever terminator is declared, and a `total` missing from a response, or changing between two pages of one walk, is not an error (ADR 0013): a page walk over live data is racy whether or not `total` moves.

**Every declared block generates a `PaginatedResponse`**, so every block names its row collection as `rows` (on the terminator for `page`/`offset`/`token`, at the top level for `seek`). Omit it only when the payload is the collection. Picking the wrapper key by looking for the one array property is the guessing the block exists to end. A response whose rows are not an array (a map keyed by id) cannot be walked this way: declare no block, and say why in `notes`.

**`seek`** is the one strategy for every walk whose next request bound is read off the rows of the previous page: a timestamp, a block height or a row id, with one bound or two.

- `cursor.field`: the row field the next bound is read from, starting with `[-1]` (`[-1][0]`, `[-1].id`), resolved against a row of `rows`. Under a timestamp bound it declares its own timestamp `format`, which may differ from the bound's: the walk reads a row value in the row's format and sends the next bound in the bound's (lighter's `epoch-seconds` fundings under an `epoch-millis` `end_timestamp`). Any two instant formats (`epoch-*`, `date-time`) convert; a `date` converts only to a `date`. A row field with no timestamp format under a timestamp bound would leave the unit to a guess, and `truewire check` refuses it.
- `cursor.unique`: whether that field is unique per row. Required, never defaulted.
- `bound.start` / `bound.end`: the request parameters bounding the range. At least one.
- `anchor`: `start` or `end`, the bound the API fills from when it truncates. The walk moves the anchored bound, so the anchor is also the walk's direction: an API anchored to `end` is walked newest-first.
- `size`: the page-size parameter; its schema `default` resolves the row cap. An integer size the caller gives is sent clamped to `min(max(size, 2), maximum)`: a page must hold one new row beside the boundary row it re-reads. A size with no `maximum` is only floored, and a `maximum` below 2 is the page size.
- `cap`: the API's fixed row cap, when `size` cannot resolve one.
- `span`: `{"parameter", "default", "unit"}`, the widest range one request may cover, for an API that refuses a wide range rather than truncating it. Makes both bounds required.
- `exclusive`: `{"parameters", "first", "far": {"parameter", "field"}}`, request parameters the API refuses alongside the moving bound (a time range next to a `fromId`). They are sent on the first request only, while the caller gave no moving bound; passing the moving bound with any of them but `far.parameter` raises. `first` names the one a walk must start from when the moving bound is omitted. `far.parameter` caps the walk client-side, and is never sent beside the moving bound: every row whose `far.field` lies past the caller's value is dropped, and the walk ends on the page that held one. `far.parameter` must be a number, an `integer-string` or a timestamp, with the same timestamp `format` as `far.field` when either has one.

The walk requests from the moving bound to the far bound (or the span edge), moves the bound to the extreme key of each full page, and deduplicates the re-fetched boundary rows: by key when `unique`, by whole-row content otherwise (a carried row absent from the next page raises `LogicError`). A full page whose rows all share one key raises `LogicError`. Without a resolvable cap the walk cannot tell a short page from a full one, and keeps going until a page brings nothing fresh, so `unique: false` requires a cap and an orderable field; `truewire check` enforces both, flags a `cap` made redundant by a `size` default, a `span.parameter` that shadows a wire parameter, and (`pagination-read`, a warning) a paginated non-`GET` operation, since a retried or resumed page repeats its request.

**The cap is never invented.** A size default lives on the parameter's schema, as `default`, because prose ("defaults to 200") does not reach the generator:

```jsonc
// WRONG: the default lives where only a human reads it
{"limit": {"type": "integer", "minimum": 1, "maximum": 1000,
  "description": "Rows per page. Range [1, 1000]; defaults to 200."}}
// RIGHT
{"limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200,
  "description": "Rows per page. Range [1, 1000]; defaults to 200."}}
```

Declaring a cap larger than the truth turns a full page into a false "exhausted" and silently drops rows, so the number is documented or measured, never guessed. A declared `maximum` clamps a caller's larger size when measuring a full page.

**Types.** The parameters a `page` or `offset` walk computes are numbers: spec them `integer` even where the API documents every query parameter as a string. A `token` cursor is opaque and exempt; so is a `seek` bound without a `span`, which is re-sent, compared, never added to. A `seek` bound with a `span` is added to, so it must be `integer`, `number`, or a `string` with `format: 'date-time'`.

**Response paths admit dotted keys and bracket indices.** `pageKey`, `data.totalPage`, `[-1].id`, `list[-1][0]`: a plain key, or an integer index in brackets (`[-1]` is the last element), composed to whatever small depth a real field needs. No wildcards, no slices, no `$` root.

**The anchor is an API fact, not a spec fact.** Nothing in an operation states it, so the audit cannot check it, and a wrong one reads as a clean spec and shows up as a walk that silently under-covers at runtime. Measure it before declaring it: request a range far wider than one page with a small explicit size, and see which end the rows cluster at. Probe with a window safely in the past, snapped to interval boundaries, never near "now": a range at the live edge can look start-anchored when rows for part of it simply do not exist yet. Record the probe in `notes`. The same goes for a documented default or maximum; one API documented `limit`'s default and maximum swapped. Bound inclusivity is not declared at all: the dedup absorbs it.

**Omission is not a lie the checker can catch.** Nothing in a spec says an endpoint paginates, so an absent block is checked against nothing. When an endpoint paginates in a shape this format cannot state, say so in its `notes`, so nobody later "completes" it.

Enforcement: `truewire check`, `error`, for every reference a present block makes.

## 9. `meta` is free-form, and every project's own core defines what it means

`meta` is the one place a core gets to know anything about the specific endpoint it is serving. Credentials are the most common instance, not the whole of it: one API needed to declare, per endpoint, whether a response is a batched action that must never auto-raise on the envelope's status. `meta` is untyped on purpose, because no two APIs need the same quirks described. Real values range from a bare string (`"hmac"`), to a bare `false` ("never"), to a dict whose shape is whatever that one core chose to read (`{"scheme": "l1", "vault_scoped": true}`). Do not copy another project's shape; design the one this core needs.

Once a project's generator config declares a JSON Schema for its core, `truewire check` validates every endpoint's `meta` against it, so a typo'd key is a spec error rather than a silent core bug.

**Keep `meta` to the minimum set of literal keys that actually discriminates real, distinct behaviors.** If every endpoint needs the same scheme and no other quirk, `{}` is correct. If an API has exactly two modes, `{"public": true}` on the public endpoints and `{}` elsewhere is the whole of it. One client carried `type`/`header`/`source` on all 178 endpoints when every one used the identical scheme; two of the three keys were never even read.

**A fact that is not a per-endpoint quirk does not belong in `meta`.** A base URL that varies by product surface, not by call, is a per-directory routing fact the generator config's core mapping already owns.

Never leave `meta` unset on the assumption that "no security block" means "public". It just as often means nobody has told this endpoint what security block it would have had.

Enforcement: `truewire check`, `error`, when the resolved core declares a schema.

## 10. A wire parameter must not be named `validate`

`validate: bool | None = None` is the keyword every generated `rpc` method reserves for the per-call override of response validation. An API that names one of its own wire parameters `validate` collides with it. This is not a reason to rename the spec's parameter; it keeps the real wire name. The fix belongs to the codegen backend, which must resolve the generated Python parameter to a distinct name. Before this was checked, `validate=True` on one client's order endpoints meant "don't submit the order," never reaching the real `validate` at all.

Enforcement: `truewire check`, `warning` (`reserved-param`). It never promotes to `error`: there is no spec-side fix for a real collision, only a codegen-side one.

## 11. A connect-only or post-RPC push stream declares `push`

A `kind: 'stream'` endpoint with no subscribe frame of its own carries a `push` block beside `spec`, tagged by `trigger`:

```jsonc
// push starts the instant the connection is accepted (a listen key in the URL, zero frames sent)
{"push": {"trigger": "connect"}}
// push starts once the named RPC's reply has been served (a `login` request, then auto-push)
{"push": {"trigger": "after_rpc", "method": "login"}}
```

These two shapes are the entire space a connect-only dialect takes across every API reviewed so far. A push-only example still records `parameters` and `messages`; `push` only changes when the mock sends them, never what.

Every other stream example sends a subscribe frame, and the mock answers it with `<id>.reply.json` and nothing else (a generic `{type: 'ack'}` is synthesized only for an `envelope.channel` dialect). With no recording the mock stays silent, and a core that waits for its ack hangs in every language's replay. So each such example records one of:

- the captured ack;
- a documented ack, when the live capture missed it: the upstream docs' example, or a sibling channel's captured ack with only the channel identity changed, with its source stated in `<id>.parameters.json`'s `description`;
- the literal `null`, when the API sends no ack at all. The mock then sends nothing, `check` skips schema validation, and a client core for that API must not wait for one.

Enforcement: `truewire check` lists every subscribe example with no `<id>.reply.json` under "Missing subscribe acks" (a warning, not yet a failure). Nothing checks the `push` declaration itself.

## 12. A wire boolean sent as the string `"true"`/`"false"` carries `format: 'boolean-string'`

Keep `"type": "string"` (the JSON Schema type has to match what the recorded example holds) and add `"format": "boolean-string"`. It renders as a real `bool`; pydantic's lax coercion needs no custom converter. Declare `format`, never `enum: ["true", "false"]`: an `enum` beside a format silently wins and reverts the field to a string `Literal`.

```jsonc
// WRONG
{"withdrawable": {"type": "string", "description": "Whether withdrawal is enabled."}}
// RIGHT
{"withdrawable": {"type": "string", "format": "boolean-string", "description": "Whether withdrawal is enabled."}}
```

Enforcement: none today.

## 13. A wire integer sent as the string `"12"` carries `format: 'integer-string'`

Same shape as rule 12, for a value that is conceptually a count: block confirmations, decimal precision. It renders as `int`. An opaque numeric-looking id (a numeric coin id used only as a label) is not this rule; nothing does arithmetic on it, so it stays a bare `string`.

```jsonc
{"depositConfirm": {"type": "string", "format": "integer-string", "description": "Confirmations required."}}
```

Enforcement: none today.

## 14. A router grouping declares its description and upstream link in `router.json`

A directory under `spec/endpoints/` that groups several endpoints into one generated class carries a sibling `router.json` with exactly two string fields:

```jsonc
{"description": "Perpetual futures and delivery contracts, the API's 'mix' product line.",
 "upstream": "https://example.com/api-doc/contract/intro"}
```

`description` is prose about what the grouping is, not a restatement of its path segment. `upstream` is one canonical URL to the API's own documentation for that domain, resolving to the specific page. Every grouping directory declares one; a directory whose endpoints span several pages cites the page covering the most of them, or reuses a parent's link. Never invent one.

Two more fields are optional. `core` names the core the subtree's endpoints are built on (`docs/cores.md`). `class` names the group's generated class, in place of the PascalCase of its directory name, for when the derived name is taken or misleading. A `chain/rpc/` group derives `Rpc`, which is also a core transport type in Python:

```jsonc
// spec/endpoints/chain/rpc/router.json
{"description": "Node RPC: health, sync status and the raw JSON-RPC passthrough.",
 "upstream": "https://example.com/api-doc/chain/rpc",
 "class": "ChainRpc"}
```

Only the type name changes. Every backend declares the group under it (the Python and TypeScript class, the Rust struct, the Go type), and the parent names it as its accessor's type, but the accessor itself is still named after the directory: `client.chain.rpc` in every language, `Chain.RPC` in Go. The words are the author's and the letter case is the language's: Go re-cases initialisms in a declared name as in every other, so the type is `ChainRPC` there. `class` is an ASCII PascalCase identifier (a capital letter, then letters and digits, not `Self`, `None`, `True` or `False`). A declared `class` is still judged by rule 18 like a derived one. It is refused on the root `router.json`, because the client root is named by each backend's `name` in `truewire.toml`.

Enforcement: `truewire standards` (presence, `error`; link reachability, `warning`, via `--only links`). Shape, including `class`, is validated on load; `truewire check` reports a `router.json` that does not load, or a `class` on the root, as an `error` naming the file, and `truewire generate` refuses it before writing anything.

## 15. A wire number sent as the string `"1.23"` carries `format: 'decimal-string'`

Same shape as rules 12 and 13, for a price, size, amount or rate sent as a string. It renders as `Decimal`, constructed from the wire string, so the API's own precision is preserved. A `str` price cannot be compared or summed; a `float` price cannot represent the value exactly.

```jsonc
{"openPriceAvg": {"type": "string", "format": "decimal-string", "description": "Average entry price."}}
```

This format is for the string-wire case. A price the API sends as a JSON number is a separate gap: do not re-type it as `string` when the API does not actually send one. `validator(Type).dump()` renders a `decimal-string` field correctly in the request direction with no special-casing (ADR 0008).

Enforcement: none today.

## 16. A directory is a leaf endpoint or a router grouping, never both

A directory carrying its own `endpoint.json` never also has an endpoint-bearing descendant. A directory holding both has no name to render its own leaf's method under: the other children are reached by attribute, and the node's own name is already spoken for by the class itself. The only mechanical answer is `__call__`, which `truewire standards` forbids. Refusing the shape is simpler than solving that naming problem.

The restructure is usually already decided by the spec: a leaf whose `function` ends one segment deeper than its directory (`v1.account.get` in `v1/account/`) moves to the subdirectory that segment names (`v1/account/get/`).

Enforcement: `truewire check`, `error`. The generator also raises, naming the directory, if a leaf's resolved function collides with a node already in the function tree.

## 17. A schema may reference itself, through a record

A comment thread, a file tree and a nested JSON value are all recursive, and a schema states that by referencing itself — directly, through an array's `items`, or around a cycle of several schemas.

```jsonc
{
  "Node": {
    "title": "Node",
    "type": "object",
    "description": "One node of a tree.",
    "properties": {
      "id": {"type": "string", "description": "Node id."},
      "children": {"type": "array", "description": "Child nodes.", "items": {"$ref": "Node"}}
    },
    "required": ["id"]
  }
}
```

The rule is that at least one schema on the cycle is a **record** — an object schema with `properties`, which renders as its own named type. A name is what lets the cycle close: whichever record is emitted first names the other as a forward reference (`children: NotRequired[list['Node']]`), which a `TypedDict` accepts. Mutual recursion (`Node.branch` → `Branch`, `Branch.node` → `Node`) works for the same reason, in either order.

A cycle where *no* schema is a record does not render, and is refused. Every schema on it is an expression pasted at each use site rather than a named type, so there is nothing for the cycle to close on:

```jsonc
{"Tree": {"title": "Tree", "anyOf": [{"type": "string"}, {"type": "array", "items": {"$ref": "Tree"}}]}}
```

`properties` is what makes a record, not `type: "object"`: an object schema declaring only `additionalProperties` renders as a `dict[...]`, an expression, and `{"Node": {"type": "object", "additionalProperties": {"$ref": "Node"}}}` is refused for the same reason as `Tree`.

The fix is to give one schema on the cycle `properties` and a `title`, so it becomes the record the cycle closes on, and to point the rest at it.

Enforcement: `truewire check`, `error`, naming the schemas on the cycle. Generation refuses the same shape with a `SchemaCycleError` rather than a `RecursionError`.

## 18. A router group's class name is not its parent's, a sibling's, or a shared schema's

Every router node generates one class, and that class names each child it composes: a group by the class the child's own directory renders (or the `class` its `router.json` declares, rule 14), an endpoint by the method it exposes. Two things claiming one name in that module is refused, in either of the two ways it happens.

The first is a group whose class name is already the composing class's own. At the root that class is the client itself, named by `[python].name`, `[typescript].name` or `[rust].name` — so a client called `Weather` over a spec with a `weather/` group is refused:

```
spec/endpoints/weather/router.json   # renders `Weather`
truewire.toml  [rust] name = "Weather"
```

There is nothing left for the root to be. Rust's `client.rs` writes `use crate::weather::Weather;` above `pub struct Weather`, so the root struct contains itself (`E0255`, then `E0072` and `E0391`); TypeScript's `main.ts` does the same by bare import (`TS2440`, `TS2395`). Python raises nothing at all, which is worse: the class shadows the import, `client.weather` returns another root client, and every endpoint under the group drops off the surface with no diagnostic anywhere. The same shape recurs one level down — `alpha/beta/beta/` renders `Beta` into the module that already declares `Beta`.

The second is two siblings that render one name. Directory names are unique but rendered class names are not: `list-orders/` and `list_orders/` both render `ListOrders`, and the module composing them binds that name twice, so only whichever is written second is reachable.

The third is a group whose class name is a shared schema's. The same module imports the shared types alongside the classes it composes, so a `forecast/` group beside a `Forecast` in `schemas.json` wants one name for two things — `api.weather.gov` really is shaped that way, since the endpoint is `/gridpoints/{office}/{x},{y}/forecast` and the thing it returns is a forecast. TypeScript's router `index.ts` imports the type and declares the class (`TS2440`, `TS2395`); Python's group `__init__.py` never imports the shared types, so it compiles and the collision goes unseen. One backend's silence is not evidence a name is free, so the spec is refused on the first.

The title's rename here is the fix that usually reads better anyway: `GridpointForecast` is the service's own term for what that endpoint returns.

The fix is a rename, and which one is the author's: choose a different `name` in the backend's section, rename the group directory, or give the group a `class` in its `router.json` (rule 14), which renames the type and leaves the accessor alone. A declared `class` is judged exactly as a derived name is, so it is refused if it collides in any of the three ways above. It is never done for you — the root class name and every group attribute are the public surface of somebody's client, and a generator that quietly picked `Weather2` would change what a caller writes without saying so.

Enforcement: `truewire check`, `error`, naming the client, the group and the `router.json` the group comes from. `truewire generate python`, `typescript` and `rust` refuse the same condition before writing a file, each judging its own declared client name, so skipping the gate cannot produce the broken tree.

## 19. A request field minted per call, nested out of `redacted`'s reach, is named under `match.ignore`

`redacted` (ADR 0007) strips flat key names: a query item, a top-level body key, a named-object JSON-RPC `params` entry. A signature, signing timestamp or nonce that sits deeper is out of its reach. Bitget's WebSocket login is one: `{"op": "login", "args": [{"apiKey", "passphrase", "timestamp", "sign"}]}`. A recording can only hold a stale value there, so the mock never matches the real frame. The endpoint names each such field by path (ADR 0018):

```json
"envelope": {"payload": "", "selector": "op", "params": "args"},
"match": {"ignore": ["args[0].apiKey", "args[0].passphrase", "args[0].timestamp", "args[0].sign"]}
```

A path uses the response-path grammar (dotted keys and bracket indices, no `$`, no wildcard), rooted at the whole WebSocket frame or HTTP JSON body. The field is dropped from both the real request and the recording before comparing, so the recording may hold a placeholder or omit it. List only what a recording cannot hold: a field the caller controls stays compared. A frame with no JSON-RPC `method` also needs `envelope.selector` and `envelope.params`, or `truewire mock` never routes it to the endpoint at all.

Enforcement: `Endpoint` validation refuses a path outside the grammar, an empty `ignore`, a `match` with no rule and any other key under `match`. Whether a field belongs there is not checked; a missing entry shows up as the mock's 422 or `unexpected_parameters` frame.

## 20. An API that reads a list in the query string comma-separated declares `match.query_arrays: "comma"`

A list-valued request field travels in the query string as one key per value (`?state=WA&state=OR`) unless the endpoint says otherwise. When the API reads one comma-separated item instead (`?state=WA,OR`, OpenAPI's `style: form, explode: false`), the endpoint declares it (ADR 0019):

```json
"match": {"query_arrays": "comma"}
```

The core writes that form, and the mock joins each recorded list the same way before comparing. Check the form against the live API with two values: a recording with one value per filter matches either form, and an API that keeps only the last of a repeated key (api.weather.gov) drops the rest without an error. Record one example with two values in a filter and assert that both come back.

Enforcement: `Endpoint` validation refuses any value other than `repeat` or `comma`. Whether the declaration matches the API is not checked; a core sending the other form gets the mock's 422 on any recording with two values in a list.
