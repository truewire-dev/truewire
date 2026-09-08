from typing_extensions import Callable, Collection, TypeVar, Generic, Protocol, Mapping, Iterable
from dataclasses import dataclass, field
import builtins
import re
from collections import defaultdict
from keyword import iskeyword

from truewire.generation.schema import Schema, SchemaCycleError
from truewire.generation.types import generation_order, RenderedTypes, Imports, merge_imports
from truewire.generation.util import indent
from truewire.generation.python.util import escape_docstring
from truewire.plan.types import refs
from .schema import Ref, Scalar, Literal, List, Tuple, Union, Dict, Record, Type
from .parser import (
  BOOLEAN_STRING_FORMATS, DECIMAL_STRING_FORMATS, INTEGER_STRING_FORMATS, Parser,
  TIMESTAMP_FORMATS, TYPES_PACKAGE,
)

SCALAR_BASES: builtins.dict[str, str] = {
  'string': 'str', 'integer': 'int', 'number': 'float', 'boolean': 'bool', 'null': 'None',
  'any': 'Any',
}
"""Neutral scalar base -> the Python name it renders to when no format narrows it."""

@dataclass
class ReservedKeywordType:
  iden: str
  fields: builtins.dict[str, str]
  docstring: str | None = None

  def code(self) -> str:
    out = f'{self.iden} = TypedDict('
    out += f"'{self.iden}', " + '{'
    out += ', '.join(f"'{k}': {v}" for k, v in self.fields.items())
    out += '})'
    if self.docstring:
      out += f'\n"""\n{indent_multiline_docstring(self.docstring)}\n"""'
    return out

def indent_multiline_docstring(docstring: str, tab: str = '  ') -> str:
  return indent(docstring, tab) if '\n' in docstring else docstring

@dataclass(kw_only=True)
class Code:
  iden: str
  defn: str | None = None
  imports: Imports = field(default_factory=dict)
  reserved_keyword_types: builtins.list[ReservedKeywordType] = field(default_factory=builtins.list)

def scalar(type: Scalar, recur: Callable[[Type], Code]) -> Code:
  """Render one wire scalar to its Python type: a builtin, `Decimal` for a
  `decimal-string`, or the `truewire_core.types` alias for a timestamp/date format.
  `Any` (an unconstrained schema) needs its `typing_extensions` import."""
  fmt = type.get('format')
  if fmt in TIMESTAMP_FORMATS:
    name = TIMESTAMP_FORMATS[fmt]
    return Code(iden=name, imports={TYPES_PACKAGE: {name}})
  if fmt in DECIMAL_STRING_FORMATS:
    return Code(iden='Decimal', imports={'decimal': {'Decimal'}})
  if fmt in INTEGER_STRING_FORMATS:
    return Code(iden='int')
  if fmt in BOOLEAN_STRING_FORMATS:
    return Code(iden='bool')
  if type['base'] == 'any':
    return Code(iden='Any', imports={'typing_extensions': {'Any'}})
  return Code(iden=SCALAR_BASES[type['base']])

def ref(type: Ref, recur: Callable[[Type], Code]) -> Code:
  """Render a reference to a named type, quoted when it is a forward reference.

  `forward` is set by `Renderer` for a record that is emitted before the record it
  references, which only happens on a reference cycle. Generated code never uses `from
  __future__ import annotations` (it breaks runtime type-checking libraries such as
  Pydantic), so a class-body annotation naming a class further down the module has to be
  a string -- the same reason a record's *own* name is quoted, done here rather than in
  `quote_self_reference` because only the renderer knows the order.
  """
  imports: Imports = {} if (pkg := type.get('package')) is None else {pkg: {type['id']}}
  return Code(
    iden=f"'{type['id']}'" if type.get('forward') else type['id'],
    imports=imports,
  )

def literal(type: Literal, recur: Callable[[Type], Code]) -> Code:
  values = ', '.join(repr(v) for v in type['values'])
  return Code(
    iden=f'Literal[{values}]',
    imports={'typing_extensions': {'Literal'}}
  )

def list(type: List, recur: Callable[[Type], Code]) -> Code:
  item = recur(type['item'])
  iden = f'list[{item.iden}]'
  return Code(
    iden=iden,
    imports=item.imports
  )

def tuple(type: Tuple, recur: Callable[[Type], Code]) -> Code:
  """Render a positional row as a fixed-length `tuple[...]`."""
  items = [recur(item) for item in type['items']]
  idens = ', '.join(item.iden for item in items)
  return Code(
    iden=f'tuple[{idens}]',
    imports=merge_imports([item.imports for item in items]),
    reserved_keyword_types=[t for item in items for t in item.reserved_keyword_types],
  )

