"""Router structs, the root, and the crate root: composition by delegation.

A router is a struct holding one value of each child endpoint struct and one of each
child router, built from the core it was given, exposing each endpoint's methods as
delegating methods with the same signature and docs, and each child router as a `pub`
field (`client.repos.get(...)`). The root (`client.rs`) is the same shape under the
project's root name; `lib.rs` declares every module and re-exports the root.

What a router's `new` takes depends on what its subtree needs. When every endpoint
beneath it holds the same contract (`Arc<dyn HttpEndpoint<DefaultMeta>>`), `new` takes that
and hands a clone to every child. When the endpoints beneath it hold several (different
`Meta` shapes, commands beside streams), `new` takes the combined trait `contract.rs`
declares for the set (`Arc<dyn CommandStreamEndpoint>`), and the value handed to each child
upcasts to the trait object that child holds. The root alone is generic instead --
`from_core<C>(core: C) where C: HttpEndpoint<DefaultMeta> + HttpEndpoint<FuturesMeta> +
'static` -- since it is the one constructor a caller writes. Nothing is imported from the
project (ADR 0011): the hand-written core implements the traits, and the compiler checks
it where the client is built.
"""
from dataclasses import dataclass, replace

from typing_extensions import Collection, Mapping

from truewire.codegen.shapes import CoreShape, child_fields, field_for
from truewire.plan.model import PackagePlan, RouterPlan

