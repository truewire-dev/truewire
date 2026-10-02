"""The TypeScript codegen backend: the plan (`truewire plan`) rendered as an ESM package.

`render_package` is the whole backend: it walks `plan.schemas`, `plan.endpoints` and
`plan.routers` and returns every file the package consists of, keyed by its path under
`<src>/<package>/`. The CLI (`truewire generate typescript`) owns the manifest, the
writing and `--check`; nothing here touches the filesystem.

Layout, mirroring the Python package one file per spec node:

- `types/index.ts` (and `types/<scope>.ts`): the shared `schemas.json` scopes.
- `<router>/<endpoint>.ts`: one endpoint's interfaces, codecs and class.
- `<router>/index.ts`: a router class delegating to its endpoints; `main.ts` the root.
- `meta.ts`: one interface per core that declares a `meta` schema.
- `policy.ts`: `RefusedByPolicy`, when `[policy].refuse` names an endpoint (W15).
- `proto.ts`: the `spec/proto` sources, when the project has any (ADR 0016).
- `index.ts`: the package's exports.

What this backend leaves out is reported in `Rendered.skipped` so the CLI can say so;
nothing is today. An `rpc` endpoint declaring both `http` and `ws` transports is rendered
in full: its class takes a `DualEndpoint<Meta>` and its method a `transport` option. An
endpoint whose spec declares `surface: handwritten` is not rendered, as in Python: the
project serves it through `[typescript.extras]`, which each router folds in.
"""
from dataclasses import dataclass, field

from truewire.plan.model import PackagePlan
from truewire.project import Project

from .endpoint import META_FILE, POLICY_FILE, EndpointModule, render_endpoint, render_stream
from .grpc import render_grpc
from .meta import meta_module
from ..policy import extras_replaced, refusal_problems, refused_functions, typescript_module, typescript_rate
from .names import pascal_case
from .printer import BANNER
from .protos import PROTO_FILE, proto_module
from ..protos import proto_sources
from .routers import INDEX_FILE, core_shapes, render_index, render_router, router_file
from .types import Module, scope_file


@dataclass
class Rendered:
  files: dict[str, str] = field(default_factory=dict)
  """Package-relative POSIX path -> content, banner included."""
  skipped: list[str] = field(default_factory=list)
  """Function paths of endpoints this backend does not emit in full, with the reason."""


def root_class_name(plan: PackagePlan, project: Project | None) -> str:
  """`[typescript].name`, else the plan's (`[python].name` or PascalCase of the project)."""
  if project is not None and project.typescript is not None and project.typescript.name:
    return project.typescript.name
  return plan.root_class


def render_package(plan: PackagePlan, project: Project | None = None) -> Rendered:
  """Every file of the TypeScript package for `plan`."""
  out = Rendered()
  root_class = root_class_name(plan, project)
  typescript = project.typescript if project is not None else None
  problems = refusal_problems(plan, project, handwritten=extras_replaced(typescript.extras if typescript is not None else None))
  if problems:
    raise ValueError('; '.join(problems))
  if refused_functions(plan):
    out.files[POLICY_FILE] = typescript_module()

  for scope, types in plan.schemas.items():
    file = scope_file(scope)
    module = Module(plan, file, local=types, visible=[scope] if scope else [''])
    module.define_all(types)
    where = 'spec/schemas.json' if scope == '' else f'spec/endpoints/{scope}/schemas.json'
    out.files[file] = module.render(BANNER, doc=f'Shapes shared by two or more endpoints, generated from `{where}`.')

  meta = meta_module(plan.cores)
  if meta is not None:
    out.files[META_FILE] = meta
  protos = proto_module(proto_sources(project))
  if protos is not None:
    out.files[PROTO_FILE] = protos

  class_by_child = {
    (*router.path, child.name): child.class_
    for router in plan.routers for child in router.children
  }
  endpoints: dict[str, EndpointModule] = {}
  for endpoint in plan.endpoints:
    if endpoint.surface == 'handwritten':
      continue
    class_name = class_by_child.get(tuple(endpoint.path), pascal_case(endpoint.path[-1]))
    if endpoint.kind == 'grpc':
      try:
        rendered = render_grpc(plan, endpoint, class_name=class_name)
      except ValueError as exc:
        out.skipped.append(str(exc))
        continue
    elif endpoint.kind == 'stream':
      rendered = render_stream(plan, endpoint, class_name=class_name)
    else:
      rendered = render_endpoint(plan, endpoint, class_name=class_name)
    endpoints[endpoint.function] = rendered
    out.files[rendered.file] = rendered.source

  routers = {
    tuple(router.path): (root_class if not router.path else class_by_child[tuple(router.path)])
    for router in plan.routers
  }
  shapes = core_shapes(plan, endpoints)
  extras = dict((project.typescript.extras or {}) if project is not None and project.typescript is not None else {})
  unknown = sorted(set(extras) - {'.'.join(router.path) for router in plan.routers})
  if unknown:
    raise ValueError(f'[typescript.extras] names no router node: {", ".join(repr(node) for node in unknown)}')
  for router in plan.routers:
    out.files[router_file(router.path)] = render_router(
      plan, router, class_name=routers[tuple(router.path)], endpoints=endpoints, routers=routers,
      shapes=shapes, extras=extras.get('.'.join(router.path), []),
      policy=None if router.path else (typescript_rate(project), project is not None and project.policy.retry),
    )
  out.files[INDEX_FILE] = render_index(
    plan, root_class=root_class, has_meta=meta is not None, root_composite=shapes[()].composite,
    has_policy=POLICY_FILE in out.files,
  )
  return out


__all__ = ['Rendered', 'render_package', 'root_class_name']
