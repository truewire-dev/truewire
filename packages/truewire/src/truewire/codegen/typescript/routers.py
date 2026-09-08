"""Router classes and the root: composition by delegation.

Python composes a router by multiple inheritance; TypeScript has none, so a router is a
class that holds one instance of each child endpoint class and one of each child router,
built from the core it was given, and exposes each endpoint's methods as delegating
methods with the same signature and JSDoc. The root (`main.ts`) is the same shape under the
project's root class name; `index.ts` re-exports it.
"""
from typing_extensions import Mapping

from truewire.plan.model import PackagePlan, RouterPlan

from .endpoint import (
  META_FILE, EndpointModule, emit_signatures, endpoint_file, render_params, render_return,
  render_type,
)
from .names import binding, camel_case, property_key
from .printer import BANNER, relative_specifier
from .types import Module, scope_file

MAIN_FILE = 'main.ts'
INDEX_FILE = 'index.ts'


def router_file(path: list[str]) -> str:
  """`main.ts` for the root, `<path>/index.ts` for a grouping."""
  return MAIN_FILE if not path else '/'.join([*path, INDEX_FILE])


def core_types(
  plan: PackagePlan, router: RouterPlan, endpoints: Mapping[str, EndpointModule],
) -> list[str]:
  """The distinct core interfaces every endpoint under `router` takes, sorted: the
  router's own constructor parameter is their intersection."""
  prefix = '.'.join(router.path)
  found: set[str] = set()
  for function, module in endpoints.items():
    if prefix and not function.startswith(prefix + '.'):
      continue
    found.add(
      f'{module.core_type}<{module.meta_type}>' if module.meta_type is not None else module.core_type
    )
  return sorted(found)


def render_router(
  plan: PackagePlan, router: RouterPlan, *, class_name: str,
  endpoints: Mapping[str, EndpointModule], routers: Mapping[tuple[str, ...], str],
) -> str:
  """One router module (or `main.ts` for the root).

  Args:
    class_name: The class to declare (`plan.rootClass` at the root, the child's `class` otherwise).
    endpoints: Every rendered endpoint by dotted function path; a child with no entry
      (a stream this backend does not emit yet) is left out of the class.
    routers: Class name of every router node that is rendered.
  """
  file = router_file(router.path)
  module = Module(plan, file, local={}, visible=[])
  w = module.writer
  taken: set[str] = set()
  kids: list[tuple[str, str, str, EndpointModule | None]] = []
  """(attribute name, alias/class, holder field, endpoint module or None for a router)."""
  for child in router.children:
    function = '.'.join([*router.path, child.name])
    if child.kind == 'endpoint':
      rendered = endpoints.get(function)
      if rendered is None:
        continue
      alias = binding(camel_case(child.name), taken)
      taken.add(alias)
      module.imports.namespace(relative_specifier(file, rendered.file), alias)
      kids.append((camel_case(child.name), alias, f'{alias}_', rendered))
    else:
      child_class = routers.get((*router.path, child.name))
      if child_class is None:
        continue
      module.imports.add(relative_specifier(file, router_file([*router.path, child.name])), child_class)
      kids.append((camel_case(child.name), child_class, camel_case(child.name), None))

  cores = core_types(plan, router, endpoints)
  for core in cores:
    module.core(core.split('<')[0], type_only=True)
    if '<' in core:
      meta = core[core.index('<') + 1:-1]
      module.imports.add(relative_specifier(file, META_FILE), meta, type_only=True)
  core_param = ' & '.join(cores) or 'unknown'

  doc = router.doc
  w.jsdoc(
    doc.description if doc is not None else None,
    tags=[f'@see {doc.upstream}'] if doc is not None and doc.upstream else [],
  )
  with w.block(f'export class {class_name} {{'):
    for attr, alias, holder, rendered in kids:
      if rendered is None:
        w.jsdoc(_router_doc(plan, [*router.path, attr]))
        w.line(f'readonly {property_key(attr)}: {alias}')
      else:
        w.line(f'private readonly {holder}: {alias}.{rendered.class_name}')
    w.blank()
    with w.block(f'constructor(readonly core: {core_param}) {{'):
      for attr, alias, holder, rendered in kids:
        target = f'{alias}.{rendered.class_name}' if rendered is not None else alias
        w.line(f'this.{holder} = new {target}(core)')
    for attr, alias, holder, rendered in kids:
      if rendered is None:
        continue
      # Every endpoint module defines its own `Request`: qualify by this child's namespace.
      module.aliases = {name: f'{alias}.{name}' for name in rendered.types}
      for method in rendered.methods:
        w.blank()
        emit_signatures(module, method)
        params = render_params(module, method.params)
        returns = render_return(module, method.returns)
        args = ', '.join(param.name for param in method.params)
        with w.block(f'{property_key(method.name)}({params}): {returns} {{'):
          w.line(f'return this.{holder}.{method.name}({args})')
  return module.render(BANNER)


def _router_doc(plan: PackagePlan, path: list[str]) -> str | None:
  for router in plan.routers:
    if router.path == path and router.doc is not None:
      return router.doc.description
  return None


def render_index(plan: PackagePlan, *, root_class: str, has_meta: bool) -> str:
  module = Module(plan, INDEX_FILE, local={}, visible=[])
  w = module.writer
  w.line(f'export {{ {root_class} }} from {_spec(relative_specifier(INDEX_FILE, MAIN_FILE))}')
  if '' in plan.schemas:
    w.line(f'export * from {_spec(relative_specifier(INDEX_FILE, scope_file("")))}')
  if has_meta:
    w.line(f'export * from {_spec(relative_specifier(INDEX_FILE, META_FILE))}')
  w.line("export type { CallOptions } from '@truewire/core'")
  return module.render(BANNER)


def _spec(specifier: str) -> str:
  from .names import string

  return string(specifier)


__all__ = ['INDEX_FILE', 'MAIN_FILE', 'core_types', 'render_index', 'render_router', 'router_file']
