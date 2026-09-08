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
- `index.ts`: the package's exports.

Stream endpoints (`kind: stream`) are not rendered yet; `render_package` reports them in
`Rendered.skipped` so the CLI can say so.
"""
from dataclasses import dataclass, field

from truewire.plan.model import PackagePlan
from truewire.project import Project

from .endpoint import META_FILE, EndpointModule, render_endpoint
from .meta import meta_module
from .names import pascal_case
from .printer import BANNER
from .routers import INDEX_FILE, render_index, render_router, router_file
from .types import Module, scope_file


@dataclass
class Rendered:
  files: dict[str, str] = field(default_factory=dict)
  """Package-relative POSIX path -> content, banner included."""
  skipped: list[str] = field(default_factory=list)
  """Function paths of endpoints this backend does not emit yet, with the reason."""


def root_class_name(plan: PackagePlan, project: Project | None) -> str:
  """`[typescript].name`, else the plan's (`[python].name` or PascalCase of the project)."""
  if project is not None and project.typescript is not None and project.typescript.name:
    return project.typescript.name
  return plan.root_class


def render_package(plan: PackagePlan, project: Project | None = None) -> Rendered:
  """Every file of the TypeScript package for `plan`."""
  out = Rendered()
  root_class = root_class_name(plan, project)

  for scope, types in plan.schemas.items():
    file = scope_file(scope)
    module = Module(plan, file, local=types, visible=[scope] if scope else [''])
    module.define_all(types)
    where = 'spec/schemas.json' if scope == '' else f'spec/endpoints/{scope}/schemas.json'
    out.files[file] = module.render(BANNER, doc=f'Shapes shared by two or more endpoints, generated from `{where}`.')

  meta = meta_module(plan.cores)
  if meta is not None:
    out.files[META_FILE] = meta

  class_by_child = {
    (*router.path, child.name): child.class_
    for router in plan.routers for child in router.children
  }
  endpoints: dict[str, EndpointModule] = {}
  for endpoint in plan.endpoints:
    if endpoint.kind != 'rpc':
      out.skipped.append(f'{endpoint.function}: stream endpoints are not generated for TypeScript yet')
      continue
    if endpoint.core in plan.cores:
      core = plan.cores[endpoint.core]
      if core.forward is not None or core.params is not None or core.children:
        out.skipped.append(
          f'{endpoint.function}: core {endpoint.core!r} declares forward/params/children, '
          'which the TypeScript backend does not compose yet'
        )
        continue
    class_name = class_by_child.get(tuple(endpoint.path), pascal_case(endpoint.path[-1]))
    rendered = render_endpoint(plan, endpoint, class_name=class_name)
    endpoints[endpoint.function] = rendered
    out.files[rendered.file] = rendered.source

  routers = {
    tuple(router.path): (root_class if not router.path else class_by_child[tuple(router.path)])
    for router in plan.routers
  }
  for router in plan.routers:
    out.files[router_file(router.path)] = render_router(
      plan, router, class_name=routers[tuple(router.path)], endpoints=endpoints, routers=routers,
    )
  out.files[INDEX_FILE] = render_index(plan, root_class=root_class, has_meta=meta is not None)
  return out


__all__ = ['Rendered', 'render_package', 'root_class_name']
