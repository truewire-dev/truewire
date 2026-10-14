from functools import lru_cache
import json
from typing_extensions import TypeVar, Generic, TypedDict as _TypedDict, Any
import pydantic
from .exceptions import ValidationError
from ._serialization import wire_serializer

T = TypeVar('T')

@pydantic.with_config(pydantic.ConfigDict(extra='allow'))
class TypedDict(_TypedDict):
  """Base for every generated wire-shape `TypedDict`; tolerates undocumented fields."""


def _pointer(loc: tuple[str | int, ...]) -> str:
  """Render a pydantic `loc` as a JSON pointer, `/` for the root, as the other runtimes do.

  A union member's name (`('n', 'int')`) is a segment too: pydantic puts it in `loc`.
  """
  if not loc:
    return '/'
  return ''.join('/' + str(key).replace('~', '~0').replace('/', '~1') for key in loc)


def _describe(value: Any) -> str:
  """A short, bounded rendering of an offending value: scalars as JSON, containers by kind."""
  if isinstance(value, dict):
    return 'object'
  if isinstance(value, list | tuple):
    return 'array'
  if type(value) is str:
    return json.dumps(value if len(value) <= 40 else value[:40] + '…', ensure_ascii=False)
  if value is None or isinstance(value, bool):
    return json.dumps(value)
  # Bound integer conversion before str(): Python may reject thousands of digits.
  if type(value) is int and value.bit_length() > 133:
    return 'integer'
  # Arbitrary objects (including scalar subclasses) can raise from __str__.
  if type(value) not in (int, float):
    return 'object'
  text = str(value)
  return text if len(text) <= 40 else text[:40] + '…'


def _message(error: pydantic.ValidationError) -> str:
  """`<reason>, got <value> at <pointer>` for the first failure, counting the rest.

  `missing` and `json_invalid` name no value: the input is the enclosing object or the
  whole document, which the reason already accounts for.
  """
  errors = error.errors(include_url=False)
  if not errors:
    return str(error.title)
  first = errors[0]
  reason = first['msg']
  if first['type'] not in ('missing', 'json_invalid') and not isinstance(first.get('input'), bytes | bytearray):
    reason += f", got {_describe(first.get('input'))}"
  message = f"{reason} at {_pointer(first['loc'])}"
  if len(errors) > 1:
    message += f' (and {len(errors) - 1} more)'
  return message


@lru_cache(maxsize=None)
def _adapter(Type: type) -> pydantic.TypeAdapter:
  """Build a `TypeAdapter` for `Type`, cached so every `validator(Type)` call site after the first reuses it."""
  return pydantic.TypeAdapter(Type)


@lru_cache(maxsize=None)
def _serializer(Type: type):
  """Compile dumping once, independently of the unchanged validation adapter."""
  return wire_serializer(_adapter(Type).core_schema)


class validator(Generic[T]):
  """Pydantic-backed validator that raises `truewire_core.ValidationError` on mismatch.

  `TypeAdapter` construction is the expensive part of validating a response, so it's
  cached per `Type` rather than rebuilt on every generated `validator(Type)` call site:
  the first call against a given `Type` pays for it, every call after (including from
  other endpoints sharing the same response type) reuses the cached adapter.
  """

  def __init__(self, Type: type[T]):
    self.adapter = _adapter(Type)
    self._type = Type

  def json(self, data: str | bytes | bytearray) -> T:
    """Validate a raw JSON document."""
    try:
      return self.adapter.validate_json(data)
    except pydantic.ValidationError as e:
      raise ValidationError(_message(e)) from e

  def python(self, data: Any) -> T:
    """Validate an already-decoded Python value."""
    try:
      return self.adapter.validate_python(data)
    except pydantic.ValidationError as e:
      raise ValidationError(_message(e)) from e

  def __call__(self, data) -> T:
    if isinstance(data, str | bytes | bytearray):
      return self.json(data)
    return self.python(data)

  def dump(self, data: T) -> bytes:
    """Dump a Python value, selecting record-union member serializers without revalidation."""
    return _serializer(self._type).to_json(data)
