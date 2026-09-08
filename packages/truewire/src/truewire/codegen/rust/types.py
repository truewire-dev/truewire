"""Render the plan's type tree as Rust: a `serde` struct per record, an enum per literal
and per union, a type alias for everything else, with the validation the derive gives.

The language-specific decisions, all fixed by `docs/rust.md`:

- A `scalar` renders by base and format: `String`/`i64`/`f64`/`bool`/`()`/
  `serde_json::Value`, and each narrowing format to its `truewire_core` newtype.
- A record is a struct with the wire's field names in `snake_case` (`#[serde(rename)]`
  carrying the wire name when they differ), an `Option` per optional field, `Option<Option>`
  behind `double_option` for a field both optional and nullable, and a flattened `extra`
  map so an undocumented key never fails validation.
- A `literal` of strings is an enum with one variant per value; any other literal is
  widened to its base scalar, since `serde` renames only strings. A `union` is an
  `#[serde(untagged)]` enum tried in order; `X | null` is `Option<X>`.
- Rust needs a name for every enum, and the plan names only the types it lists, so an
  inline literal or union is hoisted into a sibling type named after its position
  (`Issue.state_reason` becomes `IssueStateReason`), emitted before the type that uses it.
- A field whose type reaches back to its own record is boxed, so the struct has a size.
"""
from dataclasses import dataclass

from typing_extensions import Mapping

from truewire.plan.model import PackagePlan, TypeSet
from truewire.plan.types import Type, is_null, is_optional, refs, strip_null

from .names import literal, pascal_ident, snake_ident, string, unique
from .printer import Imports, Writer

CORE = 'truewire_core'
"""The runtime crate every generated module imports from."""

TYPES_MODULE = 'types'
"""The module under the crate root holding the shared `schemas.json` scopes."""

_SCALAR: Mapping[str, str] = {
  'string': 'String', 'integer': 'i64', 'number': 'f64', 'boolean': 'bool', 'null': '()',
  'any': 'serde_json::Value',
}
_FORMAT: Mapping[str, str] = {
  'decimal-string': 'DecimalString', 'integer-string': 'IntegerString',
  'boolean-string': 'BooleanString', 'epoch-seconds': 'TimestampSeconds',
  'epoch-millis': 'TimestampMillis', 'epoch-micros': 'TimestampMicros',
  'epoch-nanos': 'TimestampNanos', 'date-time': 'TimestampIso', 'date': 'DateIso',
}
_DEFAULTABLE_FORMATS = frozenset(('decimal-string', 'integer-string', 'boolean-string'))
"""Newtypes that implement `Default`; a timestamp has no meaningful zero."""

EXTRA_FIELD = 'extra'
"""The flattened map every struct ends with."""

STRUCT_DERIVES = ('Debug', 'Clone', 'PartialEq', 'Serialize', 'Deserialize')
LITERAL_DERIVES = ('Debug', 'Clone', 'Copy', 'PartialEq', 'Eq', 'Hash', 'Serialize', 'Deserialize')
UNION_DERIVES = ('Debug', 'Clone', 'PartialEq', 'Serialize', 'Deserialize')


def scope_segments(scope: str) -> list[str]:
  """The module segments of a shared scope: `[]` for the root, `['a', 'b']` for `a/b`."""
  return [snake_ident(part, fallback='scope') for part in scope.split('/') if part]


def scope_module(scope: str) -> str:
  """The Rust path of a shared scope's module: `crate::types`, `crate::types::futures`."""
  return '::'.join(['crate', TYPES_MODULE, *scope_segments(scope)])


def scope_children(plan: PackagePlan, scope: str) -> list[str]:
  """Immediate child module names of a scope, from every deeper scope the plan holds."""
  depth = len(scope_segments(scope))
  children = {
    scope_segments(other)[depth]
    for other in plan.schemas if len(scope_segments(other)) > depth
    and scope_segments(other)[:depth] == scope_segments(scope)
  }
  return sorted(children)


def scope_file(plan: PackagePlan, scope: str) -> str:
  """The file a scope's module is: `types/mod.rs` for the root, `types/a/b.rs` for a leaf
  scope, `types/a/mod.rs` for one with scopes beneath it."""
  segments = scope_segments(scope)
  if not segments or scope_children(plan, scope):
    return '/'.join([TYPES_MODULE, *segments, 'mod.rs'])
  return '/'.join([TYPES_MODULE, *segments]) + '.rs'


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


@dataclass(frozen=True)
class Field:
  """One rendered struct field."""
  wire: str
  ident: str
  type: str
  """The full field type, `Option`s included."""
  optional: bool
  nullable: bool


