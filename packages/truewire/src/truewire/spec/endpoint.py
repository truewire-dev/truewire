import re
from collections.abc import Sequence
from pathlib import Path
from typing_extensions import Annotated, Any, Literal

from truewire.generation.schema import Operation
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


KEY_SEGMENT = r'[A-Za-z_][A-Za-z0-9_-]*'
"""One dotted-key path segment: `pageKey`, `totalPage`."""

INDEX_SEGMENT = r'\[-?\d+\]'
"""One bracket-index path segment: `[0]`, `[-1]`. `N` is a signed integer literal;
`-1` addresses the last element -- there is no other index arithmetic here."""

DOTTED_KEY = re.compile(
  rf'(?:{KEY_SEGMENT}(?:{INDEX_SEGMENT})?|{INDEX_SEGMENT})'
  rf'(?:\.{KEY_SEGMENT}(?:{INDEX_SEGMENT})?|{INDEX_SEGMENT})*'
)
"""A dotted response path with optional bracket indices: `pageKey`, `data.totalPage`,
`key[0]`, `key[0].key2`, `[-1].id`, `[-1][0]`. Nothing else -- see `dotted_path`."""

PATH_LANGUAGE = ('$', '*')
"""Characters that would make a response path a general expression instead of a dotted
key with bracket indices -- a JSONPath root or a wildcard, refused outright."""


def dotted_path(value: str) -> str:
  """
  Reject a response path that is anything more than a dotted key with bracket indices.

  `docs/pagination.md` §3: paths admit bracket indices (`key[N]`, `[N].key2`, composable
  to a small, realistic depth) alongside dotted keys -- `N` a signed integer literal,
  usually `-1` for the last element of a collection. This closes a real gap the original,
  strictly-dotted-key rule left: most `window`-strategy rows are positional tuples (a
  candle's timestamp at a fixed array index), not the named objects `seek`'s own retired
  `last:<field>` prefix was built for. One grammar now expresses both, and expresses
  envelope traversal and row/field addressing in a single composed path.

  Still refused: a JSONPath root, a wildcard, a slice, or arbitrary depth beyond what a
  real declared field needs. Admitting a full path language is the thing being avoided,
  the same reasoning the original strictly-dotted-key rule stated -- indexing is a single
  new construct admitted on purpose, not a door to a general expression grammar.

  Args:
    value: Response path as written in `endpoint.json`.

  Raises:
    ValueError: When the path is not a dotted key with optional bracket indices.
  """
  found = [character for character in PATH_LANGUAGE if character in value]
  if found:
    raise ValueError(
      f'response path {value!r} uses {", ".join(repr(item) for item in found)}; only a '
      f'dotted key with optional bracket indices -- `pageKey`, `data.totalPage`, '
      f'`[-1].id`, `[-1][0]` -- is allowed. There is no general path language here to '
      f'evaluate a wildcard or a JSONPath root'
    )
  if not DOTTED_KEY.fullmatch(value):
    raise ValueError(
      f'response path {value!r} is not a dotted key with optional bracket indices; write '
      f'e.g. `pageKey`, `data.totalPage`, `[-1][0]`, or `[-1].id`'
    )
  return value


ResponsePath = Annotated[str, AfterValidator(dotted_path)]
"""Dotted key (with optional bracket indices, per `dotted_path`) naming one field of the
unwrapped response payload."""


def path_segments(path: str) -> list[tuple[Literal['key', 'index'], 'str | int']]:
  """
  Split a validated `ResponsePath` into its ordered `('key', name)`/`('index', n)`
  segments, dots discarded (they are pure separators, not their own segments).

  Permissive on purpose: `dotted_path` already rejects anything that isn't a well-formed
  dotted key/bracket-index path before this ever runs, so this only ever tokenizes an
  already-validated string.

  Args:
    path: A validated `ResponsePath`, or `''` for no segments at all.
  """
  segments: list[tuple[Literal['key', 'index'], str | int]] = []
  for match in re.finditer(rf'\[(-?\d+)\]|({KEY_SEGMENT})', path):
    index, key = match.groups()
    if index is not None:
      segments.append(('index', int(index)))
    else:
      segments.append(('key', key))
  return segments


def last_row_path(value: str) -> str:
  """
  Reject a `seek` cursor source that doesn't index the last row of its own row collection.

  `[-1]` is the fixed, literal index this narrow case needs -- the collection itself is
  always the walk's own row collection (`SeekPagination.done`'s `rows`, or the whole
  payload when unset), so nothing here says which collection, only that the field named
  after `[-1]` comes off its last element. Retired from the old bespoke
  `last:<dotted-path>` prefix (`last:id` -> `[-1].id`) onto the same general bracket-index
  grammar `dotted_path` admits everywhere -- see `docs/pagination.md` §3.

  Args:
    value: Response path as written in `endpoint.json`.

  Raises:
    ValueError: When the path is not a dotted key with optional bracket indices, or
      doesn't start with `[-1]`.
  """
  value = dotted_path(value)
  if not value.startswith('[-1]'):
    raise ValueError(
      f'seek cursor source {value!r} does not index the last row; write e.g. `[-1].id` '
      f'for a cursor read off the `id` field of the last row of the previous page'
    )
  return value


LastRowPath = Annotated[str, AfterValidator(last_row_path)]
"""`[-1]<...>`: the one field a `seek` strategy may read off the *last row* of its own row
collection -- the narrow indexing exception `dotted_path` otherwise refuses to admit past
the last-element case. Formerly a bespoke `last:<dotted-path>` prefix; now `[-1]` plus the
same general grammar every other `ResponsePath` admits (`docs/pagination.md` §3)."""


def last_row_field(path: str) -> str:
  """
  Return the path a `LastRowPath` reads relative to one row, with its fixed leading
  `[-1]` segment (and one separating `.`, when present) stripped.

  Args:
    path: A `LastRowPath`, already validated -- always starts with `[-1]`.
  """
  suffix = path[len('[-1]'):]
  return suffix[1:] if suffix.startswith('.') else suffix


def payload_path(value: str) -> str:
  """
  Same as `dotted_path`, but also accepts `''`, meaning "the whole value, no extraction".

  A `payload`/`reply_payload` declaration is otherwise forced to name a field even for a
  frame with nothing worth indexing into -- a flat subscribe ack `{id, type}` has no
  nested value to extract, unlike a JSON-RPC `{jsonrpc, id, result}` reply, which does.

  Args:
    value: Response path as written in `endpoint.json`, or `''` for the whole frame.

  Raises:
    ValueError: When the path is non-empty and not a plain dotted key.
  """
  return value if value == '' else dotted_path(value)


PayloadPath = Annotated[str, AfterValidator(payload_path)]
"""Dotted key naming one field of the wire frame, or `''` for the frame itself."""


