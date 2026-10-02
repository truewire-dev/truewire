"""Router structs and the root: composition by delegation.

A router is a struct holding each child endpoint and each child router, built from the core
it was given, exposing every endpoint's methods as delegating methods with the same
signature and docs, and each child router as an exported field
(`client.Repos.Get(ctx, ...)`). The root (`client.go`) is the same shape under the
project's root name, in the root package.

What a router's constructor takes depends on what its subtree needs: one core when every
endpoint beneath it shares a transport (`truewire.HttpEndpoint`, or a `Core` interface
embedding each contract its subtree uses), or one core per field when the router or a
descendant is composite (`[cores.<name>] children`). Nothing is imported from the project
(ADR 0011): the hand-written core satisfies the interfaces, and the compiler checks it
where the client is built.
"""
from dataclasses import dataclass

from typing_extensions import Mapping

from truewire.codegen.shapes import CoreShape, child_fields, field_for
from truewire.plan.model import PackagePlan, RouterPlan

from .endpoint import EndpointModule, emit_method, endpoint_dirs
from .names import camel_ident, pascal_ident, unique
from .printer import Entry
from .types import CORE, Module, Package

ROOT_FILE = 'client.go'

RESERVED_ALIASES = frozenset((
  CORE, 'context', 'json', 'meta', 'types', 'r', 'ctx', 'request', 'parameters', 'opts', 'core',
  'cmp', 'strings', 'strconv', 'time',
))
"""Names a router file already uses; a child package named one of them is imported under
a `pkg`-suffixed alias."""


@dataclass(frozen=True)
class RouterModule:
  file: str
  dirs: list[str]
  package: str
  import_path: str
  struct_name: str
  constructor: str
  contracts: frozenset[str]
  """Every contract an endpoint beneath this router holds."""
  shape: CoreShape
  source: str
  fields: dict[str, str]
  """Child name -> the field holding it, for a caller reaching into the tree."""
  methods: dict[str, list[tuple[str, str]]]
  """Endpoint child name -> `(method kind, method name)` it exposes here."""


def router_dirs(path: list[str]) -> list[str]:
  return endpoint_dirs(path) if path else []


def _contract_type(module: Module, contracts: set[str], name: str, interfaces: list[tuple[str, list[str]]]) -> str:
  """`truewire.X` for one contract; for several, a named interface embedding them all."""
  ordered = sorted(contracts)
  if len(ordered) == 1:
    return module.core(ordered[0])
  interface = unique(name, module.names)
  interfaces.append((interface, [module.core(c) for c in ordered]))
  return interface