class Module:
  """One generated `.rs` file while it is rendered: its imports, the names it defines,
  and the definitions hoisted out of inline literals and unions."""

  def __init__(self, plan: PackagePlan, file: str, *, local: TypeSet, visible: list[str]):
    self.plan = plan
    self.file = file
    self.local = local
    self.visible = visible
    """Shared scope keys visible from this module, nearest first."""
    self.imports = Imports()
    self.writer = Writer()
    self.names: set[str] = set(local)
    """Every type name this module defines: `local`, hoisted ones, and what `declare` adds."""
    self.fields: dict[str, list[Field]] = {}
    """Rendered fields of every record defined here, by record name."""
    self._reach: dict[tuple[str, str], bool] = {}
    self._defaultable: dict[str, bool] = {}

  def declare(self, name: str):
    """Record a type the module defines beside its plan types (a walker's request type)."""
    self.names.add(name)

  # -- references ---------------------------------------------------------------------

  def core(self, name: str):
    self.imports.add(CORE, name)

  def serde_json(self):
    """`serde_json`, through the runtime's re-export so the crate pins one version."""
    self.imports.module(f'{CORE}::serde_json')

  def lookup(self, name: str) -> Type | None:
    """The tree of a type name, from this module's own types or any shared scope."""
    if name in self.local:
      return self.local[name]
    for scope in (*self.visible, *self.plan.schemas):
      types = self.plan.schemas.get(scope)
      if types is not None and name in types:
        return types[name]
    return None

  def shared_scope(self, name: str) -> str | None:
    """The shared scope defining `name`, when it is not one of this module's own types."""
    if name in self.names:
      return None
    for scope in (*self.visible, *self.plan.schemas):
      if name in self.plan.schemas.get(scope, {}):
        return scope
    return None

  def ref(self, name: str) -> str:
    """Resolve a `Ref` id to the identifier this module reaches it by, importing it from
    its shared scope when it is not one of the module's own types."""
    if name in self.names:
      return name
    for scope in (*self.visible, *self.plan.schemas):
      if name in self.plan.schemas.get(scope, {}):
        self.imports.add(scope_module(scope), name)
        return name
    raise ValueError(f'{self.file}: type {name!r} is defined in no scope this module can see')

  def reaches(self, source: str, target: str) -> bool:
    """Whether a reference chain leads from type `source` to type `target`."""
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

  def defaultable(self, t: Type) -> bool:
    """Whether the Rust type of `t` implements `Default`, so a struct holding it can."""
    kind = t['type']
    if kind == 'scalar':
      fmt = t.get('format')
      return fmt is None or fmt in _DEFAULTABLE_FORMATS or fmt not in _FORMAT
    if kind == 'ref':
      name = t['id']
      if name not in self._defaultable:
        self._defaultable[name] = False
        target = self.lookup(name)
        self._defaultable[name] = target is not None and self.defaultable(target)
      return self._defaultable[name]
    if kind == 'record':
      return all(not f['required'] or self.defaultable(f['type']) for f in t['fields'].values())
    if kind in ('list', 'dict'):
      return True
    if kind == 'tuple':
      return len(t['items']) <= 12 and all(self.defaultable(item) for item in t['items'])
    if kind == 'union':
      return is_optional(t)
    return False

  def copyable(self, t: Type) -> bool:
    """Whether the Rust type of `t` is `Copy`, so reading it out of a borrow needs no
    `.clone()` (which `clippy` would refuse on a `Copy` value)."""
    kind = t['type']
    if kind == 'scalar':
      fmt = t.get('format')
      if fmt is not None and fmt in _FORMAT:
        return True
      return t['base'] in ('integer', 'number', 'boolean', 'null')
    if kind == 'ref':
      target = self.lookup(t['id'])
      return target is not None and target['type'] != 'record' and self.copyable(target)
    if kind == 'literal':
      return True
    if kind == 'tuple':
      return all(self.copyable(item) for item in t['items'])
    if kind == 'union':
      return is_optional(t) and self.copyable(strip_null(t))
    return False

  # -- expressions --------------------------------------------------------------------

  def type_expr(self, t: Type, *, owner: str | None = None, direct: bool = True, path: list[str] = ()) -> str:  # type: ignore[assignment]
    """The Rust type of one tree node.

    Args:
      owner: The record whose field this type is part of, for boxing a cycle.
      direct: Whether the value sits inline in `owner` (not behind a `Vec` or map).
      path: Naming segments for a hoisted enum: the owner, the field, then a position.
    """
    kind = t['type']
    if kind == 'scalar':
      fmt = t.get('format')
      name = _FORMAT.get(fmt) if fmt is not None else None
      if name is None:
        if t['base'] == 'any':
          self.serde_json()
        return _SCALAR[t['base']]
      self.core(name)
      return name
    if kind == 'ref':
      name = self.ref(t['id'])
      if direct and owner is not None and (t['id'] == owner or self.reaches(t['id'], owner)):
        return f'Box<{name}>'
      return name
    if kind == 'literal':
      if _is_string_literal(t):
        return self.hoist(path, lambda name: self.define_literal(name, t))
      return self.widened(t)
    if kind == 'list':
      item = self.type_expr(t['item'], owner=owner, direct=False, path=[*path, 'Item'])
      return f'Vec<{item}>'
    if kind == 'tuple':
      items = [
        self.type_expr(item, owner=owner, direct=direct, path=[*path, str(index)])
        for index, item in enumerate(t['items'])
      ]
      return '(' + ', '.join(items) + (',)' if len(items) == 1 else ')')
    if kind == 'union':
      if is_optional(t):
        inner = strip_null(t)
        if inner['type'] == 'union':
          # A top-level alias `X = A | B | null` names its enum `XValue`; a field names it
          # after the field, as a plain inline union would.
          hoisted = [*path, 'Value'] if len(path) == 1 else path
          return f'Option<{self.hoist(hoisted, lambda name: self.define_union(name, inner, owner=owner, direct=direct))}>'
        if is_null(inner):
          return 'Option<()>'
        return f'Option<{self.type_expr(inner, owner=owner, direct=direct, path=path)}>'
      return self.hoist(path, lambda name: self.define_union(name, t, owner=owner, direct=direct))
    if kind == 'dict':
      self.imports.add('std::collections', 'HashMap')
      value = self.type_expr(t['value'], owner=owner, direct=False, path=[*path, 'Value'])
      return f'HashMap<String, {value}>'
    if kind == 'record':
      raise ValueError(f'{self.file}: an inline record ({t["id"]}) has no Rust rendering')
    raise ValueError(f'unknown type node: {kind}')

  def widened(self, t: Type) -> str:
    """The scalar a non-string literal widens to: `i64`, `f64`, `bool`, or a JSON value."""
    values = t['values']
    if all(isinstance(v, bool) for v in values):
      return 'bool'
    if all(isinstance(v, int) and not isinstance(v, bool) for v in values):
      return 'i64'
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
      return 'f64'
    self.serde_json()
    return 'serde_json::Value'

  def hoist(self, path: list[str], define) -> str:
    """Name and emit a type for an inline node, before the definition using it."""
    name = unique(''.join(pascal_ident(segment) for segment in path) or 'Value', self.names)
    while self.lookup(name) is not None:
      name = unique(name, self.names)
    define(name)
    return name

  # -- definitions --------------------------------------------------------------------

  def _emit(self, render) -> None:
    """Render one definition into its own buffer, so that anything it hoists lands in
    the module before it, then append it."""
    outer = self.writer
    self.writer = Writer()
    try:
      render(self.writer)
    finally:
      inner, self.writer = self.writer, outer
    self.writer.blank()
    self.writer.lines(inner.render().rstrip('\n'))
    self.writer.blank()

  def define(self, name: str, t: Type):
    """Emit `name`'s definition: a struct, an enum, or a type alias."""
    kind = t['type']
    if kind == 'record':
      self.define_record(name, t)
    elif kind == 'literal' and _is_string_literal(t):
      self.define_literal(name, t)
    elif kind == 'union' and not is_optional(t):
      self.define_union(name, t, owner=None, direct=True)
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
      w.line(f'pub type {name} = {expr};')

    self._emit(render)

  def define_literal(self, name: str, t: Type):
    """An enum with one unit variant per string value."""
    def render(w: Writer):
      taken: set[str] = set()
      w.doc(t.get('docstring'))
      w.line(f'#[derive({", ".join(LITERAL_DERIVES)})]')
      with w.block(f'pub enum {name} {{'):
        for value in t['values']:
          variant = unique(pascal_ident(value), taken)
          if variant != value:
            w.attribute('serde', [f'rename = {string(value)}'])
          w.line(f'{variant},')

    self.imports.add('serde', 'Serialize')
    self.imports.add('serde', 'Deserialize')
    self._emit(render)

  def define_union(self, name: str, t: Type, *, owner: str | None, direct: bool):
    """An untagged enum with one tuple variant per member, tried in order."""
    def render(w: Writer):
      taken: set[str] = set()
      variants: list[tuple[str, str, str | None]] = []
      for index, variant in enumerate(t['variants']):
        label = unique(self.variant_name(variant['type']), taken)
        expr = self.type_expr(variant['type'], owner=owner, direct=direct, path=[name, label])
        variants.append((label, expr, variant.get('docstring')))
      w.doc(t.get('docstring'))
      w.line(f'#[derive({", ".join(UNION_DERIVES)})]')
      w.line('#[serde(untagged)]')
      with w.block(f'pub enum {name} {{'):
        for label, expr, doc in variants:
          w.doc(doc)
          w.line(f'{label}({expr}),')

    self.imports.add('serde', 'Serialize')
    self.imports.add('serde', 'Deserialize')
    self._emit(render)

  def variant_name(self, t: Type) -> str:
    """A variant label for one union member, from what it is."""
    kind = t['type']
    if kind == 'ref':
      return t['id']
    if kind == 'scalar':
      fmt = t.get('format')
      if fmt is not None and fmt in _FORMAT:
        return _FORMAT[fmt]
      return pascal_ident(t['base'])
    if kind == 'list':
      return f'{self.variant_name(t["item"])}List'
    if kind == 'union':
      return 'Union' if not is_optional(t) else f'Optional{self.variant_name(strip_null(t))}'
    return {'tuple': 'Tuple', 'dict': 'Map', 'literal': 'Literal', 'record': 'Record'}[kind]

  def field_layout(self, t: Type) -> list[Field]:
    """The identifier and flags of every field of record `t`, without rendering it:
    what a reader of a record defined in another module needs."""
    taken = {EXTRA_FIELD}
    return [
      Field(wire, unique(snake_ident(wire), taken), '', not field['required'], is_optional(field['type']))
      for wire, field in t['fields'].items()
    ]

  def define_record(self, name: str, t: Type):
    def render(w: Writer):
      fields: list[Field] = []
      for (wire, field), laid in zip(t['fields'].items(), self.field_layout(t)):
        ident, nullable, optional = laid.ident, laid.nullable, laid.optional
        if optional and nullable:
          inner = strip_null(field['type'])
          if inner['type'] == 'union':
            expr = self.hoist(
              [name, wire], lambda hoisted, inner=inner: self.define_union(hoisted, inner, owner=name, direct=True),
            )
          else:
            expr = self.type_expr(inner, owner=name, direct=True, path=[name, wire])
          expr = f'Option<Option<{expr}>>'
        else:
          expr = self.type_expr(field['type'], owner=name, direct=True, path=[name, wire])
          if optional:
            expr = f'Option<{expr}>'
        fields.append(Field(wire, ident, expr, optional, nullable))
      self.fields[name] = fields
      derives = list(STRUCT_DERIVES)
      if self.defaultable(t):
        derives.insert(3, 'Default')
      w.doc(t.get('docstring'))
      w.line(f'#[derive({", ".join(derives)})]')
      with w.block(f'pub struct {name} {{'):
        for (wire, field), rendered in zip(t['fields'].items(), fields):
          w.doc(field.get('docstring'))
          attrs: list[str] = []
          if rendered.ident != wire:
            attrs.append(f'rename = {string(wire)}')
          if rendered.optional:
            attrs.extend(('default', 'skip_serializing_if = "Option::is_none"'))
          if attrs:
            w.attribute('serde', attrs)
          if rendered.optional and rendered.nullable:
            w.attribute('serde', [f'with = "{CORE}::validation::double_option"'])
          w.line(f'pub {rendered.ident}: {rendered.type},')
        w.doc('Keys the spec does not document, kept as they came.')
        w.line('#[serde(flatten)]')
        w.line(f'pub {EXTRA_FIELD}: serde_json::Map<String, serde_json::Value>,')

    self.imports.add('serde', 'Serialize')
    self.imports.add('serde', 'Deserialize')
    self.serde_json()
    self._emit(render)

  # -- output -------------------------------------------------------------------------

  def render(self, banner: str, *, doc: str | None = None, head: list[str] = ()) -> str:  # type: ignore[assignment]
    """The file: banner, module doc, `head` lines (`pub mod` declarations), imports, then
    everything written."""
    out = Writer()
    out.line(banner)
    if doc:
      out.line('//!')
      out.doc(doc, inner=True)
    if head:
      out.blank()
      for line in head:
        out.line(line)
    imports = self.imports.render()
    if imports:
      out.blank()
      for line in imports:
        out.line(line)
    body = self.writer.render().strip('\n')
    if body:
      out.blank()
      out.lines(body)
    return out.render()


def _is_string_literal(t: Type) -> bool:
  return bool(t['values']) and all(isinstance(v, str) for v in t['values'])


__all__ = [
  'CORE', 'EXTRA_FIELD', 'TYPES_MODULE', 'Field', 'Module', 'scope_children', 'scope_file',
  'scope_module', 'scope_segments', 'visible_scopes',
]
