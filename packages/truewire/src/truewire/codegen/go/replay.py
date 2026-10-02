"""`<package>/replay/replay.go`: every generated HTTP method with a typed and a raw form,
by function path, as the `twtest.Call` that replays a recorded example through it, and
every gRPC method as a `grpctest.Call` (`truewire.dev/core/grpc/grpctest`, imported only
when the client has one). A project's replay test hands `Table(client)` to
`twtest.ReplayHTTP` and `GrpcTable(client)` to `grpctest.Replay`."""
from truewire.plan.model import PackagePlan

from .endpoint import EndpointModule
from .names import string
from .printer import BANNER, Writer
from .routers import RouterModule
from .types import Package

REPLAY_DIR = 'replay'
REPLAY_FILE = f'{REPLAY_DIR}/replay.go'
TWTEST_IMPORT = 'truewire.dev/core/twtest'
GRPCTEST_IMPORT = 'truewire.dev/core/grpc/grpctest'


def replay_module(
  plan: PackagePlan, *, package: Package, root: RouterModule, routers: dict[tuple[str, ...], RouterModule],
  endpoints: dict[str, EndpointModule],
) -> str | None:
  lines: list[str] = []
  grpc_lines: list[str] = []
  for endpoint in plan.endpoints:
    rendered = endpoints.get(endpoint.function)
    if rendered is None or rendered.transport not in ('http', 'grpc'):
      continue
    parents = endpoint.path[:-1]
    access = ['client']
    reachable = True
    for depth in range(len(parents)):
      router = routers.get(tuple(parents[:depth]))
      if router is None or parents[depth] not in router.fields:
        reachable = False
        break
      access.append(router.fields[parents[depth]])
    owner = routers.get(tuple(parents))
    if not reachable or owner is None or endpoint.path[-1] not in owner.methods:
      continue
    kinds = dict((kind, name) for kind, name in owner.methods[endpoint.path[-1]])
    base = '.'.join(access)
    if rendered.transport == 'grpc':
      if 'call' in kinds:
        grpc_lines.append(f'table[{string(endpoint.function)}] = grpctest.ReplayOf({base}.{kinds["call"]})')
      continue
    if 'call' not in kinds or 'raw' not in kinds:
      continue
    helper = 'ReplayOf' if endpoint.request.type is not None else 'ReplayOfNoRequest'
    lines.append(f'table[{string(endpoint.function)}] = twtest.{helper}({base}.{kinds["call"]}, {base}.{kinds["raw"]})')
  if not lines and not grpc_lines:
    return None
  w = Writer()
  w.line(BANNER)
  w.blank()
  if grpc_lines:
    w.doc('Package replay maps every generated HTTP and gRPC method to the replay of its recorded examples, for twtest.ReplayHTTP and grpctest.Replay.')
  else:
    w.doc('Package replay maps every generated HTTP method to the replay of its recorded examples, for twtest.ReplayHTTP.')
  w.line(f'package {REPLAY_DIR}')
  w.blank()
  alias = root.package
  root_import = package.path()
  imports = sorted([
    *([(TWTEST_IMPORT, None)] if lines else []), *([(GRPCTEST_IMPORT, None)] if grpc_lines else []),
    (root_import, alias),
  ])
  w.line('import (')
  for path, name in imports:
    w.line(f'\t{name} "{path}"' if name else f'\t"{path}"')
  w.line(')')
  w.blank()
  if lines:
    w.doc('Table is every generated HTTP method with a typed and a raw form, by function path.')
    with w.block(f'func Table(client *{alias}.{root.struct_name}) map[string]twtest.Call {{'):
      w.line('table := map[string]twtest.Call{}')
      for line in lines:
        w.line(line)
      w.line('return table')
  if grpc_lines:
    if lines:
      w.blank()
    w.doc('GrpcTable is every generated gRPC method, by function path.')
    with w.block(f'func GrpcTable(client *{alias}.{root.struct_name}) map[string]grpctest.Call {{'):
      w.line('table := map[string]grpctest.Call{}')
      for line in grpc_lines:
        w.line(line)
      w.line('return table')
  return w.render()


__all__ = ['REPLAY_FILE', 'replay_module']
