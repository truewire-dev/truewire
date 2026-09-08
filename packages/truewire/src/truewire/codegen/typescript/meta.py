"""`<package>/meta.ts`: one interface per `[cores.<name>]` entry in `truewire.toml` that
declares a `meta` schema, named `<Name>Meta`. A generated call passes its declared `meta`
as an object literal; the hand-written core takes `HttpEndpoint<DefaultMeta>`'s shape and
`tsc` matches the two structurally. The TypeScript half of `truewire.codegen.meta`."""
from typing_extensions import Any, Mapping

from truewire.plan.model import CorePlan

from .endpoint import meta_type_name
from .names import literal, property_key
from .printer import BANNER, Writer

_SCALARS: Mapping[str, str] = {
  'boolean': 'boolean', 'string': 'string', 'integer': 'number', 'number': 'number', 'null': 'null',
}


def meta_type(schema: Mapping[str, Any]) -> str:
  """One `meta` property's JSON Schema as a TypeScript type, deliberately narrow: scalars,
  `enum`/`const` as a literal union, arrays of those, a multi-type list as a union, and
  `Record<string, unknown>` for a nested object. Anything else is `unknown`."""
  if 'enum' in schema or 'const' in schema:
    values = schema['enum'] if 'enum' in schema else [schema['const']]
    return ' | '.join(literal(value) for value in values)
  kind = schema.get('type')
  if isinstance(kind, list):
    return ' | '.join(dict.fromkeys(meta_type({**schema, 'type': k}) for k in kind))
  if kind in _SCALARS:
    return _SCALARS[kind]
  if kind == 'array':
    items = schema.get('items')
    item = meta_type(items) if isinstance(items, dict) else 'unknown'
    return f'({item})[]' if ' | ' in item else f'{item}[]'
  if kind == 'object':
    return 'Record<string, unknown>'
  return 'unknown'


def meta_module(cores: Mapping[str, CorePlan]) -> str | None:
  """The module, or `None` when no core declares a `meta` schema."""
  schemas = {name: core.meta for name, core in cores.items() if core.meta is not None}
  if not schemas:
    return None
  w = Writer()
  w.line(BANNER)
  w.jsdoc(
    'Per-endpoint `meta` shapes, one interface per `[cores.<name>]` in `truewire.toml` that '
    'declares a `meta` schema. The hand-written core takes its core\'s interface as the '
    '`Meta` parameter of `HttpEndpoint`; generated calls pass a matching object literal.'
  )
  for core, schema in schemas.items():
    w.blank()
    w.jsdoc(f'`meta` for every endpoint whose nearest `router.json` resolves to the `{core}` core.')
    required = set(schema.get('required') or ())
    properties: Mapping[str, Any] = schema.get('properties') or {}
    if not properties:
      w.line(f'export type {meta_type_name(core)} = Record<string, never>')
      continue
    with w.block(f'export interface {meta_type_name(core)} {{'):
      for prop, prop_schema in properties.items():
        description = prop_schema.get('description') if isinstance(prop_schema, dict) else None
        w.jsdoc(description)
        marker = '' if prop in required else '?'
        w.line(f'{property_key(prop)}{marker}: {meta_type(prop_schema)}')
  return w.render()


__all__ = ['meta_module', 'meta_type']