class PaginationModel(BaseModel):
  """
  Base for every pagination declaration.

  `extra='forbid'` is load-bearing: a key borrowed from another strategy — `cursor` under
  `strategy: page` — has to fail validation rather than be quietly dropped, or the
  declaration stops being the single statement of how an endpoint pages.
  """

  model_config = ConfigDict(extra='forbid')


class PaginationParameter(PaginationModel):
  """Request parameter carrying one pagination value."""

  parameter: str
  """Parameter name, exactly as the operation declares it."""


class PageIndex(PaginationModel):
  """Request parameter carrying the page number."""

  parameter: str
  """Parameter name, exactly as the operation declares it."""
  start: int = 1
  """Number the API gives the first page."""


class Cursor(PaginationModel):
  """Request parameter carrying an opaque token, and where the next token is read from."""

  parameter: str
  """Parameter name, exactly as the operation declares it."""
  from_: ResponsePath = Field(validation_alias='from', serialization_alias='from')
  """Response path of the token to send as `parameter` on the next request."""


class SeekCursor(PaginationModel):
  """
  Request parameter carrying a cursor read off the *last row* of the previous page's own
  row collection -- not a plain top-level field the API hands back, which is `Cursor`'s
  shape instead. A `historical_trades` endpoint whose `fromId` is the `id` of the last row
  of the previous page is the reference shape.
  """

  parameter: str
  """Parameter name, exactly as the operation declares it."""
  from_: LastRowPath = Field(validation_alias='from', serialization_alias='from')
  """`[-1]<...>`, naming a field of the last row of the collection `SeekPagination.done`
  names -- the same collection the walk already reads to decide whether it is done,
  since that `fromId` and its emptiness check read the very same rows: it is declared
  `[-1].id` (formerly the bespoke `last:id`)."""


class TotalDone(PaginationModel):
  """Termination by a total published in the response."""

  kind: Literal['total'] = 'total'
  path: ResponsePath
  """Response path of the total."""
  counts: Literal['pages', 'items']
  """
  What the total counts.

  APIs publish both — a `totalPage` next to a `totalCount`, say — and the loop
  bound differs by a factor of the page size, so the unit cannot be left to the reader.
  """
  rows: ResponsePath | None = None
  """
  Response path of the collection a caller-facing paged wrapper yields per page.

  Optional, and irrelevant to the ordinary async-generator paged method -- it yields the
  whole response object per page, so nothing here has ever needed to know which field
  holds the rows. It matters only to a wrapper shaped like `truewire_core.util.paging.
  PaginatedResponse`, whose `next` callable returns `(rows, next_state)` rather than the
  whole response -- see `AbsentCursorDone.rows`, the same declaration for `token` strategy.
  Omit it when the payload is itself the collection.
  """


class ShortPageDone(PaginationModel):
  """Termination when a response returns fewer rows than the requested page size."""

  kind: Literal['short_page'] = 'short_page'
  rows: ResponsePath | None = None
  """
  Response path of the collection whose length is the page length.

  Omit it when the payload is itself the collection. It is not optional decoration: a
  API that wraps its rows — `{success, code, data: [...]}` — gives the
  generated loop nothing to count unless the wrapper key is named, and locating it by
  looking for the one array property is the inference this declaration replaces.
  """


class EmptyDone(PaginationModel):
  """Termination when a response returns no rows at all."""

  kind: Literal['empty'] = 'empty'
  rows: ResponsePath | None = None
  """
  Response path of the collection that goes empty.

  Omit it when the payload is itself the collection. See `ShortPageDone.rows` for why
  the wrapper cannot be guessed.
  """


class UnchangedDone(PaginationModel):
  """
  Termination when a `seek` walk's own cursor field stops advancing, whether or not the
  page it stopped on is empty.

  `short_page`/`empty` both assume the cursor field is unique enough per row that the
  API eventually serves fewer rows, or none, once the walk is truly done -- a `fromId`
  that is a genuine per-row id is exactly that. Neither is safe when it isn't: a
  `get_historical_funding` that reads its cursor off an inclusive block-height bound, where
  multiple rows can share one height, keeps re-serving the same tied
  boundary row forever rather than ever returning an empty page -- `empty` never fires
  (an infinite loop) and `short_page` only works by coincidence when the repeated tail is
  narrower than the page size. `unchanged` compares the cursor value read off the new
  page against the cursor value the request was just made with, and stops the moment
  they agree. An empty page has no last row to read a new cursor from at all -- `None` --
  which already disagrees with any real previous cursor, so it is a trivial instance of
  "the cursor did not advance," not a separate case to handle.

  Unlike `SeekOverlap`, which also exists for a non-unique cursor, this does not dedupe or
  reorder rows -- a `seek` walk terminated by `unchanged` still re-fetches (and re-yields)
  the tied rows on every page up to the last one, exactly as `short_page`/`empty` do. It
  only changes when the walk decides it is done.
  """

  kind: Literal['unchanged'] = 'unchanged'
  rows: ResponsePath | None = None
  """
  Response path of the collection whose last row's cursor field is compared.

  Omit it when the payload is itself the collection. See `ShortPageDone.rows` for why
  the wrapper cannot be guessed.
  """


class AbsentCursorDone(PaginationModel):
  """Termination when a response carries no next token."""

  kind: Literal['absent_cursor'] = 'absent_cursor'
  rows: ResponsePath | None = None
  """
  Response path of the collection a caller-facing paged wrapper yields per page.

  Optional, and irrelevant to the ordinary async-generator paged method -- it yields the
  whole response object per page, so nothing here has ever needed to know which field
  holds the rows. It matters only to a wrapper shaped like `truewire_core.util.paging.
  PaginatedResponse` (awaitable *and* async-iterable), whose `next` callable returns `(rows, next_state)` rather than the
  whole response -- declaring which field those rows are, rather than guessing, is the
  same reasoning `ShortPageDone.rows`/`EmptyDone.rows` already state for their own shape.
  Omit it when the payload is itself the collection.
  """


class WindowBound(PaginationModel):
  """The pair of request parameters bounding one window of a time walk."""

  start: str
  """Parameter carrying the window's lower bound, exactly as the operation declares it."""
  end: str
  """Parameter carrying the window's upper bound, exactly as the operation declares it."""

  @model_validator(mode='after')
  def distinct(self) -> 'WindowBound':
    """
    Reject a window whose two bounds are the same parameter.

    Raises:
      ValueError: When both bounds name one parameter, which bounds nothing.
    """
    if self.start == self.end:
      raise ValueError(
        f'the window bounds are both `{self.start}`; a window is bounded by two parameters'
      )
    return self


