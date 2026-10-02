"""`<package>/protos.rs`: the project's `spec/proto` sources as `SOURCES`, for a hand-written
core to compile with `truewire_core::proto::Protos::from_sources` (feature `proto`, ADR 0016).

Each entry is printed the way `rustfmt` leaves it: on one line when it fits, else as a
vertical tuple; a string literal is never split, so an overlong one stays as it is."""
from typing_extensions import Mapping

from .printer import BANNER

PROTOS_FILE = 'protos.rs'
PROTOS_MODULE = 'protos'
MAX_WIDTH = 100


def rust_string(text: str) -> str:
  """A Rust string literal: `\\\\`, `"`, newlines, tabs and other control characters escaped."""
  out = ['"']
  for char in text:
    if char == '\\':
      out.append('\\\\')
    elif char == '"':
      out.append('\\"')
    elif char == '\n':
      out.append('\\n')
    elif char == '\r':
      out.append('\\r')
    elif char == '\t':
      out.append('\\t')
    elif ord(char) < 0x20 or ord(char) == 0x7f:
      out.append(f'\\u{{{ord(char):x}}}')
    else:
      out.append(char)
  out.append('"')
  return ''.join(out)


def protos_module(sources: Mapping[str, str]) -> str | None:
  """The file text, or `None` for a project with no `.proto` sources."""
  if not sources:
    return None
  lines = [
    BANNER,
    '//!',
    '//! The `.proto` files under `spec/proto/`, for a core to decode binary WebSocket frames with',
    '//! `truewire_core::proto::Protos::from_sources(SOURCES)` (feature `proto`).',
    '',
    '/// Each `.proto` file under `spec/proto/`, by its path there (the name an `import` uses), and its text.',
    'pub const SOURCES: &[(&str, &str)] = &[',
  ]
  for name, text in sources.items():
    one = f'    ({rust_string(name)}, {rust_string(text)}),'
    if len(one) <= MAX_WIDTH:
      lines.append(one)
    else:
      lines += ['    (', f'        {rust_string(name)},', f'        {rust_string(text)},', '    ),']
  lines.append('];')
  return '\n'.join(lines) + '\n'


__all__ = ['PROTOS_FILE', 'PROTOS_MODULE', 'protos_module', 'rust_string']
