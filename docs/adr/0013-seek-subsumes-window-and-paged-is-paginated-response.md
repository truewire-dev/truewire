# ADR 0013: `seek` subsumes `window`, walk direction is derived from a declared truncation anchor, and every `_paged` method is a `PaginatedResponse`

- Status: accepted
- Date: 2026-09-06
- Amends: ADR 0002 (the `window` strategy and the `seek` cursor it describes are replaced)
- Carried over: 2026-09-14, from the system Truewire was extracted from (its ADR 0021)

## Context

ADR 0002 declared time-range pagination as `window`: the walk keeps the width of the
caller's own two bounds and slides it, reading nothing from the response. `seek` was added
later for a cursor read off the last row. Both grew patches: `step` (bound inclusivity
compressed to `0`/`1`), `overlap` (narrow-and-retry plus content dedup), `chunk`,
`unchanged`, `allow_truncation`. Two live findings showed the model was built on an authored
field standing in for an unmeasured venue fact:

- A venue's `limit` truncation is
  anchored to one end of the requested range. bitget keeps the newest rows. Declared
  `order: ascending`, the `overlap` narrow branch converged on the far edge in one step and
  `candles_paged` returned 5 rows of ~360, silently. `order` was a free choice in the spec;
  the anchor is a venue fact nobody had measured.
- Seven plain `window` endpoints (kucoin's six time-series endpoints, bybit's
  `historical_volatility`) declare no `size`, so they generate no truncation guard at all.
  `kucoin.spot.klines_paged` over a month yields the newest 1500 candles and returns
  cleanly. The docstring asks the caller to "choose a window the venue answers in one
  response", which moves correctness onto the caller.

Measured 2026-09-06 on public endpoints, a window two days in the past, `limit=5`:

| venue endpoint | truncation keeps |
|---|---|
| binance klines, aggTrades; mexc klines; kucoin futures klines | oldest rows (anchored to `start`) |
| bybit kline, funding history; coinbase candles; kucoin spot klines, futures index endpoints; bitget candles | newest rows (anchored to `end`) |
| coinbase candles past 350 rows | refuses the request instead of truncating |

Every shipped `overlap` endpoint happened to be declared in the direction its venue anchors.
Nothing in the spec, audit, or generator knew that.

Separately, 213 of the fleet's 571 generated `_paged` methods were bare async generators.
An async generator that raises is dead, so a downstream caller (the SDK's `Context`
middleware, whose `retry` deliberately wraps only coroutine functions) cannot retry one
page. The 358 `PaginatedResponse`-shaped ones could, except that the `page`+`total`
renderer kept walk state in `nonlocal` closure variables, so `next(state)` was not a pure
function of `state`.

## Decision

### One cursor-from-rows strategy, named `seek`

`window` is deleted. `seek` is redefined as the one strategy for any walk whose next
request bound is read off the rows of the previous page, whether the bound is a timestamp,
a block height, or a row id, and whether the caller gives one bound or two:

```jsonc
{"pagination": {
  "strategy": "seek",
  "cursor": {"field": "[-1][0]", "unique": true},
  "bound": {"start": "startTime", "end": "endTime"},
  "anchor": "end",
  "size": {"parameter": "limit"},
  "rows": "list"
}}
```

- `cursor.field`: the row field the next bound is read from, in the `docs/pagination.md`
  §3 grammar, relative to one row (`[-1]` prefix, as before).
- `cursor.unique`: whether that field is unique per row. Candle open times and row ids
  are; fill and funding timestamps are not. Required, never defaulted.
- `bound.start` / `bound.end`: the request parameters bounding the range. At least one.
- `anchor`: which bound the venue fills from when it truncates. `end` means it keeps the
  newest rows. **The walk direction is derived from this**: the anchored bound is the one
  the walk moves, and the other bound, when declared and given, caps the walk. `order` is
  deleted. A venue that keeps the newest rows can only be walked newest-first; a caller who
  wants the other order reverses the result.