class WindowStep(PaginationModel):
  """How far past a window's edge the next window begins."""

  unit: Literal['us', 'ms', 's']
  """
  Unit the bound parameters are expressed in.

  APIs differ — one bounds a kline in milliseconds, another in seconds — and a
  step is a count of the API's own ticks, so the unit cannot be assumed.
  """
  size: int = Field(ge=0)
  """
  Ticks between one window's edge and the next window's, in `unit`.

  `1` when both bounds are inclusive: a walk that set the next bound equal to the
  previous one would re-read the boundary row on every page. `0` when the far bound is
  exclusive, and windows abut exactly.
  """


WindowDone = EmptyDone
"""
How a window walk ends.

Only an empty window ends it. A short page does not: a window's length is a property of
the span the caller chose, not of the walk's progress — a three-minute window of
one-minute candles is three rows deep in the middle of years of history — so
`short_page` would stop the walk on its first sparse window.
"""

IndexedDone = Annotated[
  TotalDone | ShortPageDone | EmptyDone,
  Field(discriminator='kind'),
]
"""
How a numerically indexed walk ends.

`total` is not available everywhere — some APIs paginate by page number and
publish no count at all — so a short or empty page has to be sayable.
"""

TokenDone = Annotated[
  AbsentCursorDone | EmptyDone,
  Field(discriminator='kind'),
]
"""
How a token walk ends.

A token walk cannot recognise a short page without knowing the page count it is walking
towards, and it has no index to compare against a total, so only the token itself or an
empty page can end it.
"""


class PagePagination(PaginationModel):
  """Pagination by page number."""

  strategy: Literal['page'] = 'page'
  index: PageIndex
  """Request parameter carrying the page number."""
  size: PaginationParameter | None = None
  """Request parameter carrying the page size, when the API lets a caller set one."""
  done: IndexedDone
  """How the walk ends."""


class TokenPagination(PaginationModel):
  """Pagination by opaque token."""

  strategy: Literal['token'] = 'token'
  cursor: Cursor
  """Request parameter carrying the token, and where the next one is read from."""
  size: PaginationParameter | None = None
  """Request parameter carrying the page size, when the API lets a caller set one."""
  done: TokenDone
  """How the walk ends."""


SeekDone = Annotated[
  ShortPageDone | EmptyDone | UnchangedDone,
  Field(discriminator='kind'),
]
"""
How a `seek` walk ends.

No `total`: a seek cursor is not an index, so there is nothing to compare a reported count
against. `short_page`/`empty` fit a cursor field unique enough per row that the API
eventually serves a short or an empty page once the walk is truly done, the same as a
`token` cursor whose API publishes no total either. `unchanged` is for the case neither
covers safely: a cursor field that is not unique enough for that -- see `UnchangedDone`.
"""


class SeekOverlap(PaginationModel):
  """
  Declares that a `seek` cursor field is not unique per row -- multiple rows of one page can
  share the value the *next* request's cursor parameter takes, so a naive re-fetch from that
  value would return the shared rows again. A time-cursor endpoint is the motivating
  case: the cursor is a millisecond timestamp, and a single millisecond routinely holds
  dozens of entries.

  When declared, the generated walk drops the re-fetched rows *by position*: it verifies the
  rows already yielded for the cursor value in play reappear as an exact-order prefix of the
  next page -- a mismatch means the API's own stable-row-order guarantee broke mid-walk,
  and the walk raises rather than silently dropping or duplicating rows -- then advances the
  cursor to the *largest* field value collected off the page, not merely its last row's.
  """

  cap: int = Field(gt=0)
  """
  The API's true maximum rows per call.

  A fixed fact, not a request parameter -- unlike `SeekPagination.size`, an API may expose
  no caller-settable size for these endpoints, so there is no parameter to read it from.
  Needed so the walk can tell a page that was cut off mid-cursor-value (every row shares the
  value in play, and the page is full) from one that legitimately ends there; declaring it
  larger than the true cap silently reintroduces the loss it exists to catch.
  """


class SeekPagination(PaginationModel):
  """
  Pagination by a cursor read out of the *last row* of the previous response: a
  `historical_trades` whose `fromId` is the `id` of the last row of the previous page, not
  a plain top-level field the API hands back (`TokenPagination`'s shape instead).

  ADR 0002 deliberately left indexing out of `ResponsePath` at first; `docs/pagination.md`
  §3 later admitted bracket indices generally, and `LastRowPath` is `seek`'s own narrow
  requirement on top of that general grammar -- it must name a field of the *last* row
  (`[-1]<...>`) of the walk's own row collection, never an arbitrary index.
  """

  strategy: Literal['seek'] = 'seek'
  cursor: SeekCursor
  """Request parameter carrying the cursor, and where the next one is read from."""
  size: PaginationParameter | None = None
  """Request parameter carrying the page size, when the API lets a caller set one."""
  done: SeekDone
  """How the walk ends, over the same row collection `cursor.from_` reads its next value
  from."""
  overlap: SeekOverlap | None = None
  """Declared when the cursor field is not unique per row -- see `SeekOverlap`. `None` is
  the plain shape: a cursor that is a genuine per-row id, needing no dedup."""

  @model_validator(mode='after')
  def unchanged_excludes_overlap(self) -> 'SeekPagination':
    """
    Reject `done.kind: 'unchanged'` declared alongside `overlap`.

    `overlap`'s own generated walk (`Generator.paged_overlap_seek`) never reads
    `pagination.done` at all -- it always terminates on an empty page -- so an
    `unchanged` terminator declared beside it would be silently ignored rather than
    produce the walk its own declaration promises.

    Raises:
      ValueError: When both are declared together.
    """
    if self.overlap is not None and self.done.kind == 'unchanged':
      raise ValueError(
        "seek pagination cannot declare both `overlap` and `done.kind: 'unchanged'` -- "
        "`overlap`'s own generated walk always terminates on an empty page and never "
        "reads `done.kind`, so `unchanged` would be silently ignored"
      )
    return self


class OffsetPagination(PaginationModel):
  """Pagination by row offset."""

  strategy: Literal['offset'] = 'offset'
  offset: PaginationParameter
  """Request parameter carrying the row offset."""
  size: PaginationParameter | None = None
  """Request parameter carrying the page size, when the API lets a caller set one."""
  done: IndexedDone
  """How the walk ends."""


