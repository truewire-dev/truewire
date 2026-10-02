"""Drift as a change of shape, never of value (ADR 0012).

Two sources of `drift` findings live here, both about the raw wire body:

- `schema_findings`: the live body against the endpoint's response schema, each
  violation named by what it means for a reader (`enum_value`, `type_changed`,
  `key_removed`, ...) rather than by the validator keyword alone.
- `shape_findings`: the live body against the recorded `<id>.response.json`: keys added
  and removed, type and nullability changes, array element shape. Values are never
  compared. A difference the schema already declares on both sides (an optional key
  present in one and absent in the other, a nullable field that was null last time) is
  not a finding: the schema said it could happen.

Pointers are JSON pointers into the wire body, with every array index and every map key
(an object whose keys are data, per the schema) written `*`, so the same drift on forty
elements is one finding, and the finding's fingerprint survives a new market being listed.

Nothing here puts a response value in a finding except an enum value the schema does not
know, which is the finding itself. Both sides are otherwise types, key names and
declarations.
"""

from dataclasses import dataclass, field
from typing_extensions import Any, Iterable

from .schema import JSON_TYPES, SchemaView, json_type

MAX_VALUE_CHARS = 80
"""An enum value quoted in a finding is cut here; anything longer is not a vocabulary token."""


@dataclass
class Finding:
  """One thing that is not as the recording or the schema says it should be."""
  kind: str
  """`drift` (the API changed) or `client:python` (the API is as specced, our code is not)."""
  check: str
  """What kind of difference: `key_added`, `key_removed`, `type_changed`, `nullability`,
  `enum_value`, `array_element`, `status`, `not_json`, `schema:<keyword>`, `client`."""
  pointer: str
  """JSON pointer into the wire body, `*` standing for any array index or map key."""
  expected: Any
  """What the recording or the schema says: a type list, a key's shape, the allowed values."""
  actual: Any
  """What the live response has, in the same terms."""
  against: str
  """What `expected` comes from: `recording`, `schema` or `client`."""
  message: str
  """One line for a reader. Never quotes the response beyond an unknown enum value."""
  count: int = 1
  """How many places in the body collapsed into this one pointer."""
  fingerprint: str = ''
  first_seen: str | None = None

  def key(self) -> tuple[str, str, str, str]:
    """What makes two findings the same one, within one example."""
    extra = str(self.actual) if self.check == 'enum_value' else ''
    return (self.kind, self.check, self.pointer, extra)


@dataclass
class Shape:
  """The shape of one or more JSON values at one point: types, keys, element shapes."""
  types: set[str] = field(default_factory=set)
  properties: dict[str, 'Shape'] = field(default_factory=dict)
  """Keys of an object that are declared properties, or undeclared keys."""
  entries: 'Shape | None' = None
  """The merged shape of every map entry (keys whose names are data)."""
  items: 'Shape | None' = None
  """The merged shape of every element of a homogeneous array."""
  positions: list['Shape'] | None = None
  """Per-position shapes of an array the schema declares as a tuple."""
  lengths: set[int] = field(default_factory=set)
  """Tuple lengths seen, for `array_element` findings."""

  def merge(self, other: 'Shape') -> 'Shape':
    self.types |= other.types
    for key, child in other.properties.items():
      mine = self.properties.get(key)
      self.properties[key] = child if mine is None else mine.merge(child)
    self.entries = merge_optional(self.entries, other.entries)
    self.items = merge_optional(self.items, other.items)
    if other.positions is not None:
      if self.positions is None:
        self.positions = list(other.positions)
      else:
        for index, child in enumerate(other.positions):
          if index < len(self.positions):
            self.positions[index] = self.positions[index].merge(child)
          else:
            self.positions.append(child)
    self.lengths |= other.lengths
    return self

  def summary(self) -> Any:
    """What a finding shows of this shape: its types, and an object's keys."""
    types = sorted(self.types, key=JSON_TYPES.index if self.types <= set(JSON_TYPES) else str)
    if 'object' in self.types and self.properties:
      return {'types': types, 'keys': sorted(self.properties)}
    return types


def merge_optional(left: Shape | None, right: Shape | None) -> Shape | None:
  if left is None:
    return right
  if right is None:
    return left
  return left.merge(right)