- `size`: the page-size parameter, when the venue takes one. Its schema `default` resolves
  the cap, exactly as before. An integer size the caller gives is clamped once, before the
  walk, to `min(max(size, 2), maximum)`, and that value is both the cap and what every
  request sends. A size with no `maximum` is only floored; a `maximum` below 2 is the page
  size, with no floor. A size the caller omits stays unset.
- `cap`: the venue's fixed row cap, when `size` cannot resolve one.
- `span`: `{"parameter", "default", "unit"}`, the widest range one request may cover, for a
  venue that refuses a wide range rather than truncating it (coinbase). Optional. Declaring
  it makes both bounds required on the generated method.
- `rows`: response path of the row collection, omitted when the payload is the collection.

Deleted with `window`: `order`, `step`, `overlap`, `chunk`, `unchanged`, `WindowDone`, and
the `allow_truncation` keyword. Bound inclusivity is no longer declared: the algorithm below
does not depend on it.

### The walk

Ascending shown (`anchor: start`); descending mirrors it with `min` for `max`, `end` for
`start`, and `-` for `+`.

```
pos     ← caller's start (None when omitted or undeclared)
far     ← caller's end   (None when omitted or undeclared)
carried ← []                       # rows already yielded whose key equals pos
loop:
  edge  ← far if span is undeclared else min(pos + span, far)
  rows  ← req([start] = pos, [end] = edge)
  fresh ← rows − carried           # by key when unique, by content otherwise
  if not unique and some carried row is absent from rows: raise LogicError
  yield fresh
  ext   ← max(key(r) for r in rows)              # None when rows is empty
  full  ← cap is known and |rows| ≥ cap
  if full:                                       # more inside [pos, edge]
    if ext is None or ext = pos: raise LogicError  # every row shares one key: unreachable rest
    pos ← ext; carried ← rows with key = ext; continue
  if cap is unknown and ext is not None and (pos is None or ext ≠ pos):
    pos ← ext; carried ← rows with key = ext; continue   # cannot tell short from full: keep going while there is progress
  # [pos, edge] is exhausted
  if span is undeclared or edge ≥ far: stop
  pos ← edge; carried ← rows with key = edge
```

Written out plainly:

1. The request always spans from the moving bound to the far bound (or the span edge).
   Nothing is ever fetched outside the caller's own range.