class WindowChunk(PaginationModel):
  """
  Declares a `window`+`overlap` walk's own chunk-width keyword and its documented default
  -- `docs/pagination.md` §5's `Δt`, decoupled from the caller's own `t1 - t0` width so a
  wide caller range becomes genuine multi-request coverage instead of one all-or-nothing
  call. Purely a generated-code, Python-side keyword: the API never sees it -- it only
  ever appears baked into `[start]`/`[end]` as narrower per-request bounds.
  """

  parameter: str
  """Name the generated `_paged` method's own chunk-width keyword takes -- exposed so a
  caller can override how finely the walk probes for density (a real keyword, not
  internal-only): the existing truncation-style guard already backstops a bad value the
  same way it does today, so exposing it adds control without adding a new failure mode."""
  default: int = Field(gt=0)
  """
  Chunk width, in `WindowStep.unit`'s own ticks, used when the caller doesn't override
  `parameter`.

  Never invented -- the same "never invent" discipline `docs/spec/authoring.md` rule 8
  already asks of a `size` default: declare one only where a real per-row density fact
  backs it (a candle `interval` enum converting rows to a real time span). Omitting
  `chunk` entirely (not just this field) is how an endpoint says no such fact exists yet
  -- `Δt` then defaults to the caller's own `t1 - t0`, degrading to a single chunk with
  narrow-and-retry instead of `docs/pagination.md` §5's old unconditional raise.
  """


class WindowOverlap(PaginationModel):
  """
  Declares that a `window` walk's own per-row timestamp field is not unique per row --
  multiple rows in one chunk can share the value the walk would otherwise advance past, so
  a naive chunk-to-chunk walk can silently skip rows a single chunk didn't have room for.
  Mirrors `SeekOverlap`'s own mechanism (dropped-by-position dedup, advance to the largest
  value seen) applied to a `window` walk's own per-chunk paging instead of `seek`'s
  per-request cursor -- `docs/pagination.md` §5 is the worked algorithm.
  """

  field: LastRowPath
  """
  Path to each row's own timestamp field, resolved relative to the row collection
  (`WindowPagination.done`'s `rows`, or the raw payload when unset) -- always indexing
  the last-read row, the same `[-1]<...>` requirement `SeekCursor.from_` carries, and for
  the identical reason: `Generator.last_row_field` strips that fixed prefix unconditionally
  rather than validating it, so the type itself has to guarantee it's there.

  Mandatory once a `window` endpoint declares `overlap` at all -- it's how the walk
  computes the largest value seen in a chunk, the same role `SeekOverlap`'s own
  `cursor.from_` plays for `seek`. Uses the `docs/pagination.md` §3 bracket-index grammar,
  since most `window` rows are positional tuples (a candle's timestamp at a fixed array
  index: `[-1][0]`), not the named objects `seek`'s own retired `last:<field>` prefix was
  built for.
  """
  cap: int | None = Field(default=None, gt=0)
  """
  The API's true maximum rows per chunk, when it is not simply resolvable from the
  endpoint's own `size` parameter (a caller-always-set size, or one with a declared
  default -- the same resolution `Generator.paged_cap` already does for a plain `window`
  walk's truncation guard). Mirrors `SeekOverlap.cap`'s identical reasoning: needed only
  when nothing else already settles the row count a full chunk is measured against, and
  never both declared and redundant with a resolvable `size` default at once.
  """
  chunk: WindowChunk | None = None
  """Declares the walk's own chunk-width (`Δt`) keyword and its documented default, when
  one is warranted -- see `WindowChunk`. `None` keeps `Δt` fixed at the caller's own
  `t1 - t0`, one chunk covering the whole requested range."""


class WindowPagination(PaginationModel):
  """
  Pagination by walking a time window: klines, candles.

  The walk is arithmetic on the request bounds alone — the next window ends one step
  before this one starts — so it needs none of the response indexing `seek` needs (until
  `overlap` is declared), which is what makes it a strategy rather than a second name for
  one.
  """

  strategy: Literal['window'] = 'window'
  bound: WindowBound
  """Request parameters carrying the two ends of one window."""
  order: Literal['ascending', 'descending']
  """
  Direction the walk moves in.

  It settles which bound moves and which one the moved bound is computed from, so the two
  cannot disagree: a descending walk sets `end` to `start` minus a step, and an ascending
  walk sets `start` to `end` plus a step. Declare the direction the API itself sorts in
  — descending where it returns its newest rows first, ascending where it returns its
  oldest — so that consecutive pages read as one continuous series.
  """
  step: WindowStep
  """Distance between one window's edge and the next window's."""
  size: PaginationParameter | None = None
  """Request parameter carrying the row cap, when the API lets a caller set one."""
  done: WindowDone
  """How the walk ends."""
  overlap: WindowOverlap | None = None
  """Declared when the walk's own per-row timestamp field is not unique per row -- see
  `WindowOverlap`. `None` keeps today's plain single-chunk walk, raising unconditionally
  on a full page rather than narrowing and retrying."""


Pagination = Annotated[
  PagePagination | TokenPagination | OffsetPagination | WindowPagination | SeekPagination,
  Field(discriminator='strategy'),
]
"""
How an endpoint pages, stated rather than inferred.

Every `ResponsePath` admits bracket indices alongside dotted keys (`docs/pagination.md`
§3), but only two shapes actually reach into a row this way: `seek`'s cursor, read off the
*last row* of the walk's own row collection via `LastRowPath`'s narrow `[-1]<...>`
requirement, and `window`'s own `WindowOverlap.field`, resolved the same way relative to
the row collection. `page`/`token`/`offset` read only plain top-level (or nested-object,
non-array) response fields, and a plain (non-`overlap`) `window` walk never reads the
response at all -- it advances by arithmetic on the bounds it sent.
"""


def read_dotted_path(value: Any, path: str) -> Any | None:
  """
  Read a dotted-key/bracket-index path (`docs/pagination.md` §3) off a JSON-shaped value.

  Args:
    value: The JSON-shaped value to read from.
    path: Dotted key/bracket-index path, e.g. `result`, `data.totalPage`, or `[-1][0]`,
      or `''` to return `value` itself unread.

  Returns:
    The value at `path`, or `None` if any segment is absent -- a dict key the value
    doesn't carry, or a sequence index out of range for it (or applied to a value that
    isn't a sequence at all; a bare `str`/`bytes` doesn't count as one here, since neither
    is ever a JSON array).
  """
  if path == '':
    return value
  for kind, key in path_segments(path):
    if kind == 'index':
      if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return None
      index = key if key >= 0 else len(value) + key
      if not (0 <= index < len(value)):
        return None
      value = value[index]
    else:
      if not isinstance(value, dict) or key not in value:
        return None
      value = value[key]
  return value


def write_dotted_path(value: Any, path: str, new_value: Any) -> Any:
  """
  Return a copy of a JSON-shaped dict with `new_value` written at a dotted-key path.

  Args:
    value: The JSON-shaped dict to copy from. Non-dict input is treated as empty.
    path: Dotted key path to write to, creating intermediate dicts as needed.
    new_value: Value to write at `path`.
  """
  parts = path.split('.')
  out = dict(value) if isinstance(value, dict) else {}
  cursor = out
  for part in parts[:-1]:
    existing = cursor.get(part)
    cursor[part] = dict(existing) if isinstance(existing, dict) else {}
    cursor = cursor[part]
  cursor[parts[-1]] = new_value
  return out


