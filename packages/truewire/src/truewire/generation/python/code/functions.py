from dataclasses import dataclass, field
import re

from truewire.generation.schema import Operation, Reference, Schema, ensure_nonref, ensure_ref
from truewire.generation.types import RenderedTypes
from truewire.generation.python.util import safe_identifier, body_param_name
from truewire.generation.openapi import BODY_KEY, RESPONSE_KEY

SELF_SHADOWING_BUILTINS = frozenset({
  'list', 'dict', 'set', 'frozenset', 'tuple', 'type',
  'str', 'bytes', 'bytearray', 'object', 'int', 'float', 'complex', 'bool',
})
"""Builtins a generated method can self-shadow in its own return annotation.

Python 3.14's default lazy annotation evaluation (PEP 649) resolves a class method's
annotations against a namespace that includes the enclosing class body's own attributes.
An endpoint class carries exactly one method (`Client` -- "one class wrapping one request
method"), so the only name that can collide is that method's own: a method named `list`
returning `list[Foo]` has `list` inside its own return annotation resolve to the method,
not the builtin, once the class body finishes executing -- and `inspect.signature()`
raises evaluating it. A `sub_account_api.list` endpoint is the worked case.
"""

def self_shadowing_alias(name: str, return_type: str) -> tuple[str, str] | None:
  """Return an alias name and its module-scope definition when `name` would self-shadow.

  `name` is a method's own name and `return_type` its rendered return annotation. Emit
  the definition outside the class body, where the builtin generic it names still
  resolves to the builtin rather than to the method carrying the alias.

  Args:
    name: The generated method's own name.
    return_type: The method's rendered return annotation.

  Returns:
    The alias identifier and its `alias = return_type` definition, or None when no
    builtin used in `return_type` matches the method's own name.
  """
  if name not in SELF_SHADOWING_BUILTINS:
    return None
  if not re.search(rf'\b{re.escape(name)}\b', return_type):
    return None
  alias = f'{name[:1].upper()}{name[1:]}Response'
  return alias, f'{alias} = {return_type}'

def group_lines(codes: list[str], *, tab: str, width: int) -> list[str]:
  """Greedily pack `codes` into comma-terminated lines no wider than `width`.

  The house style is grouped multi-line headers rather than one
  parameter per line, so parameters are packed until the next one would not fit.

  Args:
    codes: Rendered parameters, in order.
    tab: Indentation prefix for each line.
    width: Maximum line length.
  """
  lines: list[str] = []
  chunk: list[str] = []
  for code in codes:
    candidate = [*chunk, code]
    if chunk and len(tab) + len(', '.join(candidate)) + 1 > width:
      lines.append(tab + ', '.join(chunk) + ',')
      chunk = [code]
    else:
      chunk = candidate
  if chunk:
    lines.append(tab + ', '.join(chunk) + ',')
  return lines

@dataclass(kw_only=True)
class Param:
  """One parameter of a generated function header."""
  name: str
  type: str | None = None
  default: str | None = None
  required: bool = True

  def code(self) -> str:
    """Render the parameter as it appears in the header.

    An optional (`required=False`) parameter's own type gets `| None` appended -- unless
    the resolved type is already nullable on its own (`Literal['rebased', 'base'] |
    None`, say, from a property whose schema is itself `anyOf: [..., {"type": "null"}]`),
    in which case appending unconditionally would double it up
    (`Literal['rebased', 'base'] | None | None`) -- valid Python, but a needless,
    confusing repetition. One real spec has 31 occurrences of exactly this shape (an
    optional property whose own type already carries `| None`).
    """
    code = self.name
    if self.type:
      code += f': {self.type}'
      if not self.required and not self.type.endswith('| None'):
        code += ' | None'
    if self.default:
      code += f' = {self.default}'
    elif not self.required:
      code += ' = None'
    return code

@dataclass(kw_only=True)
class Function:
  """Function header generator."""
  Param = Param
  
  name: str
  asyn: bool
  method: bool
  args: list[Param] = field(default_factory=list)
  kwargs: list[Param] = field(default_factory=list)
  return_type: str | None = None
  return_type_alias: str | None = None
  """`{alias} = {original return type}`, when the method's own name would otherwise
  self-shadow a builtin generic in its return annotation (`self_shadowing_alias`).
  Emit at module scope, before the class -- `return_type` already reads the alias name."""
  decorators: list[str] = field(default_factory=list)
  tab: str = '  '

  def code(self, *, max_line_length: int = 80) -> str:
    """Render the header, on one line when it fits and grouped otherwise."""
    arg_codes = ['self'] if self.method else []
    arg_codes.extend(arg.code() for arg in self.args)
    arg_len = sum(map(len, arg_codes))

    kwarg_codes = [kwarg.code() for kwarg in self.kwargs]
    kwarg_len = sum(map(len, kwarg_codes))
    
    return_type = self.return_type or ''

    code = ''
    if self.decorators:
      code += '\n'.join(self.decorators) + '\n'
    code += 'async def' if self.asyn else 'def'
    code += f' {self.name}('

    if len(code) + arg_len + kwarg_len + 1 + len(return_type) <= max_line_length:
      code += ', '.join(arg_codes)
      if self.kwargs:
        code += ', *, '
        code += ', '.join(kwarg_codes)
      code += ')'
    
    else:
      positional = [*arg_codes, '*'] if kwarg_codes else arg_codes
      lines = group_lines(positional, tab=self.tab, width=max_line_length)
      lines += group_lines(kwarg_codes, tab=self.tab, width=max_line_length)
      code += '\n' + '\n'.join(lines) + '\n)'

    if self.return_type:
      code += f' -> {self.return_type}'
    
    code += ':'
    return code

  _order = {
    'path': 0,
    'query': 1,
    'header': 2,
    'cookie': 3,
  }

  @classmethod
  def parse(
    cls, op: Operation, types: RenderedTypes, *,
    name: str, asyn: bool, method: bool,
    decorators: list[str] = [],
    body_schema: Schema | Reference | None = None,
  ):
    header = cls(name=name, asyn=asyn, method=method, decorators=decorators)
    params = [ensure_nonref(p) for p in op.parameters or []]
    for p in sorted(params, key=lambda p: cls._order[p.in_]):
      s = ensure_ref(p.schema_)
      name = safe_identifier(p.name)
      type = types.identifiers[s.ref]
      fn_param = Function.Param(
        name=name,
        type=type,
        required=p.required,
      )
      if p.in_ == 'path':
        header.args.append(fn_param)
      else:
        header.kwargs.append(fn_param)

    union: dict[str, None] = {}
    for code, r in op.responses.items():
      r = ensure_nonref(r)
      if type := types.identifiers.get(RESPONSE_KEY(code)):
        union[type] = None

    if union:
      header.return_type = ' | '.join(union)
      if (shadow := self_shadowing_alias(header.name, header.return_type)) is not None:
        header.return_type, header.return_type_alias = shadow[0], shadow[1]

    if op.request_body:
      type = types.identifiers.get(BODY_KEY)
      header.args.append(Function.Param(
        name=body_param_name(type, body_schema),
        type=type,
        required=True,
      ))

    return header