def union(type: Union, recur: Callable[[Type], Code]) -> Code:
  variants = [recur(variant['type']) for variant in type['variants']]
  imports = merge_imports([var.imports for var in variants])
  decl = ' | '.join([var.iden for var in variants])
  code = Code(
    iden=decl,
    imports=imports,
  )
  for var in variants:
    code.reserved_keyword_types.extend(var.reserved_keyword_types)
  return code

def dict(type: Dict, recur: Callable[[Type], Code]) -> Code:
  key = recur(type["key"])
  val = recur(type["value"])
  imports = merge_imports([key.imports, val.imports])
  return Code(
    iden=f'dict[{key.iden}, {val.iden}]',
    imports=imports,
    reserved_keyword_types=key.reserved_keyword_types + val.reserved_keyword_types
  )

def quote_self_reference(expr: str, self_id: str) -> str:
  """Wrap a bare self-reference to `self_id` in a forward-reference string.

  Generated code never uses `from __future__ import annotations` (it breaks runtime
  type-checking libraries such as Pydantic), so class-body annotations execute eagerly: a
  record's own name is not yet bound while its class body runs. A recursive field -- a
  `BasicOrder.children: list[BasicOrder]` -- would raise `NameError` at import time unless
  the self-reference is quoted, exactly as a forward reference to any not-yet-defined name
  must be.
  """
  return re.sub(rf'\b{re.escape(self_id)}\b', f"'{self_id}'", expr)

def record(type: Record, recur: Callable[[Type], Code]) -> Code:
  imports: builtins.list[Imports] = [{'typing_extensions': {'TypedDict'}}]

  fields = {k: recur(v["type"]) for k, v in type['fields'].items()}
  for field_code in fields.values():
    field_code.iden = quote_self_reference(field_code.iden, type['id'])
  # A field name can be unusable as a Python identifier for two different reasons: it's a
  # reserved keyword (`type`, `class`, ...), or it just isn't a valid identifier at all
  # (`30dSpotVol` -- leading digit). Both need the same functional-
  # `TypedDict` escape hatch (`class {k: v}` syntax is a syntax error for either), so both
  # route through it rather than only the keyword case.
  keyword_fields = {k: v for k, v in fields.items() if iskeyword(k) or not k.isidentifier()}
  if keyword_fields:
    base = f'{type["id"]}Keywords'
    keyword_field_code: builtins.dict[str, str] = {}
    for k, v in keyword_fields.items():
      if type['fields'][k]['required']:
        keyword_field_code[k] = v.iden
      else:
        keyword_field_code[k] = f'NotRequired[{v.iden}]'
        imports.append({'typing_extensions': {'NotRequired'}})
    reserved_keyword_types = [
      ReservedKeywordType(
        iden=base,
        fields=keyword_field_code,
        docstring='\n'.join(
          f'- `{k}`: {indent_multiline_docstring(doc)}' for k in keyword_fields
            if (doc := type['fields'][k].get('docstring')) is not None
        ) or None
      )]
  else:
    base = 'TypedDict'
    reserved_keyword_types = []

  defn = f'class {type["id"]}({base}):'

  if (doc := type.get('docstring')) is not None:
    defn += f'\n  """{escape_docstring(doc)}"""'

  emitted_own_field = False
  for k, v in type['fields'].items():
    field = fields[k]
    imports.append(field.imports)
    reserved_keyword_types.extend(field.reserved_keyword_types)

    if iskeyword(k) or not k.isidentifier():
      continue

    emitted_own_field = True
    if v['required']:
      defn += f'\n  {k}: {field.iden}'
    else:
      defn += f'\n  {k}: NotRequired[{field.iden}]'
      imports.append({'typing_extensions': {'NotRequired'}})
    if (doc := v.get('docstring')) is not None:
      defn += f'\n  """{escape_docstring(doc)}"""'

  # `class {id}({base}):` needs a real body -- either a docstring, or at least one own
  # field. Every field could be reserved-keyword/non-identifier (`continue`d above, into
  # the `Keywords` base instead) even when `type['fields']` itself is non-empty --
  # an `order-id` path segment (hyphenated, invalid as a Python identifier) is
  # the real case that surfaced this: a one-field request schema whose only field routes
  # entirely to `{id}Keywords`, leaving this class with an empty body. The original guard
  # only checked `not type['fields']` (zero fields at all), which doesn't cover this.
  if not emitted_own_field and type.get('docstring') is None:
    defn += '\n  ...'
    
  code = Code(
    iden=type['id'],
    defn=defn,
    imports=merge_imports(imports),
    reserved_keyword_types=reserved_keyword_types,
  )
  return code