class CorrelatePaths(BaseModel):
  """Distinct request/response paths for an API whose correlation field names differ."""

  model_config = ConfigDict(extra='forbid')
  request: ResponsePath
  """Path to the correlation value on the outgoing request or WS frame."""
  response: ResponsePath
  """Path to the same value on the served response, once threaded through."""


class EnvelopeSpecBase(BaseModel):
  """Shape shared by every endpoint's wire-envelope declaration, regardless of `kind`."""

  model_config = ConfigDict(extra='forbid')
  payload: PayloadPath
  """Dotted path from the raw wire frame to the value the client core hands the caller, or
  `''` when the whole frame already is that value."""
  correlate: ResponsePath | CorrelatePaths | None = None
  """How to thread a request's correlation value into a served response. `None` when the
  envelope carries nothing request-echoed."""

  @property
  def correlate_paths(self) -> CorrelatePaths | None:
    """`correlate` normalized to its full request/response form."""
    if self.correlate is None:
      return None
    if isinstance(self.correlate, CorrelatePaths):
      return self.correlate
    return CorrelatePaths(request=self.correlate, response=self.correlate)

  def extract_value(self, raw: Any) -> Any | None:
    """Read the declared `payload` path off a raw wire response."""
    return read_dotted_path(raw, self.payload)


class RpcEnvelopeSpec(EnvelopeSpecBase):
  """Envelope for a single request/reply endpoint, over HTTP or WS."""

  kind: Literal['rpc'] = 'rpc'
  selector: ResponsePath | None = None
  """Dotted path naming which logical operation a request/frame is, for a transport that
  gives no routing for free -- WS RPC, or an HTTP API where many logical operations share
  one physical URL. Defaults to `'method'`, JSON-RPC's own key, when unset."""
  params: ResponsePath | None = None
  """Dotted path naming a request/frame's arguments, paired with `selector`. Defaults to
  `'params'`, JSON-RPC's own key, when unset."""


class VerbByValue(BaseModel):
  """A frame's subscribe/unsubscribe intent read off a literal string at a fixed path.

  Covers a dialect where one field carries the verb as its own value -- `{"op":
  "subscribe"|"unsubscribe", "args": [...]}`, or `{"type": "subscribe"|"unsubscribe",
  ...}`. `subscribe`/`unsubscribe` name the literal values `path` takes for each verb; they
  need not be the words `"subscribe"`/`"unsubscribe"` themselves, only whatever the API
  actually writes there.
  """

  model_config = ConfigDict(extra='forbid')
  path: ResponsePath
  subscribe: str
  """Literal value at `path` that marks a frame as a subscribe request."""
  unsubscribe: str
  """Literal value at `path` that marks a frame as an unsubscribe request."""


Verb = VerbByValue
"""
How a WS frame states subscribe-vs-unsubscribe intent, declared per stream endpoint. Required
-- there is no default, and an endpoint that omits it is not matched at all; mock.py never
infers intent from a frame's shape or from which subscriptions happen to be active on the
connection (see ADR 0004).

An API whose subscribe and unsubscribe are two distinct RPC methods sharing one frame shape
rather than a dedicated verb field -- `public/subscribe` and `public/unsubscribe`,
differing only in `method` -- still declares `VerbByValue`, pointed at that same path
(`{"path": "method", "subscribe": "public/subscribe", "unsubscribe": "public/unsubscribe"}`):
`method` is an ordinary dotted key like any other, not a second concept. Every dialect
observed so far is expressible this way; a shape it can't express (verb encoded by which
*key* is present, rather than by a value at a fixed key) has no case here yet and should get
one -- narrowly, the same way this type itself was added for one -- once a real API needs
it, not before.
"""


class StreamEnvelopeSpec(EnvelopeSpecBase):
  """Envelope for a WS subscription, whose replayed pushes need declared channel routing."""

  kind: Literal['stream'] = 'stream'
  channel: ResponsePath | None = None
  """Dotted path into the outgoing subscribe/unsubscribe frame naming the channel identity,
  for an API whose subscribe dialect is not the mock's generic `{type, channel}` shape. The
  path may resolve to a list rather than a scalar -- a `public/subscribe` taking
  `{"channels": [...]}`, one element -- in which case matching checks membership instead of
  equality."""
  reply_payload: PayloadPath | None = None
  """Dotted path extracting the subscribe acknowledgement's own value, when it differs from
  `payload` (which names the *pushed message's* path). Defaults to `payload` for an API
  whose reply and message share one envelope shape; an API whose ack is itself a whole
  JSON-RPC reply -- `{"jsonrpc": "2.0", "id": ..., "result": [...]}`, unwrapped by
  `SocketConnection.call` the same as every other rpc reply -- declares `reply_payload:
  "result"` here rather than `payload`'s `"params.data"`, which the ack frame doesn't carry
  at all. `''` names the whole ack frame itself, for an API whose ack is flat and has
  nothing worth extracting -- `{"id": ..., "type": "ack"}` carries no nested value."""
  verb: Verb | None = None
  """How this endpoint's subscribe/unsubscribe frames state their own intent -- see `Verb`.
  Optional only at the type level, so a not-yet-migrated project's spec keeps validating; the
  spec-authoring audit (`check_ws_verb`, staged as a `warning` until every project migrates,
  see ADR 0004) is what actually requires it. `None`
  here means mock.py falls back to its pre-migration heuristics (`msg_type` sniffing, active-
  subscription-membership guessing) for this endpoint, not that the endpoint has no verb
  concept -- every `kind: 'stream'` endpoint does."""


EnvelopeSpec = Annotated[
  RpcEnvelopeSpec | StreamEnvelopeSpec,
  Field(discriminator='kind'),
]
"""
How this endpoint's core unwraps its wire envelope, declared per endpoint.

Split by `kind` rather than one shared shape because `selector`/`params` (RPC routing) and
`channel` (subscribe routing) are meaningful for exactly one of the two -- the split makes a
misplaced field a validation error instead of a silently-ignored one. `kind` itself is never
written in `endpoint.json`; `Endpoint.tag_envelope_kind` derives it from the sibling
`spec.kind` so the declaration never restates what `spec` already says.
"""


def envelope_spec(data: dict[str, Any], *, spec_kind: str) -> RpcEnvelopeSpec | StreamEnvelopeSpec:
  """
  Validate a raw `envelope` object against the subtype implied by the sibling `spec.kind`.

  For callers validating an `envelope` object on its own, outside of `Endpoint.model_validate`
  -- which cannot rely on `EnvelopeSpec`'s own discriminated-union dispatch, since that
  requires `kind` already present in `data`, and `endpoint.json` never writes it.

  Args:
    data: Raw `envelope` object as written in `endpoint.json`.
    spec_kind: The endpoint's own `spec.kind`, `'rpc'` or `'stream'`.
  """
  cls = RpcEnvelopeSpec if spec_kind == 'rpc' else StreamEnvelopeSpec
  return cls.model_validate(data)


