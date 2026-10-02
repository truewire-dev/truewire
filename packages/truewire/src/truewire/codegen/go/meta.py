"""`<package>/meta/meta.go`: one struct per `[cores.<name>]` entry in `truewire.toml` that
declares a `meta` schema, named `<Name>Meta`. A generated call passes
`meta.DefaultMeta{...}` built from the endpoint's declared `meta` as the call's `Meta`; the
hand-written core type-asserts it. The Go half of `truewire.codegen.meta`."""
from typing_extensions import Any, Mapping

from truewire.plan.model import CorePlan

from .names import json_value, literal, pascal_ident, pascal_ident as _pascal, string, unique
from .printer import BANNER, Entry, Writer

META_DIR = 'meta'
META_FILE = f'{META_DIR}/meta.go'

_SCALARS: Mapping[str, str] = {'boolean': 'bool', 'string': 'string', 'integer': 'int64', 'number': 'float64'}


def meta_type_name(core: str) -> str:
  return f'{_pascal(core)}Meta'


def meta_type(schema: Mapping[str, Any]) -> str:
  """One `meta` property's JSON Schema as a Go type, deliberately narrow: scalars, `enum`/
  `const` as the scalar their values share, slices of those, and `any` otherwise."""
  if 'enum' in schema or 'const' in schema:
    values = schema['enum'] if 'enum' in schema else [schema['const']]
    kinds = {_kind(value) for value in values}
    if len(kinds) == 1:
      kind = kinds.pop()
      if kind in _SCALARS:
        return _SCALARS[kind]
    return 'any'
  kind = schema.get('type')
  if isinstance(kind, str) and kind in _SCALARS:
    return _SCALARS[kind]
  if kind == 'array':
    items = schema.get('items')
    return '[]' + (meta_type(items) if isinstance(items, dict) else 'any')
  return 'any'


def meta_value(go_type: str, value: Any, core: str) -> str:
  """`value` as a Go expression of `go_type`; `core` is the runtime package's name."""
  if go_type == 'string':
    return string(str(value))
  if go_type in ('bool', 'int64'):
    return literal(value)
  if go_type == 'float64':
    return literal(float(value))
  if go_type.startswith('[]') and isinstance(value, list):
    inner = go_type[2:]
    return f'{go_type}{{' + ', '.join(meta_value(inner, item, core) for item in value) + '}'
  return json_value(value)


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
  """The struct a core's `meta` schema renders to."""

  def __init__(self, core: str, schema: Mapping[str, Any]):
    self.name = meta_type_name(core)
    self.required = set(schema.get('required') or ())
    self.fields: list[tuple[str, str, str, str | None]] = []
    """(wire property, Go field, Go type, description)."""
    taken: set[str] = set()
    for prop, prop_schema in (schema.get('properties') or {}).items():
      go_type = meta_type(prop_schema) if isinstance(prop_schema, dict) else 'any'
      if prop not in self.required and not go_type.startswith('[]') and go_type != 'any':
        go_type = f'*{go_type}'
      description = prop_schema.get('description') if isinstance(prop_schema, dict) else None
      self.fields.append((prop, unique(pascal_ident(prop, fallback='Field'), taken), go_type, description))

  def literal(self, meta: Mapping[str, Any], core: str) -> tuple[str, bool]:
    """`meta.XMeta{...}` for one endpoint's declared `meta` (the `meta.` qualifier left to
    the caller), and whether it needs `encoding/json`."""
    entries: list[str] = []
    for prop, field, go_type, _ in self.fields:
      if prop not in meta:
        continue
      if go_type.startswith('*'):
        entries.append(f'{field}: {core}.Ptr({meta_value(go_type[1:], meta[prop], core)})')
      else:
        entries.append(f'{field}: {meta_value(go_type, meta[prop], core)}')
    text = f'{self.name}{{{", ".join(entries)}}}'
    return text, 'json.RawMessage(' in text


def meta_shapes(cores: Mapping[str, CorePlan]) -> dict[str, MetaShape]:
  return {name: MetaShape(name, core.meta) for name, core in cores.items() if core.meta is not None}


def meta_module(cores: Mapping[str, CorePlan]) -> str | None:
  """The file, or `None` when no core declares a `meta` schema."""
  shapes = meta_shapes(cores)
  if not shapes:
    return None
  w = Writer()
  w.line(BANNER)
  w.blank()
  w.doc(
    'Package meta holds the per-endpoint `meta` shapes, one struct per `[cores.<name>]` in '
    '`truewire.toml` that declares a `meta` schema. A generated call passes its endpoint\'s '
    'declared `meta` as the call\'s `Meta`; the hand-written core type-asserts it.'
  )
  w.line(f'package {META_DIR}')
  for core, shape in shapes.items():
    w.blank()
    w.doc(f'{shape.name} is `meta` for every endpoint whose nearest `router.json` resolves to the `{core}` core.')
    entries: list[Entry] = [(field, go_type, description) for _, field, go_type, description in shape.fields]
    if not entries:
      w.line(f'type {shape.name} struct{{}}')
      continue
    w.struct(f'type {shape.name} struct {{', entries)
  return w.render()


__all__ = ['META_DIR', 'META_FILE', 'MetaShape', 'meta_module', 'meta_shapes', 'meta_type', 'meta_type_name', 'meta_value']
