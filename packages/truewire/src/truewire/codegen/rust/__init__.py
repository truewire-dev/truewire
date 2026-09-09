"""The Rust codegen backend: the plan (`truewire plan`) rendered as the modules of a crate
over `truewire-core`.

`render_package` is the whole backend: it walks `plan.schemas`, `plan.endpoints` and
`plan.routers` and returns every file the package consists of, keyed by its path under
`<src>/<package>/`. The CLI (`truewire generate rust`) owns the manifest, the writing and
`--check`; nothing here touches the filesystem.

Layout, one file per spec node, beside the Python and TypeScript packages:

- `types/mod.rs` (and `types/<scope>.rs`): the shared `schemas.json` scopes.
- `<router>/<endpoint>.rs`: one endpoint's structs and enums, and its endpoint struct.
- `<router>/mod.rs`: a router struct delegating to its endpoints; `client.rs` the root.
- `meta.rs`: one struct per core that declares a `meta` schema.
- `lib.rs`: the crate root, declaring the modules above and the hand-written `core`.

What this backend leaves out is reported in `Rendered.skipped` so the CLI can say so:
`stream` endpoints, `rpc` endpoints reached only over a WebSocket, routers under a
composite core (`children`/`forward` in `truewire.toml`) and their subtrees, and the
walkers `docs/rust.md` lists as not built. An `rpc` endpoint declaring both `http` and `ws`
transports is rendered for HTTP only.
"""
from dataclasses import dataclass, field

from truewire.codegen.shapes import core_shapes
from truewire.plan.model import PackagePlan
from truewire.project import Project

from .endpoint import EndpointModule, _Skipped, render_endpoint, render_stream_endpoint
from .meta import META_FILE, meta_module, meta_shapes
from .names import pascal_case, snake_ident
from .printer import BANNER
from .routers import LIB_FILE, RouterModule, render_lib, render_router
from .types import Module, scope_children, scope_file, scope_segments


@dataclass
class Rendered:
  files: dict[str, str] = field(default_factory=dict)
  """Package-relative POSIX path -> content, banner included."""
  skipped: list[str] = field(default_factory=list)
  """Function paths of endpoints and routers this backend does not emit, with the reason."""


def root_struct_name(plan: PackagePlan, project: Project | None) -> str:
  """`[rust].name`, else the plan's (`[python].name` or PascalCase of the project)."""
  if project is not None and project.rust is not None and project.rust.name:
    return project.rust.name
  return plan.root_class


def render_package(plan: PackagePlan, project: Project | None = None) -> Rendered:
  """Every file of the Rust package for `plan`."""
  out = Rendered()
  root_name = root_struct_name(plan, project)

  # Shared scopes, plus the intermediate modules a nested scope sits under.
  scopes = set(plan.schemas)
  for scope in list(plan.schemas):
    segments = scope_segments(scope)
    scopes.update('/'.join(scope.split('/')[:depth]) for depth in range(len(segments)))
  for scope in sorted(scopes):
    types = plan.schemas.get(scope, {})
    module = Module(plan, scope_file(plan, scope), local=types, visible=[scope] if scope else [''])
    module.define_all(types)
    where = 'spec/schemas.json' if scope == '' else f'spec/endpoints/{scope}/schemas.json'
    doc = f'Shapes shared by two or more endpoints, generated from `{where}`.' if types else None
    head = [f'pub mod {child};' for child in scope_children(plan, scope)]
    out.files[scope_file(plan, scope)] = module.render(BANNER, doc=doc, head=head)

  meta = meta_module(plan.cores)
  if meta is not None:
    out.files[META_FILE] = meta
  shapes = meta_shapes(plan.cores)

  class_by_child = {
    (*router.path, child.name): child.class_
    for router in plan.routers for child in router.children
  }
  endpoints: dict[str, EndpointModule] = {}
  for endpoint in plan.endpoints:
    if endpoint.kind == 'stream':
      struct_name = class_by_child.get(tuple(endpoint.path), pascal_case(endpoint.path[-1]))
      try:
        rendered, notes = render_stream_endpoint(
          plan, endpoint, struct_name=struct_name, meta=shapes.get(endpoint.core),
        )
      except _Skipped as skipped:
        out.skipped.append(f'{endpoint.function}: {skipped.reason}')
        continue
      out.skipped.extend(notes)
      endpoints[endpoint.function] = rendered
      out.files[rendered.file] = rendered.source
      continue
    if 'http' not in endpoint.transports:
      out.skipped.append(f'{endpoint.function}: an rpc endpoint over a WebSocket has no Rust rendering yet')
      continue
    if 'ws' in endpoint.transports:
      out.skipped.append(f'{endpoint.function}: an rpc endpoint with both transports is generated for HTTP only')
    struct_name = class_by_child.get(tuple(endpoint.path), pascal_case(endpoint.path[-1]))
    rendered, notes = render_endpoint(plan, endpoint, struct_name=struct_name, meta=shapes.get(endpoint.core))
    out.skipped.extend(notes)
    endpoints[endpoint.function] = rendered
    out.files[rendered.file] = rendered.source

  shapes_by_router = core_shapes(plan, lambda function: endpoints[function].bound if function in endpoints else None)
  routers: dict[tuple[str, ...], RouterModule] = {}
  for router in sorted(plan.routers, key=lambda r: -len(r.path)):
    struct_name = root_name if not router.path else class_by_child[tuple(router.path)]
    rendered = render_router(
      plan, router, struct_name=struct_name, endpoints=endpoints, routers=routers,
      shapes=shapes_by_router,
    )
    if rendered is not None:
      routers[tuple(router.path)] = rendered
      out.files[rendered.file] = rendered.source

  # A module nothing declares would be written but never compiled: drop every router and
  # endpoint under a router that was skipped. The drop is reported rather than silent --
  # skipping one router takes everything beneath it, and a caller who reads `skipped` as a
  # list of endpoints would otherwise see a successful `Generated` line and a library with
  # no client in it.
  reachable = {path for path in routers if all(path[:depth] in routers for depth in range(len(path)))}
  dropped_routers, dropped_endpoints = [], []
  for path, rendered in routers.items():
    if path not in reachable:
      out.files.pop(rendered.file, None)
      dropped_routers.append('.'.join(path) or '(root)')
  for function, module in list(endpoints.items()):
    if tuple(function.split('.')[:-1]) not in reachable:
      out.files.pop(module.file, None)
      del endpoints[function]
      dropped_endpoints.append(function)
  if dropped_endpoints or dropped_routers:
    lost = ', '.join(sorted(dropped_routers) + sorted(dropped_endpoints))
    out.skipped.append(
      f'{len(dropped_endpoints)} endpoint(s) and {len(dropped_routers)} router(s) under a '
      f'skipped router: nothing declares them, so they were rendered and then dropped -- '
      f'{lost}'
    )

  root = routers.get(()) if () in reachable else None
  root_modules = sorted(
    {snake_ident(path[0], fallback='router') for path in reachable if len(path) == 1}
    | {snake_ident(function, fallback='endpoint') for function in endpoints if '.' not in function}
  )
  out.files[LIB_FILE] = render_lib(plan, root=root, root_modules=root_modules, has_meta=meta is not None)
  return out


__all__ = ['Rendered', 'is_composite', 'render_package', 'root_struct_name']
