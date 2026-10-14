"""Serialization-only dispatch for tolerant wire-record unions.

Pydantic's ordinary union serializer accepts the first extra-allowing TypedDict,
which can bypass a later member's field serializers. Dispatch to a compiled member
serializer while leaving the validation schema and the input values untouched.
"""
from collections.abc import Iterator
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID

from pydantic_core import CoreSchema, SchemaSerializer, core_schema
from typing_extensions import Any, Literal, TypeGuard


_PYTHON_TYPES = {
  'str': str, 'bytes': bytes, 'bool': bool, 'int': int, 'float': float,
  'decimal': Decimal, 'datetime': datetime, 'date': date, 'time': time,
  'timedelta': timedelta, 'uuid': UUID,
}


def _copy(value: Any) -> Any:
  # Copy schema containers only, never application classes or validator instances.
  if isinstance(value, dict):
    return {key: _copy(item) for key, item in value.items()}
  if isinstance(value, list):
    return [_copy(item) for item in value]
  if isinstance(value, tuple):
    return tuple(_copy(item) for item in value)
  return value


# One pending step of the iterative score walk: visit a (schema, value) pair, or
# finish a node once its children's scores are on the stack.
_Visit = tuple[Literal['visit'], Any, Any]
_Finish = tuple[Literal['finish'], tuple[int, int], Any, str, int]


def _is_schema(value: Any) -> TypeGuard[CoreSchema]:
  return isinstance(value, dict) and isinstance(value.get('type'), str)


def _nodes(value: Any) -> Iterator[CoreSchema]:
  if isinstance(value, dict):
    if _is_schema(value):
      yield value
    for key, item in value.items():
      if key != 'serialization':
        yield from _nodes(item)
  elif isinstance(value, (list, tuple)):
    for item in value:
      yield from _nodes(item)


def _choice(value):
  return value[0] if isinstance(value, tuple) else value


