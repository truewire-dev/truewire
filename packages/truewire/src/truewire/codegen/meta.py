"""Render `<package>/meta.py`: one `TypedDict` per `[cores.<name>]` entry in `truewire.toml`
that declares a `meta` schema.

A generated endpoint passes its declared `meta` as a plain dict literal
(`meta={'signed': True}`); the hand-written core annotates its `request`/`subscribe`
parameter with the class rendered here, and a type checker matches the two structurally.
Before ADR 0011 every core hand-wrote this class to match the schema; rendering it from
the schema is what keeps the two from drifting, and `truewire init` writes the first one
so the core template can import it before the project ever generates.
"""
from typing_extensions import Any, Mapping

from truewire.generation.util import indent, pascal_case
from truewire.spec.codegen_toml import CoreConfig

META_MODULE = 'meta'
"""Module name, relative to the package root, the `Meta` classes are written to."""

_SCALARS: Mapping[str, str] = {
  'boolean': 'bool', 'string': 'str', 'integer': 'int', 'number': 'float', 'null': 'None',
}


def meta_class_name(core: str) -> str:
  """`spot` -> `SpotMeta`: the class a core named `core` annotates its `meta` with."""
  return f'{pascal_case(core)}Meta'


def meta_type(schema: Mapping[str, Any], names: set[str]) -> str:
  """Render one `meta` property's JSON Schema as a Python annotation, recording every
  `typing_extensions` name the expression needs in `names`.

  `meta` carries small per-endpoint facts (a flag, a scope, a scheme name), so the
  mapping is deliberately narrow: scalars, `enum`/`const` as `Literal`, arrays of those,
  a multi-type list as a union, and `dict[str, Any]` for any nested object. Anything else
  renders as `Any` rather than failing generation over a shape a core reads by hand.
  """
  if 'enum' in schema or 'const' in schema:
    values = schema['enum'] if 'enum' in schema else [schema['const']]
    names.add('Literal')
    return f'Literal[{", ".join(repr(v) for v in values)}]'
  kind = schema.get('type')
  if isinstance(kind, list):
    variants = [meta_type({**schema, 'type': k}, names) for k in kind]
    return ' | '.join(dict.fromkeys(variants))
  if kind in _SCALARS:
    return _SCALARS[kind]
  if kind == 'array':
    items = schema.get('items')
    item = meta_type(items, names) if isinstance(items, dict) else _any(names)
    return f'list[{item}]'
  if kind == 'object':
    return f'dict[str, {_any(names)}]'
  return _any(names)


def _any(names: set[str]) -> str:
  names.add('Any')
  return 'Any'


def meta_classes(cores: Mapping[str, CoreConfig] | None) -> dict[str, dict[str, Any]]:
  """Core name -> its declared `meta` schema, for every core that declares one."""
  if not cores:
    return {}
  return {name: core.meta for name, core in cores.items() if core.meta is not None}


def meta_module(cores: Mapping[str, CoreConfig] | None) -> str | None:
  """Render the module, or `None` when no core declares a `meta` schema (the module is
  then not planned at all; a core with no schema takes no `meta` argument)."""
  schemas = meta_classes(cores)
  if not schemas:
    return None
  names: set[str] = {'TypedDict'}
  classes: list[str] = []
  for core, schema in schemas.items():
    required = set(schema.get('required') or ())
    properties: Mapping[str, Any] = schema.get('properties') or {}
    lines = [
      f'class {meta_class_name(core)}(TypedDict):',
      indent(f'"""`meta` for every endpoint whose nearest `router.json` resolves to the `{core}` core."""'),
    ]
    if properties:
      lines.append('')
    for prop, prop_schema in properties.items():
      annotation = meta_type(prop_schema, names)
      if prop not in required:
        names.add('NotRequired')
        annotation = f'NotRequired[{annotation}]'
      lines.append(indent(f'{prop}: {annotation}'))
      description = prop_schema.get('description')
      if description:
        lines.append(indent(f'"""{description}"""'))
    classes.append('\n'.join(lines))
  header = [
    '"""Per-endpoint `meta` shapes, one `TypedDict` per `[cores.<name>]` in `truewire.toml`',
    'that declares a `meta` schema. A hand-written core annotates its `meta` parameter with',
    "its core's class; generated calls pass a matching dict literal.",
    '"""',
    '',
    f'from typing_extensions import {", ".join(sorted(names))}',
  ]
  return '\n'.join(header) + '\n\n\n' + '\n\n\n'.join(classes) + '\n'
