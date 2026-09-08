"""Compute the plan from a project: the spec tree plus `truewire.toml`, nothing rendered.

`PlanBuilder` runs the same schema pipeline every backend's types come from (normalize,
unnest, name, parse into the `truewire.plan.types` tree) and then decides, per endpoint,
what the Python backend used to decide inside `rpc_endpoint` by re-reading its own rendered
strings: the request shape and fields, the returned type and whether it is nullable,
whether a request or response type is a bare alias, and every pagination fact a walker
needs. A backend renders the result; it never recomputes it.

The type pipeline lives under `truewire.generation.python.types` for historical reasons;
since the `Scalar` node landed it emits no Python name, and the plan reuses it as is.
"""
from pathlib import Path
from typing_extensions import Any, Collection, Iterable, Mapping

from truewire.codegen.layout import (
  class_name as neutral_class_name, discover_schemas_files, load_schema_file, router_nodes,
  schemas_scope,
)
from truewire.generation.python.types import Normalizer, Parser, disambiguate
from truewire.generation.schema import Reference, Schema
from truewire.generation.types import Translate, generation_order
from truewire.generation.util import pascal_case
from truewire.project import Project, resolve
from truewire.spec import (
  Endpoint, Pagination, RpcEndpointSpec, RpcEnvelopeSpec, StreamEndpointSpec,
  StreamEnvelopeSpec, endpoint_specs, load_endpoint, load_router, select_schema,
)
from truewire.spec.request import PLACEHOLDER

from .model import (
  CorePlan, DocsPlan, EndpointPlan, NewParamPlan, PackagePlan, PaginationPlan, RequestFieldPlan,
  RequestPlan, ResponsePlan, RouterChildPlan, RouterDocPlan, RouterPlan, StreamPlan, TypeSet,
  WirePlan,
)
from .types import Type, has_zero_value, is_null, strip_null

_REF_DEPTH = 16
"""How many `Ref` hops a row-type walk follows before giving up on a cycle."""


def needs_cast(t: Type) -> bool:
  """Whether a top-level type is a bare alias a typed backend cannot pass where a real
  class is expected: a `Literal`, the unconstrained `any`, or a union led by a literal
  (which renders as `Literal[...] | ...`). A record, a list, a dict or a reference is a
  real type and needs nothing."""
  lead = t
  while lead['type'] == 'union' and lead['variants']:
    lead = lead['variants'][0]['type']
  if lead['type'] == 'literal':
    return True
  only = t
  while only['type'] == 'union' and len(only['variants']) == 1:
    only = only['variants'][0]['type']
  return only['type'] == 'scalar' and only['base'] == 'any'


def is_nullable_last(t: Type) -> bool:
  """Whether `t` is a union whose last variant is `null`: the shape a backend renders as
  `X | None`, and the one a walker guards every read on."""
  return t['type'] == 'union' and bool(t['variants']) and is_null(t['variants'][-1]['type'])


def driver_parameter(pagination: Pagination) -> str:
  """The wire name of the request parameter a paginated walk advances."""
  if pagination.strategy == 'page':
    return pagination.index.parameter
  if pagination.strategy == 'token' or pagination.strategy == 'seek':
    return pagination.cursor.parameter
  if pagination.strategy == 'window':
    bound = pagination.bound
    return bound.end if pagination.order == 'descending' else bound.start
  return pagination.offset.parameter


def direct_channel_scalar(prop: Reference | Schema) -> bool:
  """Whether one stream parameter is plain enough to interpolate straight into a channel
  template: a `$ref`, or a schema with no nested `properties`/`anyOf`/`prefixItems` and
  no `object`/`array` type of its own."""
  if isinstance(prop, Reference):
    return True
  return (
    not prop.properties and not prop.anyOf and not prop.prefixItems
    and prop.type not in ('object', 'array')
  )


