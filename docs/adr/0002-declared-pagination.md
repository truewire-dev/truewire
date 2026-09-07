# ADR 0002: Pagination is declared as an audited discriminated union, not inferred

- Status: accepted
- Date: 2026-08-06 (carried over 2026-09-06)

## Context

Before this, pagination was reinvented per client. One backend matched a page parameter by the literal name `page_num` and then sniffed `totalPage`, `totalPageNum`, `data.totalPage` and empty lists at runtime to decide when to stop. Others shipped no iterator at all; one client's own ledger recorded eleven paginated endpoints with nothing generated for them. A survey across roughly 750 endpoints found the ad hoc shapes in use clustered into a small number of families: page number, opaque token, offset plus limit, a time window, and a cursor read off the last row.

## Decision

`pagination` is an optional field on the endpoint, a sibling of `spec` rather than part of the request schema, typed as a discriminated union on `strategy`: `page`, `token`, `offset`, `window`, `seek`. Each strategy is a closed model. A field borrowed from another strategy (a `cursor` under `strategy: page`) fails to load rather than being silently ignored.

A declared block is checked, not trusted. `truewire check` verifies that every parameter it names is a real request parameter, that the parameters a walk does arithmetic on (`page`'s index, `offset`'s offset, `window`'s two bounds) are numerically typed, that every response path it names (`done.path`, `done.rows`, `cursor.from`) resolves against the response schema, and that a terminator only appears alongside the declarations it needs (`short_page` needs a `size` to be short relative to). The audit judges only a declaration that is present. An endpoint with no block is never flagged for lacking one, because that would resurrect the name-sniffing this replaces, just moved into the checker.

A declared block generates. The Python generator turns it into a `<method>_paged` variant beside the single-request method. Where the strategy can compute `(rows, next_state)` per page, the variant is a `PaginatedResponse`: usable both as `async for page in ...` and as `await ...` for the flattened rows.

Each strategy admits only the terminators it can decide: `page` and `offset` take `total`, `short_page` or `empty`; `token` takes `absent_cursor` or `empty`; `seek` takes `short_page`, `empty` or `unchanged`; `window` takes `empty` alone, plus the caller's own far bound. A `window` with a declared `size` also gets a runtime guard: a page as full as the requested size means the venue likely truncated it, so the walk raises rather than silently skipping rows, unless the caller passes `allow_truncation=True`.

## Consequences

Declaring the model surfaced two real bugs a heuristic never would have: a window's advance and its terminator disagreeing on step size when `size` was omitted, and a truncation guard that read `size is not None` and stayed silent on the common call that omits `limit` entirely. Both were fixed by making the declaration, not the generated code, the place that states the default.

Response paths admit a narrow grammar: dotted keys and bracket integer indices (`data.totalPage`, `[-1].id`, `list[-1][0]`), nothing more. No wildcards, no slices, no JSONPath. That grammar was refused entirely at first and widened once `seek` and `window` over positional rows made the gap impossible to route around; it stays deliberately small.

Left open: bound inclusivity and sort order are facts about the API, not about the spec. Nothing in a declaration states either, so a wrong `step` or `order` reads as a clean spec and shows up as a duplicated or missing row at runtime. Establish both by calling the endpoint before declaring it. See `docs/spec/authoring.md` rule 8.
