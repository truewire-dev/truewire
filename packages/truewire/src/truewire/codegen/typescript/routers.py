"""Router classes and the root: composition by delegation.

Python composes a router by multiple inheritance; TypeScript has none, so a router is a
class that holds one instance of each child endpoint class and one of each child router,
built from the core it was given, and exposes each endpoint's methods as delegating
methods with the same signature and JSDoc. The root (`main.ts`) is the same shape under the
project's root class name; `index.ts` re-exports it.

A router takes one of two things as its core. Under a core that declares neither
`children` nor `forward` in `truewire.toml`, it takes one transport -- the intersection of
its endpoints' contract interfaces -- and hands the same object to every child. Under a
core that declares either (a composite, `docs/truewire-toml.md`), it takes a *fields
object*, one property per transport field the declarations name, rendered as the exported
`<Class>Core` interface: each child endpoint or plain router receives the field its
`children` entry maps it to (`client` when unmapped), and a child that is itself a
composite receives the whole object, since `forward` means its fields are the parent's
same-named ones. Nothing is imported from the project (ADR 0011): the hand-written core
satisfies the interface by shape.
"""
from dataclasses import dataclass, field

from typing_extensions import Mapping

from truewire.codegen.shapes import (
  DEFAULT_FIELD,
  CoreShape,
  is_composite,
)
from truewire.codegen.shapes import core_shapes as shared_core_shapes
from truewire.plan.model import PackagePlan, RouterPlan

from .endpoint import META_FILE, EndpointModule, emit_signatures, render_params, render_return
from .names import binding, camel_case, member_access, property_key, string
from .printer import BANNER, relative_specifier
from .types import Module, scope_file

MAIN_FILE = 'main.ts'
INDEX_FILE = 'index.ts'


def router_file(path: list[str]) -> str:
  """`main.ts` for the root, `<path>/index.ts` for a grouping."""
  return MAIN_FILE if not path else '/'.join([*path, INDEX_FILE])


def core_class_name(class_name: str) -> str:
  """The fields-object interface a composite router's constructor takes: `KrakenCore`."""
  return f'{class_name}Core'


def endpoint_core_type(module: EndpointModule) -> str:
  return f'{module.core_type}<{module.meta_type}>' if module.meta_type is not None else module.core_type


def core_shapes(
  plan: PackagePlan, endpoints: Mapping[str, EndpointModule],
) -> dict[tuple[str, ...], CoreShape]:
  """The `CoreShape` of every router, in TypeScript's spelling of a contract."""
  def contract(function: str) -> str | None:
    module = endpoints.get(function)
    return None if module is None else endpoint_core_type(module)

  return shared_core_shapes(plan, contract)


def _intersection(module: Module, types: set[str]) -> str:
  """`A & B<Meta>` from a set of contract interface names, importing each and its meta."""
  for core in sorted(types):
    module.core(core.split('<')[0], type_only=True)
    if '<' in core:
      meta = core[core.index('<') + 1:-1]
      module.imports.add(relative_specifier(module.file, META_FILE), meta, type_only=True)
  return ' & '.join(sorted(types)) or 'unknown'


def render_router(
  plan: PackagePlan, router: RouterPlan, *, class_name: str,
  endpoints: Mapping[str, EndpointModule], routers: Mapping[tuple[str, ...], str],
  shapes: Mapping[tuple[str, ...], CoreShape],
) -> str:
  """One router module (or `main.ts` for the root).

  Args:
    class_name: The class to declare (`plan.rootClass` at the root, the child's `class` otherwise).
    endpoints: Every rendered endpoint by dotted function path.
    routers: Class name of every router node that is rendered.
    shapes: `core_shapes` of the plan: what this router's constructor takes, and what
      each child router's does.
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
      kids.append((child.name, alias, f'{alias}_', rendered))
    else:
      child_class = routers.get((*router.path, child.name))
      if child_class is None:
        continue
      module.imports.add(relative_specifier(file, router_file([*router.path, child.name])), child_class)
      kids.append((child.name, child_class, camel_case(child.name), None))

  shape = shapes[tuple(router.path)]
  mapping = (plan.cores[router.core].children if router.core in plan.cores else None) or {}
  if shape.fields is not None:
    core_param = core_class_name(class_name)
    w.jsdoc(
      f'The transports `{class_name}` is built from, one per field the composite\'s '
      '`truewire.toml` declarations name; the hand-written core satisfies it by shape.'
    )
    with w.block(f'export interface {core_param} {{'):
      for field_name, types in sorted(shape.fields.items()):
        w.line(f'{property_key(field_name)}: {_intersection(module, types)}')
    w.blank()
  else:
    core_param = _intersection(module, shape.types)

  def handed(name: str, rendered: EndpointModule | None) -> str:
    """The expression a child is constructed from."""
    if shape.fields is None:
      return 'core'
    if rendered is None and shapes[(*router.path, name)].composite:
      return 'core'
    return member_access('core', mapping.get(name, DEFAULT_FIELD), optional=False)

  doc = router.doc
  w.jsdoc(
    doc.description if doc is not None else None,
    tags=[f'@see {doc.upstream}'] if doc is not None and doc.upstream else [],
  )
  with w.block(f'export class {class_name} {{'):
    for name, alias, holder, rendered in kids:
      if rendered is None:
        w.jsdoc(_router_doc(plan, [*router.path, name]))
        w.line(f'readonly {property_key(camel_case(name))}: {alias}')
      else:
        w.line(f'private readonly {holder}: {alias}.{rendered.class_name}')
    w.blank()
    with w.block(f'constructor(readonly core: {core_param}) {{'):
      for name, alias, holder, rendered in kids:
        target = f'{alias}.{rendered.class_name}' if rendered is not None else alias
        w.line(f'this.{holder} = new {target}({handed(name, rendered)})')
    for _, alias, holder, rendered in kids:
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


def render_index(
  plan: PackagePlan, *, root_class: str, has_meta: bool, root_composite: bool,
) -> str:
  module = Module(plan, INDEX_FILE, local={}, visible=[])
  w = module.writer
  exported = root_class if not root_composite else f'{root_class}, type {core_class_name(root_class)}'
  w.line(f'export {{ {exported} }} from {string(relative_specifier(INDEX_FILE, MAIN_FILE))}')
  if '' in plan.schemas:
    w.line(f'export * from {string(relative_specifier(INDEX_FILE, scope_file("")))}')
  if has_meta:
    w.line(f'export * from {string(relative_specifier(INDEX_FILE, META_FILE))}')
  w.line("export type { CallOptions } from '@truewire/core'")
  return module.render(BANNER)


__all__ = [
  'DEFAULT_FIELD', 'INDEX_FILE', 'MAIN_FILE', 'CoreShape', 'core_class_name', 'core_shapes',
  'is_composite', 'render_index', 'render_router', 'router_file',
]
