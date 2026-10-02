"""Reading an endpoint's response schema at a point inside the body.

The shape differ and the finding normaliser both ask the same few questions of the schema
at one JSON pointer: which types it declares there, whether a key is a declared property
(and a required one), whether an object is a map whose keys are data, whether an array is
a positional tuple. `SchemaView` answers them over a schema document from
`truewire.spec.validation.schema_document`, following `$ref`, flattening `allOf` and
treating `anyOf`/`oneOf` as alternatives, any of which may declare the thing asked about.

Every answer errs toward "declared": a view that cannot tell says the schema allows it,
because a finding the schema already allowed is noise, and noise is what gets a nightly
report ignored (ADR 0012).
"""

from dataclasses import dataclass
import re
from typing_extensions import Any, Iterable

JSON_TYPES = ('null', 'boolean', 'number', 'string', 'object', 'array')
"""Wire types, as the shape differ names them. `integer` is folded into `number`: JSON has
one number type, and `1` against `1.5` is a value, not a shape."""

MAX_DEPTH = 64
"""How many `$ref` hops one lookup follows before it gives up, for a self-referential schema."""


def json_type(value: Any) -> str:
  """The wire type of one decoded JSON value."""
  if value is None:
    return 'null'
  if isinstance(value, bool):
    return 'boolean'
  if isinstance(value, int | float):
    return 'number'
  if isinstance(value, str):
    return 'string'
  if isinstance(value, dict):
    return 'object'
  if isinstance(value, list):
    return 'array'
  return type(value).__name__


@dataclass(frozen=True)
class SchemaView:
  """The alternatives a schema offers at one point in a body, `$ref`s resolved.

  An empty `alternatives` list means no schema speaks for this point at all (a key the
  schema never declared, say): everything is allowed and nothing is declared.
  """
  document: dict[str, Any]
  """The whole schema document, with `$defs`, that `$ref`s resolve against."""
  alternatives: tuple[dict[str, Any], ...]

  @classmethod
  def root(cls, document: dict[str, Any]) -> 'SchemaView':
    """The view at the top of the body."""
    return cls(document, tuple(expand(document, document)))

  def at(self, alternatives: Iterable[dict[str, Any]]) -> 'SchemaView':
    return SchemaView(self.document, tuple(
      expanded for schema in alternatives for expanded in expand(self.document, schema)
    ))

  @property
  def unconstrained(self) -> bool:
    """Whether some alternative allows any value at all here."""
    return not self.alternatives or any(not constrains_type(schema) for schema in self.alternatives)

  def declared_types(self) -> set[str] | None:
    """Every wire type some alternative declares here, or `None` when some alternative
    allows any type."""
    if self.unconstrained:
      return None
    types: set[str] = set()
    for schema in self.alternatives:
      types |= schema_types(schema)
    return types

  def property(self, key: str) -> tuple['SchemaView', bool, bool]:
    """The view at one key of an object, whether some alternative declares that key
    (as a property, a pattern property or an entry of a map), and whether every
    alternative an object can match requires it.

    An object that leaves branch A (`x` required) for branch B (`y` required) still
    matches the schema, so `x` is required only if B requires it too. A branch that
    cannot be an object (the `null` of a nullable object) has no say."""
    children: list[dict[str, Any]] = []
    declared = False
    objects = [
      schema for schema in self.alternatives
      if not constrains_type(schema) or 'object' in schema_types(schema)
    ]
    required = bool(objects) and all(key in (schema.get('required') or []) for schema in objects)
    for schema in self.alternatives:
      properties = schema.get('properties')
      if isinstance(properties, dict) and key in properties:
        children.append(properties[key])
        declared = True
        continue
      matched = False
      for pattern, child in (schema.get('patternProperties') or {}).items():
        if re.search(pattern, key):
          children.append(child)
          declared = matched = True
      if matched:
        continue
      additional = schema.get('additionalProperties')
      if isinstance(additional, dict):
        children.append(additional)
        declared = True
    return self.at(children), declared, required

  def is_map_entry(self, key: str) -> bool:
    """Whether `key` is an entry of a map (its name is data) rather than a declared
    property: no alternative lists it under `properties`, and some alternative declares
    `additionalProperties` as a schema or a matching `patternProperties`."""
    for schema in self.alternatives:
      properties = schema.get('properties')
      if isinstance(properties, dict) and key in properties:
        return False
    for schema in self.alternatives:
      if isinstance(schema.get('additionalProperties'), dict):
        return True
      if any(re.search(pattern, key) for pattern in (schema.get('patternProperties') or {})):
        return True
    return False

  def is_tuple(self) -> bool:
    """Whether some alternative declares this array positionally (`prefixItems`)."""
    return any(isinstance(schema.get('prefixItems'), list) for schema in self.alternatives)

  def item(self, index: int | None) -> 'SchemaView':
    """The view at one element of an array; `index` is read only for a tuple."""
    children: list[dict[str, Any]] = []
    for schema in self.alternatives:
      prefix = schema.get('prefixItems')
      if index is not None and isinstance(prefix, list) and index < len(prefix):
        children.append(prefix[index])
        continue
      items = schema.get('items')
      if isinstance(items, dict):
        children.append(items)
    return self.at(children)


