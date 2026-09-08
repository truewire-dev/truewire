"""Render the plan's type tree as TypeScript: an `interface`/`type` per entry and, beside
it, a `Codec<T>` value built from the `@truewire/core` combinators (`t.object`,
`t.array`, ...), declared against the interface so `tsc` proves the two agree.

The one language-specific decision is the `scalar` node: a `decimal-string` is the branded
`Decimal`, every timestamp format is a `Date` behind its alias (`TimestampMillis`, ...), a
`date` is the branded `DateIso`, and `integer-string`/`boolean-string` are `number`/`boolean`.
Everything else is the JSON value itself.
"""
from typing_extensions import Any, Mapping

from truewire.plan.model import PackagePlan, TypeSet
from truewire.plan.types import Type, is_null

from .names import literal, property_key, string
from .printer import Imports, Writer, relative_specifier

CORE = '@truewire/core'
"""The runtime package every generated module imports from."""

_SCALAR_TYPE: Mapping[str, str] = {
  'string': 'string', 'integer': 'number', 'number': 'number', 'boolean': 'boolean',
  'null': 'null', 'any': 'unknown',
}
_SCALAR_CODEC: Mapping[str, str] = {
  'string': 't.string', 'integer': 't.integer', 'number': 't.number', 'boolean': 't.boolean',
  'null': 't.null', 'any': 't.unknown',
}
_FORMAT_TYPE: Mapping[str, str] = {
  'decimal-string': 'Decimal', 'integer-string': 'number', 'boolean-string': 'boolean',
  'epoch-seconds': 'TimestampSeconds', 'epoch-millis': 'TimestampMillis',
  'epoch-micros': 'TimestampMicros', 'epoch-nanos': 'TimestampNanos',
  'date-time': 'TimestampIso', 'date': 'DateIso',
}
_FORMAT_CODEC: Mapping[str, str] = {
  'decimal-string': 't.decimal', 'integer-string': 't.integerString',
  'boolean-string': 't.booleanString', 'epoch-seconds': 't.epochSeconds',
  'epoch-millis': 't.epochMillis', 'epoch-micros': 't.epochMicros',
  'epoch-nanos': 't.epochNanos', 'date-time': 't.dateTime', 'date': 't.date',
}
CORE_TYPE_NAMES: frozenset[str] = frozenset(
  {'Decimal', 'TimestampSeconds', 'TimestampMillis', 'TimestampMicros', 'TimestampNanos',
   'TimestampIso', 'DateIso'}
)


def scope_file(scope: str) -> str:
  """The module a shared scope's types are defined in: `types/index.ts` for the root
  `spec/schemas.json`, `types/<a>/<b>.ts` for `spec/endpoints/a/b/schemas.json`."""
  return 'types/index.ts' if scope == '' else f'types/{scope}.ts'