def shape_of(value: Any, view: SchemaView) -> Shape:
  """The shape of one value, read with the schema so map keys and tuples are known."""
  shape = Shape(types={json_type(value)})
  if isinstance(value, dict):
    for key, child in value.items():
      child_view, _, _ = view.property(key)
      child_shape = shape_of(child, child_view)
      if view.is_map_entry(key):
        shape.entries = merge_optional(shape.entries, child_shape)
      else:
        shape.properties[key] = child_shape
  elif isinstance(value, list):
    if view.is_tuple():
      shape.positions = [shape_of(child, view.item(index)) for index, child in enumerate(value)]
      shape.lengths = {len(value)}
    else:
      element = view.item(None)
      for child in value:
        shape.items = merge_optional(shape.items, shape_of(child, element))
  return shape


def pointer_join(pointer: str, part: str | int) -> str:
  text = str(part).replace('~', '~0').replace('/', '~1')
  return f'{pointer}/{text}'


def shape_findings(recorded: Any, live: Any, view: SchemaView) -> list[Finding]:
  """Every change of shape between the recorded body and the live one.

  Args:
    recorded: The recorded `<id>.response.json` payload.
    live: The live wire body.
    view: The response schema at the top of the body.
  """
  out: list[Finding] = []
  diff(shape_of(recorded, view), shape_of(live, view), view, '', out)
  return out


def diff(recorded: Shape, live: Shape, view: SchemaView, pointer: str, out: list[Finding]) -> None:
  if recorded.types != live.types:
    declared = view.declared_types()
    if declared is None or not (recorded.types | live.types) <= declared:
      out.append(type_finding(recorded, live, pointer))
  if 'object' in recorded.types and 'object' in live.types:
    diff_object(recorded, live, view, pointer, out)
  if 'array' in recorded.types and 'array' in live.types:
    diff_array(recorded, live, view, pointer, out)


def type_finding(recorded: Shape, live: Shape, pointer: str) -> Finding:
  was, now = recorded.types, live.types
  rest_was, rest_now = was - {'null'}, now - {'null'}
  if 'null' in was ^ now and (rest_was == rest_now or not rest_was or not rest_now):
    gained = 'null' in now
    message = 'now null where the recording was not' if gained else 'no longer null'
    return Finding(
      'drift', 'nullability', pointer, recorded.summary(), live.summary(), 'recording', message,
    )
  return Finding(
    'drift', 'type_changed', pointer, recorded.summary(), live.summary(), 'recording',
    f'was {" | ".join(sorted(was))}, now {" | ".join(sorted(now))}',
  )


def diff_object(recorded: Shape, live: Shape, view: SchemaView, pointer: str, out: list[Finding]) -> None:
  for key in sorted(live.properties.keys() - recorded.properties.keys()):
    _, declared, _ = view.property(key)
    if not declared:
      out.append(Finding(
        'drift', 'key_added', pointer_join(pointer, key), None, live.properties[key].summary(),
        'recording', f'`{key}` is new, and the schema does not declare it',
      ))
  for key in sorted(recorded.properties.keys() - live.properties.keys()):
    _, declared, required = view.property(key)
    if not declared or required:
      out.append(Finding(
        'drift', 'key_removed', pointer_join(pointer, key), recorded.properties[key].summary(), None,
        'recording', f'`{key}` is gone' + (', and the schema requires it' if required else ''),
      ))
  for key in sorted(recorded.properties.keys() & live.properties.keys()):
    child, _, _ = view.property(key)
    diff(recorded.properties[key], live.properties[key], child, pointer_join(pointer, key), out)
  if recorded.entries is not None and live.entries is not None:
    diff(recorded.entries, live.entries, map_entry_view(view), pointer_join(pointer, '*'), out)


def map_entry_view(view: SchemaView) -> SchemaView:
  children: list[dict[str, Any]] = []
  for schema in view.alternatives:
    additional = schema.get('additionalProperties')
    if isinstance(additional, dict):
      children.append(additional)
    children.extend((schema.get('patternProperties') or {}).values())
  return view.at(children)


