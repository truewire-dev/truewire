"""Render the plan's type tree as Go: a struct per record, a named string type per string
literal, a struct of variant pointers per union and of positions per tuple, and a type alias
for everything else, each with the `MarshalJSON`/`UnmarshalJSON` that validate it through
`truewire.dev/core`.

The language-specific decisions, all fixed by `docs/go.md`:

- A `scalar` renders by base and format: `string`/`int64`/`float64`/`bool`/`any`, and each
  narrowing format to its `truewire` type (`truewire.Decimal`, `truewire.TimestampMillis`).
- A record is a struct with `PascalCase` fields and an `Extra` map, so an undocumented key
  never fails validation; its methods name every key as the wire spells it. Required keys
  hold the value; an optional or nullable key holds a nil-able form (`*T`, or the slice,
  map or `any` itself); a key both optional and nullable holds `truewire.Optional[...]`, the
  one form that tells an absent key from a null one.
- A `literal` of strings is a named string type with one constant per value; any other
  literal widens to its base scalar. A `union` is a struct with one pointer per variant,
  tried in order; `X | null` is the nil-able form of `X`. A `tuple` is a struct of `V0`,
  `V1`, ... fields.
- Go needs a name for every struct and named string type, and the plan names only the types
  it lists, so an inline literal, union or tuple is hoisted into a sibling type named after
  its position (`Issue.state_reason` becomes `IssueStateReason`), emitted before its user.
- A field whose type reaches back to its own record is held by pointer, so the struct has a
  size.
"""
from dataclasses import dataclass

from typing_extensions import Mapping

from truewire.plan.model import PackagePlan, TypeSet
from truewire.plan.types import Type, is_null, is_optional, refs, strip_null

from .names import literal, package_ident, pascal_ident, unique
from .printer import BANNER, Entry, Imports, Writer

CORE_IMPORT = 'truewire.dev/core'
"""The runtime module every generated package imports."""

CORE = 'truewire'

GRPC_IMPORT = 'truewire.dev/core/grpc'
"""The gRPC runtime, its own module so `truewire.dev/core` has no gRPC dependency (ADR 0017)."""

GRPC = 'twgrpc'
"""The name every generated file imports `GRPC_IMPORT` under."""
"""The name every generated file imports `CORE_IMPORT` under."""

TYPES_DIR = 'types'
"""The directory, under the root package, holding the shared `schemas.json` scopes."""

_SCALAR: Mapping[str, str] = {
  'string': 'string', 'integer': 'int64', 'number': 'float64', 'boolean': 'bool', 'null': 'any',
  'any': 'any',
}
FORMATS: Mapping[str, str] = {
  'decimal-string': 'Decimal', 'integer-string': 'IntegerString',
  'boolean-string': 'BooleanString', 'epoch-seconds': 'TimestampSeconds',
  'epoch-millis': 'TimestampMillis', 'epoch-micros': 'TimestampMicros',
  'epoch-nanos': 'TimestampNanos', 'date-time': 'TimestampIso', 'date': 'DateIso',
}

EXTRA_FIELD = 'Extra'
"""The map of undocumented keys every record struct ends with."""

RECORD_METHODS = frozenset((EXTRA_FIELD, 'MarshalJSON', 'UnmarshalJSON'))


@dataclass(frozen=True)
class Package:
  """Where the generated packages live: the import path of the root package, so every
  file can import every other by its full path."""
  import_path: str

  def path(self, *dirs: str) -> str:
    return '/'.join([self.import_path, *dirs])


def scope_dirs(scope: str) -> list[str]:
  """The directories of a shared scope under the root package: `['types']` for the root,
  `['types', 'futures']` for `futures`."""
  return [TYPES_DIR, *(package_ident(part, fallback='scope') for part in scope.split('/') if part)]


def scope_file(scope: str) -> str:
  return '/'.join([*scope_dirs(scope), 'types.go'])


