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
- `policy.rs`: `RefusedByPolicy`, when `[policy].refuse` names an endpoint (W15).
- `lib.rs`: the crate root, declaring the modules above and the hand-written `core`.

- `contract.rs`: the combined traits for every set of contracts one core must satisfy.
- `dispatch.rs`: `call`/`subscribe` on the root, every method by its function path; a
  private module, its methods hidden from the docs.

What this backend leaves out is reported in `Rendered.skipped` so the CLI can say so: the
walkers `docs/rust.md` lists as not built. `rpc` endpoints over a WebSocket, dual-transport ones (the transport
picked per call through `CallOptions::transport`), streams and composite cores are rendered.
"""
from dataclasses import dataclass, field, replace

from truewire.codegen.shapes import core_shapes
from truewire.plan.model import EndpointPlan, PackagePlan
from truewire.project import Project

from .contract import CONTRACT_FILE, CONTRACT_MODULE, Contracts
from .dispatch import DISPATCH_FILE, render_dispatch
from .endpoint import POLICY_FILE, EndpointModule, _Skipped, render_endpoint, render_stream_endpoint
from .grpc import CODEC_FILE, CODEC_MODULE, codec_module, render_grpc_endpoint
from .prost_names import PROTOS_MODULE
from .meta import META_FILE, meta_module, meta_shapes
from ..policy import refusal_problems, refused_functions, rust_module, rust_rate
from .names import pascal_case, snake_ident, unique
from .printer import BANNER
from .protos import PROTOS_FILE, protos_module
from .routers import LIB_FILE, RouterModule, render_lib, render_router
from .types import Module, scope_children, scope_file, scope_segments
from ..protos import proto_sources


@dataclass
class Rendered:
  files: dict[str, str] = field(default_factory=dict)
  """Package-relative POSIX path -> content, banner included."""
  skipped: list[str] = field(default_factory=list)
  """Function paths of endpoints and routers this backend does not emit, with the reason."""


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


def handwritten_bound(endpoint: EndpointPlan, shapes) -> str:
  """The trait a hand-written endpoint's core would satisfy had it been rendered: what its
  router holds for the method written beside it."""
  meta = shapes.get(endpoint.core)
  generic = f'<{meta.name}>' if meta is not None else ''
  if endpoint.kind == 'stream':
    return f'StreamEndpoint{generic}'
  traits = {'http': f'HttpEndpoint{generic}', 'ws': f'CommandEndpoint{generic}'}
  return ' + '.join(sorted(traits[t] for t in endpoint.transports if t in traits))


def root_struct_name(plan: PackagePlan, project: Project | None) -> str:
  """`[rust].name`, else the plan's (`[python].name` or PascalCase of the project)."""
  if project is not None and project.rust is not None and project.rust.name:
    return project.rust.name
  return plan.root_class


