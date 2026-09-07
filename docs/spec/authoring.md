# Spec Authoring

Rules for `spec/schemas.json` and every `spec/endpoints/**/endpoint.json`. `truewire check` enforces most of them; each rule says whether it does, and at what severity. An `error` fails the check. A `warning` is a heuristic that can be wrong, so it reports but never fails.

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

Every declared format needs a matching type in the client's own core types module. A missing pair generates a module that fails with `NameError` on import.

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

**Pagination paths are relative to what the method returns.** `done.rows`, `done.path` and `cursor.from` resolve inside the schema at `envelope.payload`, never from the frame root, because the generated walk reads them off the value the core handed back.

**Be consistent with the core, not with the venue.** Envelope is declared per endpoint, never per project, because a real client can be two cores' worth of behavior: 9 JSON-RPC endpoints that unwrap `result` and 32 REST endpoints that do not, under one package. A project-level default could only pick one answer.

**A response field that is a genuine secret or PII gets an obviously fake, shape-preserving placeholder, never the real value.** An API key's own secret in a recorded response is a literal `"REDACTED_CLIENT_SECRET"` string: obviously fake, right shape. This needs no mechanism; a response body is never compared by the mock server, only replayed. `truewire standards` flags a credential-shaped response field with no obvious fake marker.

A spec written under the previous form of this rule, where the schema described the unwrapped value, is rewritten by `truewire migrate`: it wraps each such schema into the frame its recordings show.

Enforcement: `truewire check`, `error` (`envelope`): `envelope.payload` names a property of the response schema. A `$ref` or `anyOf` on the path is undecidable from the endpoint alone and is not flagged.

## 7. Describe everything that becomes a docstring

`description` is required on the operation, every request property, the response, and every response object property. Each one lands in generated source: the operation description is the method docstring, a request property description is its `Args:` bullet, a response property description is the `TypedDict` field docstring. Schema-level descriptions do not substitute for property-level ones.

Enforcement: `truewire check`, `error`.

## 8. Pagination is declared, not inferred

An endpoint the API paginates carries a `pagination` block beside `spec`, tagged by `strategy` (`page`, `token`, `offset`, `window`, `seek`), naming request parameters by their spec names and stating how the walk ends. See ADR 0002.

```jsonc
{"pagination": {"strategy": "page",
  "index": {"parameter": "page", "start": 1}, "size": {"parameter": "pageSize"},
  "done": {"kind": "total", "path": "data.totalPage", "counts": "pages"}}}
{"pagination": {"strategy": "token",
  "cursor": {"parameter": "cursor", "from": "nextPageCursor"}, "size": {"parameter": "limit"},
  "done": {"kind": "absent_cursor"}}}
{"pagination": {"strategy": "offset",
  "offset": {"parameter": "ofs"}, "size": {"parameter": "limit"},
  "done": {"kind": "total", "path": "count", "counts": "items"}}}
{"pagination": {"strategy": "window",
  "bound": {"start": "start", "end": "end"}, "order": "descending",
  "step": {"unit": "ms", "size": 1}, "size": {"parameter": "limit"},
  "done": {"kind": "empty", "rows": "list"}}}
{"pagination": {"strategy": "seek",
  "cursor": {"parameter": "fromId", "from": "[-1].id"}, "size": {"parameter": "limit"},
  "done": {"kind": "short_page"}}}
```

**Terminators.** Each strategy admits only the terminators it can decide: `page` and `offset` take `total`, `short_page` or `empty`; `token` takes `absent_cursor` or `empty`; `seek` takes `short_page`, `empty` or `unchanged`; `window` takes `empty` alone. A `total` states what it counts (`pages` or `items`); an item count needs a declared `size` to convert. `page` does not require a total: `short_page` works, and needs a `size` to be short relative to.

A `total` is checked for the whole walk. The generated walk raises `LogicError` if a response is ever missing the declared total, or if a later page's total disagrees with an earlier one in the same walk. A missing total is the API breaking the contract the terminator exists to use, not a signal to stop quietly. Concatenating rows fetched against two different totals is a splice of two moments, not a result.

`short_page`, `empty`, `unchanged` and `total` name the collection they measure as `rows`. Omit it only when the payload is the collection. Picking the wrapper key by looking for the one array property is the guessing the block exists to end.

