"""Naming for the TypeScript backend.

Two rules, and only two. Identifiers Truewire invents (methods, classes, files' exported
namespaces) are camelCase/PascalCase of the function segment: `list_commits` becomes
`listCommits`, its class `ListCommits`. Identifiers the API invented (request and response
property names) stay verbatim, quoted when they are not JavaScript identifiers, so the
request object *is* the wire object and a recorded example binds to a call with no
field map in between.
"""
import json
import re

from truewire.codegen.layout import class_name as pascal_case

RESERVED: frozenset[str] = frozenset((
  'await', 'break', 'case', 'catch', 'class', 'const', 'continue', 'debugger', 'default',
  'delete', 'do', 'else', 'enum', 'export', 'extends', 'false', 'finally', 'for', 'function',
  'if', 'implements', 'import', 'in', 'instanceof', 'interface', 'let', 'new', 'null',
  'package', 'private', 'protected', 'public', 'return', 'static', 'super', 'switch', 'this',
  'throw', 'true', 'try', 'typeof', 'var', 'void', 'while', 'with', 'yield',
  # Not reserved, but a binding by one of these names shadows something every module reads.
  'undefined', 'NaN', 'Infinity', 'arguments', 'eval',
))
"""Words that cannot name a binding (`import * as delete`); fine as property or method names."""

_IDENTIFIER = re.compile(r'^[A-Za-z_$][A-Za-z0-9_$]*$')


def camel_case(segment: str) -> str:
  """`list_commits` -> `listCommits`; `get` -> `get`; `getRepo` -> `getRepo`."""
  parts = [part for part in segment.replace('-', '_').split('_') if part]
  if not parts:
    return segment
  head, *rest = parts
  return head[:1].lower() + head[1:] + ''.join(part[:1].upper() + part[1:] for part in rest)


def is_identifier(name: str) -> bool:
  """Whether `name` can appear bare as a property key or member name."""
  return bool(_IDENTIFIER.match(name))


def is_binding(name: str) -> bool:
  """Whether `name` can be declared (`const name`, `import * as name`)."""
  return is_identifier(name) and name not in RESERVED


def binding(name: str, taken: set[str] = frozenset()) -> str:  # type: ignore[assignment]
  """`name` made declarable and unique against `taken`, by a `$` suffix."""
  candidate = name if is_binding(name) else f'{name}$'
  while candidate in taken:
    candidate += '$'
  return candidate


def string(value: str) -> str:
  """A single-quoted TypeScript string literal."""
  body = json.dumps(value, ensure_ascii=False)[1:-1].replace("\\'", "'").replace("'", "\\'").replace('\\"', '"')
  return f"'{body}'"


def property_key(name: str) -> str:
  """`name` as an object or interface key: bare when it is an identifier, quoted otherwise."""
  return name if is_identifier(name) else string(name)


def member_access(subject: str, name: str, optional: bool = True) -> str:
  """`subject?.name` or `subject?.['weird-name']` (plain `.` when not `optional`)."""
  dot = '?.' if optional else '.'
  if is_identifier(name):
    return f'{subject}{dot}{name}'
  return f'{subject}{dot}[{string(name)}]' if optional else f'{subject}[{string(name)}]'


def literal(value: object) -> str:
  """A JSON value as a TypeScript literal: single-quoted strings, bare keys where possible."""
  if isinstance(value, str):
    return string(value)
  if value is None:
    return 'null'
  if value is True:
    return 'true'
  if value is False:
    return 'false'
  if isinstance(value, (int, float)):
    return json.dumps(value)
  if isinstance(value, list):
    return '[' + ', '.join(literal(item) for item in value) + ']'
  if isinstance(value, dict):
    if not value:
      return '{}'
    return '{ ' + ', '.join(f'{property_key(str(k))}: {literal(v)}' for k, v in value.items()) + ' }'
  raise TypeError(f'not a JSON value: {value!r}')


__all__ = [
  'RESERVED', 'binding', 'camel_case', 'is_binding', 'is_identifier', 'literal', 'member_access',
  'pascal_case', 'property_key', 'string',
]