class Module:
  """One generated `.ts` file while it is rendered: its imports, the names it defines
  itself, and which codecs it has already emitted (a reference to a later one goes
  through `t.lazy`, so a recursive shape initialises)."""

  def __init__(self, plan: PackagePlan, file: str, *, local: TypeSet, visible: list[str]):
    self.plan = plan
    self.file = file
    self.local = local
    self.visible = visible
    """Shared scope keys visible from this module, nearest first."""
    self.imports = Imports()
    self.writer = Writer()
    self.defined: set[str] = set()
    """Codec constants already emitted in this module."""
    self.aliases: dict[str, str] = {}
    """Names reached through a namespace import (`list.Request`), for a router."""
    self.names: set[str] = set(local)
    """Every name this module defines: `local` plus what `declare` adds."""

  def declare(self, name: str):
    """Record a type alias the module defines beside its plan types (a walker's request type)."""
    self.names.add(name)

  # -- references ---------------------------------------------------------------------

  def core(self, name: str, *, type_only: bool = False):
    self.imports.add(CORE, name, type_only=type_only)

  def ref(self, name: str, *, type_only: bool = False) -> str:
    """Resolve a `Ref` id to the identifier this module reaches it by, importing it from
    its shared scope when it is not one of the module's own types."""
    if name in self.aliases:
      return self.aliases[name]
    if name in self.names:
      return name
    for scope in self.visible:
      if name in self.plan.schemas.get(scope, {}):
        specifier = relative_specifier(self.file, scope_file(scope))
        self.imports.add(specifier, name, type_only=type_only)
        return name
    for scope, types in self.plan.schemas.items():
      if name in types:
        specifier = relative_specifier(self.file, scope_file(scope))
        self.imports.add(specifier, name, type_only=type_only)
        return name
    raise ValueError(f'{self.file}: type {name!r} is defined in no scope this module can see')

  # -- expressions --------------------------------------------------------------------

  def type_expr(self, t: Type) -> str:
    """The TypeScript type of one tree node."""
    kind = t['type']
    if kind == 'scalar':
      fmt = t.get('format')
      name = _FORMAT_TYPE.get(fmt) if fmt is not None else None
      if name is None:
        return _SCALAR_TYPE[t['base']]
      if name in CORE_TYPE_NAMES:
        self.core(name, type_only=True)
      return name
    if kind == 'ref':
      return self.ref(t['id'], type_only=True)
    if kind == 'literal':
      return ' | '.join(literal(value) for value in t['values']) or 'never'
    if kind == 'list':
      return f'{self._wrapped(t["item"])}[]'
    if kind == 'tuple':
      return 'readonly [' + ', '.join(self.type_expr(item) for item in t['items']) + ']'
    if kind == 'union':
      return ' | '.join(dict.fromkeys(self.type_expr(v['type']) for v in t['variants'])) or 'never'
    if kind == 'dict':
      return f'Record<string, {self.type_expr(t["value"])}>'
    if kind == 'record':
      return t['id']
    raise ValueError(f'unknown type node: {kind}')

  def _wrapped(self, t: Type) -> str:
    """`type_expr`, parenthesised when it is a union that would bind wrongly in `X[]`."""
    expr = self.type_expr(t)
    if expr.startswith('readonly ') or (' | ' in expr and not expr.startswith('Record<')):
      return f'({expr})'
    return expr

  def codec_expr(self, t: Type) -> str:
    """The codec value of one tree node."""
    self.core('t')
    kind = t['type']
    if kind == 'scalar':
      fmt = t.get('format')
      codec = _FORMAT_CODEC.get(fmt) if fmt is not None else None
      return codec if codec is not None else _SCALAR_CODEC[t['base']]
    if kind == 'ref':
      name = self.ref(t['id'])
      if name in self.local and name not in self.defined:
        return f't.lazy(() => {name})'
      return name
    if kind == 'literal':
      return 't.literal(' + ', '.join(literal(value) for value in t['values']) + ')'
    if kind == 'list':
      return f't.array({self.codec_expr(t["item"])})'
    if kind == 'tuple':
      return 't.tuple([' + ', '.join(self.codec_expr(item) for item in t['items']) + '])'
    if kind == 'union':
      variants = t['variants']
      if len(variants) == 2 and any(is_null(v['type']) for v in variants):
        (other,) = [v for v in variants if not is_null(v['type'])]
        return f't.nullable({self.codec_expr(other["type"])})'
      return 't.union(' + ', '.join(self.codec_expr(v['type']) for v in variants) + ')'
    if kind == 'dict':
      return f't.record({self.codec_expr(t["value"])})'
    if kind == 'record':
      return t['id']
    raise ValueError(f'unknown type node: {kind}')

  # -- definitions --------------------------------------------------------------------

  def define(self, name: str, t: Type):
    """Emit `name`'s interface (or type alias) and its codec constant."""
    w = self.writer
    self.core('Codec', type_only=True)
    if t['type'] == 'record':
      w.jsdoc(t.get('docstring'))
      fields = t['fields']
      if not fields:
        w.line(f'export interface {name} {{}}')
      else:
        with w.block(f'export interface {name} {{'):
          for field_name, field in fields.items():
            w.jsdoc(field.get('docstring'))
            marker = '' if field['required'] else '?'
            w.line(f'{property_key(field_name)}{marker}: {self.type_expr(field["type"])}')
      w.blank()
      if not fields:
        w.line(f'export const {name}: Codec<{name}> = t.object({{}})')
        self.core('t')
      else:
        with w.block(f'export const {name}: Codec<{name}> = t.object({{', '})'):
          self.core('t')
          for field_name, field in fields.items():
            codec = self.codec_expr(field['type'])
            if not field['required']:
              codec = f't.optional({codec})'
            w.line(f'{property_key(field_name)}: {codec},')
    else:
      w.jsdoc(t.get('docstring'))
      w.line(f'export type {name} = {self.type_expr(t)}')
      w.blank()
      w.line(f'export const {name}: Codec<{name}> = {self.codec_expr(t)}')
    self.defined.add(name)
    w.blank()

  def define_all(self, types: TypeSet):
    for name, t in types.items():
      self.define(name, t)

  # -- output -------------------------------------------------------------------------

  def render(self, banner: str, *, doc: str | None = None) -> str:
    """The file: banner, module doc, imports, then everything written."""
    head = Writer()
    head.line(banner)
    if doc:
      head.jsdoc(doc)
    for line in self.imports.render():
      head.line(line)
    body = self.writer.render().lstrip('\n')
    return head.render() + ('\n' + body if body.strip() else '')


def visible_scopes(plan: PackagePlan, path: list[str]) -> list[str]:
  """Shared scope keys a function path can see, nearest first: every ancestor directory
  of the endpoint under `spec/endpoints/` that owns a `schemas.json`, then the root."""
  scopes: list[str] = []
  for depth in range(len(path), 0, -1):
    key = '/'.join(path[:depth])
    if key in plan.schemas:
      scopes.append(key)
  if '' in plan.schemas or not scopes:
    scopes.append('')
  return scopes


def json_value(value: Any) -> str:
  """A JSON value as a TypeScript literal (re-exported for the emitters)."""
  return literal(value)


__all__ = [
  'CORE', 'CORE_TYPE_NAMES', 'Module', 'json_value', 'scope_file', 'string', 'visible_scopes',
]