def scope_package(scope: str) -> str:
  """The package name a scope's file declares."""
  return scope_dirs(scope)[-1]


def scope_alias(scope: str) -> str:
  """The name a file imports a scope under: `types` for the root, `typesFutures` for a
  nested one, so two nested scopes with the same last segment never clash."""
  dirs = scope_dirs(scope)
  return dirs[0] + ''.join(pascal_ident(d) for d in dirs[1:])


def visible_scopes(plan: PackagePlan, path: list[str]) -> list[str]:
  """Shared scope keys a function path can see, nearest first."""
  scopes: list[str] = []
  for depth in range(len(path), 0, -1):
    key = '/'.join(path[:depth])
    if key in plan.schemas:
      scopes.append(key)
  if '' in plan.schemas or not scopes:
    scopes.append('')
  return scopes


@dataclass(frozen=True)
class Field:
  """One rendered struct field of a record."""
  wire: str
  ident: str
  type: str
  """The full Go field type."""
  optional: bool
  nullable: bool
  shape: str
  """How the value is held: `value` (the type itself), `pointer` (`*T`), `nilable` (a slice,
  map or `any` whose nil means absent or null) or `optional` (`truewire.Optional[...]`)."""
  descriptor: str
  """The `truewire` field descriptor the record's methods use for it."""


