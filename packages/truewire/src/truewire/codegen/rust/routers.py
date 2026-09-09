"""Router structs, the root, and the crate root: composition by delegation.

A router is a struct holding one value of each child endpoint struct and one of each
child router, built from the core it was given, exposing each endpoint's methods as
delegating methods with the same signature and docs, and each child router as a `pub`
field (`client.repos.get(...)`). The root (`client.rs`) is the same shape under the
project's root name; `lib.rs` declares every module and re-exports the root.

What a router's `new` takes depends on what its subtree needs. When every endpoint
beneath it holds the same contract (`Arc<dyn HttpEndpoint<DefaultMeta>>`), `new` takes that
and hands a clone to every child. When the endpoints beneath it hold different `Meta`
shapes, `new` is generic in the core -- `new<C>(core: Arc<C>) where C:
HttpEndpoint<DefaultMeta> + HttpEndpoint<FuturesMeta> + 'static` -- and the `Arc<C>` handed
to each child coerces to the trait object that child holds. Nothing is imported from the
project (ADR 0011): the hand-written core implements the traits, and the compiler checks
it where the client is built.
"""
from dataclasses import dataclass

from typing_extensions import Mapping

from truewire.codegen.shapes import CoreShape, field_for
from truewire.plan.model import PackagePlan, RouterPlan

from .endpoint import EndpointModule, emit_method
from .meta import META_FILE
from .names import snake_ident
from .printer import BANNER, Writer
from .types import TYPES_MODULE, Module

ROOT_FILE = 'client.rs'
LIB_FILE = 'lib.rs'
CORE_MODULE = 'core'
"""The module the crate root declares for the hand-written core."""


@dataclass(frozen=True)
class RouterModule:
  file: str
  struct_name: str
  module: str
  """The module name a parent declares (`pub mod issues;`) and reaches it by."""
  bounds: frozenset[str]
  """Every contract an endpoint beneath this router holds."""
  shape: CoreShape
  """What this router's own `new` takes: one transport, or a field per transport."""
  source: str


def router_file(path: list[str]) -> str:
  """`client.rs` for the root, `<path>/mod.rs` for a grouping."""
  return ROOT_FILE if not path else '/'.join([*(snake_ident(p, fallback='router') for p in path), 'mod.rs'])


def bound_meta(bound: str) -> str | None:
  """The `meta.rs` type a contract names, or `None` for the unit meta."""
  return bound[bound.index('<') + 1:-1] if '<' in bound else None


def render_router(
  plan: PackagePlan, router: RouterPlan, *, struct_name: str,
  endpoints: Mapping[str, EndpointModule], routers: Mapping[tuple[str, ...], RouterModule],
  shapes: Mapping[tuple[str, ...], CoreShape],
) -> RouterModule | None:
  """One router module (or `client.rs` for the root), or `None` when nothing beneath it
  was rendered.

  Args:
    struct_name: The struct to declare (the root name at the root, the child's `class` otherwise).
    endpoints: Every rendered endpoint by dotted function path.
    routers: Every rendered child router by path.
    shapes: What every router's constructor takes, by path.
  """
  file = router_file(router.path)
  module = Module(plan, file, local={}, visible=[])
  w = module.writer
  kids: list[tuple[str, str, EndpointModule | None, RouterModule | None]] = []
  """(field name, module name, endpoint or None, router or None)."""
  for child in router.children:
    function = '.'.join([*router.path, child.name])
    if child.kind == 'endpoint':
      rendered = endpoints.get(function)
      if rendered is None:
        continue
      name = snake_ident(child.name, fallback='endpoint')
      kids.append((name, name, rendered, None))
    else:
      child_router = routers.get((*router.path, child.name))
      if child_router is None:
        continue
      kids.append((child_router.module, child_router.module, None, child_router))
  if not kids:
    return None

  bounds: set[str] = set()
  for _, _, endpoint, child_router in kids:
    bounds.update(child_router.bounds if child_router is not None else {endpoint.bound})  # type: ignore[union-attr]
  shape = shapes[tuple(router.path)]
  for bound in bounds:
    module.core(bound.split('<')[0])
  module.imports.add('std::sync', 'Arc')
  for bound in bounds:
    meta = bound_meta(bound)
    if meta is not None:
      module.imports.add(f'crate::{META_FILE[:-3]}', meta)
  # The root's children are modules `lib.rs` declares, so it imports them; a grouping
  # declares its own children (`pub mod list;`) and reaches them by module name.
  is_root = not router.path
  if is_root:
    for _, module_name, endpoint, child_router in kids:
      if endpoint is not None:
        module.imports.module(f'crate::{module_name}')
      else:
        module.imports.add(f'crate::{module_name}', child_router.struct_name)  # type: ignore[union-attr]

  def child_type(module_name: str, child_router: RouterModule) -> str:
    return child_router.struct_name if is_root else f'{module_name}::{child_router.struct_name}'

  doc = router.doc
  w.blank()
  w.doc(doc.description if doc is not None else None, f'See <{doc.upstream}>.' if doc is not None and doc.upstream else None)
  w.line('#[derive(Clone)]')
  with w.block(f'pub struct {struct_name} {{'):
    for name, module_name, endpoint, child_router in kids:
      if child_router is not None:
        w.doc(_router_doc(plan, [*router.path, name]))
        w.line(f'pub {name}: {child_type(module_name, child_router)},')
      else:
        w.line(f'{name}: {module_name}::{endpoint.struct_name},')  # type: ignore[union-attr]
  w.blank()
  with w.block(f'impl {struct_name} {{'):
    if shape.composite:
      _emit_composite_new(plan, w, router, kids, shape, child_type)
    else:
      if len(bounds) == 1:
        (bound,) = bounds
        w.line(f'pub fn new(core: Arc<dyn {bound}>) -> Self {{')
      else:
        w.line('pub fn new<C>(core: Arc<C>) -> Self')
        w.line('where')
        with w.indented():
          w.line(f'C: {" + ".join(sorted(bounds))} + \'static,')
        w.line('{')
      with w.indented():
        entries: list[str] = []
        for index, (name, module_name, endpoint, child_router) in enumerate(kids):
          handed = 'core' if index == len(kids) - 1 else 'core.clone()'
          target = f'{module_name}::{endpoint.struct_name}' if endpoint is not None else child_type(module_name, child_router)  # type: ignore[arg-type]
          entries.append(f'{name}: {target}::new({handed})')
        w.struct_literal('', 'Self', entries)
      w.line('}')
    for name, module_name, endpoint, _ in kids:
      if endpoint is None:
        continue
      for method in endpoint.methods:
        w.blank()
        emit_method(module, method, qualifier=module_name)
  head = [] if is_root else [f'pub mod {module_name};' for _, module_name, _, _ in kids]
  return RouterModule(
    file=file, struct_name=struct_name, module=snake_ident(router.path[-1], fallback='router') if router.path else '',
    bounds=frozenset(bounds), shape=shape, source=module.render(BANNER, head=sorted(head)),
  )


