"""`<package>/meta.rs`: one struct per `[cores.<name>]` entry in `truewire.toml` that
declares a `meta` schema, named `<Name>Meta`. A generated call passes `&<Name>Meta { .. }`
built from the endpoint's declared `meta`; the hand-written core implements
`HttpEndpoint<<Name>Meta>` and the compiler matches the two. The Rust half of
`truewire.codegen.meta`."""
from typing_extensions import Any, Mapping

from truewire.plan.model import CorePlan

from .names import json_expr, literal, pascal_case, snake_ident, string, unique
from .printer import BANNER, Writer

META_FILE = 'meta.rs'
"""Where `meta_module` writes the per-core `meta` structs."""

_SCALARS: Mapping[str, str] = {
  'boolean': 'bool', 'string': 'String', 'integer': 'i64', 'number': 'f64',
}


def meta_type_name(core: str) -> str:
  return f'{pascal_case(core)}Meta'


def meta_type(schema: Mapping[str, Any]) -> str:
  """One `meta` property's JSON Schema as a Rust type, deliberately narrow: scalars,
  `enum`/`const` as the scalar their values share, arrays of those, and `serde_json::Value`
  for a multi-type list, a nested object, or anything else."""
  if 'enum' in schema or 'const' in schema:
    values = schema['enum'] if 'enum' in schema else [schema['const']]
    kinds = {_kind(value) for value in values}
    if len(kinds) == 1:
      kind = kinds.pop()
      if kind in _SCALARS:
        return _SCALARS[kind]
    return 'serde_json::Value'
  kind = schema.get('type')
  if isinstance(kind, str) and kind in _SCALARS:
    return _SCALARS[kind]
  if kind == 'array':
    items = schema.get('items')
    item = meta_type(items) if isinstance(items, dict) else 'serde_json::Value'
    return f'Vec<{item}>'
  return 'serde_json::Value'


def meta_value(rust_type: str, value: Any) -> str:
  """`value` as an expression of the type `meta_type` chose for its property."""
  if rust_type == 'String':
    return f'{string(str(value))}.to_string()'
  if rust_type in ('bool', 'i64', 'f64'):
    return literal(float(value) if rust_type == 'f64' else value)
  if rust_type.startswith('Vec<') and isinstance(value, list):
    inner = rust_type[4:-1]
    return 'vec![' + ', '.join(meta_value(inner, item) for item in value) + ']'
  return json_expr(value)


def _kind(value: Any) -> str:
  if isinstance(value, bool):
    return 'boolean'
  if isinstance(value, int):
    return 'integer'
  if isinstance(value, float):
    return 'number'
  if isinstance(value, str):
    return 'string'
  return 'other'


class MetaShape:
  """The struct a core's `meta` schema renders to: its Rust field per property."""

  def __init__(self, core: str, schema: Mapping[str, Any]):
    self.name = meta_type_name(core)
    self.required = set(schema.get('required') or ())
    self.fields: list[tuple[str, str, str, str | None]] = []
    """(wire property, Rust field, Rust type, description)."""
    taken: set[str] = set()
    for prop, prop_schema in (schema.get('properties') or {}).items():
      rust_type = meta_type(prop_schema) if isinstance(prop_schema, dict) else 'serde_json::Value'
      if prop not in self.required:
        rust_type = f'Option<{rust_type}>'
      description = prop_schema.get('description') if isinstance(prop_schema, dict) else None
      self.fields.append((prop, unique(snake_ident(prop), taken), rust_type, description))

  @property
  def uses_json(self) -> bool:
    return any('serde_json::Value' in rust_type for _, _, rust_type, _ in self.fields)

  def literal_fields(self, meta: Mapping[str, Any]) -> list[str]:
    """The `field: value` entries of a literal for one endpoint's declared `meta`."""
    out: list[str] = []
    for prop, field, rust_type, _ in self.fields:
      if prop in self.required:
        out.append(f'{field}: {meta_value(rust_type, meta[prop])}')
      elif prop in meta:
        out.append(f'{field}: Some({meta_value(rust_type[7:-1], meta[prop])})')
      else:
        out.append(f'{field}: None')
    return out


def meta_shapes(cores: Mapping[str, CorePlan]) -> dict[str, MetaShape]:
  """One `MetaShape` per core that declares a `meta` schema."""
  return {name: MetaShape(name, core.meta) for name, core in cores.items() if core.meta is not None}


def meta_module(cores: Mapping[str, CorePlan]) -> str | None:
  """The module, or `None` when no core declares a `meta` schema."""
  shapes = meta_shapes(cores)
  if not shapes:
    return None
  w = Writer()
  w.line(BANNER)
  w.line('//!')
  w.doc(
    'Per-endpoint `meta` shapes, one struct per `[cores.<name>]` in `truewire.toml` that '
    'declares a `meta` schema. The hand-written core implements `HttpEndpoint<XMeta>` for '
    'its core\'s struct; generated calls pass a matching value.', inner=True,
  )
  if any(shape.uses_json for shape in shapes.values()):
    w.blank()
    w.line('use truewire_core::serde_json;')
  for core, shape in shapes.items():
    w.blank()
    w.doc(f'`meta` for every endpoint whose nearest `router.json` resolves to the `{core}` core.')
    w.line('#[derive(Debug, Clone, PartialEq)]')
    if not shape.fields:
      w.line(f'pub struct {shape.name} {{}}')
      continue
    with w.block(f'pub struct {shape.name} {{'):
      for _, field, rust_type, description in shape.fields:
        w.doc(description)
        w.line(f'pub {field}: {rust_type},')
  return w.render()


__all__ = ['META_FILE', 'MetaShape', 'meta_module', 'meta_shapes', 'meta_type', 'meta_type_name', 'meta_value']