class ConnectPush(BaseModel):
  """
  Push declared example messages the instant a connection is accepted, before any frame is
  read from it -- a private user-data stream with a listenKey embedded in the URL
  path, zero outgoing frames, push starting immediately on connect.
  """

  model_config = ConfigDict(extra='forbid')
  trigger: Literal['connect'] = 'connect'


class AfterRpcPush(BaseModel):
  """
  Push declared example messages once one particular `kind: 'rpc'` example's reply has been
  served -- or skipped, for a no-reply RPC -- with no subscribe frame of this endpoint's own
  ever sent. A normal `login` request/reply followed by auto-push, or an `authenticate`
  whose `reply` is `null` followed by an unprompted firehose, are both this shape.
  """

  model_config = ConfigDict(extra='forbid')
  trigger: Literal['after_rpc'] = 'after_rpc'
  method: str
  """
  The gating RPC example's own method/selector value, exactly as recorded on that
  endpoint (`RpcEndpointSpec.path` for the ordinary JSON-RPC case, or whatever
  `RpcEnvelopeSpec.selector` reads for an API with a non-default selector key).
  """


Push = Annotated[ConnectPush | AfterRpcPush, Field(discriminator='trigger')]
"""
How a `kind: 'stream'` endpoint with no subscribe/unsubscribe frame at all starts pushing,
declared per `docs/spec/authoring.md` rule 11. Exactly two trigger shapes: `connect` (push
starts the instant the connection is accepted) and `after_rpc` (push starts once one named
RPC example's reply -- or its absence, for a no-reply RPC -- has been served). A sibling of
`pagination`/`envelope`/`surface` for the same reason every one of them is: OpenAPI has
nothing to say about it, and it is generation/mock-serving metadata, not part of the wire
operation itself.
"""


SYMBOL = re.compile(
  r'[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*'
)
"""A hand-written callable, as `module.path:name` relative to the project's package root."""


def callable_symbol(value: str) -> str:
  """
  Reject a symbol a check cannot go and look for.

  The whole point of declaring where a hand-written callable lives is that the declaration
  is falsifiable: rename the method and the check fails. A symbol naming a module but no
  method, or a sentence about where the code roughly is, is prose again, and prose is what
  let seven WebSocket specs pass every gate with nothing behind them.

  Args:
    value: Symbol as written in `endpoint.json`.

  Raises:
    ValueError: When the symbol is not `module.path:name`.
  """
  if not SYMBOL.fullmatch(value):
    raise ValueError(
      f'symbol {value!r} is not `module.path:name` relative to the package root; write '
      f'`futures.streams.market.deal:deal`, so that the method it names can be looked up'
    )
  return value


CallableSymbol = Annotated[str, AfterValidator(callable_symbol)]
"""Dotted module path and method name, separated by `:`, both resolvable from the package."""


FUNCTION_PATH = re.compile(
  r'[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*'
)
"""A dotted attribute-chase path: `streams.spot_margin.ticker`. No colon -- that's a `symbol`."""


def function_path(value: str) -> str:
  """
  Reject a `function` that isn't a clean dotted attribute chase.

  `resolve_endpoint_function` (`truewire.examples`) walks this string with repeated
  `getattr`, one segment at a time, against a live client instance. A malformed value passes
  silently today and only surfaces as an opaque `AttributeError` deep inside example replay
  or mock serving. This makes the same class of mistake `callable_symbol` already catches for
  `symbol` fail at spec-load time instead.

  Args:
    value: `function` as written in `endpoint.json`.

  Raises:
    ValueError: When the value is not a clean dotted identifier chain.
  """
  if not FUNCTION_PATH.fullmatch(value):
    raise ValueError(
      f'function {value!r} is not a dotted attribute path; write '
      f'`streams.spot_margin.ticker`, so it resolves via repeated `getattr` on a live client'
    )
  return value


FunctionPath = Annotated[str, AfterValidator(function_path)]
"""Dot-separated Python identifiers, resolved via `getattr` chasing on a live client instance.
Not a `symbol` -- see `CallableSymbol` for the static, colon-separated, source-file kind."""


def directory_function(endpoint_path: Path, spec_root: Path) -> str:
  """
  Dotted Python path for an endpoint, derived from its position under `spec/endpoints/`.

  Replaces the authored `function` field (docs/spec/authoring.md rule 0) --
  a leaf directory `market/orderbook/` under `spec/endpoints/` derives `market.orderbook`,
  through the same sanitizing rules that apply to each segment.

  Args:
    endpoint_path: Path to the endpoint's own `endpoint.json`.
    spec_root: The client's `spec/` directory.

  Returns:
    Dotted function path derived from the endpoint's position in the directory tree.
  """
  relative = endpoint_path.parent.relative_to(spec_root / 'endpoints')
  return '.'.join(part for part in relative.parts)


class HandwrittenSurface(BaseModel):
  """An endpoint whose callable is written by hand, and the symbol that proves it exists."""

  model_config = ConfigDict(extra='forbid')
  kind: Literal['handwritten'] = 'handwritten'
  symbol: CallableSymbol
  """The method a caller reaches, as `module.path:name` under the project's package root."""
  reason: str
  """Why the backend does not emit it. Read by people; the `symbol` is what is checked."""


class AbsentSurface(BaseModel):
  """An endpoint with no callable at all, recorded so that nobody has to notice it twice."""

  model_config = ConfigDict(extra='forbid')
  kind: Literal['absent'] = 'absent'
  reason: str
  """Why nothing can be emitted or written yet. This is a debt, and it is stated as one."""


Surface = Annotated[
  HandwrittenSurface | AbsentSurface,
  Field(discriminator='kind'),
]
"""
How an endpoint reaches a caller when the codegen backend does not emit it.

A backend that cannot emit an endpoint is legitimate — one project's sockets are
hand-written, another drops the payloads its spec leaves untyped — but a silent skip reads
exactly like a success, and seven WebSocket specs once passed every gate with no method
anywhere in the package because of it. So the skip has to be said out loud, here, next to the spec it
is about.

`handwritten` is the answer to prefer, because it can be wrong: the symbol is looked up,
and a renamed or deleted method fails the check. `absent` cannot be wrong in that way and
is therefore the weaker record — it is checked in the other direction instead, against the
backend, so a declaration left behind after the backend learned to emit the endpoint fails
too. Neither is a way to make an endpoint stop counting: both are counted and named in the
summary, which is the difference between a recorded exclusion and a silent one.
"""