def channel_direct_params(parameters_schema: Schema | None, channel: str) -> bool:
  """Whether a stream's declared parameters are exactly the channel's own placeholders --
  every one required, nothing left over on either side, every one a
  `direct_channel_scalar` -- so a backend fills the template from its own locals and
  passes no parameters object at all."""
  if parameters_schema is None or parameters_schema.anyOf:
    return False
  properties = parameters_schema.properties or {}
  required = set(parameters_schema.required or [])
  names = set(properties.keys())
  if names != required:
    return False
  if names != set(PLACEHOLDER.findall(channel)):
    return False
  return all(direct_channel_scalar(prop) for prop in properties.values())


def connect_channel_param(
  push: Any | None, parameters_schema: Schema | None, channel: str,
) -> str | None:
  """The one parameter of a connect-only push whose channel template is exactly that
  parameter's placeholder (a listenKey-style private stream: connecting is the
  subscription), or `None` for every other shape."""
  if push is None or push.trigger != 'connect':
    return None
  if parameters_schema is None or parameters_schema.anyOf:
    return None
  properties = parameters_schema.properties or {}
  required = set(parameters_schema.required or [])
  if len(properties) != 1 or required != set(properties):
    return None
  (name,) = properties.keys()
  if channel != f'{{{name}}}':
    return None
  return name


def canonical(t: Type) -> Type:
  """`t` without the keys the parser leaves as `None` (`id`, `docstring`), so the tree
  reads the same whether it came from the builder or back from its JSON."""
  out: dict[str, Any] = {k: v for k, v in t.items() if v is not None}
  kind = t['type']
  if kind == 'list':
    out['item'] = canonical(t['item'])
  elif kind == 'tuple':
    out['items'] = [canonical(item) for item in t['items']]
  elif kind == 'union':
    out['variants'] = [
      {k: v for k, v in {**variant, 'type': canonical(variant['type'])}.items() if v is not None}
      for variant in t['variants']
    ]
  elif kind == 'dict':
    out['key'] = canonical(t['key'])
    out['value'] = canonical(t['value'])
  elif kind == 'record':
    out['fields'] = {
      name: {k: v for k, v in {**field, 'type': canonical(field['type'])}.items() if v is not None}
      for name, field in t['fields'].items()
    }
  return out  # type: ignore[return-value]


def _bare_ref(schema: Any) -> str | None:
  """The id a schema node references when it is nothing but a `$ref` (a description
  beside it is allowed, ADR 0010), else `None`."""
  if isinstance(schema, dict) and isinstance(schema.get('$ref'), str):
    return schema['$ref']
  return None


def _dump(model: Any) -> dict[str, Any] | None:
  """A pydantic declaration as plain data, `None` passed through."""
  if model is None:
    return None
  return model.model_dump(mode='json', by_alias=True, exclude_none=True)