def render_router(
  plan: PackagePlan, router: RouterPlan, *, package: Package, root_package: str, struct_name: str,
  endpoints: Mapping[str, EndpointModule], routers: Mapping[tuple[str, ...], RouterModule],
  shapes: Mapping[tuple[str, ...], CoreShape], handwritten: Mapping[str, str] | None = None,
) -> RouterModule | None:
  """One router file (`client.go` for the root), or `None` when nothing beneath it was rendered.

  `handwritten` maps the function path of every endpoint with a hand-written surface to the
  contract it would hold. A router whose only endpoints are hand-written is still rendered,
  holding that core, so the methods written beside it have a package and a transport.
  """
  is_root = not router.path
  dirs = router_dirs(router.path)
  name = dirs[-1] if dirs else root_package
  file = ROOT_FILE if is_root else '/'.join([*dirs, f'{dirs[-1]}.go'])
  module = Module(plan, file, package=package, name=name, local={}, visible=[])
  module.names.add(struct_name)
  kids: list[tuple[str, str, str, EndpointModule | None, RouterModule | None]] = []
  """(child name, field, import alias, endpoint, router)."""
  aliases: set[str] = set(RESERVED_ALIASES) | {struct_name}
  fields_taken: set[str] = set()
  method_names: set[str] = set()
  for child in router.children:
    function = '.'.join([*router.path, child.name])
    if child.kind == 'endpoint' and function in endpoints:
      for method in endpoints[function].methods:
        method_names.add(method.name)
  for child in router.children:
    function = '.'.join([*router.path, child.name])
    if child.kind == 'endpoint':
      rendered = endpoints.get(function)
      if rendered is None:
        continue
      alias = unique(rendered.package if rendered.package not in aliases else f'{rendered.package}pkg', aliases)
      field = unique(camel_ident(child.name, fallback='endpoint'), fields_taken)
      kids.append((child.name, field, alias, rendered, None))
    else:
      child_router = routers.get((*router.path, child.name))
      if child_router is None:
        continue
      alias = unique(child_router.package if child_router.package not in aliases else f'{child_router.package}pkg', aliases)
      field = unique(pascal_ident(child.name, fallback='Router'), fields_taken | method_names)
      fields_taken.add(field)
      kids.append((child.name, field, alias, None, child_router))
  held_by_hand = {
    (handwritten or {})[function]
    for child in router.children
    if child.kind == 'endpoint' and (function := '.'.join([*router.path, child.name])) in (handwritten or {})
  }
  if not kids and not held_by_hand:
    return None

  contracts: set[str] = set(held_by_hand)
  for _, _, _, endpoint, child_router in kids:
    contracts.update(child_router.contracts if child_router is not None else {endpoint.contract})  # type: ignore[union-attr]
  shape = shapes[tuple(router.path)]
  for _, _, alias, endpoint, child_router in kids:
    target = endpoint if endpoint is not None else child_router
    module.imports.add(target.import_path, alias if alias != target.package else None)  # type: ignore[union-attr]

  interfaces: list[tuple[str, list[str]]] = []
  if shape.composite:
    params = [
      (camel_ident(field_name, fallback='core'), _contract_type(module, set(types), f'{pascal_ident(field_name)}Core', interfaces))
      for field_name, types in sorted((shape.fields or {}).items())
    ]
  else:
    params = [('core', _contract_type(module, contracts, 'Core', interfaces))]
  constructor = 'FromCores' if is_root and shape.composite else 'FromCore' if is_root else 'New'
  # Every core a router is built from is kept, unexported, so a hand-written method in the
  # same package (`docs/go.md`, "Hand-written methods") reaches the transport the generated
  # ones use.
  held = [(unique(param, fields_taken | {struct_name}), param, type_) for param, type_ in params]

  w = module.writer
  for interface, embedded in interfaces:
    w.blank()
    w.doc(f'{interface} is what {constructor} takes: one core serving every contract beneath it.')
    with w.block(f'type {interface} interface {{'):
      for line in embedded:
        w.line(line)

  doc = router.doc
  w.blank()
  w.doc(
    f'{struct_name} {"is the client" if is_root else "groups"}: {doc.description}' if doc is not None else None,
    f'See {doc.upstream}.' if doc is not None and doc.upstream else None,
  )
  entries: list[Entry] = [
    (field_name, type_, 'The core this router was built from, for hand-written methods beside the generated ones.' if index == 0 else None)
    for index, (field_name, _, type_) in enumerate(held)
  ]
  for child_name, field, alias, endpoint, child_router in kids:
    if child_router is not None:
      entries.append((field, f'*{alias}.{child_router.struct_name}', _router_doc(plan, [*router.path, child_name])))
    else:
      entries.append((field, f'*{alias}.{endpoint.struct_name}', None))  # type: ignore[union-attr]
  w.struct(f'type {struct_name} struct {{', entries)
  w.blank()

  def handed(child_name: str, child_router: RouterModule | None) -> list[str]:
    if not shape.composite:
      return ['core']
    if child_router is not None and child_router.shape.composite:
      handed_fields = child_fields(plan, tuple(router.path), child_name, child_router.shape)
      return [camel_ident(handed_fields[f], fallback='core') for f in sorted(child_router.shape.fields or {})]
    return [camel_ident(field_for(plan, tuple(router.path), child_name), fallback='core')]

  literal = ', '.join([f'{field_name}: {param}' for field_name, param, _ in held] + [
    f'{field}: {alias}.{(endpoint or child_router).constructor}({", ".join(handed(child_name, child_router))})'  # type: ignore[union-attr]
    for child_name, field, alias, endpoint, child_router in kids
  ])
  signature = ', '.join(f'{param} {type_}' for param, type_ in params)
  w.doc(f'{constructor} returns the {"client" if is_root else "router"} over {"its cores" if shape.composite else "core"}.')
  with w.block(f'func {constructor}({signature}) *{struct_name} {{'):
    w.line(f'return &{struct_name}{{{literal}}}')

  # An endpoint's own method keeps its name; a `Raw`/`Paged` twin colliding with a
  # sibling's (`orders_by_instrument_raw` beside `OrdersByInstrument`'s `Raw`) is renamed.
  taken_names = {m.name for _, _, _, endpoint, _ in kids if endpoint is not None for m in endpoint.methods if m.kind == 'call'}
  taken_names |= {field for _, field, _, _, child_router in kids if child_router is not None}
  methods: dict[str, list[tuple[str, str]]] = {}
  for child_name, field, alias, endpoint, _ in kids:
    if endpoint is None:
      continue
    methods[child_name] = []
    for method in endpoint.methods:
      exposed = method.name
      if method.kind != 'call':
        exposed = unique(method.name, taken_names)
        taken_names.add(exposed)
      methods[child_name].append((method.kind, exposed))
      w.blank()
      emit_method(module, method, receiver=f'r *{struct_name}', qualifier=alias, target=f'r.{field}', name=exposed)
  doc_text = None
  if is_root:
    doc_text = f'Package {name} is the {struct_name} client, generated by truewire.'
    if doc is not None:
      doc_text = f'{doc_text} {doc.description}'
  else:
    doc_text = f'Package {name} is the `{".".join(router.path)}` router.'
  return RouterModule(
    file=file, dirs=dirs, package=name, import_path=package.path(*dirs), struct_name=struct_name,
    constructor=constructor, contracts=frozenset(contracts), shape=shape, source=module.render(doc=doc_text),
    fields={child_name: field for child_name, field, _, _, _ in kids}, methods=methods,
  )


def _router_doc(plan: PackagePlan, path: list[str]) -> str | None:
  for router in plan.routers:
    if router.path == path and router.doc is not None:
      return router.doc.description
  return None


__all__ = ['ROOT_FILE', 'RouterModule', 'render_router', 'router_dirs']
