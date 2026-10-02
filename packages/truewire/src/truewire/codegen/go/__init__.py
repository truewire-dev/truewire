"""The Go codegen backend: the plan (`truewire plan`) rendered as Go packages over
`truewire.dev/core`.

`render_package` is the whole backend: it walks `plan.schemas`, `plan.endpoints` and
`plan.routers` and returns every file, keyed by its path under `<src>/<package>/`. The CLI
(`truewire generate go`) owns the manifest, the writing and `--check`.

Layout, one package per spec node (`docs/go.md`):

- `client.go`: the root package, the root struct and `FromCore`/`FromCores`.
- `<router>/<router>.go`: a router struct delegating to its endpoints.
- `<router>/<endpoint>/<endpoint>.go`: one endpoint's types and its `Endpoint` struct.
- `types/types.go` (and `types/<scope>/types.go`): the shared `schemas.json` scopes.
- `meta/meta.go`: one struct per core that declares a `meta` schema.
- `replay/replay.go`: every HTTP method by function path, for `twtest.ReplayHTTP`.

What this backend leaves out is reported in `Rendered.skipped`: an endpoint with a
hand-written surface, and a walker the declaration gives no resumable state (or whose
cursor has no Go reading) is left to per-page calls. An `rpc` endpoint declaring both
`http` and `ws` is rendered over both, picked per call with `truewire.WithTransport`.
"""
import os
from dataclasses import dataclass, field

from truewire.codegen.shapes import core_shapes
from truewire.plan.model import EndpointPlan, PackagePlan
from truewire.project import Project

from .endpoint import EndpointModule, _Skipped, render_endpoint, render_stream_endpoint
from .grpc import render_grpc_endpoint
from .meta import META_FILE, meta_module, meta_shapes
from .names import package_ident, pascal_ident
from .protos import PROTOS_FILE, protos_module
from .replay import REPLAY_FILE, replay_module
from ..protos import proto_sources
from .routers import RouterModule, render_router
from .types import Module, Package, scope_file, scope_package


@dataclass
class Rendered:
  files: dict[str, str] = field(default_factory=dict)
  skipped: list[str] = field(default_factory=list)


def root_struct_name(plan: PackagePlan, project: Project | None) -> str:
  """`[go].name`, else the plan's (`[python].name` or PascalCase of the project)."""
  if project is not None and project.go is not None and project.go.name:
    return project.go.name
  return plan.root_class


def import_path(plan: PackagePlan, project: Project | None) -> str:
  """The import path of the root package: `[go].module` joined with the package directory
  relative to `[go].root` (the directory holding `go.mod`)."""
  if project is None or project.go is None:
    return f'example.com/{plan.name}'
  go = project.go
  relative = os.path.relpath(project.go_package_dir, project.root / go.root)
  parts = [part for part in relative.replace(os.sep, '/').split('/') if part not in ('', '.')]
  return '/'.join([go.module, *parts])


def render_package(plan: PackagePlan, project: Project | None = None) -> Rendered:
  """Every file of the Go package for `plan`."""
  out = Rendered()
  package = Package(import_path(plan, project))
  root_name = root_struct_name(plan, project)
  root_dir = (project.go.package if project is not None and project.go is not None and project.go.package else plan.name)
  root_package = package_ident(os.path.basename(root_dir), fallback='client')

  for scope in sorted(plan.schemas):
    types = plan.schemas[scope]
    module = Module(plan, scope_file(scope), package=package, name=scope_package(scope), local=types, visible=[scope], scope=scope)
    module.define_all(types)
    where = 'spec/schemas.json' if scope == '' else f'spec/endpoints/{scope}/schemas.json'
    out.files[module.file] = module.render(doc=f'Package {module.name} holds the shapes shared by two or more endpoints, generated from `{where}`.')

  meta = meta_module(plan.cores)
  if meta is not None:
    out.files[META_FILE] = meta
  protos = protos_module(proto_sources(project))
  if protos is not None:
    out.files[PROTOS_FILE] = protos
  shapes = meta_shapes(plan.cores)

  handwritten = _handwritten_surfaces(project)
  class_by_child = {(*router.path, child.name): child.class_ for router in plan.routers for child in router.children}
  endpoints: dict[str, EndpointModule] = {}
  for endpoint in plan.endpoints:
    if endpoint.function in handwritten:
      out.skipped.append(f'{endpoint.function}: a hand-written surface ({handwritten[endpoint.function]})')
      continue
    render = (
      render_stream_endpoint if endpoint.kind == 'stream'
      else render_grpc_endpoint if endpoint.kind == 'grpc'
      else render_endpoint
    )
    try:
      rendered, notes = render(plan, endpoint, package=package, meta=shapes.get(endpoint.core))
    except _Skipped as skipped:
      out.skipped.append(f'{endpoint.function}: {skipped.reason}')
      continue
    out.skipped.extend(notes)
    endpoints[endpoint.function] = rendered
    out.files[rendered.file] = rendered.source

  # A hand-written endpoint is not rendered, but its router still holds the contract it would
  # have: the method written beside the router reaches the transport a generated sibling would.
  handwritten_contracts = {
    endpoint.function: _contract_of(endpoint) for endpoint in plan.endpoints if endpoint.function in handwritten
  }

  def contract(function: str) -> str | None:
    return endpoints[function].contract if function in endpoints else handwritten_contracts.get(function)

  shapes_by_router = core_shapes(plan, contract)
  routers: dict[tuple[str, ...], RouterModule] = {}
  for router in sorted(plan.routers, key=lambda r: -len(r.path)):
    struct_name = root_name if not router.path else pascal_ident(class_by_child.get(tuple(router.path), router.path[-1]))
    rendered_router = render_router(
      plan, router, package=package, root_package=root_package, struct_name=struct_name,
      endpoints=endpoints, routers=routers, shapes=shapes_by_router, handwritten=handwritten_contracts,
    )
    if rendered_router is not None:
      routers[tuple(router.path)] = rendered_router
      out.files[rendered_router.file] = rendered_router.source

  root = routers.get(())
  if root is not None:
    replay = replay_module(plan, package=package, root=root, routers=routers, endpoints=endpoints)
    if replay is not None:
      out.files[REPLAY_FILE] = replay
  return out


def _contract_of(endpoint: EndpointPlan) -> str:
  """The `truewire` interface an endpoint's core satisfies, as its rendering would hold it."""
  if endpoint.kind == 'stream':
    return 'StreamEndpoint'
  if 'http' in endpoint.transports:
    return 'RpcEndpoint' if 'ws' in endpoint.transports else 'HttpEndpoint'
  return 'CommandEndpoint'


def _handwritten_surfaces(project: Project | None) -> dict[str, str]:
  """Function path -> symbol, for every endpoint whose spec declares a hand-written
  `surface`: its method is written by hand beside the generated router, not generated."""
  if project is None:
    return {}
  from truewire.spec import HandwrittenSurface, endpoint_records

  out: dict[str, str] = {}
  for record in endpoint_records(project):
    surface = record.endpoint.surface
    if isinstance(surface, HandwrittenSurface):
      out[record.endpoint.resolved_function(record.path, project.spec_dir)] = surface.symbol
  return out


__all__ = ['Rendered', 'import_path', 'render_package', 'root_struct_name']