class Module:
  """One generated `.go` file while it is rendered: its imports, the package-level names it
  defines, and the definitions hoisted out of inline literals, unions and tuples."""

  def __init__(
    self, plan: PackagePlan, file: str, *, package: Package, name: str, local: TypeSet,
    visible: list[str], scope: str | None = None,
  ):
    self.plan = plan
    self.file = file
    self.package = package
    self.name = name
    """The Go package name this file declares."""
    self.local = local
    self.visible = visible
    self.scope = scope
    """The shared scope this file *is*, when it renders one: its names are local."""
    self.imports = Imports()
    self.writer = Writer()
    self.names: set[str] = set(local)
    """Every package-level identifier this file defines."""
    self.fields: dict[str, list[Field]] = {}
    self.hoisted: list[tuple[Type, str]] = []
    """Every inline node hoisted to a named definition, with its name, so a later reference
    to the same node (a walker's row) reuses the definition instead of hoisting a twin."""
    self._reach: dict[tuple[str, str], bool] = {}

  def declare(self, name: str):
    self.names.add(name)

  # -- references ---------------------------------------------------------------------

  def core(self, name: str) -> str:
    """`truewire.Name`; a `grpc.`-prefixed name (`grpc.Endpoint`) lives in the separate
    `truewire.dev/core/grpc` module (ADR 0017) and is imported as `twgrpc`."""
    if name.startswith('grpc.'):
      self.imports.add(GRPC_IMPORT, GRPC)
      return f'{GRPC}.{name[len("grpc."):]}'
    self.imports.add(CORE_IMPORT, CORE)
    return f'{CORE}.{name}'

  def std(self, path: str):
    self.imports.std(path)

  def raw_json(self) -> str:
    self.imports.std('encoding/json')
    return 'json.RawMessage'

  def lookup(self, name: str) -> Type | None:
    if name in self.local:
      return self.local[name]
    for scope in (*self.visible, *self.plan.schemas):
      types = self.plan.schemas.get(scope)
      if types is not None and name in types:
        return types[name]
    return None

  def shared_scope(self, name: str) -> str | None:
    """The shared scope defining `name`, when it is not one of this file's own types."""
    if name in self.names:
      return None
    for scope in (*self.visible, *self.plan.schemas):
      if name in self.plan.schemas.get(scope, {}):
        return scope
    return None

  def import_scope(self, scope: str) -> str:
    """Import a shared scope's package and return the name it is reached by."""
    alias = scope_alias(scope)
    self.imports.add(self.package.path(*scope_dirs(scope)), alias if alias != scope_package(scope) else None)
    return alias

  def ref(self, name: str) -> str:
    """A `Ref` id as this file reaches it: the bare name for its own type, `types.Name`
    (importing the scope) for a shared one."""
    if name in self.names:
      return name
    scope = self.shared_scope(name)
    if scope is None:
      raise ValueError(f'{self.file}: type {name!r} is defined in no scope this file can see')
    if scope == self.scope:
      return name
    return f'{self.import_scope(scope)}.{name}'

  def reaches(self, source: str, target: str) -> bool:
    key = (source, target)
    if key in self._reach:
      return self._reach[key]
    self._reach[key] = False
    tree = self.lookup(source)
    found = tree is not None and any(
      ref['id'] == target or self.reaches(ref['id'], target) for ref in refs(tree)
    )
    self._reach[key] = found
    return found

  def resolved(self, t: Type | None) -> Type | None:
    """`t` through every `Ref` to the tree it names."""
    seen: set[str] = set()
    while t is not None and t['type'] == 'ref' and t['id'] not in seen:
      seen.add(t['id'])
      t = self.lookup(t['id'])
    return t

  def nilable(self, t: Type | None) -> bool:
    """Whether the Go type `t` renders to already has nil (a slice, map, `any` or pointer),
    so an absent or null value needs no extra pointer."""
    t = self.resolved(t)
    if t is None:
      return False
    kind = t['type']
    if kind == 'scalar':
      return t['base'] in ('any', 'null') and t.get('format') not in FORMATS
    if kind in ('list', 'dict'):
      return True
    if kind == 'union':
      return is_optional(t)
    if kind == 'literal':
      return not _is_string_literal(t) and self.widened_base(t) == 'any'
    return False

  def nullable(self, t: Type) -> bool:
    """Whether `t` admits null: a union with a null variant, or `null` itself (a key declared
    `{"type": "null"}`: hyperliquid's `vaultDetails.followerState`, mexc's `confirmNo`),
    directly or through an alias."""
    if is_optional(t) or is_null(t):
      return True
    resolved = self.resolved(t)
    return resolved is not None and resolved is not t and (is_optional(resolved) or is_null(resolved))

  # -- expressions --------------------------------------------------------------------

  def type_expr(self, t: Type, *, owner: str | None = None, direct: bool = True, path: list[str] = ()) -> str:  # type: ignore[assignment]
    """The Go type of one tree node.

    Args:
      owner: The record whose field this type is part of, for a pointer on a cycle.
      direct: Whether the value sits inline in `owner` (not behind a slice or map).
      path: Naming segments for a hoisted type: the owner, the field, then a position.
    """
    kind = t['type']
    if kind == 'scalar':
      fmt = t.get('format')
      if fmt is not None and fmt in FORMATS:
        return self.core(FORMATS[fmt])
      return _SCALAR[t['base']]
    if kind == 'ref':
      name = self.ref(t['id'])
      target = self.resolved(t)
      if (
        direct and owner is not None and (t['id'] == owner or self.reaches(t['id'], owner))
        and not self.nilable(t) and target is not None and target['type'] == 'record'
      ):
        return f'*{name}'
      return name
    if kind == 'literal':
      if _is_string_literal(t):
        return self.hoist(path, lambda name: self.define_literal(name, t), t)
      return self.widened(t)
    if kind == 'list':
      return '[]' + self.type_expr(t['item'], owner=owner, direct=False, path=[*path, 'Item'])
    if kind == 'tuple':
      return self.hoist(path, lambda name: self.define_tuple(name, t, owner=owner, direct=direct), t)
    if kind == 'union':
      if is_optional(t):
        inner = strip_null(t)
        if inner['type'] == 'union':
          hoisted = [*path, 'Value'] if len(path) == 1 else path
          name = self.hoist(hoisted, lambda name: self.define_union(name, inner, owner=owner, direct=direct), inner)
          return f'*{name}'
        if is_null(inner):
          return 'any'
        expr = self.type_expr(inner, owner=owner, direct=direct, path=path)
        return expr if self.nilable(inner) or expr.startswith('*') else f'*{expr}'
      return self.hoist(path, lambda name: self.define_union(name, t, owner=owner, direct=direct), t)
    if kind == 'dict':
      return 'map[string]' + self.type_expr(t['value'], owner=owner, direct=False, path=[*path, 'Value'])
    if kind == 'record':
      raise ValueError(f'{self.file}: an inline record ({t["id"]}) has no Go rendering')
    raise ValueError(f'unknown type node: {kind}')

  def widened_base(self, t: Type) -> str:
    values = t['values']
    if values and all(isinstance(v, bool) for v in values):
      return 'bool'
    if values and all(isinstance(v, int) and not isinstance(v, bool) for v in values):
      return 'int64'
    if values and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
      return 'float64'
    return 'any'

  def widened(self, t: Type) -> str:
    """The scalar a non-string literal widens to."""
    return self.widened_base(t)

  def hoist(self, path: list[str], define, node: Type | None = None) -> str:
    name = unique(''.join(pascal_ident(segment) for segment in path) or 'Value', self.names)
    while self.lookup(name) is not None and name not in self.local:
      name = unique(name, self.names)
    if node is not None:
      self.hoisted.append((node, name))
    define(name)
    return name

  def hoisted_name(self, node: Type) -> str | None:
    """The name an inline node was hoisted under in this file: the same node first, else an
    equal one."""
    for seen, name in self.hoisted:
      if seen is node:
        return name
    for seen, name in self.hoisted:
      if seen == node:
        return name
    return None

  # -- definitions --------------------------------------------------------------------

  def _emit(self, render) -> None:
    """Render one definition into its own buffer, so anything it hoists lands before it."""
    outer = self.writer
    self.writer = Writer()
    try:
      render(self.writer)
    finally:
      inner, self.writer = self.writer, outer
    self.writer.blank()
    self.writer.raw(inner.render().rstrip('\n'))
    self.writer.blank()

  def define(self, name: str, t: Type):
    kind = t['type']
    if kind == 'record':
      self.define_record(name, t)
    elif kind == 'literal' and _is_string_literal(t):
      self.define_literal(name, t)
    elif kind == 'union' and not is_optional(t):
      self.define_union(name, t, owner=None, direct=True)
    elif kind == 'tuple':
      self.define_tuple(name, t, owner=None, direct=True)
    else:
      self.define_alias(name, t)

  def define_all(self, types: TypeSet):
    for name, t in types.items():
      self.define(name, t)

  def define_alias(self, name: str, t: Type):
    def render(w: Writer):
      expr = self.type_expr(t, path=[name])
      doc = t.get('docstring')
      if t['type'] == 'literal':
        values = ', '.join(f'`{literal(v)}`' for v in t['values'])
        doc = f'{doc}\n\nOne of {values}.' if doc else f'One of {values}.'
      w.doc(doc)
      w.line(f'type {name} = {expr}')

    self._emit(render)

  def define_literal(self, name: str, t: Type):
    """A named string type, one constant per value, and the methods that check them."""
    def render(w: Writer):
      constants: list[str] = []
      for value in t['values']:
        constants.append(unique(name + pascal_ident(value, fallback='Empty'), self.names))
      w.doc(t.get('docstring'))
      w.line(f'type {name} string')
      w.blank()
      for constant, value in zip(constants, t['values']):
        w.line(f'const {constant} {name} = {literal(value)}')
      w.blank()
      args = ', '.join(constants)
      core = self.core
      w.doc('UnmarshalJSON accepts only the literal\'s values.')
      with w.block(f'func (v *{name}) UnmarshalJSON(data []byte) error {{'):
        w.line(f'return {core("DecodeLiteral")}(data, v, {args})')
      w.blank()
      w.doc('MarshalJSON writes the value, checking it is one of the literal\'s.')
      with w.block(f'func (v {name}) MarshalJSON() ([]byte, error) {{'):
        w.line(f'return {core("EncodeLiteral")}(v, {args})')

    self._emit(render)

  def define_union(self, name: str, t: Type, *, owner: str | None, direct: bool):
    """A struct with one pointer per variant, tried in order."""
    def render(w: Writer):
      taken: set[str] = set()
      entries: list[Entry] = []
      labels: list[str] = []
      for variant in t['variants']:
        label = unique(self.variant_name(variant['type']), taken)
        expr = self.type_expr(variant['type'], owner=owner, direct=False, path=[name, label])
        entries.append((label, f'*{expr}', variant.get('docstring')))
        labels.append(label)
      doc = t.get('docstring')
      w.doc(doc, 'Exactly one field is set: the first variant, in declaration order, that the value decodes as.')
      w.struct(f'type {name} struct {{', entries)
      w.blank()
      variants = ', '.join(f'{self.core("VariantOf")}(&u.{label})' for label in labels)
      w.doc('UnmarshalJSON sets the first variant the value decodes as.')
      with w.block(f'func (u *{name}) UnmarshalJSON(data []byte) error {{'):
        w.line(f'return {self.core("DecodeUnion")}(data, {variants})')
      w.blank()
      w.doc('MarshalJSON writes the variant that is set.')
      with w.block(f'func (u {name}) MarshalJSON() ([]byte, error) {{'):
        w.line(f'return {self.core("EncodeUnion")}({variants})')

    self._emit(render)

  def define_tuple(self, name: str, t: Type, *, owner: str | None, direct: bool):
    """A struct with one field per position, on the wire an array."""
    def render(w: Writer):
      entries: list[Entry] = []
      for index, item in enumerate(t['items']):
        expr = self.type_expr(item, owner=owner, direct=direct, path=[name, str(index)])
        entries.append((f'V{index}', expr, None))
      w.doc(t.get('docstring'), 'On the wire, an array of the fields in order.')
      w.struct(f'type {name} struct {{', entries)
      w.blank()
      items = ', '.join(f'{self.core("ItemOf")}(&t.V{i})' for i in range(len(t['items'])))
      w.doc('UnmarshalJSON decodes the array, position by position.')
      with w.block(f'func (t *{name}) UnmarshalJSON(data []byte) error {{'):
        w.line(f'return {self.core("DecodeTuple")}(data, {items})')
      w.blank()
      w.doc('MarshalJSON writes the positions as an array.')
      with w.block(f'func (t {name}) MarshalJSON() ([]byte, error) {{'):
        w.line(f'return {self.core("EncodeTuple")}({items})')

    self._emit(render)

  def variant_name(self, t: Type) -> str:
    kind = t['type']
    if kind == 'ref':
      return pascal_ident(t['id'])
    if kind == 'scalar':
      fmt = t.get('format')
      if fmt is not None and fmt in FORMATS:
        return FORMATS[fmt]
      return pascal_ident(t['base'])
    if kind == 'list':
      return f'{self.variant_name(t["item"])}List'
    if kind == 'union':
      return 'Union' if not is_optional(t) else f'Optional{self.variant_name(strip_null(t))}'
    return {'tuple': 'Tuple', 'dict': 'Map', 'literal': 'Literal', 'record': 'Record'}[kind]

  def field_layout(self, t: Type) -> list[tuple[str, str]]:
    """`(wire, ident)` of every field of record `t`, without rendering it."""
    taken = set(RECORD_METHODS)
    return [(wire, unique(pascal_ident(wire, fallback='Field'), taken)) for wire in t['fields']]

  def record_field(self, record: str, wire: str, field, ident: str) -> Field:
    """One field's Go type, shape and descriptor."""
    t = field['type']
    optional = not field['required']
    nullable = self.nullable(t)
    path = [record, wire]
    if is_optional(t):
      inner = strip_null(t)
      if inner['type'] == 'union':
        base = '*' + self.hoist(path, lambda name: self.define_union(name, inner, owner=record, direct=True))
      elif is_null(inner):
        base = 'any'
      else:
        expr = self.type_expr(inner, owner=record, direct=True, path=path)
        base = expr if self.nilable(inner) or expr.startswith('*') else f'*{expr}'
    else:
      base = self.type_expr(t, owner=record, direct=True, path=path)
    holds_nil = base.startswith('*') or self.nilable(t)
    if not optional and not nullable:
      shape = 'pointer' if base.startswith('*') else 'nilable' if holds_nil else 'value'
      return Field(wire, ident, base, optional, nullable, shape, 'Required')
    if not optional:
      shape = 'pointer' if base.startswith('*') else 'nilable'
      return Field(wire, ident, base, optional, nullable, shape, 'RequiredNullable')
    if not nullable:
      if holds_nil:
        shape = 'pointer' if base.startswith('*') else 'nilable'
        return Field(wire, ident, base, optional, nullable, shape, 'OptionalField')
      return Field(wire, ident, f'*{base}', optional, nullable, 'pointer', 'OptionalField')
    return Field(wire, ident, f'{self.core("Optional")}[{base}]', optional, nullable, 'optional', 'OptionalNullable')

  def define_record(self, name: str, t: Type):
    def render(w: Writer):
      fields = [
        self.record_field(name, wire, field, ident)
        for (wire, field), (_, ident) in zip(t['fields'].items(), self.field_layout(t))
      ]
      self.fields[name] = fields
      entries: list[Entry] = [
        (f.ident, f.type, field.get('docstring'))
        for f, field in zip(fields, t['fields'].values())
      ]
      entries.append((EXTRA_FIELD, f'map[string]{self.raw_json()}', 'Extra holds the keys the spec does not document, kept as they came.'))
      w.doc(t.get('docstring'))
      w.struct(f'type {name} struct {{', entries)
      w.blank()
      descriptors = [f'{self.core(f.descriptor)}({_go_string(f.wire)}, &r.{f.ident}),' for f in fields]
      w.doc('UnmarshalJSON decodes the wire object, checking every documented key.')
      with w.block(f'func (r *{name}) UnmarshalJSON(data []byte) error {{'):
        self._call(w, f'{self.core("DecodeObject")}(data, &r.{EXTRA_FIELD}', descriptors)
      w.blank()
      w.doc('MarshalJSON writes the wire object: the documented keys, then Extra.')
      with w.block(f'func (r {name}) MarshalJSON() ([]byte, error) {{'):
        self._call(w, f'{self.core("EncodeObject")}(r.{EXTRA_FIELD}', descriptors)

    self._emit(render)

  @staticmethod
  def _call(w: Writer, head: str, args: list[str]):
    if not args:
      w.line(f'return {head})')
      return
    w.line(f'return {head},')
    with w.indented():
      for arg in args:
        w.line(arg)
    w.line(')')

  # -- output -------------------------------------------------------------------------

  def render(self, *, doc: str | None = None) -> str:
    out = Writer()
    out.line(BANNER)
    out.blank()
    out.doc(doc)
    out.line(f'package {self.name}')
    imports = self.imports.render()
    if imports:
      out.blank()
      for line in imports:
        out.line(line)
    body = self.writer.render().strip('\n')
    if body:
      out.blank()
      out.raw(body)
    return out.render()


def _go_string(value: str) -> str:
  from .names import string
  return string(value)


def _is_string_literal(t: Type) -> bool:
  return bool(t['values']) and all(isinstance(v, str) for v in t['values'])


__all__ = [
  'CORE', 'CORE_IMPORT', 'EXTRA_FIELD', 'FORMATS', 'Field', 'Module', 'Package', 'TYPES_DIR',
  'scope_alias', 'scope_dirs', 'scope_file', 'scope_package', 'visible_scopes',
]
