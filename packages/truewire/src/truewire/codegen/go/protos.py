"""`<package>/protos/protos.go`: the project's `spec/proto` sources as `protos.Sources`, for a
hand-written core to compile with `protoframes.Compile` from `truewire.dev/core/protoframes`
(ADR 0016).

The map is filled from `init` one assignment per file rather than written as a keyed
literal, so the output needs no `gofmt` alignment whatever the file names are."""
import json
from typing_extensions import Mapping

from .printer import BANNER

PROTOS_DIR = 'protos'
PROTOS_FILE = f'{PROTOS_DIR}/protos.go'


def go_string(text: str) -> str:
  """A Go interpreted string literal: JSON's escapes (`\\n`, `\\"`, `\\uXXXX`) are all valid Go."""
  return json.dumps(text, ensure_ascii=True)


def protos_module(sources: Mapping[str, str]) -> str | None:
  """The file text, or `None` for a project with no `.proto` sources."""
  if not sources:
    return None
  lines = [
    BANNER,
    '',
    '// Package protos holds the `.proto` files under `spec/proto/`. A core decodes binary',
    '// WebSocket frames with protoframes.Compile(protos.Sources, "<Envelope>") from',
    '// truewire.dev/core/protoframes.',
    'package protos',
    '',
    '// Sources maps each `.proto` file under `spec/proto/`, by its path there, to its text.',
    'var Sources = map[string]string{}',
    '',
    'func init() {',
    *(f'\tSources[{go_string(name)}] = {go_string(text)}' for name, text in sources.items()),
    '}',
  ]
  return '\n'.join(lines) + '\n'


__all__ = ['PROTOS_DIR', 'PROTOS_FILE', 'protos_module']
