"""`proto.ts`: the project's `spec/proto` sources as `PROTO_SOURCES`, for a hand-written core
to compile with `ProtoFrames.compile` from `@truewire/core/protobuf` (ADR 0016)."""
import json
from typing_extensions import Mapping

from .printer import BANNER

PROTO_FILE = 'proto.ts'


def proto_module(sources: Mapping[str, str]) -> str | None:
  """The module text, or `None` for a project with no `.proto` sources."""
  if not sources:
    return None
  lines = [
    BANNER,
    '/**',
    ' * The `.proto` files under `spec/proto/`, by their path there. A core decodes binary',
    " * WebSocket frames with `ProtoFrames.compile(PROTO_SOURCES, '<Envelope>')` from",
    ' * `@truewire/core/protobuf`.',
    ' */',
    'export const PROTO_SOURCES: Readonly<Record<string, string>> = {',
    *(f'  {json.dumps(name)}: {json.dumps(text)},' for name, text in sources.items()),
    '}',
  ]
  return '\n'.join(lines) + '\n'


__all__ = ['PROTO_FILE', 'proto_module']