def expand(document: dict[str, Any], schema: Any, depth: int = 0) -> list[dict[str, Any]]:
  """Flatten one schema into the plain alternatives it offers.

  `$ref` is followed (its siblings kept), `allOf` merged into one schema, and each
  `anyOf`/`oneOf` branch becomes its own alternative with the parent's own keywords
  merged in. `true`, `{}` and anything unreadable become an unconstrained alternative.
  """
  if not isinstance(schema, dict) or depth > MAX_DEPTH:
    return [{}]
  schema = dict(schema)
  ref = schema.pop('$ref', None)
  if isinstance(ref, str):
    target = resolve_pointer(document, ref)
    return [merge(base, schema) for base in expand(document, target, depth + 1)]
  all_of = schema.pop('allOf', None)
  if isinstance(all_of, list):
    merged: list[dict[str, Any]] = [schema]
    for part in all_of:
      merged = [merge(left, right) for left in merged for right in expand(document, part, depth + 1)]
    return [out for item in merged for out in expand(document, item, depth + 1)]
  for keyword in ('anyOf', 'oneOf'):
    branches = schema.pop(keyword, None)
    if isinstance(branches, list) and branches:
      out: list[dict[str, Any]] = []
      for branch in branches:
        out.extend(merge(schema, expanded) for expanded in expand(document, branch, depth + 1))
      return out
  return [schema]


def resolve_pointer(document: dict[str, Any], ref: str) -> Any:
  """The node a local `$ref` (`#/$defs/Pet`) names, or `{}` when it names nothing."""
  if not ref.startswith('#'):
    return {}
  node: Any = document
  for part in ref[1:].split('/')[1:] if ref.startswith('#/') else []:
    part = part.replace('~1', '/').replace('~0', '~')
    if isinstance(node, dict) and part in node:
      node = node[part]
    elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
      node = node[int(part)]
    else:
      return {}
  return node


def merge(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
  """Combine two schemas that both apply, for reading declarations (not for validating).

  Properties and required keys are unioned; a type both declare is narrowed to what they
  share, a type only one declares is kept.
  """
  out = dict(left)
  for key, value in right.items():
    if key == 'properties' and isinstance(out.get(key), dict) and isinstance(value, dict):
      out[key] = {**out[key], **value}
    elif key == 'required' and isinstance(out.get(key), list) and isinstance(value, list):
      out[key] = list(dict.fromkeys([*out[key], *value]))
    elif key == 'type' and key in out:
      shared = as_list(out[key])
      narrowed = [t for t in as_list(value) if t in shared]
      out[key] = narrowed or as_list(value)
    else:
      out[key] = value
  return out


def as_list(value: Any) -> list[Any]:
  return value if isinstance(value, list) else [value]


def constrains_type(schema: dict[str, Any]) -> bool:
  """Whether a flattened schema says anything about which wire type is allowed."""
  return any(key in schema for key in (
    'type', 'enum', 'const', 'properties', 'additionalProperties', 'patternProperties',
    'items', 'prefixItems', 'required',
  ))


def schema_types(schema: dict[str, Any]) -> set[str]:
  """The wire types one flattened (and type-constraining) schema allows."""
  types: set[str] = set()
  declared = schema.get('type')
  if declared is not None:
    for name in as_list(declared):
      types.add('number' if name == 'integer' else name)
  if 'enum' in schema and isinstance(schema['enum'], list):
    types |= {json_type(value) for value in schema['enum']}
  if 'const' in schema:
    types.add(json_type(schema['const']))
  if declared is None and 'enum' not in schema and 'const' not in schema:
    if any(key in schema for key in ('properties', 'additionalProperties', 'patternProperties', 'required')):
      types.add('object')
    if any(key in schema for key in ('items', 'prefixItems')):
      types.add('array')
  if schema.get('nullable') is True:
    types.add('null')
  return types