def render_package(plan: PackagePlan, project: Project | None = None) -> Rendered:
  """Every file of the Rust package for `plan`."""
  out = Rendered()
  root_name = root_struct_name(plan, project)
  contracts = Contracts()
  handwritten = _handwritten_surfaces(project)
  problems = refusal_problems(plan, project, handwritten=set(handwritten))
  if problems:
    raise ValueError('; '.join(problems))

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
    if endpoint.kind == 'grpc':
      struct_name = class_by_child.get(tuple(endpoint.path), pascal_case(endpoint.path[-1]))
      try:
        rendered, notes = render_grpc_endpoint(
          plan, endpoint, struct_name=struct_name, meta=shapes.get(endpoint.core), contracts=contracts,
        )
      except _Skipped as skipped:
        out.skipped.append(f'{endpoint.function}: {skipped.reason}')
        continue
      out.skipped.extend(notes)
      endpoints[endpoint.function] = rendered
      out.files[rendered.file] = rendered.source
      continue
    if endpoint.function in handwritten:
      # Written by hand as an inherent `impl` on the router, which keeps its core for it.
      out.skipped.append(f'{endpoint.function}: a hand-written surface ({handwritten[endpoint.function]})')
      continue
    if endpoint.kind == 'stream':
      struct_name = class_by_child.get(tuple(endpoint.path), pascal_case(endpoint.path[-1]))
      try:
        rendered, notes = render_stream_endpoint(
          plan, endpoint, struct_name=struct_name, meta=shapes.get(endpoint.core), contracts=contracts,
        )
      except _Skipped as skipped:
        out.skipped.append(f'{endpoint.function}: {skipped.reason}')
        continue
      out.skipped.extend(notes)
      endpoints[endpoint.function] = rendered
      out.files[rendered.file] = rendered.source
      continue
    struct_name = class_by_child.get(tuple(endpoint.path), pascal_case(endpoint.path[-1]))
    rendered, notes = render_endpoint(
      plan, endpoint, struct_name=struct_name, meta=shapes.get(endpoint.core), contracts=contracts,
    )
    out.skipped.extend(notes)
    endpoints[endpoint.function] = rendered
    out.files[rendered.file] = rendered.source

  endpoints = _router_aliases(endpoints)
  # A hand-written endpoint is not rendered, but its router still holds the contract it would
  # have, so a router whose endpoints are all hand-written is rendered with that core.
  handwritten_bounds = {
    endpoint.function: handwritten_bound(endpoint, shapes)
    for endpoint in plan.endpoints if endpoint.function in handwritten and endpoint.kind != 'grpc'
  }
  shapes_by_router = core_shapes(
    plan, lambda function: endpoints[function].bound if function in endpoints else handwritten_bounds.get(function),
  )
  routers: dict[tuple[str, ...], RouterModule] = {}
  for router in sorted(plan.routers, key=lambda r: -len(r.path)):
    struct_name = root_name if not router.path else class_by_child[tuple(router.path)]
    rendered = render_router(
      plan, router, struct_name=struct_name, endpoints=endpoints, routers=routers,
      shapes=shapes_by_router, contracts=contracts, handwritten=handwritten,
      handwritten_bounds=handwritten_bounds,
      policy=None if router.path else (rust_rate(project), project is not None and project.policy.retry),
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
  extra: list[str] = []
  if any(module.kind == 'grpc' for module in endpoints.values()):
    # `protos/mod.rs` is written by `truewire protos rust`, not here (ADR 0017); it holds
    # `FILE_DESCRIPTOR_SET`, so a project with gRPC endpoints gets no `protos.rs` beside it.
    extra.append(PROTOS_MODULE)
    out.files[CODEC_FILE] = codec_module()
    extra.append(CODEC_MODULE)
  else:
    protos = protos_module(proto_sources(project))
    if protos is not None:
      out.files[PROTOS_FILE] = protos
      extra.append(PROTOS_MODULE)
  # Only what a module still declares needs a combined trait: re-render from the kept files.
  contract = contracts.render()
  if contract is not None:
    out.files[CONTRACT_FILE] = contract
    extra.append(CONTRACT_MODULE)
  if refused_functions(plan):
    out.files[POLICY_FILE] = rust_module()
    extra.append(POLICY_FILE[:-3])
  private: list[str] = []
  if root is not None:
    dispatch = render_dispatch(plan, root_name=root.struct_name, endpoints=endpoints)
    if dispatch is not None:
      out.files[DISPATCH_FILE] = dispatch
      # Only an `impl` on the root struct: its methods are reached through the root.
      private.append(DISPATCH_FILE[:-3])
  out.files[LIB_FILE] = render_lib(
    plan, root=root, root_modules=root_modules, has_meta=meta is not None, extra_modules=extra,
    private_modules=private,
  )
  return out


def _router_aliases(endpoints: dict[str, EndpointModule]) -> dict[str, EndpointModule]:
  """`endpoints` with `aliases` set where a router would delegate two methods under one name:
  every endpoint's own (typed) method keeps its name, and a `_raw` twin or `_paged` walker
  that collides with a sibling's is numbered, as the Go backend numbers it."""
  by_router: dict[str, list[str]] = {}
  for function in sorted(endpoints):
    by_router.setdefault(function.rpartition('.')[0], []).append(function)
  out = dict(endpoints)
  for functions in by_router.values():
    taken = {endpoints[function].main for function in functions}
    for function in functions:
      module = endpoints[function]
      aliases: dict[str, str] = {}
      for method in module.methods:
        if method.name == module.main:
          continue
        name = unique(method.name, taken)
        if name != method.name:
          aliases[method.name] = name
      if aliases:
        out[function] = replace(module, aliases=aliases)
  return out


__all__ = ['Rendered', 'is_composite', 'render_package', 'root_struct_name']