class _Members:
  def __init__(self, choices, definitions):
    self.choices = choices
    self.definitions = definitions
    self.serializers = []

  def score(self, schema, value, seen):
    """Count declared fields and type exactness without Python recursion.

    No coercion, default, constraint or user validator is run. Unknown fields are
    not traversed. Active schema/value pairs detect cycles; a stack holds partial
    sums so valid deeply recursive wire values do not consume Python call frames.
    """
    active = set(seen)
    work: list[_Visit | _Finish] = [('visit', schema, value)]
    scores = []
    while work:
      task = work.pop()
      if task[0] == 'finish':
        _, pair, base, operation, count = task
        children = scores[-count:] if count else []
        if count:
          del scores[-count:]
        if operation == 'max':
          result = max((score for score in children if score is not None), default=None)
        elif base is None or any(score is None for score in children):
          result = None
        else:
          result = (base[0] + sum(score[0] for score in children),
                    min([base[1], *(score[1] for score in children)]))
        scores.append(result)
        active.remove(pair)
        continue
      _, schema, value = task
      pair = (id(schema), id(value))
      if pair in active:
        scores.append(None)
        continue
      active.add(pair)
      base, children, operation = self._shape(schema, value)
      work.append(('finish', pair, base, operation, len(children)))
      work.extend(('visit', child, item) for child, item in reversed(children))
    return scores[0]

  def _shape(self, schema, value):
    """Describe one node's score and edges; serializers still own conversion."""
    exact, mismatch = ((0, 2), [], 'sum'), (None, [], 'sum')
    kind = schema['type']
    if kind == 'definition-ref':
      return (0, 2), [(self.definitions[schema['schema_ref']], value)], 'sum'
    if kind in ('definitions', 'function-before', 'function-after', 'function-wrap', 'default'):
      return (0, 2), [(schema['schema'], value)], 'sum'
    if kind == 'nullable':
      return exact if value is None else ((0, 2), [(schema['schema'], value)], 'sum')
    if kind == 'none':
      return exact if value is None else mismatch
    if kind == 'any':
      return (0, 0), [], 'sum'
    if kind == 'literal':
      return exact if any(type(value) is type(item) and value == item for item in schema['expected']) else mismatch
    if kind in _PYTHON_TYPES:
      expected = _PYTHON_TYPES[kind]
      # bool is not an integer field. Other supported subclasses match, with
      # lower exactness than the declared native type; conversions still belong
      # to the selected serializer, not this matcher.
      if kind in ('int', 'float') and isinstance(value, bool):
        return mismatch
      if isinstance(value, expected):
        return (0, 2 if type(value) is expected else 1), [], 'sum'
      if kind == 'float' and isinstance(value, int):
        return (0, 0), [], 'sum'
      return mismatch
    if kind in ('is-instance', 'enum', 'model', 'dataclass'):
      return exact if isinstance(value, schema['cls']) else mismatch
    if kind in ('union', 'tagged-union'):
      choices = schema['choices'].values() if kind == 'tagged-union' else schema['choices']
      return (0, 2), [(_choice(choice), value) for choice in choices], 'max'
    if kind == 'json-or-python':
      return (0, 2), [(schema['python_schema'], value)], 'sum'
    if kind == 'lax-or-strict':
      return (0, 2), [(schema['strict_schema'], value)], 'sum'
    if kind == 'chain':
      return (0, 2), [(schema['steps'][-1], value)], 'sum'
    if kind == 'typed-dict':
      if not isinstance(value, dict):
        return mismatch
      children = []
      for name, field in schema['fields'].items():
        if name not in value:
          if field.get('required', True):
            return mismatch
          continue
        children.append((field['schema'], value[name]))
      return (len(children), 2), children, 'sum'
    if kind in ('list', 'set', 'frozenset'):
      if not isinstance(value, {'list': list, 'set': set, 'frozenset': frozenset}[kind]):
        return mismatch
      children = [(schema.get('items_schema', {'type': 'any'}), item) for item in value]
    elif kind == 'dict':
      if not isinstance(value, dict):
        return mismatch
      keys = schema.get('keys_schema', {'type': 'any'})
      values = schema.get('values_schema', {'type': 'any'})
      children = [(keys, key) for key in value] + [(values, item) for item in value.values()]
    elif kind == 'tuple':
      if not isinstance(value, tuple):
        return mismatch
      items = schema['items_schema']
      variadic = schema.get('variadic_item_index')
      if variadic is None:
        if len(items) != len(value):
          return mismatch
      else:
        if len(value) < len(items) - 1:
          return mismatch
        items = items[:variadic] + [items[variadic]] * (len(value) - len(items) + 1) + items[variadic + 1:]
      children = list(zip(items, value))
    else:
      return mismatch
    return (0, 2), children, 'sum'

  def __call__(self, value):
    best, best_score = None, (-1, -1)
    for index, choice in enumerate(self.choices):
      score = self.score(choice, value, set())
      if score is not None and score > best_score:
        best, best_score = index, score
    return best

  def serialize(self, value, handler, info):
    index = self(value)
    if index is None:
      # Preserve Pydantic's warning/error behavior for invalid or unsupported input.
      return handler(value)
    # Calling a selected member directly is intentional: a tagged-union serializer
    # retries other members after a conversion fails, which could bypass the error.
    return self.serializers[index].to_python(
      value, mode=info.mode, include=info.include, exclude=info.exclude,
      by_alias=info.by_alias, exclude_unset=info.exclude_unset,
      exclude_defaults=info.exclude_defaults, exclude_none=info.exclude_none,
      round_trip=info.round_trip, context=info.context,
    )


def wire_serializer(schema) -> SchemaSerializer:
  """Compile a cached serialization schema; validation uses the original schema.

  Only unions reaching an extra-allowing TypedDict need dispatch. Existing explicit
  serializers and tagged/discriminated unions retain their own selection behavior.
  """
  schema = _copy(schema)
  nodes = list(_nodes(schema))
  definitions = {node['ref']: node for node in nodes if 'ref' in node}

  def has_record(node, seen):
    if id(node) in seen:
      return False
    seen = seen | {id(node)}
    for child in _nodes(node):
      if child['type'] == 'typed-dict' and child.get('extra_behavior') == 'allow':
        return True
      if child['type'] == 'definition-ref' and has_record(definitions[child['schema_ref']], seen):
        return True
    return False

  members = []
  for node in nodes:
    if node['type'] != 'union' or 'serialization' in node or not has_record(node, set()):
      continue
    choices = [_choice(choice) for choice in node['choices']]
    member = _Members(choices, definitions)
    members.append(member)
    node['serialization'] = core_schema.wrap_serializer_function_ser_schema(member.serialize, info_arg=True)
  # Resolve every reference after installing all nested dispatchers. Compiling these
  # once avoids schema construction (and validation) on each dump.
  for member in members:
    member.serializers = [SchemaSerializer(core_schema.definitions_schema(
      core_schema.definition_reference_schema(choice['ref']) if 'ref' in choice else choice,
      list(definitions.values()),
    )) for choice in member.choices]
  return SchemaSerializer(schema)