class Unverified(BaseModel):
  """
  An endpoint this run built but did not call, and why.

  ADR 0001 (endpoint outcome taxonomy) names this outcome "built,
  unverified": the spec, the generated method and the docs all exist — `surface` above
  answers whether a caller can reach the endpoint at all, a different question — and the
  only thing missing is a captured example, because something about *this* run kept the
  call from being made. Declaring it here is what lets `truewire examples
  --require-verified` tell that debt apart from an endpoint nobody has gotten to yet, the
  same way `surface` lets `truewire surface` tell a hand-written callable apart from
  a silent skip.
  """

  model_config = ConfigDict(extra='forbid')
  reason: Literal[
    'missing_credentials',
    'program_enrollment',
    'unsafe',
    'requires_state',
    'runtime_error',
    'not_captured',
  ]
  """
  Closed vocabulary for why the call could not be made.

  `missing_credentials`: the provisioned credential, or its tier, cannot reach this surface.
  `program_enrollment`: gated behind a business line or program this account is not enrolled
  in — broker, affiliate, institutional. `unsafe`: effectful under this run's no-writes
  constraint; the endpoint is still specced and still generates a method — the constraint
  forbids executing the call, not building it. `requires_state`: needs account or resource
  state this run has no way to create, such as an existing order id. `runtime_error`: the
  call was attempted and the API's own response is why nothing was captured.
  `not_captured`: nobody has tried yet -- the endpoint came from an imported document with
  no example of its own, and a live capture is still owed.
  """
  detail: str
  """Which credential, which state, which error. Read by people; `reason` is what is counted."""


class RpcEndpointSpec(BaseModel):
  """Single request, single reply, over one or more transports."""

  model_config = ConfigDict(extra='forbid')
  kind: Literal['rpc'] = 'rpc'
  transports: list[Literal['http', 'ws']] = Field(min_length=1)
  path: str
  """The operation identifier: a REST path, a JSON-RPC method name, or a WS command's event
  name -- whichever the declared transport(s) call for. The one field regardless of transport
  (ADR 0006); a dual-transport JSON-RPC operation uses the same value on both."""
  method: str | None = None
  """HTTP verb, when stating it matters. Unconditionally optional -- whether an API's HTTP
  verb needs declaring per endpoint is API-specific, not something this schema enforces.
  An HTTP-transported JSON-RPC API that is uniformly POST can leave this unset on every
  endpoint and let the core default it, the same way a WS-only or JSON-RPC-method-named
  operation needs no verb at all (ADR 0006)."""
  openapi: Operation | None = None
  """Legacy shape -- superseded by `request`/`response`. Kept only until every project has
  migrated, then removed."""
  request: dict[str, Any] | None = None
  """JSON Schema for every non-templated wire field, replacing `openapi.parameters`/
  `requestBody` -- docs/spec/authoring.md rule 0 (rewritten)."""
  response: dict[str, Any] | None = None
  """JSON Schema for the 2xx response, replacing `openapi.responses['200']`."""
  description: str | None = None
  """Operation docstring, for the new-shape path -- there is no `openapi.description` to
  reuse (mirrors `GrpcEndpointSpec.description`)."""

  @model_validator(mode='after')
  def _one_shape(self) -> 'RpcEndpointSpec':
    """Exactly one of `openapi` or `request`/`response` is set -- never both, never neither."""
    new_shape_present = self.request is not None or self.response is not None
    openapi_present = self.openapi is not None
    if new_shape_present == openapi_present:
      raise ValueError(
        'RpcEndpointSpec must declare exactly one of `openapi` (legacy) or `request`/`response`'
      )
    return self


class StreamEndpointSpec(BaseModel):
  """Subscription pushing messages repeatedly. WS-only -- no `transports` field at all."""

  model_config = ConfigDict(extra='forbid')
  kind: Literal['stream'] = 'stream'
  channel: str
  openapi: Operation | None = None
  """Legacy shape -- superseded by `request`/`parameters`/`payload`. Kept only until every
  project has migrated, then removed."""
  request: dict[str, Any] | None = None
  """JSON Schema for the subscribe call's non-templated wire fields, replacing `openapi.parameters`."""
  parameters: dict[str, Any] | None = None
  """JSON Schema for the subscribe call's non-templated fields (same as `request` for stream)."""
  payload: dict[str, Any] | None = None
  """JSON Schema for pushed messages, replacing `openapi.responses['message']`."""
  description: str | None = None
  """Operation docstring, for the new-shape path -- there is no `openapi.description` to
  reuse (mirrors `GrpcEndpointSpec.description`)."""

  @model_validator(mode='after')
  def _one_shape(self) -> 'StreamEndpointSpec':
    """Exactly one of `openapi` or `request`/`parameters`/`payload` is set -- never both, never neither."""
    new_shape_present = self.request is not None or self.parameters is not None or self.payload is not None
    openapi_present = self.openapi is not None
    if new_shape_present == openapi_present:
      raise ValueError(
        'StreamEndpointSpec must declare exactly one of `openapi` (legacy) or `request`/`parameters`/`payload`'
      )
    return self


class GrpcEndpointSpec(BaseModel):
  """Single gRPC call. `.proto` is the sole source of truth for wire types -- there is
  deliberately no `openapi: Operation` field here."""

  model_config = ConfigDict(extra='forbid')
  kind: Literal['grpc'] = 'grpc'
  service: str
  """Fully-qualified proto service, e.g. `cosmos.bank.v1beta1.Query`."""
  rpc: str
  """Proto method name, e.g. `Balance`."""
  streaming: Literal['unary', 'server', 'client', 'bidi'] = 'unary'
  """gRPC call pattern. Only `'unary'` has a codegen path today."""
  request: str
  """Fully-qualified proto request message type."""
  response: str
  """Fully-qualified proto response message type."""
  proto: str
  """Path to the source `.proto` file, relative to this project's `spec/proto/` tree."""
  description: str
  """Operation docstring -- there is no `openapi.description` to reuse."""
  optional_scalars: list[str] = Field(default_factory=list)
  """Proto3 field names on `request` whose absence is meaningful (caller omitted this
  filter) but is wire-indistinguishable from the zero value -- proto3 itself carries no
  `optional` keyword to derive this from (confirmed against
  cosmos.gov.v1.QueryProposalsRequest)."""


EndpointSpec = Annotated[
  RpcEndpointSpec | StreamEndpointSpec | GrpcEndpointSpec,
  Field(discriminator='kind'),
]


