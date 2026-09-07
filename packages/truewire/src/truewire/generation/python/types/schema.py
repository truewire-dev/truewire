from typing_extensions import TypedDict, Literal as _Literal, NotRequired, Any

class Ref(TypedDict):
  type: _Literal['ref']
  id: str
  package: NotRequired[str]
  """Package to import from."""

class Literal(TypedDict):
  type: _Literal['literal']
  values: list[Any]
  id: NotRequired[str|None]

class List(TypedDict):
  type: _Literal['list']
  item: 'InlineType'
  id: NotRequired[str|None]

class Tuple(TypedDict):
  type: _Literal['tuple']
  items: list['InlineType']
  """Types of each position, in order."""
  id: NotRequired[str|None]

class Variant(TypedDict):
  type: 'InlineType'
  docstring: NotRequired[str|None]

class Union(TypedDict):
  type: _Literal['union']
  variants: list[Variant]
  id: NotRequired[str|None]

class Dict(TypedDict):
  type: _Literal['dict']
  key: 'InlineType'
  value: 'InlineType'
  id: NotRequired[str|None]

class Field(TypedDict):
  type: 'InlineType'
  required: bool
  docstring: NotRequired[str|None]

class Record(TypedDict):
  type: _Literal['record']
  id: str
  fields: dict[str, Field]
  docstring: NotRequired[str|None]

InlineType = Ref | Literal | List | Tuple | Union | Dict
Type = InlineType | Record

