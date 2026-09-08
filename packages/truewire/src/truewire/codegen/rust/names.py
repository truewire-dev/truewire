"""Naming for the Rust backend.

Rust has one convention per kind of identifier and `rustc` warns on every departure, so
the wire's names are not kept verbatim the way TypeScript keeps them: a field is
`snake_case` of the wire name and carries `#[serde(rename = "...")]` with the wire name
whenever the two differ, a type is `PascalCase`, a literal's variants are `PascalCase` of
their values. Identifiers Truewire invents (methods, modules, structs) come from the
function segment the same way (`list_commits`, `ListCommits`, `list_commits_paged`).
"""
import json
import re

from truewire.codegen.layout import class_name as pascal_case

RESERVED: frozenset[str] = frozenset((
  'as', 'async', 'await', 'break', 'const', 'continue', 'crate', 'dyn', 'else', 'enum',
  'extern', 'false', 'fn', 'for', 'if', 'impl', 'in', 'let', 'loop', 'match', 'mod', 'move',
  'mut', 'pub', 'ref', 'return', 'self', 'Self', 'static', 'struct', 'super', 'trait', 'true',
  'type', 'unsafe', 'use', 'where', 'while', 'abstract', 'become', 'box', 'do', 'final',
  'gen', 'macro', 'override', 'priv', 'try', 'typeof', 'unsized', 'virtual', 'yield', 'union',
))
"""Keywords, strict and reserved, plus `union` (contextual, but confusing as a field)."""

_WORD = re.compile(r'[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z0-9]+|[A-Z]+|[0-9]+')
_IDENTIFIER = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def words(name: str) -> list[str]:
  """The words of a wire name, lower-cased: `htmlUrl` -> `html url`, `HTMLParser` ->
  `html parser`, `X-Rate` -> `x rate`, `per_page` -> `per page`."""
  out: list[str] = []
  for chunk in re.split(r'[^A-Za-z0-9]+', name):
    out.extend(part.lower() for part in _WORD.findall(chunk))
  return out


def snake_ident(name: str, *, fallback: str = 'field') -> str:
  """`name` as a Rust `snake_case` identifier: words joined by `_`, a leading digit
  prefixed by `_`, a keyword suffixed by `_`, an empty name replaced by `fallback`."""
  out = '_'.join(words(name)) or fallback
  if out[0].isdigit():
    out = f'_{out}'
  if out in RESERVED:
    out = f'{out}_'
  return out


def pascal_ident(name: str, *, fallback: str = 'Value') -> str:
  """`name` as a Rust `PascalCase` identifier (a type, a variant): a leading digit is
  prefixed by `N`, an empty name replaced by `fallback`."""
  out = ''.join(word[:1].upper() + word[1:] for word in words(name)) or fallback
  if out[0].isdigit():
    out = f'N{out}'
  return out


def is_identifier(name: str) -> bool:
  return bool(_IDENTIFIER.match(name)) and name not in RESERVED


def unique(name: str, taken: set[str]) -> str:
  """`name`, or `name2`, `name3`, ... until it is not in `taken`; the result is added."""
  candidate = name
  n = 1
  while candidate in taken:
    n += 1
    candidate = f'{name}{n}'
  taken.add(candidate)
  return candidate


def string(value: str) -> str:
  """A Rust string literal: JSON escaping is a subset of Rust's."""
  return json.dumps(value, ensure_ascii=False)


def literal(value: object) -> str:
  """A JSON scalar as the Rust literal of the type `meta_type`/`scalar` renders it to:
  `"x"` for a string, `1` for an integer, `1.5` for a number, `true`/`false`."""
  if isinstance(value, str):
    return string(value)
  if value is True:
    return 'true'
  if value is False:
    return 'false'
  if value is None:
    return 'serde_json::Value::Null'
  if isinstance(value, int):
    return str(value)
  if isinstance(value, float):
    text = repr(value)
    return text if '.' in text or 'e' in text else f'{text}.0'
  raise TypeError(f'not a scalar: {value!r}')


def json_expr(value: object) -> str:
  """A JSON value as a `serde_json::json!(...)` invocation."""
  return f'serde_json::json!({json.dumps(value, ensure_ascii=False)})'


__all__ = [
  'RESERVED', 'is_identifier', 'json_expr', 'literal', 'pascal_case', 'pascal_ident',
  'snake_ident', 'string', 'unique', 'words',
]