2. A full page means "more inside this range": move the bound to the extremum key seen and
   fetch again. Rows with that key are carried over and deduplicated on the next page: by
   key when the field is unique (no content comparison, so a row whose other fields change
   between requests, an open candle, is never mistaken for a missing one), by content
   otherwise (a carried row genuinely absent from the next page still raises: that is the
   venue's row-stability guarantee breaking).
3. A short page means the requested range is exhausted. Without a resolvable cap the walk
   cannot tell a short page from a full one, so it keeps moving the bound to the extremum
   until a page yields nothing fresh. A field declared `unique: false` therefore requires a
   resolvable cap; the audit enforces it.
4. A full page whose rows all share one key cannot be advanced past and raises
   `LogicError`, as `overlap` already did.
5. With `span`, an exhausted chunk advances the bound to the chunk edge; the walk ends when
   the edge reaches the far bound.

The walk requests pages of at least 2 rows and at most the size's `maximum`: a page must
hold one new row beside the one it re-reads. Against an inclusive moving bound, a page of 1
is the carried boundary row alone, full and sharing one key, so step 4 would raise on every
walk. A unique cursor needs only 2; a non-unique one may need more, and step 4 covers that
case. An exclusive bound loses nothing to the floor. A `maximum` below 2 leaves no room for
it: the walk requests pages of that maximum, which an exclusive bound still walks. (Amended
2026-09-28, TRU-56: the walkers clamped only the cap and sent the caller's raw size.)

No `step`: the next bound is the extremum key itself, and step 2's dedup absorbs whichever
way the venue treats the boundary. No `order`: it is the anchor. No truncation guard and no
`allow_truncation`: a full page is progress, not a fault.

### Parameters the venue refuses beside the moving bound: `exclusive` (2026-09-28)

aster's `userTrades` walks by `fromId` but answers `-1106` when `startTime` or `endTime` comes
with it (measured on testnet, recorded in the endpoint's `notes`). Without `fromId` it
answers the newest rows unless `startTime` is given, so an ascending walk has to start there.

```jsonc
"exclusive": {
  "parameters": ["startTime", "endTime"],
  "first": "startTime",
  "far": {"parameter": "endTime", "field": "[-1].time"}
}
```

- `parameters` are sent on the first request only, while the walk has no position of its
  own. Once it moves `fromId` they are left out. A caller passing the moving bound and any
  of them but `far.parameter` is refused before a request is made.
- `first`, optional, is the one the caller must give when the moving bound is omitted.
- `far`, optional, is the bound the walk enforces itself, since it can no longer send it:
  every row whose `field` lies past the caller's `parameter` (after it, walking forwards) is
  dropped, and the walk ends on the page that held one. It may come with the moving bound
  (`fromId` + `endTime` resumes a capped walk): it is then never sent, only kept on the rows.

`truewire check` requires every named parameter to exist and `far.field` to resolve in, and
order against, the row. `far.parameter` must have an order of its own (a number, an
`integer-string`, a timestamp `format`), and when either it or `far.field` declares a
timestamp format, both declare the same one: every backend reads the row's field through the
parameter's converter. `exclusive` beside `span` is refused: a span re-sends both bounds on
every request. The runtimes carry the far bound as `until` (`@truewire/core`),
`Seek::until`/`step_until` (`truewire-core`) and `Seek.Past` (Go).

### Every `_paged` method returns `PaginatedResponse`

All five remaining walks (`page`, `offset`, `token`, `seek`) render as
`truewire_core.util.paging.PaginatedResponse[Row, State]`. The plain async-generator renderer
and its `max_pages` keyword are deleted (`break` out of `async for` does the same).

Each renderer honours the contract now stated in `truewire_core.util.paging`: `next` is a pure
function of `state`, all walk state lives in `State`, no clock reads, read-only requests.
`seek`'s state is `(pos, carried)`, a tuple of the moving bound's value and the carried
rows. `page`/`offset`'s is the index. `token`'s is the cursor.

The strict `total` check (ADR 0002 §4: raise when a page's total is missing or disagrees
with an earlier page) is dropped. A page walk over live data is racy regardless of whether
`total` moves, so the raise added spurious failures on active accounts and the closure state
that broke purity, without adding correctness. `total` is now used only to decide when to
stop, and an empty page always stops every indexed walk.

### Enforcement

- `truewire check`: `seek` requires `anchor` to name a declared bound, `cursor.field`
  to resolve against a row of `rows`, `cursor.unique: false` to have a resolvable cap,
  `span` to have both bounds declared; a `pagination` block on a non-`GET` `rpc` endpoint
  is a `warning` (a read-shaped `POST` is legitimate and gets a `notes` entry).
- `truewire standards --only paged-shape` (S24, now `error`): every generated
  `_paged` method's return annotation is `PaginatedResponse[...]`.

## Consequences

- Breaking for every client: 213 methods change return type, `max_pages` and
  `allow_truncation` disappear, and eight formerly-ascending walks (bitget's five candle
  endpoints, kucoin's three index endpoints) become descending because that is the only
  direction their venues can be walked safely.
- The seven unguarded plain-window endpoints become correctly walkable with no `size` at
  all: the algorithm needs a cursor field, not a page size.
- 54 endpoint declarations (37 `window`, 17 `seek`) are re-declared. `anchor` is a measured
  fact, recorded per endpoint in `notes` with the probe that established it, never inferred
  from documentation alone.
- `truewire-core` already carries the `PaginatedResponse` purity contract, `pages()`, `resume()` and `via()`; no runtime release is needed.
- Downstream, the hand-unrolled `init`/`next` loop is replaced by `paging.via(call)`, and
  `paging.pages()`/`resume()` give checkpoint-and-resume for long walks.
- `docs/pagination.md` and `docs/spec/authoring.md` rule 8 are rewritten against this ADR;
  ADR 0002's `window` section and its "seek cursors are not one of the four strategies"
  paragraph are superseded, the rest of ADR 0002 stands.