def diff_array(recorded: Shape, live: Shape, view: SchemaView, pointer: str, out: list[Finding]) -> None:
  if recorded.positions is not None and live.positions is not None:
    if recorded.positions and live.positions and recorded.lengths != live.lengths:
      out.append(Finding(
        'drift', 'array_element', pointer, {'lengths': sorted(recorded.lengths)},
        {'lengths': sorted(live.lengths)}, 'recording',
        f'tuple length was {sorted(recorded.lengths)}, now {sorted(live.lengths)}',
      ))
    for index in range(min(len(recorded.positions), len(live.positions))):
      diff(recorded.positions[index], live.positions[index], view.item(index), pointer_join(pointer, index), out)
    return
  # An empty array on either side says nothing about element shape: absence of evidence.
  if recorded.items is not None and live.items is not None:
    diff(recorded.items, live.items, view.item(None), pointer_join(pointer, '*'), out)


def schema_findings(errors: Iterable[Any], view: SchemaView) -> list[Finding]:
  """Name each schema violation of the live body for a reader.

  Args:
    errors: `jsonschema` errors from validating the live body.
    view: The response schema at the top of the body, for normalising pointers.
  """
  out: list[Finding] = []
  for error in errors:
    out.extend(error_findings(error, view))
  return out


def error_findings(error: Any, view: SchemaView) -> list[Finding]:
  """The findings for one `jsonschema` error: usually one, one per key for `required` and
  `additionalProperties`, and for a failed `anyOf`/`oneOf` those of the alternative that
  names the problem."""
  keyword = error.validator
  if keyword in ('anyOf', 'oneOf') and error.context:
    return alternatives_findings(error, view)
  pointer = normalised_pointer(list(error.absolute_path), view)
  if keyword in ('enum', 'const'):
    allowed = error.validator_value if keyword == 'enum' else [error.validator_value]
    return [schema_enum_finding(pointer, list(allowed), error.instance)]
  if keyword == 'type':
    return [schema_type_finding(pointer, as_type_list(error.validator_value), error.instance)]
  if keyword == 'required':
    present = error.instance if isinstance(error.instance, dict) else {}
    return [
      Finding(
        'drift', 'key_removed', pointer_join(pointer, key), 'required', None, 'schema',
        f'`{key}` is required by the schema and missing',
      )
      for key in error.validator_value if key not in present
    ]
  if keyword in ('additionalProperties', 'unevaluatedProperties'):
    present = error.instance if isinstance(error.instance, dict) else {}
    declared = set((error.schema or {}).get('properties') or {})
    return [
      Finding(
        'drift', 'key_added', pointer_join(pointer, key), None, [json_type(present[key])], 'schema',
        f'`{key}` is not allowed by the schema',
      )
      for key in sorted(set(present) - declared)
    ]
  return [Finding(
    'drift', f'schema:{keyword}', pointer, describe(error.validator_value),
    json_type(error.instance), 'schema', f'fails `{keyword}`',
  )]


def alternatives_findings(error: Any, view: SchemaView) -> list[Finding]:
  """Name a value that no alternative of an `anyOf`/`oneOf` accepts.

  Kraken writes a nullable enum as `anyOf: [{enum}, {type: null}]`. A new value there fails
  the `null` alternative on `type` and every other on `enum`: that is an `enum_value`
  finding with the union of the declared values. A value of a type no alternative takes is
  a `type_changed` finding with the union of the declared types. Anything else is the
  finding for the error `jsonschema` ranks most relevant among the non-`null` alternatives.
  """
  from jsonschema.exceptions import best_match
  pointer = normalised_pointer(list(error.absolute_path), view)
  branches: dict[Any, list[Any]] = {}
  for sub in error.context:
    branches.setdefault(sub.relative_schema_path[0], []).append(sub)
  failures = list(branches.values())
  if all(at_root(subs, ('type',)) for subs in failures):
    declared = unique(kind for subs in failures for sub in subs for kind in as_type_list(sub.validator_value))
    return [schema_type_finding(pointer, declared, error.instance)]
  non_null = [subs for subs in failures if not is_null_alternative(subs)]
  if non_null and all(at_root(subs, ('enum', 'const')) for subs in non_null):
    allowed = unique(
      value for subs in non_null for sub in subs
      for value in (sub.validator_value if sub.validator == 'enum' else [sub.validator_value])
    )
    return [schema_enum_finding(pointer, allowed, error.instance)]
  return error_findings(best_match(sub for subs in non_null or failures for sub in subs), view)


def at_root(errors: list[Any], keywords: tuple[str, ...]) -> bool:
  """Whether an alternative fails only on these keywords, at the value itself."""
  return all(not sub.relative_path and sub.validator in keywords for sub in errors)