from .contract import Contracts, atoms
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
  """Every single trait an endpoint beneath this router holds (`HttpEndpoint<DefaultMeta>`)."""
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
  shapes: Mapping[tuple[str, ...], CoreShape], contracts: Contracts | None = None,
  handwritten: Collection[str] = (), handwritten_bounds: Mapping[str, str] | None = None,
  policy: tuple[str, bool] | None = None,
) -> RouterModule | None:
  """One router module (or `client.rs` for the root), or `None` when nothing beneath it
  was rendered.

  Args:
    struct_name: The struct to declare (the root name at the root, the child's `class` otherwise).
    endpoints: Every rendered endpoint by dotted function path.
    routers: Every rendered child router by path.
    shapes: What every router's constructor takes, by path.
    handwritten: Function paths whose method is written by hand. A grouping built from one
      core that parents one keeps that core as `pub(crate) core`, so the inherent `impl`
      written beside it reaches the transport (the Go backend's `core` field).
    handwritten_bounds: The trait each hand-written endpoint's core would hold, by function
      path. A router whose only endpoints are hand-written is still rendered, holding that
      core, so the methods written beside it have a struct and a transport.
    policy: At the root, `[policy].rate` as an `Option<f64>` expression and
      `[policy].retry`, stated as the associated consts `RATE`/`RETRY` for the core to
      build its `HttpClient` from (W15). No new runtime API is named, so a crate on
      `truewire-core` 0.1 still builds.
  """
  contracts = contracts if contracts is not None else Contracts()
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
  held_by_hand = {
    (handwritten_bounds or {})[function]
    for child in router.children
    if child.kind == 'endpoint' and (function := '.'.join([*router.path, child.name])) in (handwritten_bounds or {})
  }
  if not kids and not held_by_hand:
    return None

  bounds: set[str] = set(atoms(held_by_hand)) if held_by_hand else set()
  for _, _, endpoint, child_router in kids:
    bounds.update(child_router.bounds if child_router is not None else atoms(endpoint.bound))  # type: ignore[union-attr]
  shape = shapes[tuple(router.path)]
  module.imports.add('std::sync', 'Arc')
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

  def handwritten_child(child) -> bool:
    function = '.'.join([*router.path, child.name])
    if child.kind == 'endpoint':
      return function in handwritten
    # A child router with nothing rendered beneath it because every endpoint there is
    # hand-written: its methods are written on a hand-written struct reached from this one.
    return routers.get((*router.path, child.name)) is None and any(h.startswith(f'{function}.') for h in handwritten)

  keeps_core = not is_root and not shape.composite and any(handwritten_child(child) for child in router.children)
  doc = router.doc
  w.blank()
  w.doc(doc.description if doc is not None else None, f'See <{doc.upstream}>.' if doc is not None and doc.upstream else None)
  w.line('#[derive(Clone)]')
  with w.block(f'pub struct {struct_name} {{'):
    for name, module_name, endpoint, child_router in kids:
      if child_router is not None:
        w.doc(_router_doc(plan, [*router.path, name]))
        w.declaration(f'pub {name}: {child_type(module_name, child_router)}')
      else:
        w.declaration(f'{name}: {module_name}::{endpoint.struct_name}')  # type: ignore[union-attr]
    if keeps_core:
      w.doc('The core this router was built from, for hand-written methods beside the generated ones.')
      w.line(f'pub(crate) core: Arc<dyn {contracts.holder(module, bounds)}>,')
  w.blank()
  with w.block(f'impl {struct_name} {{'):
    if policy is not None:
      rate, retry = policy
      w.doc("`[policy].rate`: requests per second the core's `HttpClient` paces to; `None` for none.")
      w.line(f'pub const RATE: ::core::option::Option<f64> = {rate};')
      w.doc("`[policy].retry`: whether the core's `HttpClient` retries on its own.")
      w.line(f'pub const RETRY: bool = {"true" if retry else "false"};')
      w.blank()
    if shape.composite:
      _emit_composite_new(plan, w, router, kids, shape, child_type, is_root=is_root, module=module, contracts=contracts)
    else:
      prelude: list[str] = []
      if not is_root:
        # A grouping is plumbing: its parent already holds the core and hands it down, so
        # it takes what the parent has rather than taking ownership again.
        w.line(f'pub fn new(core: Arc<dyn {contracts.holder(module, bounds)}>) -> Self {{')
      elif len(bounds) == 1:
        # The root is the one struct a caller constructs, so `new` is left for the
        # hand-written core to define -- an inherent impl on the generated type, in the
        # same crate, which is the Rust answer to the base class `[python.cores.root]`
        # names. It can then read `Weather::new("you@example.com")` instead of the
        # plumbing. Taking `impl Trait` rather than `Arc<dyn Trait>` is the other half:
        # the `Arc` is our storage decision, not the caller's, and `truewire-core`
        # implements the endpoint traits for `Arc<T>` so a shared core still fits.
        bound = contracts.holder(module, bounds)
        w.line(f'pub fn from_core(core: impl {bound} + \'static) -> Self {{')
        prelude.append(f'let core: Arc<dyn {bound}> = Arc::new(core);')
      else:
        w.line('pub fn from_core<C>(core: C) -> Self')
        w.line('where')
        with w.indented():
          w.bounds('C: ', [*contracts.bound(module, bounds).split(' + '), "'static"], ',')
        w.line('{')
        prelude.append('let core = Arc::new(core);')
      with w.indented():
        for line in prelude:
          w.line(line)
        entries: list[str] = []
        for index, (name, module_name, endpoint, child_router) in enumerate(kids):
          handed = 'core' if index == len(kids) - 1 and not keeps_core else 'core.clone()'
          target = f'{module_name}::{endpoint.struct_name}' if endpoint is not None else child_type(module_name, child_router)  # type: ignore[arg-type]
          entries.append(f'{name}: {target}::new({handed})')
        if keeps_core:
          entries.append('core')
        w.struct_literal('', 'Self', entries)
      w.line('}')
    for name, module_name, endpoint, _ in kids:
      if endpoint is None:
        continue
      for method in endpoint.methods:
        w.blank()
        alias = endpoint.aliases.get(method.name, method.name)
        emit_method(module, replace(method, name=alias), qualifier=module_name, delegate=method.name)
  head = [] if is_root else [f'pub mod {module_name};' for _, module_name, _, _ in kids]
  return RouterModule(
    file=file, struct_name=struct_name, module=snake_ident(router.path[-1], fallback='router') if router.path else '',
    bounds=frozenset(bounds), shape=shape, source=module.render(BANNER, head=sorted(head, key=_declared_module)),
  )