def _emit_composite_new(
  plan: PackagePlan, w, router: RouterPlan, kids, shape: CoreShape, child_type,
) -> None:
  """`new` for a router built from more than one transport: one parameter per field.

  `[cores.<name>] children` maps a child to the field it is handed, so the constructor
  takes the fields themselves rather than one core -- `Bluesky::new(client, socket)` --
  and each child is built from the one it was mapped to. A child that is itself composite
  takes its own fields, in the same order it declares them, so the parameters pass
  straight through.

  A field handed to several children is cloned for all but its last use; a field handed to
  none is still a parameter, because the shape is a fact about the declarations rather
  than about what happens to be reachable.
  """
  fields = sorted(shape.fields or {})
  params = []
  for name in fields:
    held = sorted((shape.fields or {})[name])
    if len(held) == 1:
      params.append(f'{name}: Arc<dyn {held[0]}>')
    else:
      # More than one contract on one field: keep it concrete so every bound is satisfied
      # by the same value, the way a single-transport router does.
      params.append(f'{name}: Arc<dyn {" + ".join(held)}>')
  w.line(f'pub fn new({", ".join(params)}) -> Self {{')

  def handed_fields(child_name: str, child_router: 'RouterModule | None') -> list[str]:
    """The fields a child is constructed from, in the order its own `new` takes them."""
    if child_router is not None and child_router.shape.composite:
      return sorted(child_router.shape.fields or {})
    return [field_for(plan, tuple(router.path), child_name)]

  plan_uses: dict[str, int] = {}
  for name, _, _, child_router in kids:
    for field in handed_fields(name, child_router):
      plan_uses[field] = plan_uses.get(field, 0) + 1

  seen: dict[str, int] = {}
  with w.indented():
    entries: list[str] = []
    for name, module_name, endpoint, child_router in kids:
      args = []
      for field in handed_fields(name, child_router):
        seen[field] = seen.get(field, 0) + 1
        args.append(field if seen[field] == plan_uses[field] else f'{field}.clone()')
      target = f'{module_name}::{endpoint.struct_name}' if endpoint is not None else child_type(module_name, child_router)
      entries.append(f'{name}: {target}::new({", ".join(args)})')
    w.struct_literal('', 'Self', entries)
  w.line('}')


def _router_doc(plan: PackagePlan, path: list[str]) -> str | None:
  for router in plan.routers:
    if router.path == path and router.doc is not None:
      return router.doc.description
  return None


def render_lib(
  plan: PackagePlan, *, root: RouterModule | None, root_modules: list[str], has_meta: bool,
) -> str:
  """`lib.rs`: the crate root, declaring every generated module and the hand-written
  `core`, and re-exporting the root struct and `CallOptions`."""
  w = Writer()
  w.line(BANNER)
  root_plan = next((router for router in plan.routers if not router.path), None)
  if root_plan is not None and root_plan.doc is not None:
    w.line('//!')
    w.doc(root_plan.doc.description, f'See <{root_plan.doc.upstream}>.' if root_plan.doc.upstream else None, inner=True)
  w.blank()
  modules = [CORE_MODULE, *root_modules]
  if root is not None:
    modules.append(ROOT_FILE[:-3])
  if has_meta:
    modules.append(META_FILE[:-3])
  if plan.schemas:
    modules.append(TYPES_MODULE)
  for name in sorted(modules):
    w.line(f'pub mod {name};')
  w.blank()
  if root is not None:
    w.line(f'pub use {ROOT_FILE[:-3]}::{root.struct_name};')
  w.line('pub use truewire_core::CallOptions;')
  return w.render()


__all__ = [
  'CORE_MODULE', 'LIB_FILE', 'ROOT_FILE', 'RouterModule', 'bound_meta', 'render_lib',
  'render_router', 'router_file',
]
