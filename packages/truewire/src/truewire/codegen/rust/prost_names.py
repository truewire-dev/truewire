"""How `prost-build` names what it generates from a `.proto` tree, mirrored so a generated
endpoint can name the stubs `truewire protos rust` builds without reading them (ADR 0017).

`prost-build` (behind `protoc-gen-prost`) spells identifiers with `heck`: a package is a
module per segment (`cosmos::bank::v1beta1`), a message or enum is `UpperCamelCase`, a
nested type lives in a module named `snake_case` after its parent (`search_response::Hit`),
and a field is `snake_case`. Rust keywords become raw identifiers (`r#type`), and the few
that cannot (`self`, `super`, `crate`, `extern`, `Self`, `_`) take a trailing `_`.

Types: a singular message field is `Option<T>`, a proto3 `optional` scalar `Option<T>`,
`repeated` a `Vec<T>`, `map` a `HashMap`, `bytes` a `Vec<u8>`, an enum an `i32`, and the
well-known types come from `prost-types` (`google.protobuf.Empty` is `()`).
"""
from truewire.plan.model import ProtoFieldPlan, ProtoTypePlan

_RAW = frozenset((
  'as', 'break', 'const', 'continue', 'else', 'enum', 'false', 'fn', 'for', 'if', 'impl', 'in',
  'let', 'loop', 'match', 'mod', 'move', 'mut', 'pub', 'ref', 'return', 'static', 'struct',
  'trait', 'true', 'type', 'unsafe', 'use', 'where', 'while', 'dyn', 'abstract', 'become',
  'box', 'do', 'final', 'macro', 'override', 'priv', 'typeof', 'unsized', 'virtual', 'yield',
  'async', 'await', 'try', 'gen',
))
_SUFFIXED = frozenset(('_', 'super', 'self', 'Self', 'extern', 'crate'))

SCALARS = {
  'double': 'f64', 'float': 'f32', 'int32': 'i32', 'int64': 'i64', 'uint32': 'u32', 'uint64': 'u64',
  'sint32': 'i32', 'sint64': 'i64', 'fixed32': 'u32', 'fixed64': 'u64', 'sfixed32': 'i32',
  'sfixed64': 'i64', 'bool': 'bool', 'string': 'String', 'bytes': 'Vec<u8>',
}
"""The Rust type `prost` gives each proto scalar."""

COPY_SCALARS = frozenset(('f64', 'f32', 'i32', 'i64', 'u32', 'u64', 'bool'))

PROTOS_MODULE = 'protos'
WELL_KNOWN_PREFIX = 'google/protobuf/'


def heck_words(name: str) -> list[str]:
  """`heck`'s word split: on every non-alphanumeric character, between a lowercase letter
  and an uppercase one, and before the last capital of a capital run followed by a
  lowercase letter (`HTTPServer` -> `HTTP`, `Server`). Digits never start a word."""
  out: list[str] = []
  for word in ''.join(c if c.isalnum() else ' ' for c in name).split():
    init, mode = 0, 'boundary'
    for i, c in enumerate(word):
      if i + 1 >= len(word):
        out.append(word[init:])
        break
      following = word[i + 1]
      next_mode = 'lower' if c.islower() else 'upper' if c.isupper() else mode
      if next_mode == 'lower' and following.isupper():
        out.append(word[init:i + 1])
        init, mode = i + 1, 'boundary'
      elif mode == 'upper' and c.isupper() and following.islower():
        if i > init:
          out.append(word[init:i])
        init, mode = i, 'boundary'
      else:
        mode = next_mode
  return [w for w in out if w]


def sanitize(ident: str) -> str:
  if ident in _RAW:
    return f'r#{ident}'
  if ident in _SUFFIXED:
    return f'{ident}_'
  if ident[:1].isdigit():
    return f'_{ident}'
  return ident


def to_snake(name: str) -> str:
  return sanitize('_'.join(word.lower() for word in heck_words(name)))


def to_upper_camel(name: str) -> str:
  return sanitize(''.join(word[:1].upper() + word[1:].lower() for word in heck_words(name)))


def package_path(package: str) -> list[str]:
  """`cosmos.bank.v1beta1` -> `['cosmos', 'bank', 'v1beta1']`: the modules of a package."""
  return [to_snake(segment) for segment in package.split('.') if segment]


def type_path(t: ProtoTypePlan) -> str:
  """The Rust path of a message or enum type: `crate::protos::demo::bank::v1::search_response::Hit`,
  `::prost_types::Timestamp`. An enum's Rust field type is `i32`; this names the enum itself."""
  if t.kind == 'scalar':
    return SCALARS[t.name]
  if t.file is not None and t.file.startswith(WELL_KNOWN_PREFIX):
    local = t.name.rsplit('.', 1)[-1]
    return '()' if local == 'Empty' else f'::prost_types::{local}'
  package = t.package or ''
  relative = t.name[len(package) + 1:] if package and t.name.startswith(package + '.') else t.name.rsplit('.', 1)[-1]
  parts = relative.split('.')
  modules = [*package_path(package), *(to_snake(parent) for parent in parts[:-1])]
  return '::'.join(['crate', PROTOS_MODULE, *modules, to_upper_camel(parts[-1])])


def field_ident(field: ProtoFieldPlan) -> str:
  return to_snake(field.name)


def is_option(field: ProtoFieldPlan) -> bool:
  """Whether `prost` wraps the field in an `Option`: a singular message, or an `optional` scalar."""
  if field.repeated or field.map_key is not None or field.oneof is not None:
    return False
  return field.type.kind == 'message' or field.optional


def value_type(field: ProtoFieldPlan) -> str | None:
  """The Rust type of a singular non-message field's value (inside its `Option` when it has
  one): a scalar's, `i32` for an enum; `None` for anything else."""
  if field.repeated or field.map_key is not None:
    return None
  if field.type.kind == 'scalar':
    return SCALARS.get(field.type.name)
  if field.type.kind == 'enum':
    return 'i32'
  return None


__all__ = [
  'COPY_SCALARS', 'PROTOS_MODULE', 'SCALARS', 'field_ident', 'heck_words', 'is_option', 'package_path',
  'to_snake', 'to_upper_camel', 'type_path', 'value_type',
]