**`window`** walks a time range and reads nothing out of the response. It keeps the width the caller's own two bounds state and moves that window along. `order` says which way; the bound that moves is derived from it. `step` is the distance past the edge just covered: `1` when both bounds are inclusive (a walk that omitted it would re-read the boundary row forever), `0` when the far bound is exclusive (a walk that stepped would skip a row). The walk ends once advancing would cross the caller's own far bound, or earlier if a response empties out. A caller asking for `[start, end]` never gets rows past `end`; before the far-bound check existed, two shipped walkers did exactly that.

A full page is the opposite signal: a window holding more rows than `size` was truncated by the API, silently, and a walk that moves on skips the rows it never saw. Declaring `size` on a `window` generates a guard: a page as full as requested raises `LogicError` naming the endpoint and the bounds, and a caller who means to accept a capped read passes `allow_truncation=True`. **A `window` also declares its size parameter's `default`**, as `default` on that property's schema, because on the call a caller usually makes (bounds and no size) "full" means the API's own default, not the caller's `None`. Prose ("defaults to 200") does not reach the generator.

```jsonc
// WRONG: the default lives where only a human reads it
{"limit": {"type": "integer", "minimum": 1, "maximum": 1000,
  "description": "Rows per page. Range [1, 1000]; defaults to 200."}}
// RIGHT
{"limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200,
  "description": "Rows per page. Range [1, 1000]; defaults to 200."}}
```

Never invent the number. A guessed default raises on windows the API answered in full, a false stop the caller cannot refuse without giving up the guard. No `size`, or no declared default, generates no guard and a docstring that states the loss instead of promising a raise.

Declaring `overlap` on a `window` replaces the raise with an attempt to make progress first: the walk reads the largest `overlap.field` value off the full page, narrows the window to it and retries, raising only when every row in a full page shares one value. `chunk` declares a real step size decoupled from the caller's span; declare its default only where a real per-row density fact backs it (a candle interval), never as a guess.

**Types.** The parameters a walk computes are numbers. Spec them `integer` even where the API documents every query parameter as a string; a `window` declared over `string` bounds raises `TypeError` on the first page. A `window` bound alone may instead be `string` with `format: 'date-time'`, since that renders to a real `datetime` that supports the same arithmetic. A `page` index or an `offset` gets no such exception. A `token` or `seek` cursor is exempt: both are opaque to the walk.

**Response paths admit dotted keys and bracket indices.** `pageKey`, `data.totalPage`, `[-1].id`, `list[-1][0]`: a plain key, or an integer index in brackets (`[-1]` is the last element), composed to whatever small depth a real field needs. No wildcards, no slices, no `$` root. This was refused entirely at first and widened once `seek` and positional candle rows made the gap impossible to route around; it stays narrow for the same reason it was refused.

**`seek`** reads its cursor off the last row of the previous page (`cursor.from: "[-1].id"`), resolved against `done.rows` or the whole payload. Its plain form assumes the cursor is unique per row. A cursor that is not (a millisecond timestamp where one millisecond holds dozens of fills) needs `overlap`, declaring the API's true per-call row cap as `overlap.cap`:

```jsonc
{"pagination": {"strategy": "seek",
  "cursor": {"parameter": "startTime", "from": "[-1].time"},
  "done": {"kind": "empty"}, "overlap": {"cap": 500}}}
```

The generated walk then advances to the largest value seen on a page, drops the rows already yielded for that value from the next page by position (verified as an exact prefix, raising `LogicError` if the API's stable order broke), and raises when a page fills to `cap` while every row shares one value. `overlap` requires the payload to be the row collection (`done.rows` unset).

A `seek` cursor can also fail by never seeing an empty page: an inclusive bound over a non-unique field re-serves the tied boundary row forever. `done: {"kind": "unchanged"}` ends the walk the moment a page's last-row cursor stops differing from the cursor the request was made with. It cannot be declared alongside `overlap`, whose walk always terminates on an empty page.

**Bound inclusivity and sort order are API facts, not spec facts.** Nothing in an operation states either, so the audit cannot check them, and a wrong `step` or `order` reads as a clean spec and shows up as a duplicated or missing row at runtime. Establish both by calling the endpoint before declaring it. Probe with a window safely in the past, snapped to interval boundaries, never near "now": a range at the live edge can look start-inclusive when rows for part of it simply do not exist yet. The same goes for a documented default or maximum; one API documented `limit`'s default and maximum swapped.

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

Enforcement: none today.

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

Enforcement: `truewire standards` (presence, `error`; link reachability, `warning`, via `--only links`). Shape is validated on load.

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