class PlanBuilder:
  """Builds the plan for one project. Construct once; `build` computes everything, and
  `endpoint` computes one endpoint's plan on its own (what the Python backend calls when
  no whole-package plan was attached to it)."""

  def __init__(self, project: Project):
    self.project = project
    self._normalize = Normalizer.iterative()
    self._parser = Parser()
    self._scopes: dict[str, TypeSet] = {}
    """Shared types per scope key (`''` for the root file)."""
    self._scope_names: dict[Path, dict[str, str]] = {}
    """Each `schemas.json`'s own ids -> the names its types are defined under."""
    self._shared: dict[str, Type] = {}
    """Every shared type by name, across scopes, for walking a `Ref` off an endpoint."""
    for path in discover_schemas_files(project):
      file_schemas = load_schema_file(path)
      types, translations = self.type_set(file_schemas, forbidden=(), fixed={})
      self._scopes['/'.join(schemas_scope(path, project))] = types
      self._scope_names[path] = {id: translations[id] for id in file_schemas}
      for name, t in types.items():
        self._shared.setdefault(name, t)

  # -- the type pipeline -------------------------------------------------------------

  def type_set(
    self, schemas: Mapping[str, Schema], *, forbidden: Collection[str], fixed: Mapping[str, str],
  ) -> tuple[TypeSet, dict[str, str]]:
    """Normalize, unnest, name and parse one module's schemas into its `TypeSet`.

    The same steps every backend's type generator runs, stopped before rendering: the
    returned names are what the types are defined under, and the second value maps each
    input id (`$request`, `$response`, ...) to its name.

    Args:
      schemas: Id -> schema, as the module renders them.
      forbidden: Names the module reserves (its endpoint class), which no type may take.
      fixed: Shared schema id -> the name it is defined under elsewhere, so a `$ref` to
        it resolves to that name and no local type shadows it.
    """
    normalized = self._normalize(schemas)
    translations = disambiguate(normalized, forbidden=forbidden, fixed=fixed)
    translated = Translate.of(translations).all(normalized)
    types: TypeSet = {}
    for id in generation_order(translated):
      schema = translated.get(id)
      if schema is not None:
        types[id] = canonical(self._parser(schema, id=id))
    return types, translations

  def visible_names(self, endpoint_dir: Path) -> dict[str, str]:
    """Shared schema id -> name, for every `schemas.json` scope visible from a directory:
    its own and its ancestors' under `spec/endpoints/`, then the root file."""
    endpoints_root = self.project.endpoints_dir
    merged: dict[str, str] = {}
    current = endpoint_dir
    while True:
      merged.update(self._scope_names.get(current / 'schemas.json', {}))
      if current == endpoints_root or current.parent == current:
        break
      current = current.parent
    merged.update(self._scope_names.get(self.project.schemas_path, {}))
    return merged

  def core_name(self, endpoint_dir: Path) -> str:
    """The symbolic core the nearest `router.json` at or above a directory declares.

    Raises:
      ValueError: When no ancestor declares one; the root `router.json` must.
    """
    endpoints_root = self.project.endpoints_dir
    current = endpoint_dir
    while True:
      doc = load_router(current)
      if doc is not None and doc.core is not None:
        return doc.core
      if current == endpoints_root or current.parent == current:
        raise ValueError(f'{endpoint_dir}: no ancestor router.json declares `core`')
      current = current.parent

  def _deref(self, t: Type, types: TypeSet) -> Type | None:
    """Follow `Ref` nodes through the endpoint's own types, then the shared scopes."""
    for _ in range(_REF_DEPTH):
      if t['type'] != 'ref':
        return t
      target = types.get(t['id'], self._shared.get(t['id']))
      if target is None:
        return None
      t = target
    return None

  def _walk(self, t: Type, segments: list[str], types: TypeSet) -> Type | None:
    """Resolve the element type at a dotted path ending in a list: through records by
    field, through unions by trying each non-null variant, through references by name."""
    resolved = self._deref(t, types)
    if resolved is None:
      return None
    if resolved['type'] == 'union':
      for variant in resolved['variants']:
        if is_null(variant['type']):
          continue
        found = self._walk(variant['type'], segments, types)
        if found is not None:
          return found
      return None
    if not segments:
      return resolved['item'] if resolved['type'] == 'list' else None
    if resolved['type'] == 'record':
      field = resolved['fields'].get(segments[0])
      return self._walk(field['type'], segments[1:], types) if field is not None else None
    return None

  def rows_type(self, payload: Type, rows: str, types: TypeSet) -> Type | None:
    """One row's type at `rows` inside `payload` (`''` when the payload is the rows)."""
    segments = (rows.split('/') if '/' in rows else rows.split('.')) if rows else []
    return self._walk(payload, segments, types)

  # -- per endpoint -----------------------------------------------------------------

  def endpoint(self, endpoint: Endpoint, endpoint_dir: Path) -> EndpointPlan | None:
    """The plan for one endpoint, or `None` for a shape the plan does not cover yet (an
    OpenAPI-shaped or gRPC endpoint).

    Args:
      endpoint: The loaded spec -- used as given, so a caller may plan a modified copy.
      endpoint_dir: Its directory under `spec/endpoints/`, which names its core, its
        visible shared schemas and (unless `function` is authored) its function path.
    """
    plan, _ = self._endpoint(endpoint, endpoint_dir)
    return plan

  def _endpoint(self, endpoint: Endpoint, endpoint_dir: Path) -> tuple[EndpointPlan | None, str]:
    spec = endpoint.spec
    function = endpoint.resolved_function(endpoint_dir / 'endpoint.json', self.project.spec_dir)
    path = function.split('.')
    if isinstance(spec, RpcEndpointSpec) and (spec.request is not None or spec.response is not None):
      return self._rpc(endpoint, spec, path, endpoint_dir)
    if isinstance(spec, StreamEndpointSpec):
      parameters = spec.parameters if spec.parameters is not None else spec.request
      if parameters is not None or spec.payload is not None:
        return self._stream(endpoint, spec, path, endpoint_dir, parameters)
    return None, neutral_class_name(path[-1])

  def _class_name(self, segment: str, schemas: Mapping[str, Schema], fixed: Mapping[str, str]) -> str:
    """The endpoint class: PascalCase of the leaf, grown past any record or shared type
    the module would otherwise shadow, the way every backend names it."""
    name = neutral_class_name(segment)
    if not schemas:
      return name
    types, _ = self.type_set(schemas, forbidden=(), fixed=fixed)
    taken = {id for id, t in types.items() if t['type'] == 'record'}
    taken.update(self._referenced(types.values(), fixed))
    while name in taken:
      name += 'Endpoint'
    return name

  def _referenced(self, types: Iterable[Type], fixed: Mapping[str, str]) -> set[str]:
    """Shared names the given types reference."""
    from .types import refs

    shared = set(fixed.values())
    return {ref['id'] for t in types for ref in refs(t) if ref['id'] in shared}

  def _returned_response(self, endpoint: Endpoint, spec: RpcEndpointSpec) -> dict[str, Any] | None:
    """`spec.response`, or the node `envelope.payload` selects inside it; the whole
    response when the path does not resolve (the backend reports that itself)."""
    if spec.response is None:
      return None
    envelope = endpoint.envelope
    if not isinstance(envelope, RpcEnvelopeSpec) or envelope.payload == '':
      return spec.response
    try:
      return select_schema(spec.response, envelope.payload, shared=self._raw_shared())
    except LookupError:
      return spec.response

  def _raw_shared(self) -> dict[str, Any]:
    from truewire.spec import load_shared_schemas

    cached = getattr(self, '_raw_shared_cache', None)
    if cached is None:
      cached = load_shared_schemas(self.project)
      self._raw_shared_cache = cached
    return cached

  def _rpc(
    self, endpoint: Endpoint, spec: RpcEndpointSpec, path: list[str], endpoint_dir: Path,
  ) -> tuple[EndpointPlan, str]:
    fixed = self.visible_names(endpoint_dir)
    request_schema = Schema.model_validate(spec.request) if spec.request is not None else None
    returned = self._returned_response(endpoint, spec)
    response_ref = _bare_ref(returned)
    schemas: dict[str, Schema] = {}
    if request_schema is not None:
      schemas['$request'] = request_schema.model_copy(update={'title': None})
    if returned is not None and response_ref is None:
      schemas['$response'] = Schema.model_validate(returned)
    class_ = self._class_name(path[-1], schemas, fixed)
    types, translations = self.type_set(schemas, forbidden={class_}, fixed=fixed)
    request_id = translations.get('$request')
    response_id = translations.get('$response')
    request = self._request_plan(request_schema, request_id, types)
    if response_ref is not None:
      payload_id: str | None = fixed.get(response_ref, response_ref)
      payload_type: Type | None = None
    else:
      payload_id = response_id
      payload_type = types.get(response_id) if response_id is not None else None
    envelope = endpoint.envelope
    selector = envelope.payload if isinstance(envelope, RpcEnvelopeSpec) else ''
    wire_types: TypeSet = {}
    wire_id = payload_id
    if selector and spec.response is not None:
      wire_ref = _bare_ref(spec.response)
      if wire_ref is not None:
        wire_id = fixed.get(wire_ref, wire_ref)
      else:
        wire_types, wire_translations = self.type_set(
          {'$wire': Schema.model_validate(spec.response)}, forbidden=(), fixed=fixed,
        )
        wire_id = wire_translations.get('$wire')
    response = ResponsePlan(
      wire=wire_id, payload=payload_id, selector=selector,
      optional=payload_type is not None and is_nullable_last(payload_type),
      needs_cast=payload_type is not None and needs_cast(payload_type),
    )
    pagination = None
    if endpoint.pagination is not None:
      pagination = self._pagination_plan(
        endpoint.pagination, request, request_schema, payload_id, payload_type, types, fixed,
      )
    plan = EndpointPlan(
      path=path, kind='rpc', transports=list(spec.transports),
      wire=WirePlan(
        path=spec.path, method=spec.method, placeholders=PLACEHOLDER.findall(spec.path),
      ),
      core=self.core_name(endpoint_dir),
      meta=endpoint.meta if isinstance(endpoint.meta, dict) else {},
      deprecated=bool(endpoint.deprecated),
      request=request, response=response, pagination=pagination,
      types=types, wire_types=wire_types,
      docs=DocsPlan(description=spec.description, url=endpoint.docs, notes=list(endpoint.notes or [])),
    )
    return plan, class_

  def _stream(
    self, endpoint: Endpoint, spec: StreamEndpointSpec, path: list[str], endpoint_dir: Path,
    parameters_raw: dict[str, Any] | None,
  ) -> tuple[EndpointPlan, str]:
    fixed = self.visible_names(endpoint_dir)
    parameters_schema = (
      Schema.model_validate(parameters_raw) if parameters_raw is not None else None
    )
    payload_ref = (
      spec.payload['$ref']
      if isinstance(spec.payload, dict) and set(spec.payload) == {'$ref'}
      else None
    )
    connect_param = connect_channel_param(endpoint.push, parameters_schema, spec.channel)
    direct = connect_param is None and channel_direct_params(parameters_schema, spec.channel)
    schemas: dict[str, Schema] = {}
    if parameters_schema is not None and connect_param is None and not direct:
      schemas['$parameters'] = parameters_schema.model_copy(update={'title': None})
    if spec.payload is not None and payload_ref is None:
      schemas['$payload'] = Schema.model_validate(spec.payload)
    class_ = self._class_name(path[-1], schemas, fixed)
    types, translations = self.type_set(schemas, forbidden={class_}, fixed=fixed)
    request = self._request_plan(parameters_schema, translations.get('$parameters'), types)
    if payload_ref is not None:
      payload_id: str | None = fixed.get(payload_ref, payload_ref)
      payload_type: Type | None = None
    else:
      payload_id = translations.get('$payload')
      payload_type = types.get(payload_id) if payload_id is not None else None
    envelope = endpoint.envelope if isinstance(endpoint.envelope, StreamEnvelopeSpec) else None
    response = ResponsePlan(
      wire=payload_id, payload=payload_id,
      selector=envelope.payload if envelope is not None else '',
      optional=payload_type is not None and is_nullable_last(payload_type),
      needs_cast=payload_type is not None and needs_cast(payload_type),
    )
    placeholders = PLACEHOLDER.findall(spec.channel)
    properties = set((parameters_schema.properties or {}) if parameters_schema is not None else {})
    plan = EndpointPlan(
      path=path, kind='stream', transports=['ws'],
      wire=WirePlan(channel=spec.channel, placeholders=placeholders),
      core=self.core_name(endpoint_dir),
      meta=endpoint.meta if isinstance(endpoint.meta, dict) else {},
      deprecated=bool(endpoint.deprecated),
      request=request, response=response,
      stream=StreamPlan(
        channel_params=[name for name in placeholders if name in properties],
        connect_only=connect_param is not None, direct_channel=direct,
        push=_dump(endpoint.push),
        verb=_dump(envelope.verb) if envelope is not None else None,
        reply_payload=envelope.reply_payload if envelope is not None else None,
      ),
      types=types,
      docs=DocsPlan(description=spec.description, url=endpoint.docs, notes=list(endpoint.notes or [])),
    )
    return plan, class_

  def _request_plan(self, schema: Schema | None, type_id: str | None, types: TypeSet) -> RequestPlan:
    """The request side: its shape, its type's name, and -- for a flat object -- one
    entry per property, typed from the normalized record so a nested title resolves to
    the name it is defined under."""
    if schema is None:
      return RequestPlan(shape='none')
    top = types.get(type_id) if type_id is not None else None
    cast = top is not None and needs_cast(top)
    if schema.anyOf:
      return RequestPlan(shape='union', type=type_id, needs_cast=cast)
    if schema.type == 'array':
      return RequestPlan(shape='array', type=type_id, needs_cast=cast)
    record_fields = top['fields'] if top is not None and top['type'] == 'record' else {}
    required = set(schema.required or [])
    fields: list[RequestFieldPlan] = []
    for name, prop in (schema.properties or {}).items():
      field = record_fields.get(name)
      field_type = field['type'] if field is not None else canonical(self._inline(prop))
      is_required = name in required
      fixed = None
      default = None
      description = None
      if isinstance(prop, Schema):
        if is_required and prop.enum is not None and len(prop.enum) == 1:
          fixed = prop.enum[0]
        default = prop.default
        description = prop.description
      fields.append(RequestFieldPlan(
        wire=name, required=is_required, type=field_type, description=description,
        default=default, fixed=fixed,
      ))
    return RequestPlan(shape='fields', type=type_id, fields=fields, needs_cast=cast)

  def _inline(self, prop: Reference | Schema) -> Type:
    """A property's own type when no record carries it (a connect-only or direct-channel
    stream registers no `Parameters`); a nested record it cannot inline is `any`."""
    try:
      return self._parser.inline(prop, id=None)
    except ValueError:
      return {'type': 'scalar', 'base': 'any'}

  def _pagination_plan(
    self, pagination: Pagination, request: RequestPlan, request_schema: Schema | None,
    payload_id: str | None, payload_type: Type | None, types: TypeSet, fixed: Mapping[str, str],
  ) -> PaginationPlan:
    by_wire = {field.wire: field for field in request.fields}
    driver = driver_parameter(pagination)
    driver_field = by_wire.get(driver)
    strategy = pagination.strategy
    driver_required = (
      strategy in ('token', 'seek') and driver_field is not None and driver_field.required
    )
    size = None
    size_default = None
    if pagination.size is not None:
      if pagination.size.parameter in by_wire:
        size = pagination.size.parameter
      properties = request_schema.properties if request_schema is not None else None
      prop = (properties or {}).get(pagination.size.parameter)
      if isinstance(prop, Schema) and isinstance(prop.default, int) and not isinstance(prop.default, bool):
        size_default = prop.default
    done = pagination.done
    rows = done.rows
    state_type: Type = (
      strip_null(driver_field.type) if driver_field is not None
      else {'type': 'scalar', 'base': 'string'}
    )
    overlap = getattr(pagination, 'overlap', None)
    eligible = (
      (strategy == 'token' and done.kind == 'absent_cursor' and rows is not None)
      or (strategy == 'seek' and overlap is None and done.kind != 'unchanged')
      or (
        strategy == 'page' and (
          (done.kind == 'total' and rows is not None)
          or done.kind == 'empty'
          or (done.kind == 'short_page' and size is not None)
        )
      )
    )
    overlap_rows = strategy in ('window', 'seek') and overlap is not None and rows is not None
    row_type: Type | None = None
    if eligible or overlap_rows:
      payload = payload_type
      if payload is None and payload_id is not None:
        payload = {'type': 'ref', 'id': payload_id}
      if payload is not None:
        row_type = self.rows_type(payload, rows or '', types)
    seedable = (
      strategy not in ('token', 'seek') or has_zero_value(state_type) or driver_required
    )
    if eligible and row_type is not None and seedable:
      walker = 'paginated'
    elif payload_id is None:
      walker = 'none'
    elif strategy == 'offset' and not self._offset_steps(pagination, by_wire, size, size_default):
      walker = 'none'
    else:
      walker = 'generator'
    window = None
    if strategy == 'window':
      window = {
        'bound': _dump(pagination.bound), 'order': pagination.order, 'step': _dump(pagination.step),
      }
    return PaginationPlan(
      strategy=strategy, driver=driver, driver_required=driver_required,
      size=size, size_default=size_default,
      start=pagination.index.start if strategy == 'page' else None,
      done=_dump(done) or {}, rows=rows,
      cursor_from=pagination.cursor.from_ if strategy in ('token', 'seek') else None,
      overlap=_dump(overlap), window=window,
      row_type=row_type, state_type=state_type, seedable=seedable, walker=walker,
    )

  def _offset_steps(
    self, pagination: Pagination, by_wire: Mapping[str, RequestFieldPlan],
    size: str | None, size_default: int | None,
  ) -> bool:
    """Whether an `offset` walk has something to advance by: rows to count, a page size
    the method always sends, or a documented default for an optional one."""
    done = pagination.done
    counts_rows = done.kind in ('short_page', 'empty', 'unchanged') or (
      done.kind == 'total' and done.rows is not None
    )
    if counts_rows:
      return True
    if size is None:
      return False
    field = by_wire[size]
    return (field.required and field.fixed is None) or size_default is not None

  # -- the whole package ------------------------------------------------------------

  def build(self) -> PackagePlan:
    """Every core, shared scope, router and endpoint of the project."""
    config = self.project.config
    python = config.python
    cores: dict[str, CorePlan] = {}
    names = set(config.cores or {}) | set(python.cores if python is not None else {})
    for name in sorted(names):
      declared = (config.cores or {}).get(name)
      bound = python.cores.get(name) if python is not None else None
      cores[name] = CorePlan(
        meta=declared.meta if declared is not None else None,
        forward=list(bound.forward) if bound is not None and bound.forward is not None else None,
        params=(
          {k: NewParamPlan(type=v.type, required=v.required) for k, v in bound.new_params.items()}
          if bound is not None and bound.params is not None else None
        ),
        children=dict(bound.children) if bound is not None and bound.children is not None else None,
      )

    endpoints: list[EndpointPlan] = []
    functions: list[str] = []
    class_by_function: dict[str, str] = {}
    for spec_file in endpoint_specs(self.project):
      endpoint = load_endpoint(spec_file)
      if endpoint.surface is not None and endpoint.surface.kind == 'absent':
        continue
      plan, class_ = self._endpoint(endpoint, spec_file.parent)
      function = endpoint.resolved_function(spec_file, self.project.spec_dir)
      functions.append(function)
      class_by_function[function] = class_
      if plan is not None:
        endpoints.append(plan)
    endpoints.sort(key=lambda plan: plan.path)

    nodes = router_nodes(functions) if functions else []
    node_set = set(nodes)
    leaves = {tuple(function.split('.')) for function in functions}
    routers: list[RouterPlan] = []
    for node in nodes:
      depth = len(node)
      child_names = sorted({
        parts[depth] for parts in leaves if len(parts) > depth and parts[:depth] == node
      })
      children: list[RouterChildPlan] = []
      for name in child_names:
        child = (*node, name)
        if child in node_set:
          children.append(RouterChildPlan(name=name, kind='router', class_=neutral_class_name(name)))
        else:
          children.append(RouterChildPlan(
            name=name, kind='endpoint',
            class_=class_by_function.get('.'.join(child), neutral_class_name(name)),
          ))
      directory = self.project.endpoints_dir.joinpath(*node)
      doc = load_router(directory)
      try:
        core: str | None = self.core_name(directory)
      except ValueError:
        core = None
      routers.append(RouterPlan(
        path=list(node), core=core,
        doc=RouterDocPlan(description=doc.description, upstream=doc.upstream) if doc is not None else None,
        children=children,
      ))

    return PackagePlan(
      name=self.project.name,
      root_class=(python.name if python is not None and python.name else None) or pascal_case(self.project.name),
      cores=cores, schemas=dict(self._scopes), routers=routers, endpoints=endpoints,
    )


def build_plan(root: Path | Project) -> PackagePlan:
  """The plan for a project (or a project root)."""
  return PlanBuilder(resolve(root)).build()