def is_null_alternative(errors: list[Any]) -> bool:
  """Whether an alternative is the `{type: null}` of a nullable schema."""
  return len(errors) == 1 and at_root(errors, ('type',)) and as_type_list(errors[0].validator_value) == ['null']


def unique(values: Iterable[Any]) -> list[Any]:
  out: list[Any] = []
  for value in values:
    if value not in out:
      out.append(value)
  return out


def schema_enum_finding(pointer: str, allowed: list[Any], instance: Any) -> Finding:
  return Finding(
    'drift', 'enum_value', pointer, allowed, quoted(instance), 'schema',
    f'{quoted(instance)!r} is not one of the declared values',
  )


def schema_type_finding(pointer: str, declared: list[str], instance: Any) -> Finding:
  return Finding(
    'drift', 'nullability' if instance is None else 'type_changed', pointer, declared, [json_type(instance)], 'schema',
    f'schema declares {" | ".join(declared)}, the API sent {json_type(instance)}',
  )


def as_type_list(value: Any) -> list[str]:
  return [str(item) for item in (value if isinstance(value, list) else [value])]


def quoted(value: Any) -> Any:
  """An enum value as a finding shows it: scalars as they are, long strings cut."""
  if isinstance(value, str) and len(value) > MAX_VALUE_CHARS:
    return value[:MAX_VALUE_CHARS] + '...'
  if isinstance(value, dict | list):
    return json_type(value)
  return value


def describe(value: Any) -> Any:
  """A schema keyword's value as a finding shows it: scalars as they are, anything bigger
  by its type, so a `oneOf` finding does not paste three schemas into a report."""
  if isinstance(value, dict | list):
    return f'<{json_type(value)}>'
  return value


def normalised_pointer(path: list[Any], view: SchemaView) -> str:
  """A JSON pointer for a concrete path, with array indices and map keys as `*`.

  A tuple position keeps its index (position 3 of a Kraken ticker level is not position 0),
  and a declared property keeps its name.
  """
  pointer = ''
  for part in path:
    if isinstance(part, int):
      if view.is_tuple():
        pointer = pointer_join(pointer, part)
        view = view.item(part)
      else:
        pointer = pointer_join(pointer, '*')
        view = view.item(None)
    else:
      pointer = pointer_join(pointer, '*' if view.is_map_entry(part) else part)
      view, _, _ = view.property(part)
  return pointer


def declared_pointer(pointer: str, path: Iterable[Any], view: SchemaView) -> tuple[str, SchemaView]:
  """`pointer` extended by a path that is not all wire keys (a pydantic `loc`), and the
  view at its end.

  A key some alternative declares under `properties` keeps its name and a tuple position
  its index. Everything else is `*`: an array index, a map key, a key of an object the
  schema does not describe, and pydantic's own parts (a union member's tag or class).
  Those last name no level of the body, so the view stays where it is.
  """
  for part in path:
    if isinstance(part, int):
      tuple_ = view.is_tuple()
      pointer = pointer_join(pointer, part if tuple_ else '*')
      view = view.item(part if tuple_ else None)
      continue
    part = str(part)
    declared = any(
      isinstance(schema.get('properties'), dict) and part in schema['properties']
      for schema in view.alternatives
    )
    if declared:
      pointer = pointer_join(pointer, part)
      view, _, _ = view.property(part)
    elif view.is_map_entry(part):
      pointer = pointer_join(pointer, '*')
      view = map_entry_view(view)
    else:
      pointer = pointer_join(pointer, '*')
  return pointer, view


def combine(schema: Iterable[Finding], recording: Iterable[Finding]) -> list[Finding]:
  """One finding per (kind, check, pointer), schema violations first.

  The same violation on every element of a list is one finding with a count, not forty.
  Where the recording and the schema report the same thing at the same pointer, the
  recording's version is kept, since it has both sides of the change, with the schema's
  count.
  """
  kept: dict[tuple[str, str, str, str], Finding] = {}
  for finding in schema:
    existing = kept.get(finding.key())
    if existing is None:
      kept[finding.key()] = finding
    else:
      existing.count += 1
  for finding in recording:
    existing = kept.get(finding.key())
    if existing is not None:
      finding.count = existing.count
    kept[finding.key()] = finding
  return list(kept.values())
