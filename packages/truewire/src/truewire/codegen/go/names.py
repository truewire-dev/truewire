"""Naming for the Go backend.

Go exports by capitalisation, so every identifier a caller reaches (a type, a struct field,
a method, a constant) is `PascalCase` of the wire name, with Go's common initialisms kept
upper-case (`html_url` -> `HTMLURL`, `node_id` -> `NodeID`). The wire name itself is never
lost: a record's generated `MarshalJSON`/`UnmarshalJSON` name every key as the wire spells
it. A package is the lower-cased words of its directory segment run together, the Go
convention for package names. Identifiers Truewire invents come from the function segment
the same way (`ListCommits`, `ListCommitsPaged`, package `listcommits`).
"""
import json
import re

KEYWORDS: frozenset[str] = frozenset((
  'break', 'case', 'chan', 'const', 'continue', 'default', 'defer', 'else', 'fallthrough',
  'for', 'func', 'go', 'goto', 'if', 'import', 'interface', 'map', 'package', 'range',
  'return', 'select', 'struct', 'switch', 'type', 'var',
))
"""Go's keywords: never an identifier."""

PREDECLARED: frozenset[str] = frozenset((
  'any', 'bool', 'byte', 'comparable', 'complex64', 'complex128', 'error', 'float32',
  'float64', 'int', 'int8', 'int16', 'int32', 'int64', 'rune', 'string', 'uint', 'uint8',
  'uint16', 'uint32', 'uint64', 'uintptr', 'true', 'false', 'iota', 'nil', 'append', 'cap',
  'clear', 'close', 'complex', 'copy', 'delete', 'imag', 'len', 'make', 'max', 'min', 'new',
  'panic', 'print', 'println', 'real', 'recover',
))
"""Predeclared identifiers: legal to shadow, confusing as a package or local name."""

INITIALISMS: frozenset[str] = frozenset((
  'acl', 'api', 'ascii', 'cpu', 'css', 'dns', 'eof', 'guid', 'html', 'http', 'https', 'id',
  'ip', 'json', 'jwt', 'lhs', 'qps', 'ram', 'rhs', 'rpc', 'sla', 'smtp', 'sql', 'ssh', 'tcp',
  'tls', 'ttl', 'udp', 'ui', 'uid', 'uri', 'url', 'utf8', 'uuid', 'vm', 'xml', 'xmpp', 'xsrf',
  'xss',
))
"""The initialisms `golint`/`staticcheck` want upper-case in an identifier."""

_WORD = re.compile(r'[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z0-9]+|[A-Z]+|[0-9]+')


def words(name: str) -> list[str]:
  """The words of a wire name, lower-cased: `htmlUrl` -> `html url`, `per_page` -> `per page`."""
  out: list[str] = []
  for chunk in re.split(r'[^A-Za-z0-9]+', name):
    out.extend(part.lower() for part in _WORD.findall(chunk))
  return out


def pascal_ident(name: str, *, fallback: str = 'Value') -> str:
  """`name` as an exported Go identifier: each word capitalised (an initialism upper-cased),
  a leading digit prefixed by `N`, an empty name replaced by `fallback`."""
  out = ''.join(word.upper() if word in INITIALISMS else word[:1].upper() + word[1:] for word in words(name))
  if not out:
    out = fallback
  if out[0].isdigit():
    out = f'N{out}'
  return out


def camel_ident(name: str, *, fallback: str = 'value') -> str:
  """`name` as an unexported Go identifier (`listCommits`), a keyword or predeclared name
  suffixed by `_`."""
  parts = words(name)
  if not parts:
    return fallback
  head, rest = parts[0], parts[1:]
  out = head + ''.join(word.upper() if word in INITIALISMS else word[:1].upper() + word[1:] for word in rest)
  if out[0].isdigit():
    out = f'n{out}'
  if out in KEYWORDS or out in PREDECLARED:
    out = f'{out}_'
  return out


def package_ident(name: str, *, fallback: str = 'pkg') -> str:
  """`name` as a Go package name: its words lower-cased and run together (`list_commits` ->
  `listcommits`), a leading digit prefixed by `n`, a keyword or predeclared name suffixed by
  `pkg`."""
  out = ''.join(words(name)) or fallback
  if out[0].isdigit():
    out = f'n{out}'
  if out in KEYWORDS or out in PREDECLARED:
    out = f'{out}pkg'
  return out


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
  """A Go interpreted string literal. JSON escaping is a subset of Go's, except that Go
  needs no escape for `/` and JSON never emits one; non-ASCII stays as it is."""
  return json.dumps(value, ensure_ascii=False)


def literal(value: object) -> str:
  """A JSON scalar as a Go literal of the type `meta_type`/`scalar` renders it to."""
  if isinstance(value, str):
    return string(value)
  if value is True:
    return 'true'
  if value is False:
    return 'false'
  if value is None:
    return 'nil'
  if isinstance(value, int):
    return str(value)
  if isinstance(value, float):
    text = repr(value)
    return text if '.' in text or 'e' in text else f'{text}.0'
  raise TypeError(f'not a scalar: {value!r}')


def json_value(value: object) -> str:
  """Any JSON value as a Go expression of type `any` that marshals back to it: a scalar
  literal, or `json.RawMessage` of its text for a structure."""
  if isinstance(value, (dict, list)):
    return f'json.RawMessage({string(json.dumps(value, ensure_ascii=False, separators=(",", ":")))})'
  return literal(value)


__all__ = [
  'INITIALISMS', 'KEYWORDS', 'PREDECLARED', 'camel_ident', 'json_value', 'literal',
  'package_ident', 'pascal_ident', 'string', 'unique', 'words',
]
