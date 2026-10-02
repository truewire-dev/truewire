import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing_extensions import Annotated, Any, Literal

from truewire.generation.schema import Operation
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter, model_validator


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
  strictly-dotted-key rule left: most `seek`-strategy rows are positional tuples (a
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


_ORDINALS = ('first', 'second', 'third', 'fourth', 'fifth', 'sixth', 'seventh', 'eighth', 'ninth', 'tenth')


def _element_prose(index: int) -> str:
  """`0` -> "first element", `-1` -> "last element", `-2` -> "second-to-last element"."""
  if 0 <= index < len(_ORDINALS):
    return f'{_ORDINALS[index]} element'
  if index == -1:
    return 'last element'
  if -len(_ORDINALS) <= index < -1:
    return f'{_ORDINALS[-index - 1]}-to-last element'
  return f'element {index}' if index >= 0 else f'element {-index} from the end'


def last_row_field_prose(path: str, *, value: bool = False) -> str:
  """
  Name the row field a `LastRowPath` reads in a generated doc, without its spec-path syntax.

  A named field reads as itself in backticks (`` `properties.timestamp` ``); an index reads
  as the element of the row it is (`[-1][0]` is "first element", `[-1][-1]` "last element",
  `[-1].legs[2].id` "`legs`'s third element's `id`"), since `[0]` means nothing to a caller
  of the generated method; a bare `[-1]` is the row itself ("row").

  Args:
    path: A `LastRowPath`, already validated -- always starts with `[-1]`.
    value: Name the field's value rather than the field (`` `properties.timestamp` value ``,
      and "value" for the row itself); an element already reads as a value.
  """
  field = last_row_field(path)
  if not field:
    return 'value' if value else 'row'
  if '[' not in field:
    return f'`{field}` value' if value else f'`{field}`'
  groups: list[str] = []
  keys: list[str] = []
  for kind, segment in path_segments(field):
    if kind == 'key':
      keys.append(str(segment))
      continue
    if keys:
      groups.append(f'`{".".join(keys)}`')
      keys = []
    groups.append(_element_prose(int(segment)))
  if keys:
    groups.append(f'`{".".join(keys)}`')
  return "'s ".join(groups)


def seek_move_prose(
  path: str, *, descending: bool, ordered: bool, cap: bool | str, span: bool,
) -> str:
  """
  Say where a `seek` walk moves its bound after each page, as its runtime does: the phrase
  after "moving `<bound>`" in a generated walker's doc, shared by every backend.

  A page holding as many rows as the cap moves the bound to its extreme key; with no cap
  known, every page that brings a new key does; with a span, any other page moves it to the
  edge of the range it requested. An unordered key (a plain string id) is the last row's.

  Args:
    path: The cursor's `LastRowPath`.
    descending: Whether the walk moves backwards (to the earliest key).
    ordered: Whether keys compare by order; `False` takes the last row's in wire order.
    cap: `True` when a row cap always resolves, `False` when none ever does, or the name of
      the size parameter the cap is known from only while the caller sets it.
    span: Whether the walk requests the range in spans.
  """
  field = last_row_field_prose(path)
  bare = not last_row_field(path)
  extreme = 'earliest' if descending else 'latest'
  if not ordered:
    target = 'the last row' if bare else f'the {field} of the last row'
  elif bare or '[' not in last_row_field(path):
    target = f'the {extreme} {field}'
  else:
    target = f'the {extreme} {field} among the rows'
  if cap is True:
    moves, other = ' that came back full', 'a short one'
  elif cap is False:
    moves, other = '', 'a page that brings nothing new'
  else:
    moves, other = f' that came back full, or of every page while `{cap}` is unset', 'any other page'
  prose = f'to {target} of each page{moves}'
  if span:
    prose += f', and to the edge of the range it requested after {other}'
  return prose


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
"""Dotted key naming one field of the wire frame, or `''` for the frame itself. On an rpc
endpoint it selects, inside the response schema (which describes the whole frame, ADR
0010), the value the generated method returns -- see `select_schema`."""


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
  The row field a `seek` walk reads its next request bound from, and whether that field is
  unique per row (ADR 0013).
  """

  field: LastRowPath
  """`[-1]<...>`, naming a field of one row of the collection `SeekPagination.rows` names
  (or of the payload itself, when `rows` is unset), in the `docs/pagination.md` §3 grammar:
  `[-1][0]` for a candle's open time at tuple position 0, `[-1].id` for a named id."""
  unique: bool
  """
  Whether `field` is unique per row. A candle's open time and a row id are; a fill or
  funding timestamp is not (many rows share one millisecond).

  Decides how the walk deduplicates the boundary rows it re-fetches: by key when unique
  (a row whose other fields change between requests, an open candle, is never mistaken for
  a missing one), by whole-row content otherwise. A non-unique field also needs a
  resolvable row cap (`SeekPagination.cap`, or a `size` with a schema `default`) so the
  walk can tell "all rows share one key and the page is full" from "exhausted" -- the
  audit enforces it. Required, never defaulted: it is a venue fact the author has to state.
  """


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
  Response path of the collection the generated `PaginatedResponse` yields per page.

  Omit it when the payload is itself the collection. Every paged method extracts exactly
  this field (ADR 0013), so a wrapped payload has to name it -- see `ShortPageDone.rows`
  for why the wrapper cannot be guessed.
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


class AbsentCursorDone(PaginationModel):
  """Termination when a response carries no next token."""

  kind: Literal['absent_cursor'] = 'absent_cursor'
  rows: ResponsePath | None = None
  """
  Response path of the collection the generated `PaginatedResponse` yields per page.

  Omit it when the payload is itself the collection. Every paged method extracts exactly
  this field (ADR 0013) -- declaring which field it is, rather than guessing, is the same
  reasoning `ShortPageDone.rows`/`EmptyDone.rows` already state for their own shape.
  """


class SeekBound(PaginationModel):
  """
  The request parameters bounding the range a `seek` walk covers: a lower bound, an upper
  bound, or both. hyperliquid's `startTime`/`endTime`, bitget's lone `idLessThan`, mexc's
  lone `fromId`.
  """

  start: str | None = None
  """Parameter carrying the range's lower bound, exactly as the operation declares it."""
  end: str | None = None
  """Parameter carrying the range's upper bound, exactly as the operation declares it."""

  @model_validator(mode='after')
  def at_least_one_distinct(self) -> 'SeekBound':
    """
    Reject a bound naming no parameter, or naming one parameter twice.

    Raises:
      ValueError: When neither bound is declared, or both name the same parameter.
    """
    if self.start is None and self.end is None:
      raise ValueError('a seek walk needs at least one of `bound.start`/`bound.end`')
    if self.start is not None and self.start == self.end:
      raise ValueError(
        f'the bounds are both `{self.start}`; a range is bounded by two distinct parameters'
      )
    return self


class SeekSpan(PaginationModel):
  """
  The widest range one request may cover, for a venue that refuses a wide range outright
  rather than truncating it (coinbase's candles: at most 350 rows per request, an error
  past that). Purely a generated-code keyword: the venue never sees it, it only ever
  appears baked into the request's two bounds. Declaring it makes both bounds required on
  the generated method, since a span has to know where the caller's own range ends.
  """

  parameter: str
  """Name the generated `_paged` method's own span keyword takes, so a caller can widen or
  narrow it per call."""
  default: int = Field(gt=0)
  """Span used when the caller doesn't override `parameter`, in `unit`. Never invented:
  declare only what the venue documents, the same discipline as a `size` default."""
  unit: Literal['us', 'ms', 's']
  """Unit `default` (and the keyword) is expressed in. Read only when the bounds render as
  `datetime`, where it names the `timedelta` unit; an integer-valued bound (a block
  height, an id) takes the span as a bare count of its own ticks instead."""


class SeekFar(PaginationModel):
  """
  An exclusive parameter the walk enforces itself instead of sending: the caller's value,
  compared with a field of every row. aster's `endTime`, checked against each trade's
  `time` once the walk has moved on to `fromId` and may no longer send it.
  """

  parameter: str
  """The exclusive parameter carrying the caller's far bound, one of `parameters`."""
  field: LastRowPath
  """`[-1]<...>`, the row field compared with `parameter`'s value, in the same grammar as
  `SeekCursor.field`. A row whose field lies past the caller's value (after it, walking
  forwards) is dropped, and the walk ends on the page that held it."""


class SeekExclusive(PaginationModel):
  """
  Request parameters the venue refuses alongside the moving bound (ADR 0013): aster's
  `userTrades` answers `startTime`/`endTime` together with `fromId` with an error. The walk
  sends them on its first request only, while it has no position of its own; every later
  request carries the moving bound instead.
  """

  parameters: list[str] = Field(min_length=1)
  """The refused parameters, exactly as the operation declares them."""
  first: str | None = None
  """The one of `parameters` a walk has to start from when the caller gives no moving
  bound, because without it the venue answers from the end of the range the walk cannot
  move away from. `None` when the venue's own default start is walkable."""
  far: SeekFar | None = None
  """The one of `parameters` that caps the walk, enforced on the rows rather than sent."""

  @model_validator(mode='after')
  def names_its_own_parameters(self) -> 'SeekExclusive':
    """
    Reject a repeated parameter, or a `first`/`far.parameter` outside `parameters`.

    Raises:
      ValueError: When a parameter is listed twice, or `first`/`far.parameter` is not one
        of `parameters`.
    """
    if len(set(self.parameters)) != len(self.parameters):
      raise ValueError('`exclusive.parameters` lists a parameter twice')
    for location, name in (('first', self.first), ('far.parameter', self.far and self.far.parameter)):
      if name is not None and name not in self.parameters:
        raise ValueError(
          f'`exclusive.{location}` is `{name}`, which is not one of `exclusive.parameters`'
        )
    return self


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


class SeekPagination(PaginationModel):
  """
  Pagination by a bound read off the rows of the previous page (ADR 0013): the one
  strategy for every walk whose next request bound comes out of the data rather than out
  of a counter or a venue-issued token -- candles bounded by a time range (bybit, binance,
  bitget, coinbase, kucoin, mexc), fills bounded by a time cursor (hyperliquid), rows
  bounded by an id (bitget's `idLessThan`, mexc's `fromId`) or a block height (dYdX).

  The venue fact that decides everything is `anchor`: which of the two bounds the venue
  fills from when a range holds more rows than it returns. The walk moves that bound to
  the extreme `cursor.field` value it has seen and re-requests; the other bound, when the
  caller gives one, caps the walk. There is no declared direction, step, or inclusivity:
  the direction is the anchor, and re-fetched boundary rows are deduplicated rather than
  stepped over. See `docs/pagination.md` §2.4 for the exact algorithm.
  """

  strategy: Literal['seek'] = 'seek'
  cursor: SeekCursor
  """Row field the next bound is read from, and whether it is unique per row."""
  bound: SeekBound
  """Request parameter(s) bounding the range: the anchored one moves, the other caps."""
  anchor: Literal['start', 'end']
  """
  Which bound the venue keeps rows adjacent to when it truncates. `end` means a range
  holding more rows than the cap comes back as the newest ones (bybit, coinbase, kucoin
  spot, bitget); `start` means the oldest ones (binance, mexc, kucoin futures klines). A
  measured fact, never read off documentation: request a range far wider than one page
  with a small explicit size and see which end the rows cluster at (ADR 0013). The walk
  can only move the anchored bound safely, so this also fixes the order pages arrive in.
  """
  size: PaginationParameter | None = None
  """Request parameter carrying the page size, when the venue lets a caller set one. Its
  schema `default` is what resolves the row cap a full page is measured against."""
  cap: int | None = Field(default=None, gt=0)
  """
  The venue's fixed maximum rows per request, when `size` cannot resolve one -- no size
  parameter at all (hyperliquid), or one with no documented default. Declaring it larger
  than the truth turns a full page into a false "exhausted" and silently drops rows.
  """
  span: SeekSpan | None = None
  """Widest range one request may cover, for a venue that refuses a wide range rather
  than truncating it -- see `SeekSpan`. `None` for every venue that truncates."""
  exclusive: SeekExclusive | None = None
  """Parameters the venue refuses alongside the moving bound, sent on the first request
  only -- see `SeekExclusive`. `None` for every venue that takes them together."""
  rows: ResponsePath | None = None
  """
  Response path of the row collection. Omit it when the payload is itself the collection.
  It is not optional decoration: a venue that wraps its rows (bybit's `{category, symbol,
  list}`) gives the walk nothing to read unless the wrapper key is named, and locating it
  by looking for the one array property is the inference this declaration replaces.
  """

  @model_validator(mode='after')
  def anchor_names_a_bound(self) -> 'SeekPagination':
    """
    Reject an `anchor` naming a bound the declaration doesn't carry, or a `span` without
    both bounds to confine it.

    Raises:
      ValueError: When `anchor` names an undeclared bound, or `span` is declared with
        only one bound.
    """
    if getattr(self.bound, self.anchor) is None:
      raise ValueError(
        f'`anchor` is `{self.anchor}` but `bound.{self.anchor}` is not declared; the anchor '
        f'is the bound the walk moves, so it has to exist'
      )
    if self.span is not None and (self.bound.start is None or self.bound.end is None):
      raise ValueError(
        '`span` needs both `bound.start` and `bound.end`: a span is measured from the '
        'moving bound towards the far one, so both have to be declared'
      )
    if self.exclusive is not None and self.span is not None:
      raise ValueError(
        '`exclusive` with `span` has no walker: a span re-sends both bounds on every request, '
        'while an exclusive parameter is sent on the first one only'
      )
    if self.exclusive is not None:
      walked = {
        name for name in (self.bound.start, self.bound.end, self.size and self.size.parameter)
        if name is not None
      }
      clash = sorted(walked & set(self.exclusive.parameters))
      if clash:
        raise ValueError(
          f'`exclusive.parameters` names {", ".join(f"`{name}`" for name in clash)}, which '
          f'the walk sends on every request; an exclusive parameter is one it stops sending'
        )
    return self

  @property
  def moving(self) -> str:
    """Parameter name of the bound the walk moves: the anchored one."""
    bound = self.bound.start if self.anchor == 'start' else self.bound.end
    assert bound is not None, 'validated by anchor_names_a_bound'
    return bound

  @property
  def far(self) -> str | None:
    """Parameter name of the bound that caps the walk, when one is declared."""
    return self.bound.end if self.anchor == 'start' else self.bound.start

  @property
  def descending(self) -> bool:
    """Whether the walk moves towards older rows: the venue fills from `end`."""
    return self.anchor == 'end'


class OffsetPagination(PaginationModel):
  """Pagination by row offset."""

  strategy: Literal['offset'] = 'offset'
  offset: PaginationParameter
  """Request parameter carrying the row offset."""
  size: PaginationParameter | None = None
  """Request parameter carrying the page size, when the API lets a caller set one."""
  done: IndexedDone
  """How the walk ends."""


Pagination = Annotated[
  PagePagination | TokenPagination | OffsetPagination | SeekPagination,
  Field(discriminator='strategy'),
]
"""
How an endpoint pages, stated rather than inferred (ADR 0004, ADR 0013).

Every `ResponsePath` admits bracket indices alongside dotted keys (`docs/pagination.md`
§3), but only one shape actually reaches into a row this way: `seek`'s `cursor.field`,
resolved relative to the walk's own row collection via `LastRowPath`'s narrow `[-1]<...>`
requirement. `page`/`token`/`offset` read only plain top-level (or nested-object,
non-array) response fields.
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


def select_schema(
  schema: dict[str, Any], path: str, *, shared: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
  """
  The sub-schema a `PayloadPath` names inside a wire-body schema (ADR 0010).

  Walks `path` the way `read_dotted_path` walks a value: a key segment steps through
  `properties`, an index segment through `prefixItems` (a positional row, rule 4) or a
  homogeneous array's `items`. A `$ref` met on the way is resolved against `shared`
  (`spec/schemas.json`'s raw schemas, keyed by id) when given; without it, or for an id
  `shared` lacks, the walk cannot continue and says so rather than guess. An `anyOf` on
  the way is refused for the same reason: the endpoint alone cannot say which branch
  carries the field.

  Args:
    schema: Response schema, as written in `endpoint.json`.
    path: A validated `PayloadPath`; `''` returns `schema` itself.
    shared: Raw shared schemas, for resolving a `$ref` on the path.

  Returns:
    The schema node at `path`, as written (a `$ref` node is returned unresolved, so a
    caller can render it as the reference it is).

  Raises:
    LookupError: When a segment names nothing the schema declares, or the walk meets a
      node it cannot step into; the message names the segment.
  """
  node: Any = schema
  walked: list[str] = []
  for kind, key in path_segments(path):
    label = f'[{key}]' if kind == 'index' else str(key)
    if isinstance(node, dict) and isinstance(node.get('$ref'), str):
      ref = node['$ref']
      if shared is None or ref not in shared:
        raise LookupError(
          f'`{ref}` is a `$ref` the walk to `{label}` cannot resolve'
          + ('' if shared is None else ' (no such shared schema)')
        )
      node = shared[ref]
    if not isinstance(node, dict):
      raise LookupError(f'`{".".join(walked) or "<root>"}` is not a schema object')
    if node.get('anyOf'):
      raise LookupError(f'`{".".join(walked) or "<root>"}` is an `anyOf`; which branch carries `{label}` is undecidable')
    if kind == 'index':
      prefix_items = node.get('prefixItems')
      if isinstance(prefix_items, list):
        index = key if key >= 0 else len(prefix_items) + key  # type: ignore[operator]
        if not (0 <= index < len(prefix_items)):
          raise LookupError(f'`{label}` is outside the {len(prefix_items)} positions `prefixItems` declares')
        node = prefix_items[index]
      else:
        items = node.get('items')
        if not isinstance(items, dict):
          raise LookupError(f'`{".".join(walked) or "<root>"}` declares no `items` to index with `{label}`')
        node = items
    else:
      properties = node.get('properties')
      if not isinstance(properties, dict) or key not in properties:
        declared = ', '.join(f'`{name}`' for name in properties) if isinstance(properties, dict) and properties else 'no properties'
        raise LookupError(f'`{label}` is not a property of `{".".join(walked) or "<root>"}`, which declares {declared}')
      node = properties[key]
    walked.append(label)
  if not isinstance(node, dict):
    raise LookupError(f'`{path}` names a non-schema value')
  return node


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
  `''` when the whole frame already is that value. For an rpc endpoint the response schema
  describes the whole frame and this path selects the returned value inside it (ADR 0010):
  the generator types the method's return value from the schema at this path, and
  `truewire check` validates a recording against the whole schema, extracting nothing."""
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


class PositionalSpread(BaseModel):
  """Positional slots holding an array-valued request property's elements, one slot each
  (`[tx1, tx2]` for `transactions`)."""

  model_config = ConfigDict(extra='forbid')
  spread: str


class PositionalFold(BaseModel):
  """One positional slot holding an object built from the listed request properties that
  are present (`{pageKey?, maxCount?}`). Absent when none of them is."""

  model_config = ConfigDict(extra='forbid')
  fold: list[str] = Field(min_length=1)


PositionalSlot = str | PositionalSpread | PositionalFold
"""One entry of `RpcEnvelopeSpec.positional`: a request property name (its value fills the
slot), a `{"spread": name}`, or a `{"fold": [names]}`."""

_POSITIONAL_SLOT: TypeAdapter[PositionalSlot] = TypeAdapter(PositionalSlot)


def positional_slot_names(slot: PositionalSlot) -> list[str]:
  """The request property names one positional slot reads."""
  if isinstance(slot, str):
    return [slot]
  if isinstance(slot, PositionalSpread):
    return [slot.spread]
  return list(slot.fold)


def positional_params(slots: Sequence[PositionalSlot], request: Mapping[str, Any] | None) -> list[Any]:
  """Pack a flat request dict into the positional `params` array `slots` declares.

  A named slot whose property is absent, and a fold with none of its properties present,
  are absent: trailing absent slots are dropped, any other becomes `null`. A spread of an
  absent property contributes no slots.

  Args:
    slots: The endpoint's `envelope.positional`.
    request: The recorded (or wire-ready) flat request dict.
  """
  request = request or {}
  absent = object()
  out: list[Any] = []
  for raw_slot in slots:
    slot: PositionalSlot = raw_slot if isinstance(raw_slot, str) else _POSITIONAL_SLOT.validate_python(raw_slot)
    if isinstance(slot, str):
      out.append(request.get(slot, absent))
    elif isinstance(slot, PositionalSpread):
      value = request.get(slot.spread, absent)
      if value is not absent:
        out.extend(value if isinstance(value, list) else [value])
    else:
      folded = {name: request[name] for name in slot.fold if name in request}
      out.append(folded if folded else absent)
  while out and out[-1] is absent:
    out.pop()
  return [None if value is absent else value for value in out]


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
  positional: list[PositionalSlot] | None = None
  """How the flat `request` is packed into a positional `params` array, one entry per wire
  slot in order (see `PositionalSlot`, `positional_params`). `None`, the default, is the
  whole request object as the one argument, `[request]`. `truewire mock` matches a JSON-RPC
  call against the array this builds from the recorded request; every name must be a
  declared `request` property, used once."""


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
  subscribe_channel: str | None = None
  """The channel identity the subscribe frame names at `channel`, when it differs from
  `spec.channel`, the channel the pushes arrive on. Hyperliquid subscribes with
  `{"subscription": {"type": "userEvents"}}` and pushes on `user`. A template like
  `spec.channel`: `{name}` placeholders are filled from the example's `parameters`. Read only
  by the mock's subscribe and unsubscribe matching; `None` means the frame names `spec.channel`."""
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


def rpc_selector(envelope: 'EnvelopeSpec | None') -> str:
  """Dotted path naming an RPC-shaped frame's operation, declared or JSON-RPC's default.

  The one canonical rule, read by the mock server when it routes an incoming frame to the
  example that recorded it, and by `truewire capture` when it picks the exchange belonging
  to the endpoint being captured out of everything the core sent.

  Args:
    envelope: The endpoint's `envelope` block, or `None` when it declares none.
  """
  if isinstance(envelope, RpcEnvelopeSpec) and envelope.selector is not None:
    return envelope.selector
  return 'method'


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
  reply: dict[str, Any] | None = None
  """JSON Schema for the subscribe acknowledgement's own value, replacing
  `openapi.responses['reply']` (ADR 0014). Describes what the client core hands back as
  `Stream.reply` -- the reply frame *after* `envelope.reply_payload` (or `envelope.payload`)
  extraction, exactly as `payload` describes a pushed message after `envelope.payload`
  extraction. Optional: a venue whose ack is shaped like its pushes, or carries nothing
  worth typing, leaves it out and the generated method's reply slot stays `Any`."""
  description: str | None = None
  """Operation docstring, for the new-shape path -- there is no `openapi.description` to
  reuse (mirrors `GrpcEndpointSpec.description`)."""

  @property
  def new_shape(self) -> bool:
    """Whether this spec is authored in the `parameters`/`payload`/`reply` shape (design
    §8) rather than the legacy `openapi` one -- the one predicate every dual-shape gate
    (`_one_shape`, `Endpoint._require_meta_for_new_shape`, `operation_json`) shares."""
    return (
      self.request is not None or self.parameters is not None
      or self.payload is not None or self.reply is not None
    )

  @model_validator(mode='after')
  def _one_shape(self) -> 'StreamEndpointSpec':
    """Exactly one of `openapi` or `request`/`parameters`/`payload`/`reply` is set -- never both, never neither."""
    if self.new_shape == (self.openapi is not None):
      raise ValueError(
        'StreamEndpointSpec must declare exactly one of `openapi` (legacy) or '
        '`request`/`parameters`/`payload`/`reply`'
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


class MatchSpec(BaseModel):
  """Declared request-matching rules for `truewire mock` (ADR 0018, ADR 0019).

  `ignore` names request fields whose value is minted per call -- a signature, a signing
  timestamp, a nonce -- by a located path, so a recorded example can match a freshly signed
  request on everything else. The complement of `redacted` (ADR 0007): `redacted` strips a
  flat key name wherever the comparison is looking, which cannot reach a field nested inside
  a positional array (`{"op": "login", "args": [{"sign": ...}]}`) or tell two same-named keys
  apart; a path can.

  Paths use the response-path grammar (`dotted_path`: dotted keys plus bracket indices,
  no `$` root, no wildcard) and are rooted at the whole request value the mock compares: the
  parsed WebSocket frame, or the parsed HTTP JSON body. A query item or header is flat and is
  what `redacted` already covers. The field is removed from both the real request and the
  recorded one before comparing, so a path the recording lacks still matches; the field
  itself is not required to be present. Never read when an example is replayed through the
  real client -- like `redacted`, it only widens what the mock accepts.

  `query_arrays` states how the API reads a list in the query string (ADR 0019). The
  default, `repeat`, is one item per value (`?state=WA&state=OR`); `comma` is one item whose
  value joins them (`?state=WA,OR`, OpenAPI's `style: form, explode: false`). The mock joins
  a recorded list the declared way before comparing, so a client sending the other form is a
  422, not a match: an API that keeps only the last repeated key would drop values silently.
  """

  model_config = ConfigDict(extra='forbid')
  ignore: list[ResponsePath] | None = Field(default=None, min_length=1)
  """Located request paths the mock drops from both sides before comparing, e.g.
  `args[0].sign`, `args[0].timestamp`."""
  query_arrays: Literal['repeat', 'comma'] = 'repeat'
  """How a list-valued query field is written on the wire: `repeat` (one `key=value` per
  value) or `comma` (one `key=a,b`)."""

  @model_validator(mode='after')
  def declares_a_rule(self) -> 'MatchSpec':
    if self.ignore is None and 'query_arrays' not in self.model_fields_set:
      raise ValueError('match declares no rule. Fix: add `ignore` or `query_arrays`, or remove `match`.')
    return self


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
  match: 'MatchSpec | None' = None
  """How `truewire mock` compares a real request against this endpoint's recorded examples,
  beyond the default structural equality -- see `MatchSpec`, ADR 0018 and ADR 0019."""
  docs: str | None = None
  notes: list[str] | None = None
  spec: EndpointSpec
  pagination: Pagination | None = None
  envelope: EnvelopeSpec | None = None
  """
  How this endpoint's raw wire response is unwrapped into what the client core returns,
  when the core does. A sibling of `spec`/`pagination` for the same reason: OpenAPI has
  nothing to say about it, and it is generation/validation metadata. The response schema
  describes the whole wire frame either way (`docs/spec/authoring.md` rule 6, ADR 0010);
  `payload` selects the returned value inside it. Left unset on any endpoint whose core
  returns the frame itself (rule 6's "keep the envelope" branch) -- most endpoints,
  including every REST-passthrough one.
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
    `parameters`/`payload`/`reply`, folded into its own `new_shape` predicate), and a
    stream endpoint migrated with only `payload` set (the legitimate push-only-stream
    shape, rule 11) would silently skip the meta requirement under the reused-pair
    version, defeating design §2's "never omittable" for that endpoint shape. Each branch
    must mirror its own spec class's `_one_shape` field set."""
    spec = self.spec
    if isinstance(spec, RpcEndpointSpec):
      new_shape = spec.request is not None or spec.response is not None
    elif isinstance(spec, StreamEndpointSpec):
      new_shape = spec.new_shape
    else:
      return self
    if new_shape and self.meta is None:
      raise ValueError('meta is required on a request/response-shaped endpoint')
    return self

  @model_validator(mode='after')
  def _check_positional_slots(self) -> 'Endpoint':
    """`envelope.positional` names only declared `request` properties, each once, and
    spreads only an array-typed one."""
    envelope = self.envelope
    if not isinstance(envelope, RpcEnvelopeSpec) or envelope.positional is None:
      return self
    request = self.spec.request if isinstance(self.spec, RpcEndpointSpec) else None
    properties = (request or {}).get('properties')
    if not isinstance(properties, dict):
      raise ValueError('envelope.positional needs a `request` schema with flat `properties`')
    seen: set[str] = set()
    for slot in envelope.positional:
      for name in positional_slot_names(slot):
        if name not in properties:
          raise ValueError(f'envelope.positional names `{name}`, which is not a `request` property')
        if name in seen:
          raise ValueError(f'envelope.positional names `{name}` more than once')
        seen.add(name)
      if isinstance(slot, PositionalSpread):
        declared = properties[slot.spread]
        if isinstance(declared, dict) and declared.get('type', 'array') != 'array':
          raise ValueError(f'envelope.positional spreads `{slot.spread}`, which is not an array')
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

  @property
  def ignored_paths(self) -> tuple[str, ...]:
    """`match.ignore` paths, empty when none are declared (ADR 0018)."""
    return tuple(self.match.ignore) if self.match is not None and self.match.ignore else ()

  @property
  def query_arrays(self) -> Literal['repeat', 'comma']:
    """`match.query_arrays`, `repeat` when none is declared (ADR 0019)."""
    return self.match.query_arrays if self.match is not None else 'repeat'

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
