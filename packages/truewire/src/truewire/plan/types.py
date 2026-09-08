"""The language-neutral type tree every backend renders from.

Eight node kinds, all plain dicts so a plan serializes to JSON as-is. A `Scalar` carries
the wire base type and the spec `format` that narrows it (`decimal-string`,
`epoch-millis`, ...); it is the one node whose rendering differs per language, so no
Python name (`Decimal`, `TimestampMillis`) ever appears here. A `Ref` names a type
defined elsewhere: a record or alias in the same `TypeSet`, or one in a shared
`schemas.json` scope.
"""
from typing_extensions import Any, Iterator, Literal as _Literal, NotRequired, TypedDict

ScalarBase = _Literal['string', 'integer', 'number', 'boolean', 'null', 'any']
"""JSON's own value kinds plus `any` for a schema that constrains nothing."""

TIMESTAMP_FORMATS: tuple[str, ...] = (
  'epoch-seconds', 'epoch-millis', 'epoch-micros', 'epoch-nanos', 'date-time', 'date',
)
"""Spec formats that render to a real timestamp/date type (`docs/spec/authoring.md` rule 3)."""

NARROWING_FORMATS: tuple[str, ...] = ('decimal-string', 'integer-string', 'boolean-string')
"""String formats that change the rendered type (rules 12, 13, 15)."""

OPAQUE_FORMATS: tuple[str, ...] = ('uuid', 'hostname', 'uri')
"""Standard string formats that document a value without narrowing its type."""

KNOWN_FORMATS: frozenset[str] = frozenset((*TIMESTAMP_FORMATS, *NARROWING_FORMATS, *OPAQUE_FORMATS))


class Scalar(TypedDict):
  type: _Literal['scalar']
  base: ScalarBase
  format: NotRequired[str]
  """One of `KNOWN_FORMATS`, when the schema declares one."""


class Ref(TypedDict):
  type: _Literal['ref']
  id: str
  """The referenced type's name, as its own `TypeSet` (or the shared scope) defines it."""
  package: NotRequired[str]
  """Where a backend imports it from; only a backend ever fills this in."""
  forward: NotRequired[bool]
  """Whether the referenced type is defined *after* the one referencing it.

  True only on a reference cycle between records, which is where a name can be used
  before it is bound: something has to be emitted first (`docs/spec/authoring.md` rule
  17). A language that binds names lazily ignores this; one that does not renders the
  reference as a forward reference. Only a backend ever fills this in -- it is a fact
  about emission order, not about the spec.
  """


class Literal(TypedDict):
  type: _Literal['literal']
  values: list[Any]
  id: NotRequired[str | None]


class List(TypedDict):
  type: _Literal['list']
  item: 'InlineType'
  id: NotRequired[str | None]


class Tuple(TypedDict):
  type: _Literal['tuple']
  items: list['InlineType']
  """Types of each position, in order."""
  id: NotRequired[str | None]


class Variant(TypedDict):
  type: 'InlineType'
  docstring: NotRequired[str | None]


class Union(TypedDict):
  type: _Literal['union']
  variants: list[Variant]
  id: NotRequired[str | None]


class Dict(TypedDict):
  type: _Literal['dict']
  key: 'InlineType'
  value: 'InlineType'
  id: NotRequired[str | None]


class Field(TypedDict):
  type: 'InlineType'
  required: bool
  docstring: NotRequired[str | None]


class Record(TypedDict):
  type: _Literal['record']
  id: str
  fields: dict[str, Field]
  docstring: NotRequired[str | None]


InlineType = Scalar | Ref | Literal | List | Tuple | Union | Dict
Type = InlineType | Record


def is_null(t: Type) -> bool:
  """Whether `t` is the `null` scalar."""
  return t['type'] == 'scalar' and t['base'] == 'null'


def is_any(t: Type) -> bool:
  """Whether `t` is the unconstrained `any` scalar."""
  return t['type'] == 'scalar' and t['base'] == 'any'


def is_optional(t: Type) -> bool:
  """Whether `t` is a union with a `null` variant (a schema-nullable value)."""
  return t['type'] == 'union' and any(is_null(v['type']) for v in t['variants'])


def strip_null(t: Type) -> Type:
  """`t` without its `null` variant: `X | null` -> `X`, `X | Y | null` -> `X | Y`, else `t`."""
  if t['type'] != 'union':
    return t
  rest = [v for v in t['variants'] if not is_null(v['type'])]
  if len(rest) == len(t['variants']):
    return t
  if len(rest) == 1:
    return rest[0]['type']
  return {'type': 'union', 'variants': rest, 'id': t.get('id')}


def has_zero_value(t: Type) -> bool:
  """Whether a backend can seed a cursor of this type with a plain zero value (`''`,
  `0`, `False`): a scalar the wire narrows no further than a builtin. A timestamp or a
  decimal has no meaningful zero; a record, list or union has none at all."""
  if t['type'] != 'scalar' or t['base'] in ('null', 'any'):
    return False
  fmt = t.get('format')
  return fmt is None or fmt in ('integer-string', 'boolean-string') or fmt in OPAQUE_FORMATS


def refs(t: Type) -> Iterator[Ref]:
  """Every `Ref` node reachable inside `t`, depth first."""
  kind = t['type']
  if kind == 'ref':
    yield t
  elif kind == 'list':
    yield from refs(t['item'])
  elif kind == 'tuple':
    for item in t['items']:
      yield from refs(item)
  elif kind == 'union':
    for variant in t['variants']:
      yield from refs(variant['type'])
  elif kind == 'dict':
    yield from refs(t['key'])
    yield from refs(t['value'])
  elif kind == 'record':
    for field in t['fields'].values():
      yield from refs(field['type'])