class Endpoint(BaseModel):
  """Top-level endpoint record shared by generation workflows, tests, and mock tooling.

  `function` is the stable logical identifier used by client implementations and the
  examples tree; transport-specific details live under the discriminated `spec`
  field.

  `pagination` is a sibling of `spec` rather than part of the operation because it is
  generation metadata, like `function`: OpenAPI has nothing to say about it, and no
  OpenAPI tool should try.
  """

  model_config = ConfigDict(extra='forbid')

  @model_validator(mode='before')
  @classmethod
  def tag_envelope_kind(cls, data: Any) -> Any:
    """
    Derive `envelope`'s `kind` discriminator from the sibling `spec.kind`, so a declaration
    never has to redundantly restate what `spec` already says.

    Args:
      data: Raw input passed to `Endpoint` validation, before any field parsing.
    """
    if not isinstance(data, dict):
      return data
    envelope = data.get('envelope')
    if not isinstance(envelope, dict) or 'kind' in envelope:
      return data
    spec = data.get('spec')
    spec_kind = spec.get('kind') if isinstance(spec, dict) else getattr(spec, 'kind', None)
    if spec_kind not in ('rpc', 'stream'):
      return data
    return {**data, 'envelope': {**envelope, 'kind': spec_kind}}

  function: FunctionPath | None = None
  deprecated: bool | None = None
  meta: Any | None = None
  redacted: list[str] | None = None
  """Key names the mock matcher ignores when comparing requests against examples. Never read by
  `coerce_example_call` -- only widens what the mock accepts, never substitutes for a real
  `parameters`/`payload` entry."""
  docs: str | None = None
  notes: list[str] | None = None
  spec: EndpointSpec
  pagination: Pagination | None = None
  envelope: EnvelopeSpec | None = None
  """
  How this endpoint's raw wire response is unwrapped into what the client core returns,
  when the core does. A sibling of `spec`/`pagination` for the same reason: OpenAPI has
  nothing to say about it, and it is generation/validation metadata. Left unset on any
  endpoint whose response schema already describes the whole frame (rule 5's "keep the
  envelope" branch) -- most endpoints, including every REST-passthrough one.
  """
  push: Push | None = None
  """
  How this `kind: 'stream'` endpoint starts pushing when it has no subscribe/unsubscribe
  frame of its own -- see `Push`. A sibling of `pagination`/`envelope` for the same reason:
  OpenAPI has nothing to say about it. Left unset for every ordinary subscribe-matched
  stream, which is nearly all of them.
  """
  surface: Surface | None = None
  """
  Where a caller reaches this endpoint when the backend does not generate it.

  A sibling of `spec` for the same reason `pagination` is one: it is generation metadata,
  and OpenAPI has nothing to say about it. Left unset on every endpoint the backend emits,
  which is nearly all of them — declaring it there is itself an error, because it claims
  the backend refused something it did not.
  """
  unverified: Unverified | None = None
  """
  Why this endpoint has no captured example, when it is not simply unbuilt.

  A sibling of `surface` and `pagination` for the same reason: it is generation metadata,
  and OpenAPI has nothing to say about it. Left unset on every endpoint an example was
  captured for, or that nobody has reached yet — declaring it is itself a claim, and a
  wrong one reads as debt nobody can find. `truewire examples --require-verified`
  subtracts every endpoint carrying it from the count it demands paired examples for.
  """

  @model_validator(mode='after')
  def _require_meta_for_new_shape(self) -> 'Endpoint':
    """`meta` is required once an endpoint has migrated to the request/response shape --
    optional only for a legacy openapi-shaped endpoint, so migration can proceed project
    by project without breaking every not-yet-migrated spec at once.

    Only checked for `RpcEndpointSpec`/`StreamEndpointSpec` -- `GrpcEndpointSpec` already
    has its own, unrelated `request`/`response` fields (proto FQCN strings, always
    present) that must never be read as this dual-shape migration signal. A naive
    `getattr(self.spec, 'request', None)` check would treat every existing gRPC endpoint
    as already-migrated and require `meta` immediately. gRPC's own meta requirement is
    not enforced here.

    The two branches use different field sets on purpose, not a copy-paste of the same
    pair: `StreamEndpointSpec` has no `response` field at all (it uses `request`/
    `parameters`/`payload`), and a stream endpoint migrated with only `payload` set (the
    legitimate push-only-stream shape, rule 11) would silently skip the meta requirement
    under the reused-pair version. Each branch must mirror its own spec class's
    `_one_shape` field set."""
    spec = self.spec
    if isinstance(spec, RpcEndpointSpec):
      new_shape = spec.request is not None or spec.response is not None
    elif isinstance(spec, StreamEndpointSpec):
      new_shape = spec.request is not None or spec.parameters is not None or spec.payload is not None
    else:
      return self
    if new_shape and self.meta is None:
      raise ValueError('meta is required on a request/response-shaped endpoint')
    return self

  @property
  def transports(self) -> list[str]:
    """Transports this operation is reachable over. Always `['ws']` for a stream, always
    `[]` for a grpc endpoint (gRPC is not one of the `transports` literal's values)."""
    if isinstance(self.spec, RpcEndpointSpec):
      return self.spec.transports
    if isinstance(self.spec, StreamEndpointSpec):
      return ['ws']
    return []

  @property
  def path(self) -> str | None:
    if isinstance(self.spec, RpcEndpointSpec):
      return self.spec.path
    return None

  @property
  def method(self) -> str | None:
    if isinstance(self.spec, RpcEndpointSpec):
      return self.spec.method
    return None

  @property
  def channel(self) -> str | None:
    if isinstance(self.spec, StreamEndpointSpec):
      return self.spec.channel
    if isinstance(self.spec, RpcEndpointSpec) and 'ws' in self.spec.transports:
      return self.spec.path
    return None

  @property
  def openapi(self) -> Operation | None:
    """Return the OpenAPI-style operation metadata, or None for a grpc endpoint or new-shape endpoint."""
    if isinstance(self.spec, GrpcEndpointSpec):
      return None
    if isinstance(self.spec, RpcEndpointSpec):
      return self.spec.openapi
    if isinstance(self.spec, StreamEndpointSpec):
      return self.spec.openapi
    return None

  @property
  def request(self) -> dict[str, Any] | None:
    """Return the request JSON Schema for RPC endpoints, None otherwise."""
    if isinstance(self.spec, RpcEndpointSpec):
      return self.spec.request
    return None

  @property
  def response(self) -> dict[str, Any] | None:
    """Return the response JSON Schema for RPC endpoints, None otherwise."""
    if isinstance(self.spec, RpcEndpointSpec):
      return self.spec.response
    return None

  @property
  def redacted_names(self) -> frozenset[str]:
    """Redacted key names normalized to a frozenset, empty when none are declared."""
    return frozenset(self.redacted) if self.redacted else frozenset()

  def resolved_function(self, endpoint_path: Path, spec_root: Path) -> str:
    """
    Return the function path, preferring the authored `function` field when set.

    Falls back to deriving the path from the endpoint's directory position when `function`
    is None.

    Args:
      endpoint_path: Path to the endpoint's own `endpoint.json`.
      spec_root: The client's `spec/` directory.

    Returns:
      The endpoint's function path as a dotted string.
    """
    if self.function is not None:
      return self.function
    return directory_function(endpoint_path, spec_root)
