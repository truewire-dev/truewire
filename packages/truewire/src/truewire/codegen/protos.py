"""The project's `spec/proto/**/*.proto` sources, as the non-Python backends embed them (ADR 0016).

A protobuf-framed WebSocket push is typed by the `.proto` files under `spec/proto/`; each
backend writes them verbatim into the generated package (TypeScript `proto.ts`, Go
`protos/protos.go`, Rust later) and the runtime compiles them on first use
(`@truewire/core/protobuf`, `truewire.dev/core/protoframes`). No protoc step, so a project
regenerates on any machine that runs `truewire generate`.
"""
from pathlib import Path

from truewire.project import Project

PROTO_DIR = 'proto'
"""`spec/<PROTO_DIR>/`: where a project keeps its `.proto` sources."""


def proto_sources(project: Project | None) -> dict[str, str]:
  """Every `.proto` file under `<spec>/proto/`, keyed by its POSIX path relative to that
  directory (the name an `import` statement uses), sorted; `{}` when there is none."""
  if project is None:
    return {}
  root: Path = project.spec_dir / PROTO_DIR
  if not root.is_dir():
    return {}
  return {
    path.relative_to(root).as_posix(): path.read_text(encoding='utf-8')
    for path in sorted(root.rglob('*.proto'))
    if path.is_file()
  }


__all__ = ['PROTO_DIR', 'proto_sources']