def mark_forward_references(
  type: Type, *, defined: Collection[str], scope: Mapping[str, object]
) -> None:
  """Flag every reference in `type` that names a record this module has not emitted yet.

  Only a record is marked, and only for a reference to *another* schema in the same
  module: a record's own name is quoted by `quote_self_reference` (which reads the
  rendered expression, and so covers a `CodeGenerator` used without a `Renderer`), and a
  reference out of the module resolves to an `import` at the top of the file, bound long
  before any class body runs.

  A reference can only point forward on a cycle, so on an acyclic module this marks
  nothing and the rendered source is byte-for-byte what it was. A cycle that reaches a
  schema rendering inline never gets here -- `InlineSchemas` raises `SchemaCycleError`
  during normalization.

  Args:
    type: One schema's parsed type tree, mutated in place.
    defined: Ids already emitted into this module.
    scope: Every schema this module renders, so a reference out of it can be told from
      one that merely comes later.
  """
  if type['type'] != 'record':
    return
  for node in refs(type):
    if node['id'] != type['id'] and node['id'] in scope and node['id'] not in defined:
      node['forward'] = True

T = TypeVar('T', bound=Type, contravariant=True)

class GeneratorFn(Protocol, Generic[T]):
  def __call__(self, type: T, recur: Callable[[Type], Code], /) -> Code:
    ...

@dataclass
class CodeGenerator:
  scalar: GeneratorFn[Scalar] = scalar
  ref: GeneratorFn[Ref] = ref
  literal: GeneratorFn[Literal] = literal
  list: GeneratorFn[List] = list
  tuple: GeneratorFn[Tuple] = tuple
  union: GeneratorFn[Union] = union
  dict: GeneratorFn[Dict] = dict
  record: GeneratorFn[Record] = record

  def __call__(self, type: Type, /, *, inline: bool = False) -> Code:
    match type['type']:
      case 'record':
        return self.record(type, self.__call__)
      case 'union':
        return self.union(type, self.__call__)
      case 'dict':
        return self.dict(type, self.__call__)
      case 'list':
        return self.list(type, self.__call__)
      case 'tuple':
        return self.tuple(type, self.__call__)
      case 'scalar':
        return self.scalar(type, self.__call__)
      case 'ref':
        return self.ref(type, self.__call__)
      case 'literal':
        return self.literal(type, self.__call__)
      case _:
        raise ValueError(f'Unknown type: {type}')

@dataclass(frozen=True)
class Renderer:
  parser: Parser = field(default_factory=Parser)
  code: CodeGenerator = field(default_factory=CodeGenerator)

  def __call__(self, schemas: Mapping[str, Schema], /, *, inline: bool = False) -> RenderedTypes:
    definitions: builtins.dict[str, str] = {}
    identifiers: builtins.dict[str, str] = {}
    imports: builtins.list[Imports] = []
    order: builtins.list[str] = []
    emitted: builtins.set[str] = set()

    for id in generation_order(schemas):
      if (s := schemas.get(id)) is not None:
        ir = self.parser(s, id=id)
        if ir['type'] == 'record':
          mark_forward_references(ir, defined=emitted, scope=schemas)
        else:
          # A schema that renders as an expression (`X = dict[str, Y]`, `X = list[Y]`)
          # has no class body to quote a name in: its right-hand side is evaluated the
          # moment the module is imported, so every name in it must already be bound.
          # `generation_order` guarantees that off a cycle; on one it cannot, and a
          # quoted forward reference is no help either -- a bare alias carries no module
          # to resolve it against later, so `pydantic` refuses the type at runtime.
          # `truewire check` reports the same shape as a `schema-cycle` violation
          # (`docs/spec/authoring.md` rule 17); this is the generator refusing to write
          # a module that would raise `NameError` on import if the gate were skipped.
          unbound = sorted(
            node['id'] for node in refs(ir)
            if node['id'] in schemas and node['id'] not in emitted
          )
          if unbound:
            raise SchemaCycleError([id, *(name for name in unbound if name != id)])
        code = self.code(ir)
        for type in code.reserved_keyword_types:
          order.append(type.iden)
          definitions[type.iden] = type.code()

        if code.defn is not None:
          order.append(id)
          definitions[id] = code.defn
          identifiers[id] = code.iden
        elif inline and code.iden != 'None':
          # A top-level schema resolving to the bare `None` ref (JSON Schema `{"type":
          # "null"}`) is the one ref identifier that can never be given a named alias:
          # `id = None` is valid Python, but the alias name is then a *variable* bound
          # to `None`, not usable as a type expression (`-> id:` -- pyright
          # reportInvalidTypeForm), unlike every other ref target (`bool`, a shared
          # schema's own class name, ...), which are real types. Fall through to the
          # plain (non-aliased) branch below instead, so `identifiers[id]` resolves
          # directly to the literal `None` -- valid in a return-annotation position,
          # the same rendering the OpenAPI-shaped predecessor of this code path
          # already produced for a `null`-typed response.
          order.append(id)
          definitions[id] = f'{id} = {code.iden}'
          identifiers[id] = id
        else:
          identifiers[id] = code.iden

        imports.append(code.imports)
        emitted.add(id)

    return RenderedTypes(
      definitions=definitions,
      identifiers=identifiers,
      imports=merge_imports(imports),
      generation_order=order
    )