def _emit_composite_new(
  plan: PackagePlan, w, router: RouterPlan, kids, shape: CoreShape, child_type, *, is_root: bool,
  module: Module, contracts: Contracts,
) -> None:
  """The constructor for a router built from more than one transport: one parameter per field.

  `[cores.<name>] children` maps a child to the field it is handed, so the constructor
  takes the fields themselves rather than one core -- `Bluesky::from_cores(client, socket)`
  -- and each child is built from the one it was mapped to. A child that is itself composite
  takes its own fields, in the same order it declares them, so the parameters pass
  straight through.

  A field handed to several children is cloned for all but its last use; a field handed to
  none is still a parameter, because the shape is a fact about the declarations rather
  than about what happens to be reachable.

  The root takes its fields by value and wraps them itself; a grouping takes what its
  parent already holds. See `emit_router` for why the root's is named `from_cores`.
  """
  fields = sorted(shape.fields or {})
  params = []
  prelude: list[tuple[str, str]] = []
  for name in fields:
    held = atoms((shape.fields or {})[name])
    # More than one contract on one field: keep it one value so every bound is satisfied
    # by the same core, held as the combined trait `contract.rs` declares for the set.
    holder = contracts.holder(module, held)
    if is_root:
      params.append(f"{name}: impl {contracts.bound(module, held)} + 'static")
      prelude.append((f'let {name}: Arc<dyn {holder}>', f'Arc::new({name})'))
    else:
      params.append(f'{name}: Arc<dyn {holder}>')
  if len(params) > 7:
    # One parameter per transport the root declares (bybit has ten); clippy's limit is seven.
    w.line('#[allow(clippy::too_many_arguments)]')
  w.signature(f'pub fn {"from_cores" if is_root else "new"}', params, ' -> Self {')

  def handed_fields(child_name: str, child_router: 'RouterModule | None') -> list[str]:
    """The fields a child is constructed from, in the order its own `new` takes them."""
    if child_router is not None and child_router.shape.composite:
      handed = child_fields(plan, tuple(router.path), child_name, child_router.shape)
      return [handed[f] for f in sorted(child_router.shape.fields or {})]
    return [field_for(plan, tuple(router.path), child_name)]

  plan_uses: dict[str, int] = {}
  for name, _, _, child_router in kids:
    for field in handed_fields(name, child_router):
      plan_uses[field] = plan_uses.get(field, 0) + 1

  seen: dict[str, int] = {}
  with w.indented():
    for head, value in prelude:
      w.let_binding(head, value)
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
  extra_modules: list[str] | None = None, private_modules: list[str] | None = None,
) -> str:
  """`lib.rs`: the crate root, declaring every generated module and the hand-written
  `core`, and re-exporting the root struct and `CallOptions`. `private_modules` are
  declared without `pub`: modules that only add methods to a public type, so their own
  page in the docs would be empty. `rustfmt` orders the lines by name whatever their
  visibility."""
  w = Writer()
  w.line(BANNER)
  root_plan = next((router for router in plan.routers if not router.path), None)
  if root_plan is not None and root_plan.doc is not None:
    w.line('//!')
    w.doc(root_plan.doc.description, f'See <{root_plan.doc.upstream}>.' if root_plan.doc.upstream else None, inner=True)
  w.blank()
  modules = [CORE_MODULE, *root_modules, *(extra_modules or []), *(private_modules or [])]
  if root is not None:
    modules.append(ROOT_FILE[:-3])
  if has_meta:
    modules.append(META_FILE[:-3])
  if plan.schemas:
    modules.append(TYPES_MODULE)
  for name in sorted(modules):
    w.line(f'mod {name};' if name in (private_modules or []) else f'pub mod {name};')
  w.blank()
  if root is not None:
    w.line(f'pub use {ROOT_FILE[:-3]}::{root.struct_name};')
  w.line('pub use truewire_core::CallOptions;')
  return w.render()


__all__ = [
  'CORE_MODULE', 'LIB_FILE', 'ROOT_FILE', 'RouterModule', 'bound_meta', 'render_lib',
  'render_router', 'router_file',
]


def _declared_module(line: str) -> str:
  """The module a `pub mod name;` line declares: `rustfmt` orders the lines by that name, so
  `orderbook_level5` comes before `orderbook_level50` (a whole-line sort puts `;` after `0`)."""
  return line.removeprefix('pub mod ').removesuffix(';')
