"""One endpoint module: its types and codecs, and a class holding the method (plus the
`<method>Paged` walker when pagination is declared).

The class takes its core as a constructor argument typed by the `@truewire/core` contract
(`HttpEndpoint<Meta>` for HTTP, `CommandEndpoint<Meta>` for a WebSocket command,
`DualEndpoint<Meta>` for an `rpc` endpoint declaring both transports,
`StreamEndpoint<Meta>` for a channel subscription) and calls exactly one verb on it. The
request object is the wire object: the method's first parameter is the endpoint's
`Request` (a stream's `Parameters`) interface, whose keys are the API's own names, and its
second is the `CallOptions` object (`validate`, `signal`), or `TransportOptions` (plus
`transport`) for an endpoint declaring both transports.
"""
import re
from dataclasses import dataclass, field

from typing_extensions import Literal

from truewire.codegen.policy import POLICY_MODULE
from truewire.codegen.seek import plan_exclusive_sentences
from truewire.plan.model import EndpointPlan, PackagePlan, PaginationPlan, RequestFieldPlan
from truewire.plan.types import Type, is_null, seek_cursor_format, strip_null
from truewire.spec.endpoint import last_row_field, seek_move_prose

from .names import binding, camel_case, is_binding, literal, member_access, pascal_case, property_key, string
from .printer import BANNER, relative_specifier
from .types import Module, visible_scopes

META_FILE = 'meta.ts'
"""Where `meta_module` writes the per-core `meta` interfaces."""
POLICY_FILE = f'{POLICY_MODULE}.ts'
"""`RefusedByPolicy` and `refuse`, written when `[policy].refuse` names an endpoint (W15)."""

PAGED_SUFFIX = 'Paged'

_WALK_LOCALS = frozenset(
  ('rows', 'response', 'request', 'options', 'next', 'total', 'totalRaw', 'totalSeen', 'following')
)
"""Names the walker binds itself; a driver parameter by one of these is walked as `state`."""

Token = tuple[Literal['local', 'plan', 'core', 'unknown'], object]
"""A type in a method signature, kept symbolic so a router can render it qualified: a
name defined in the endpoint module (`('local', 'Request')`), a plan type
(`('plan', Type)`), a `@truewire/core` type (`('core', 'CallOptions')`), or `unknown`
(`('unknown', None)`, what a `validate: false` call returns)."""

RAW_OPTIONS = 'CallOptions & { validate: false }'
"""The options type of the overload that returns the reply as the wire sent it."""
RAW_DOC = 'With `validate: false`: the parsed body as it came, typed `unknown`.'
"""JSDoc of that overload; the declared one carries the endpoint's own description."""


@dataclass(frozen=True)
class Param:
  name: str
  type: Token
  optional: bool = False


@dataclass(frozen=True)
class Method:
  """One method of the endpoint class, as a router needs it to delegate."""
  name: str
  params: list[Param]
  returns: tuple[str, tuple[Token, ...]]
  """`('promise', (T,))`, `('void', ())`, `('paginated', (row, state))`, `('seek', (row, key))`,
  `('generator', (T,))` or `('subscription', (message,))`."""
  doc: list[str | None] = field(default_factory=list)
  tags: list[str] = field(default_factory=list)
  is_async: bool = True
  generator: bool = False
  raw: bool = True
  """Whether a `validate: false` overload exists; a gRPC method has none."""


@dataclass(frozen=True)
class EndpointModule:
  file: str
  class_name: str
  core_type: str
  """The core interface this class takes: `HttpEndpoint`, `CommandEndpoint`, `DualEndpoint` or `StreamEndpoint`."""
  meta_type: str | None
  """The `meta.ts` interface the core is parameterised by, or `None` for a core with no schema."""
  types: list[str]
  """Names the module defines, for a router to qualify."""
  methods: list[Method]
  source: str


def endpoint_file(path: list[str]) -> str:
  """`repos.list_commits` -> `repos/list_commits.ts`: the spec directory, as a file."""
  # A leaf named `index` would be its grouping's router module (`<path>/index.ts`).
  leaf = f'{path[-1]}_' if path[-1] == 'index' else path[-1]
  return '/'.join([*path[:-1], f'{leaf}.ts'])


def meta_type_name(core: str) -> str:
  return f'{pascal_case(core)}Meta'


def core_interface(endpoint: EndpointPlan) -> str:
  """Which contract interface the endpoint's kind and transport call for."""
  if endpoint.kind == 'stream':
    return 'ReplyStreamEndpoint' if endpoint.stream is not None and endpoint.stream.reply else 'StreamEndpoint'
  if is_dual(endpoint):
    return 'DualEndpoint'
  return 'HttpEndpoint' if 'http' in endpoint.transports else 'CommandEndpoint'


def is_dual(endpoint: EndpointPlan) -> bool:
  """Whether an `rpc` endpoint declares both `http` and `ws`: its method takes a `transport` option."""
  return endpoint.kind == 'rpc' and 'http' in endpoint.transports and 'ws' in endpoint.transports


def options_type(endpoint: EndpointPlan) -> str:
  """The options object an endpoint's methods take: `TransportOptions` when the caller picks
  the transport, `CallOptions` otherwise."""
  return 'TransportOptions' if is_dual(endpoint) else 'CallOptions'


def render_type(module: Module, token: Token) -> str:
  kind, value = token
  if kind == 'unknown':
    return 'unknown'
  if kind == 'local':
    return module.ref(str(value), type_only=True)
  if kind == 'plan':
    return module.type_expr(value)  # type: ignore[arg-type]
  module.core(str(value), type_only=True)
  return str(value)


def render_return(module: Module, returns: tuple[str, tuple[Token, ...]]) -> str:
  shape, tokens = returns
  if shape == 'void':
    return 'Promise<void>'
  if shape == 'promise':
    return f'Promise<{render_type(module, tokens[0])}>'
  if shape == 'paginated':
    module.core('PaginatedResponse', type_only=True)
    return f'PaginatedResponse<{render_type(module, tokens[0])}, {render_type(module, tokens[1])}>'
  if shape == 'seek':
    module.core('PaginatedResponse', type_only=True)
    module.core('SeekState', type_only=True)
    row = render_type(module, tokens[0])
    return f'PaginatedResponse<{row}, SeekState<{row}, {render_type(module, tokens[1])}>>'
  if shape == 'subscription':
    module.core('Subscription', type_only=True)
    return f'Subscription<{", ".join(render_type(module, token) for token in tokens)}>'
  return f'AsyncGenerator<{render_type(module, tokens[0])}, void, undefined>'


def render_implementation_return(module: Module, returns: tuple[str, tuple[Token, ...]]) -> str:
  """The return type an implementation signature declares under its overloads: the declared
  one, widened by the `validate: false` overload's for a `seek` walker, whose state carries
  rows and so is invariant in the row type (neither overload's type is assignable to the
  other's). Callers only ever see the overloads."""
  raw = raw_returns(returns)
  if returns[0] == 'seek' and raw is not None:
    return f'{render_return(module, returns)} | {render_return(module, raw)}'
  return render_return(module, returns)


def render_params(module: Module, params: list[Param]) -> str:
  return ', '.join(
    f'{param.name}{"?" if param.optional else ""}: {render_type(module, param.type)}'
    for param in params
  )


def raw_returns(returns: tuple[str, tuple[Token, ...]]) -> tuple[str, tuple[Token, ...]] | None:
  """`returns` as a `validate: false` call leaves it: the value (a promise's, a walker's
  rows, a generator's pages, a subscription's messages) is the body as the wire sent it,
  so `unknown` takes the declared type's place while a walker's state type stays (a
  subscription's declared reply goes raw too). `None`
  for a method that returns nothing, or whose value is `unknown` already (a stream with no
  declared message): there is no second overload to make."""
  shape, tokens = returns
  if shape == 'subscription':
    # A declared reply is validated like each push, so `validate: false` leaves both raw.
    if all(token[0] == 'unknown' for token in tokens):
      return None
    return shape, tuple(('unknown', None) for _ in tokens)
  if shape == 'void' or tokens[0][0] == 'unknown':
    return None
  return shape, (('unknown', None), *tokens[1:])


def emit_signatures(module: Module, method: Method):
  """Write `method`'s JSDoc and, when it returns a value, the two overload signatures
  its implementation follows.

  A generated method returns the parsed value -- `Date`s, `Decimal`s, literal unions --
  only when the reply was validated; `validate: false` hands back the body as the wire
  sent it, and one `Promise<Commits>` lies for that call. The honest typing is an
  overload per outcome, `validate: false` first (declaration order decides, and
  `CallOptions` would otherwise match it): `options: CallOptions & { validate: false }`
  returns `unknown` in the declared type's place, and `options?: CallOptions` returns
  the declared type. Any other `validate` -- `true`, omitted, a `boolean` variable --
  resolves to the declared one; only a literal `false` at the call site is knowably raw.
  The implementation signature and body are unchanged, so the runtime is too.

  The raw overload's request parameter is required even when the declared one's is not
  (`request?: X`): its options object is required and follows it.
  """
  w = module.writer
  raw = raw_returns(method.returns) if method.raw else None
  if raw is None:
    w.jsdoc(*method.doc, tags=method.tags)
    return
  name = property_key(method.name)
  request = [Param(p.name, p.type, optional=False) for p in method.params if p.name != 'options']
  head = render_params(module, request)
  options = next((str(p.type[1]) for p in method.params if p.name == 'options'), 'CallOptions')
  module.core(options, type_only=True)
  raw_options = RAW_OPTIONS if options == 'CallOptions' else f'{options} & {{ validate: false }}'
  w.jsdoc(RAW_DOC)
  w.line(f'{name}({head + ", " if head else ""}options: {raw_options}): {render_return(module, raw)}')
  w.jsdoc(*method.doc, tags=method.tags)
  w.line(f'{name}({render_params(module, method.params)}): {render_return(module, method.returns)}')


def method_docs(endpoint: EndpointPlan) -> tuple[list[str | None], list[str]]:
  tags: list[str] = []
  if endpoint.docs.url:
    tags.append(f'@see {endpoint.docs.url}')
  if endpoint.deprecated:
    tags.append('@deprecated')
  return [endpoint.docs.description, *([refused_doc(endpoint)] if endpoint.refused else [])], tags


def refused_doc(endpoint: EndpointPlan) -> str:
  """The docs line of a refused endpoint's methods: a subscription throws, every other
  method returns a promise that rejects."""
  fails = 'throws' if endpoint.kind == 'stream' else 'rejects with'
  return f'Refused by `[policy].refuse`: {fails} `RefusedByPolicy` before any request is made.'


def emit_refusal(module: Module, endpoint: EndpointPlan):
  """`refuse('<function>')`, the first statement of a body that reaches the core, when
  `[policy].refuse` names the endpoint: the call throws (an `async` one rejects) before
  any request. Every other method of the class reaches the core through that body."""
  if endpoint.refused:
    module.imports.add(relative_specifier(module.file, POLICY_FILE), 'refuse')
    module.writer.line(f'refuse({string(endpoint.function)})')


# -- response paths ---------------------------------------------------------------------

_SEGMENT = re.compile(r'\[(-?\d+)\]|([^.\[\]]+)')


def read_path(subject: str, path: str, *, optional: bool) -> str:
  """An expression reading a dotted/indexed response path (`data.rows`, `[-1].id`) off
  `subject`, optional-chained at every hop so a missing level reads as `undefined`."""
  expr = subject
  first = True
  for index, key in _SEGMENT.findall(path):
    chain = '?.' if (optional or not first) else '.'
    if index:
      n = int(index)
      expr = f'{expr}{chain}at({n})' if n < 0 else f'{expr}{chain}[{n}]'
    else:
      expr = member_access(expr, key, optional=chain == '?.')
    first = False
  return expr


# -- the module -------------------------------------------------------------------------


@dataclass(frozen=True)
class _Head:
  """What every endpoint module opens with: its types defined, the contract interface
  and `CallOptions` imported, and the constructor parameter's type."""
  module: Module
  core_type: str
  meta_type: str | None
  core_param: str


def _open(plan: PackagePlan, endpoint: EndpointPlan) -> _Head:
  file = endpoint_file(endpoint.path)
  module = Module(plan, file, local=endpoint.types, visible=visible_scopes(plan, endpoint.path))
  module.define_all(endpoint.types)
  core_type = core_interface(endpoint)
  core_plan = plan.cores.get(endpoint.core)
  meta_type = meta_type_name(endpoint.core) if core_plan is not None and core_plan.meta is not None else None
  module.core(core_type, type_only=True)
  module.core(options_type(endpoint), type_only=True)
  if meta_type is not None:
    module.imports.add(relative_specifier(file, META_FILE), meta_type, type_only=True)
  core_param = f'{core_type}<{meta_type}>' if meta_type is not None else core_type
  return _Head(module, core_type, meta_type, core_param)


def render_endpoint(plan: PackagePlan, endpoint: EndpointPlan, *, class_name: str) -> EndpointModule:
  """Render one `rpc` endpoint's module."""
  head = _open(plan, endpoint)
  module, core_type, meta_type, core_param = head.module, head.core_type, head.meta_type, head.core_param
  file = module.file
  w = module.writer

  method = camel_case(endpoint.path[-1])
  request = endpoint.request
  request_type = request.type
  fixed = [f for f in request.fields if f.fixed is not None] if request.shape == 'fields' else []
  args_type = request_type
  if fixed and request_type is not None:
    args_type = f'{class_name}Args'
    omitted = ' | '.join(string(f.wire) for f in fixed)
    defaults = '; '.join(f'{property_key(f.wire)}?: {literal(f.fixed)}' for f in fixed)
    w.jsdoc(
      f'`{method}`\'s request: `{request_type}` with the fixed wire values the method '
      'fills in for you.'
    )
    w.line(f'export type {args_type} = Omit<{request_type}, {omitted}> & {{ {defaults} }}')
    module.declare(args_type)
    w.blank()
  request_optional = request.shape == 'fields' and not any(
    f.required and f.fixed is None for f in request.fields
  )
  payload = endpoint.response.payload

  methods: list[Method] = []
  doc, tags = method_docs(endpoint)
  main_params: list[Param] = []
  if request_type is not None:
    main_params.append(Param('request', ('local', args_type), optional=request_optional))
  main_params.append(Param('options', ('core', options_type(endpoint)), optional=True))
  returns: tuple[str, tuple[Token, ...]] = (
    ('promise', (('local', payload),)) if payload is not None else ('void', ())
  )
  main = Method(method, main_params, returns, doc=doc, tags=tags)

  paged: Method | None = None
  paged_note: str | None = None
  pagination = endpoint.pagination
  if pagination is not None and pagination.walker != 'none' and request_type is not None:
    paged, paged_note = _paged_method(
      module, endpoint, pagination, method=method, class_name=class_name,
      request_type=args_type, request_optional=request_optional,
    )
  if paged is not None:
    methods.append(paged)
  methods.append(main)

  w.jsdoc(endpoint.docs.description)
  if paged_note is not None:
    w.line(f'// {paged_note}')
  with w.block(f'export class {class_name} {{'):
    w.line(f'constructor(readonly core: {core_param}) {{}}')
    if paged is not None:
      w.blank()
      _emit_paged(module, endpoint, pagination, paged, main=main, request_type=args_type)  # type: ignore[arg-type]
    w.blank()
    emit_signatures(module, main)
    signature = f'async {main.name}({render_params(module, main.params)}): {render_return(module, main.returns)}'
    with w.block(f'{signature} {{'):
      emit_refusal(module, endpoint)
      if fixed:
        fills = ', '.join(
          f'{property_key(f.wire)}: {member_access("request", f.wire, optional=request_optional)} ?? {literal(f.fixed)}'
          for f in fixed
        )
        w.line(f'const wire: {request_type} = {{ ...request, {fills} }}')
      call = 'await ' if payload is None else 'return '
      with w.block(f'{call}this.core.request({{', '})'):
        if core_type in ('HttpEndpoint', 'DualEndpoint'):
          w.line(f'method: {string(endpoint.wire.method) if endpoint.wire.method else "undefined"},')
        w.line(f'path: {string(endpoint.wire.path or "")},')
        if request_type is None:
          w.line('request: undefined,')
          w.line('requestCodec: undefined,')
        else:
          w.line('request: wire,' if fixed else ('request: request ?? {},' if request_optional else 'request,'))
          w.line(f'requestCodec: {module.ref(request_type)},')
        w.line(f'responseCodec: {module.ref(payload) if payload is not None else "undefined"},')
        w.line(f'meta: {literal(endpoint.meta) if meta_type is not None else "{}"},')
        w.line('...options,')
        if core_type == 'DualEndpoint':
          w.line(f'transport: options?.transport ?? {string(endpoint.transports[0])},')

  defined = list(endpoint.types)
  if fixed:
    defined.append(args_type)
  if paged is not None:
    defined.append(str(paged.params[0].type[1]))
  return EndpointModule(
    file=file, class_name=class_name, core_type=core_type, meta_type=meta_type,
    types=defined, methods=methods, source=module.render(BANNER),
  )


# -- streams ----------------------------------------------------------------------------

_CHANNEL_PLACEHOLDER = re.compile(r'\{([^{}]+)\}')


def channel_expr(channel: str, subject: str) -> str:
  """The expression a direct-channel or connect-only stream subscribes with: the channel
  template with every `{name}` read off `subject`, as a template literal -- or the bare
  member access when the whole template is one placeholder (a listenKey-style stream,
  where the value *is* the channel)."""
  if _CHANNEL_PLACEHOLDER.fullmatch(channel):
    return member_access(subject, channel[1:-1], optional=False)
  parts: list[str] = []
  last = 0
  for m in _CHANNEL_PLACEHOLDER.finditer(channel):
    parts.append(_template_text(channel[last:m.start()]))
    parts.append('${' + member_access(subject, m.group(1), optional=False) + '}')
    last = m.end()
  parts.append(_template_text(channel[last:]))
  return '`' + ''.join(parts) + '`'


def _template_text(text: str) -> str:
  return text.replace('\\', '\\\\').replace('`', '\\`').replace('${', '\\${')


def _declare_parameters(
  module: Module, fields: list[RequestFieldPlan], *, class_name: str, method: str, channel: str,
) -> str:
  """The parameters interface of a stream whose values only fill the channel template
  (`directChannel`, `connectOnly`): the plan registers no `Parameters` record and no
  codec, since nothing is sent as a subscribe frame, so the method's parameter type is
  declared here from the fields alone."""
  w = module.writer
  name = 'Parameters' if 'Parameters' not in module.names else f'{class_name}Parameters'
  w.jsdoc(f'`{method}`\'s parameters: the placeholders of `{channel}`, which the method fills in.')
  with w.block(f'export interface {name} {{'):
    for f in fields:
      w.jsdoc(f.description)
      w.line(f'{property_key(f.wire)}{"" if f.required else "?"}: {module.type_expr(f.type)}')
  module.declare(name)
  w.blank()
  return name


def render_stream(plan: PackagePlan, endpoint: EndpointPlan, *, class_name: str) -> EndpointModule:
  """Render one `stream` endpoint's module: the parameters, the pushed message, and a
  class whose one method returns the core's `Subscription` for the channel.

  What the Python backend passes to `self.subscribe(...)` is passed here to
  `core.subscribe({...})`: the channel template and the parameters object with its codec
  for the general shape; the channel filled from the parameters and no object for a
  direct-channel or connect-only stream (`docs/plan.md`); the message codec, the reply
  codec when the stream declares `reply` (ADR 0014, and the class then takes a
  `ReplyStreamEndpoint`), the declared `meta`, and the call options.
  """
  head = _open(plan, endpoint)
  module, meta_type, core_param = head.module, head.meta_type, head.core_param
  w = module.writer
  stream = endpoint.stream
  assert stream is not None and endpoint.wire.channel is not None
  channel = endpoint.wire.channel
  method = camel_case(endpoint.path[-1])
  request = endpoint.request
  parameters_type = request.type
  fills_channel = parameters_type is None and bool(request.fields)
  if fills_channel:
    parameters_type = _declare_parameters(
      module, request.fields, class_name=class_name, method=method, channel=channel,
    )
  parameters_optional = not fills_channel and request.shape == 'fields' and not any(
    f.required for f in request.fields
  )
  message = endpoint.response.payload
  reply = stream.reply

  params: list[Param] = []
  if parameters_type is not None:
    params.append(Param('parameters', ('local', parameters_type), optional=parameters_optional))
  params.append(Param('options', ('core', 'CallOptions'), optional=True))
  doc, tags = method_docs(endpoint)
  pushed: Token = ('local', message) if message is not None else ('unknown', None)
  # A declared `reply` (ADR 0014) types the acknowledgement: the subscription's second argument.
  main = Method(
    method, params, ('subscription', (pushed, ('local', reply)) if reply else (pushed,)),
    doc=doc, tags=tags, is_async=False,
  )

  w.jsdoc(endpoint.docs.description)
  with w.block(f'export class {class_name} {{'):
    w.line(f'constructor(readonly core: {core_param}) {{}}')
    w.blank()
    emit_signatures(module, main)
    signature = f'{main.name}({render_params(module, main.params)}): {render_return(module, main.returns)}'
    with w.block(f'{signature} {{'):
      emit_refusal(module, endpoint)
      with w.block('return this.core.subscribe({', '})'):
        w.line(f'channel: {channel_expr(channel, "parameters") if fills_channel else string(channel)},')
        if parameters_type is None or fills_channel:
          w.line('parameters: undefined,')
          w.line('parametersCodec: undefined,')
        else:
          w.line('parameters: parameters ?? {},' if parameters_optional else 'parameters,')
          w.line(f'parametersCodec: {module.ref(parameters_type)},')
        w.line(f'messageCodec: {module.ref(message) if message is not None else "undefined"},')
        if reply:
          w.line(f'replyCodec: {module.ref(reply)},')
        w.line(f'meta: {literal(endpoint.meta) if meta_type is not None else "{}"},')
        w.line('...options,')

  defined = list(endpoint.types)
  if fills_channel and parameters_type is not None:
    defined.append(parameters_type)
  return EndpointModule(
    file=module.file, class_name=class_name, core_type=head.core_type, meta_type=meta_type,
    types=defined, methods=[main], source=module.render(BANNER),
  )


# -- pagination -------------------------------------------------------------------------


def _clamp(size: str, pagination: PaginationPlan) -> str:
  """`size` capped at the size's schema `maximum`, when declared: a caller asking for more
  gets a full page at the maximum, which must not read as a short last page."""
  return f'Math.min({size}, {pagination.size_maximum})' if pagination.size_maximum is not None else size


def _payload_is_union(module: Module, endpoint: EndpointPlan) -> bool:
  """Whether the response payload is (a reference to) a union of more than one non-null variant."""
  payload = endpoint.response.payload
  seen: set[str] = set()
  while payload is not None and payload not in seen:
    seen.add(payload)
    t = module.local.get(payload)
    if t is None:
      t = next((types[payload] for types in module.plan.schemas.values() if payload in types), None)
    if t is None:
      return False
    if t['type'] == 'ref':
      payload = t['id']
      continue
    return t['type'] == 'union' and sum(1 for v in t['variants'] if not is_null(v['type'])) > 1
  return False


def _lookup_type(module: Module, name: str) -> Type | None:
  t = module.local.get(name)
  if t is None:
    t = next((types[name] for types in module.plan.schemas.values() if name in types), None)
  return t


def _resolved(module: Module, t: Type | None) -> Type | None:
  """`t` through every `Ref` and nullable layer to the tree it names."""
  seen: set[str] = set()
  while t is not None:
    if t['type'] == 'union' and any(is_null(v['type']) for v in t['variants']):
      inner = strip_null(t)
      if inner is t:
        return t
      t = inner
    elif t['type'] == 'ref' and t['id'] not in seen:
      seen.add(t['id'])
      t = _lookup_type(module, t['id'])
    else:
      return t
  return t


def _walk_to(module: Module, t: Type | None, path: str) -> Type | None:
  """The tree a dotted response path (no indices) leads to, or `None`."""
  for index, key in _SEGMENT.findall(path):
    t = _resolved(module, t)
    if index or t is None or t['type'] != 'record' or key not in t['fields']:
      return None
    t = t['fields'][key]['type']
  return t


def _union_rows(module: Module, endpoint: EndpointPlan, pagination: PaginationPlan) -> Type | None:
  """The row type of a token walk over a union payload: every variant's own row type,
  joined into one union in declaration order (bybit's `market.instruments`, one variant
  per product category), as the Python backend does. `None` when a variant has no rows
  list or no variant carries the cursor. A `page` walk not ended by a `total` joins the
  rows the same way (binance's broker `sub_account_futures_summary_v2`, one variant per
  `futuresType`), with no cursor to carry."""
  token = pagination.strategy == 'token'
  indexed = pagination.strategy == 'page' and pagination.done.get('kind') != 'total'
  if not (token or indexed) or pagination.walker != 'paginated' or not pagination.rows:
    return None
  if token and not pagination.cursor_from:
    return None
  payload = _resolved(module, {'type': 'ref', 'id': endpoint.response.payload}) if endpoint.response.payload else None  # type: ignore[typeddict-item]
  if payload is None or payload['type'] != 'union':
    return None
  items: list[Type] = []
  cursor = False
  for variant in payload['variants']:
    if is_null(variant['type']):
      continue
    rows = _resolved(module, _walk_to(module, variant['type'], pagination.rows))
    if rows is None or rows['type'] != 'list':
      return None
    if rows['item'] not in items:
      items.append(rows['item'])
    cursor = cursor or (token and _walk_to(module, variant['type'], pagination.cursor_from or '') is not None)
  if not items or (token and not cursor):
    return None
  if len(items) == 1:
    return items[0]
  return {'type': 'union', 'variants': [{'type': item} for item in items]}  # type: ignore[typeddict-item]


def _page_view(paths: list[tuple[str, str]], *, optional: bool) -> str:
  """A structural type every union variant is cast to, holding just the paths the walk
  reads (`{ list?: Row[] | null; nextPageCursor?: string | null }`), each optional."""
  tree: dict = {}
  for path, leaf in paths:
    node = tree
    keys = [key for _, key in _SEGMENT.findall(path)]
    for key in keys[:-1]:
      node = node.setdefault(key, {})
    node[keys[-1]] = leaf

  def render(node: dict) -> str:
    entries = '; '.join(f'{property_key(k)}?: {render(v) if isinstance(v, dict) else v} | null' for k, v in node.items())
    return f'{{ {entries} }}'

  return render(tree) + (' | null | undefined' if optional else '')


def _size_expr(pagination: PaginationPlan, endpoint: EndpointPlan) -> tuple[str | None, str | None]:
  """The page-size expression the walk measures a page against (clamped to the size's
  schema `maximum` when declared), and, for an optional size with no documented default,
  the caller's raw value to test against `undefined` first (`None` otherwise). The raw
  property narrows in TypeScript where a clamped expression would not."""
  if pagination.size is None:
    return None, None
  size = member_access('request', pagination.size, optional=False)
  field = next((f for f in endpoint.request.fields if f.wire == pagination.size), None)
  # An `int64` size is `number | bigint`; a page walk counts rows in `number`s.
  count = (lambda e: f'Number({e})') if field is not None and _is_int64(field.type) else (lambda e: e)
  if field is not None and field.required:
    return _clamp(count(size), pagination), None
  if pagination.size_default is not None:
    return _clamp(count(f'({size} ?? {pagination.size_default})'), pagination), None
  return _clamp(count(size), pagination), size


def _exhausted(kind: str, size: str | None, size_unknown: str | None) -> str:
  """The condition under which a page ends the walk, for `short_page`/`empty`."""
  if kind == 'empty' or size is None:
    return 'rows.length === 0'
  if size_unknown:
    return f'rows.length === 0 || ({size_unknown} !== undefined && rows.length < {size})'
  return f'rows.length === 0 || rows.length < {size}'


def _is_int64(t: Type) -> bool:
  """Whether `t` is an `int64` integer, rendered `number | bigint`."""
  return t['type'] == 'scalar' and t.get('format') == 'int64'


def _walk_state_type(pagination: PaginationPlan) -> Type:
  """The state a resumable walker carries. A page index is a small count: an `int64` index
  parameter (`number | bigint`) still walks as a `number`, which the request field accepts,
  so `page + 1` type-checks and the declared `PaginatedResponse` matches the walk."""
  assert pagination.state_type is not None
  if pagination.strategy == 'page' and _is_int64(pagination.state_type):
    return {'type': 'scalar', 'base': 'integer'}
  return pagination.state_type


def _zero(t: Type) -> str:
  if t['type'] == 'scalar' and t.get('format') == 'integer-string':
    return '0n'
  if t['type'] == 'scalar' and t['base'] in ('integer', 'number'):
    return '0'
  if t['type'] == 'scalar' and t['base'] == 'boolean':
    return 'false'
  return "''"


def _paged_method(
  module: Module, endpoint: EndpointPlan, pagination: PaginationPlan, *, method: str,
  class_name: str, request_type: str, request_optional: bool,
) -> tuple[Method | None, str | None]:
  """The walker's signature, or `(None, note)` for a declaration this backend has no
  walker for yet."""
  union_rows: Type | None = None
  if pagination.strategy != 'seek' and _payload_is_union(module, endpoint):
    # Each variant may lack the rows path or the cursor: a token walk reads both through a
    # structural view of the page, over the union of every variant's rows.
    union_rows = _union_rows(module, endpoint, pagination)
    if union_rows is None:
      return None, f'truewire: a walk over a union payload has no TypeScript walker; call `{method}` per page.'
  if pagination.strategy == 'seek':
    return _seek_method(
      module, endpoint, pagination, method=method, class_name=class_name,
      request_type=request_type, request_optional=request_optional,
    )
  paged_type = f'{class_name}{PAGED_SUFFIX}Request'
  w = module.writer
  if pagination.driver_required:
    w.jsdoc(f'`{method}{PAGED_SUFFIX}`\'s request: `{request_type}`, whose `{pagination.driver}` seeds the walk.')
    w.line(f'export type {paged_type} = {request_type}')
  else:
    w.jsdoc(
      f'`{method}{PAGED_SUFFIX}`\'s request: `{request_type}` without `{pagination.driver}`, '
      'which the walk advances.'
    )
    w.line(f'export type {paged_type} = Omit<{request_type}, {string(pagination.driver)}>')
  module.declare(paged_type)
  w.blank()
  doc, tags = method_docs(endpoint)
  params = [
    Param('request', ('local', paged_type), optional=request_optional and not pagination.driver_required),
    Param('options', ('core', options_type(endpoint)), optional=True),
  ]
  if pagination.walker == 'paginated':
    assert pagination.row_type is not None and pagination.state_type is not None
    doc = [*doc, f'Paged variant of `{method}`: awaitable (flattens every page) or async-iterable (one page at a time).']
    return Method(
      f'{method}{PAGED_SUFFIX}', params,
      ('paginated', (('plan', union_rows or pagination.row_type), ('plan', _walk_state_type(pagination)))),
      doc=doc, tags=tags, is_async=False,
    ), None
  doc = [*doc, f'Paged variant of `{method}`: an async iterator over every page\'s response.']
  assert endpoint.response.payload is not None
  return Method(
    f'{method}{PAGED_SUFFIX}', params, ('generator', (('local', endpoint.response.payload),)),
    doc=doc, tags=tags, generator=True,
  ), None


def _emit_paged(
  module: Module, endpoint: EndpointPlan, pagination: PaginationPlan, paged: Method, *,
  main: Method, request_type: str,
):
  if pagination.strategy == 'seek':
    _emit_seek(module, endpoint, pagination, paged, main=main)
    return
  w = module.writer
  emit_signatures(module, paged)
  params, returns = _paged_head(module, paged)
  driver = pagination.driver
  state = driver if is_binding(driver) and driver not in _WALK_LOCALS else 'state'
  key = property_key(driver)
  size, size_unknown = _size_expr(pagination, endpoint)
  rows_path = pagination.rows or ''
  optional = endpoint.response.optional
  rows_expr = read_path('response', rows_path, optional=optional)
  if rows_path or optional:
    rows_expr = f'{rows_expr} ?? []'
  done = pagination.done
  kind = done.get('kind')
  zero_is_absent = pagination.strategy == 'token' and not pagination.driver_required

  def call(value: str) -> str:
    entry = value if value == key else f'{key}: {value}'
    return f'await this.{main.name}({{ ...request, {entry} }}, options)'

  if pagination.walker == 'paginated':
    assert pagination.row_type is not None and pagination.state_type is not None
    # A readonly tuple or union row binds wrongly unparenthesised in `X[]` (`readonly [a, b][]`
    # is a readonly array of mutable tuples), as it would in any list type.
    union_rows = _union_rows(module, endpoint, pagination) if _payload_is_union(module, endpoint) else None
    rows_type = f'{module._wrapped(union_rows or pagination.row_type)}[]'
    state_type = module.type_expr(_walk_state_type(pagination))
    subject = 'response'
    if union_rows is not None:
      # `page` is also a page walk's own state binding (binance's `page` parameter).
      subject = 'view' if state == 'page' else 'page'
      read = [(rows_path, rows_type)]
      if pagination.strategy == 'token':
        read.append((pagination.cursor_from or '', state_type))
      view = _page_view(read, optional=optional)
      rows_expr = f'{read_path(subject, rows_path, optional=optional)} ?? []'

    module.core('PaginatedResponse')
    with w.block(f'{paged.name}({params}): {returns} {{'):
      if pagination.strategy == 'page' and kind == 'total':
        w.line('let totalSeen: number | null = null')
        module.core('LogicError')
      with w.block(f'const next = async ({state}: {state_type}): Promise<[{rows_type}, {state_type} | null]> => {{'):
        w.line(f'const response = {call(f"{state} || undefined" if zero_is_absent else state)}')
        if union_rows is not None:
          w.line(f'const {subject} = response as {view}')
        w.line(f'const rows = {rows_expr}')
        if pagination.strategy == 'page':
          start = pagination.start if pagination.start is not None else 1
          if kind == 'total':
            _emit_total_check(w, paged.name, read_path('response', str(done.get('path', '')), optional=optional))
            w.line(f'if ({_total_done(done, state, start, size, size_unknown)}) return [rows, null]')
          else:
            w.line(f'if ({_exhausted(str(kind), size, size_unknown)}) return [rows, null]')
          w.line(f'return [rows, {state} + 1]')
        else:  # token; a `seek` walk renders through `_emit_seek`
          if kind == 'empty':
            w.line('if (rows.length === 0) return [rows, null]')
          cursor = read_path(subject, pagination.cursor_from or '', optional=optional)
          w.line(f'const following = {cursor} ?? null')
          w.line('return [rows, following || null]')
      if pagination.strategy == 'page':
        seed = str(pagination.start if pagination.start is not None else 1)
      elif pagination.driver_required:
        seed = member_access('request', driver, optional=False)
      else:
        seed = _zero(pagination.state_type)
      w.line(f'return new PaginatedResponse({seed}, next)')
    return

  # generator: yield every response, advancing the driver until the declared terminator.
  countable = bool(rows_path) or _payload_is_list(module, endpoint)
  with w.block(f'async *{paged.name}({params}): {returns} {{'):
    state_type = module.type_expr(pagination.state_type) if pagination.state_type is not None else 'number'
    if pagination.strategy == 'page':
      w.line(f'let {state} = {pagination.start if pagination.start is not None else 1}')
    elif pagination.strategy == 'offset':
      w.line(f'let {state} = 0')
    else:  # token
      seed = member_access('request', driver, optional=False) if pagination.driver_required else 'undefined'
      w.line(f'let {state}: {state_type} | undefined = {seed}')
    if kind == 'total':
      w.line('let totalSeen: number | null = null')
      module.core('LogicError')
    # Annotated: a token cursor read back into the next call's argument is otherwise a
    # circular inference (`response` -> `following` -> the driver -> `response`).
    payload = render_type(module, ('local', endpoint.response.payload)) if endpoint.response.payload else None
    with w.block('while (true) {'):
      w.line(f'const response{": " + payload if payload else ""} = {call(state)}')
      w.line('yield response')
      if countable:
        w.line(f'const rows = {rows_expr}')
      if pagination.strategy == 'token':
        cursor = read_path('response', pagination.cursor_from or '', optional=optional)
        w.line(f'const following = {cursor} ?? null')
        if kind == 'empty' and countable:
          w.line('if (rows.length === 0) return')
        w.line('if (!following) return')
        w.line(f'{state} = following')
        return
      step = 'rows.length' if countable else size
      if kind == 'total':
        _emit_total_check(w, paged.name, read_path('response', str(done.get('path', '')), optional=optional))
        if pagination.strategy == 'page':
          w.line(f'if ({_total_done(done, state, pagination.start if pagination.start is not None else 1, size, size_unknown)}) return')
        elif done.get('counts') == 'items':
          empty = 'rows.length === 0 || ' if countable else ''
          w.line(f'if ({empty}{state} + {step} >= total) return')
        else:
          w.line(f'if ({state} / {size or "1"} + 1 >= total) return')
      else:
        w.line(f'if ({_exhausted(str(kind), size, size_unknown)}) return')
      if pagination.strategy == 'page':
        w.line(f'{state} += 1')
      else:
        w.line(f'{state} += {step}')


def _paged_head(module: Module, paged: Method) -> tuple[str, str]:
  """A walker's rendered parameter list (the request defaulting to `{}` when optional) and return type."""
  request_param, options_param = paged.params
  request_text = f'request: {render_type(module, request_param.type)}'
  if request_param.optional:
    request_text += ' = {}'
  return (
    f'{request_text}, options?: {render_type(module, options_param.type)}',
    render_implementation_return(module, paged.returns),
  )


_SEEK_CONVERTERS = {
  'epoch-seconds': 'timestampSeconds', 'epoch-millis': 'timestampMillis',
  'epoch-micros': 'timestampMicros', 'epoch-nanos': 'timestampNanos', 'date-time': 'timestampIso',
}
"""Timestamp formats a `seek` bound can take -> the `@truewire/core` converter its row keys parse through."""

_SPAN_UNIT_MS = {'us': '0.001', 'ms': '1', 's': '1000'}
"""A `seek` span's unit -> milliseconds per unit, for a bound that renders as a `Date`."""


def _seek_keys(module: Module | None, bound: Type, cursor: Type | None = None) -> tuple[str, bool]:
  """How the walk normalizes a row's cursor value to compare with the bound, and whether the
  bound is a time: a timestamp converter for a timestamp format, `'number'` for a numeric
  bound (a numeral-string id still compares numerically), `'string'` otherwise (unordered:
  the runtime takes the last row's). `module` imports the converter; `None` only asks.

  The converter is the *row field's* when it declares an instant format other than the
  bound's (lighter's `epoch-seconds` fundings under an `epoch-millis` bound, TRU-197): a
  `validate: false` row carries the raw epoch, and both parse to a `Date`. The bound's own
  converter only ever encodes the request, through the plain method's codec."""
  if bound['type'] == 'scalar':
    converter = _SEEK_CONVERTERS.get(seek_cursor_format(cursor, bound) or bound.get('format', ''))
    if converter is not None:
      if module is not None:
        module.core(converter)
      return converter, True
    if bound.get('format') in ('integer-string', 'int64'):
      # An int64 bound may pass 2^53, so its row keys compare as bigints, never rounded.
      return "'bigint'", False
    if bound['base'] in ('integer', 'number'):
      return "'number'", False
  return "'string'", False


def _seek_size_field(endpoint: EndpointPlan, pagination: PaginationPlan) -> RequestFieldPlan | None:
  """The request field carrying a `seek` walk's page size, when it is a number the walk can
  clamp: `integer` (an `int64` included) or `number`. A size sent as a string (`string`,
  `integer-string`, a `bigint`) is left alone, as Python leaves a non-`int` one."""
  field = next((f for f in endpoint.request.fields if f.wire == pagination.size), None) if pagination.size else None
  if field is None:
    return None
  tree = strip_null(field.type)
  if tree['type'] != 'scalar' or tree['base'] not in ('integer', 'number') or tree.get('format') not in (None, 'int64'):
    return None
  return field


def _seek_size(endpoint: EndpointPlan, pagination: PaginationPlan, seek: dict) -> tuple[str | None, str]:
  """The statement binding `size`, the page size every request of the walk sends (`None`
  when there is no size the walk can clamp), and the cap a full page holds.

  A given size is clamped once, before the walk: at most the size's schema `maximum`, and at
  least 2, since a page must hold one new row beside the boundary row it re-reads (with an
  inclusive moving bound, a page of 1 re-serves that row alone and cannot advance). The
  clamped size is the cap. A size the caller omits stays unset on the wire, and the cap is
  the declared fixed `cap` or the size's schema default, else `undefined` (unknown); so is it
  for a size `_seek_size_field` leaves alone."""
  fallback = seek.get('cap') if seek.get('cap') is not None else pagination.size_default
  field = _seek_size_field(endpoint, pagination)
  if field is None:
    return None, str(fallback) if fallback is not None else 'undefined'
  size = member_access('request', field.wire, optional=False)
  # An `int64` size is `number | bigint`; the clamp counts rows in `number`s.
  clamped = f'Math.max({f"Number({size})" if _is_int64(strip_null(field.type)) else size}, 2)'
  if pagination.size_maximum is not None:
    clamped = f'Math.min({clamped}, {pagination.size_maximum})'
  if field.required:
    return f'const size = {clamped}', 'size'
  return f'const size = {size} == null ? undefined : {clamped}', 'size' if fallback is None else f'size ?? {fallback}'


def _seek_cap_known(endpoint: EndpointPlan, pagination: PaginationPlan, seek: dict) -> bool | str:
  """Whether `_seek_size`'s cap always resolves (`True`), never does (`False`), or only
  while the caller sets the optional size it names."""
  if (seek.get('cap') if seek.get('cap') is not None else pagination.size_default) is not None:
    return True
  field = _seek_size_field(endpoint, pagination)
  if field is None:
    return False
  return True if field.required else field.wire


def _seek_size_rule(pagination: PaginationPlan) -> str:
  """The docstring sentence stating `_seek_size`'s clamp. A maximum below 2 leaves no room
  for the floor, so the sentence then claims none."""
  maximum = pagination.size_maximum
  if maximum is not None and maximum < 2:
    return f'The walk requests pages of {maximum} row{"" if maximum == 1 else "s"}.'
  bounds = f'at least 2 rows and at most {maximum}' if maximum is not None else 'at least 2 rows'
  return f'The walk requests pages of {bounds}: a page must hold one new row beside the one it re-reads.'


def _seek_method(
  module: Module, endpoint: EndpointPlan, pagination: PaginationPlan, *, method: str,
  class_name: str, request_type: str, request_optional: bool,
) -> tuple[Method | None, str | None]:
  """A `seek` walker's signature (ADR 0013): the whole request, both bounds kept (the
  moving one seeds the walk, the far one caps it), plus the span keyword when declared."""
  seek = pagination.seek or {}
  if pagination.walker != 'paginated' or pagination.row_type is None or pagination.state_type is None:
    return None, f'truewire: a `seek` walk whose row type cannot be named has no TypeScript walker; call `{method}` per page.'
  paged_type = f'{class_name}{PAGED_SUFFIX}Request'
  span = seek.get('span')
  w = module.writer
  if span:
    w.jsdoc(
      f'`{method}{PAGED_SUFFIX}`\'s request: `{request_type}` plus `{span["parameter"]}`, the widest '
      f'range one request covers, in `{span["unit"]}` (`{span["default"]}` by default).'
    )
    w.line(f'export type {paged_type} = {request_type} & {{ {property_key(span["parameter"])}?: number }}')
  else:
    w.jsdoc(f'`{method}{PAGED_SUFFIX}`\'s request: `{request_type}`, whose `{pagination.driver}` the walk starts from and moves.')
    w.line(f'export type {paged_type} = {request_type}')
  module.declare(paged_type)
  w.blank()
  doc, tags = method_docs(endpoint)
  descending = bool(seek.get('descending'))
  move = seek_move_prose(
    seek['cursor']['field'], descending=descending, ordered=_seek_keys(None, pagination.state_type)[0] != "'string'",
    cap=_seek_cap_known(endpoint, pagination, seek), span=bool(span),
  )
  far = f', never past the caller\'s own `{seek["far"]}`' if seek.get('far') else ''
  exclusive = plan_exclusive_sentences(seek.get('exclusive'), moving=pagination.driver)
  doc = [
    *doc,
    (
      f'Paged variant of `{method}`: walks {"backwards" if descending else "forwards"} by moving '
      f'`{pagination.driver}` {move}{far}; '
      'awaitable (flattens every page) or async-iterable (one page at a time).'
      + (f' {_seek_size_rule(pagination)}' if _seek_size_field(endpoint, pagination) is not None else '')
    ),
    *([' '.join(exclusive)] if exclusive else []),
  ]
  params = [
    Param('request', ('local', paged_type), optional=request_optional and not span),
    Param('options', ('core', options_type(endpoint)), optional=True),
  ]
  return Method(
    f'{method}{PAGED_SUFFIX}', params,
    ('seek', (('plan', pagination.row_type), ('plan', pagination.state_type))),
    doc=doc, tags=tags, is_async=False,
  ), None


def _emit_seek(
  module: Module, endpoint: EndpointPlan, pagination: PaginationPlan, paged: Method, *, main: Method,
):
  """One call to `@truewire/core`'s `seek`: the walk itself lives in the runtime, so what is
  rendered is the declaration -- where the cursor sits on a row, how its keys compare, the
  cap, the bounds -- and the fetch of one page through the plain method."""
  assert pagination.row_type is not None and pagination.state_type is not None
  w = module.writer
  seek = pagination.seek or {}
  emit_signatures(module, paged)
  params, returns = _paged_head(module, paged)
  row = module.type_expr(pagination.row_type)
  key = module.type_expr(pagination.state_type)
  moving, far, span = seek['moving'], seek.get('far'), seek.get('span')
  cursor = seek['cursor']
  segments = _SEGMENT.findall(cursor['field'])[1:]
  path = ', '.join(index if index else string(name) for index, name in segments)
  keys, is_time = _seek_keys(module, pagination.state_type, pagination.cursor_type)
  moving_field = next((f for f in endpoint.request.fields if f.wire == moving), None)
  moving_required = moving_field is not None and moving_field.required
  rows_path = pagination.rows or ''
  optional = endpoint.response.optional
  rows_expr = read_path('response', rows_path, optional=optional)
  if rows_path or optional:
    rows_expr = f'{rows_expr} ?? []'
  module.core('seek')
  module.core('rowField')
  exclusive = seek.get('exclusive')
  until = exclusive.get('far') if exclusive else None
  until_keys = "'string'"
  if until:
    # Resolved before the locals are bound, so the converter it imports is taken too.
    far_field = next((f for f in endpoint.request.fields if f.wire == until['parameter']), None)
    if far_field is not None:
      until_keys = _seek_keys(module, strip_null(far_field.type))[0]
  with w.block(f'{paged.name}({params}): {returns} {{'):
    source = 'request'
    size, cap = _seek_size(endpoint, pagination, seek)
    if size is not None:
      w.line(size)
    if span:
      w.line(f'const {{ {property_key(span["parameter"])}: span = {span["default"]}, ...base }} = request')
      source = 'base'
    firsts: dict[str, str] = {}
    if exclusive:
      # The exclusive parameters, bound once: every request but the first is sent without
      # them. A local never shadows what the module imports (`seek`, a converter).
      taken = {
        'request', 'options', 'size', 'rest', 'span', 'base', 'pos', 'edge', 'response', 'row',
        *module.imports.bound(),
      }
      for name in exclusive['parameters']:
        firsts[name] = binding(camel_case(name) if not is_binding(name) else name, taken)
        taken.add(firsts[name])
      entries = ', '.join(
        property_key(name) if local == name else f'{property_key(name)}: {local}' for name, local in firsts.items()
      )
      w.line(f'const {{ {entries}, ...rest }} = request')
      moving_value = member_access('request', moving, optional=False)
      # Beside a caller's moving bound only the non-far parameters are refused: the far one
      # is then never sent, and kept on the rows all the same.
      refused = {name: local for name, local in firsts.items() if not until or name != until['parameter']}
      if refused:
        given = ' || '.join(f'{local} !== undefined' for local in refused.values())
        given = f'({given})' if len(refused) > 1 else given
        listed = '/'.join(f'`{name}`' for name in refused)
        with w.block(f'if ({moving_value} !== undefined && {given}) {{'):
          w.line(
            f'throw new TypeError({string(f"`{paged.name}` walks by `{moving}`, which the venue refuses alongside {listed}: pass one or the other")})'
          )
      first = exclusive.get('first')
      if first and not moving_required:
        with w.block(f'if ({moving_value} === undefined && {firsts[first]} === undefined) {{'):
          w.line(
            f'throw new TypeError({string(f"`{paged.name}` needs `{moving}` or `{first}` to start from: without either the venue answers from the wrong end of the range")})'
          )
    with w.block(f'return seek<{row}, {key}>({member_access("request", moving, optional=False)}, {{', '})'):
      w.line(f'method: {string(paged.name)},')
      w.line(f'field: {string(last_row_field(cursor["field"]))},')
      w.line(f'read: row => rowField(row, [{path}]),')
      w.line(f'keys: {keys},')
      w.line(f'unique: {"true" if cursor.get("unique") else "false"},')
      w.line(f'descending: {"true" if seek.get("descending") else "false"},')
      w.line(f'cap: {cap},')
      if far is not None:
        w.line(f'far: {member_access("request", far, optional=False)},')
      if span:
        w.line('span,')
        if is_time:
          w.line(f'spanUnitMs: {_SPAN_UNIT_MS[span["unit"]]},')
      if until:
        until_segments = _SEGMENT.findall(until['field'])[1:]
        until_path = ', '.join(index if index else string(name) for index, name in until_segments)
        w.line(
          f'until: {{ read: row => rowField(row, [{until_path}]), keys: {until_keys}, '
          f'value: {firsts[until["parameter"]]} }},'
        )
      entries = [f'{property_key(moving)}: {"pos!" if moving_required or span else "pos"}']
      if size is not None:
        entries.insert(0, f'{property_key(pagination.size or "")}: size')
      if span and far is not None:
        entries.append(f'{property_key(far)}: edge!')
      with w.block(f'fetch: async ({"pos, edge" if span else "pos"}) => {{', '},'):
        if firsts:
          # Only while the walk has no position of its own: the venue refuses them beside it.
          # The first request still sends the clamped size, as every later one does.
          head = f'{{ ...{source}, {property_key(pagination.size or "")}: size }}' if size is not None else source
          w.line(
            f'const response = await this.{main.name}(pos === undefined ? {head} : {{ ...rest, {", ".join(entries)} }}, options)'
          )
        else:
          w.line(f'const response = await this.{main.name}({{ ...{source}, {", ".join(entries)} }}, options)')
        w.line(f'return {rows_expr}')


def _payload_is_list(module: Module, endpoint: EndpointPlan) -> bool:
  """Whether the value the method returns is itself the row collection."""
  payload = endpoint.response.payload
  seen: set[str] = set()
  while payload is not None and payload not in seen:
    seen.add(payload)
    t = endpoint.types.get(payload)
    if t is None:
      for scope in module.plan.schemas.values():
        if payload in scope:
          t = scope[payload]
          break
    if t is None:
      return False
    if t['type'] == 'list':
      return True
    if t['type'] == 'union':
      rest = [v['type'] for v in t['variants'] if v['type']['type'] != 'scalar' or v['type']['base'] != 'null']
      if len(rest) == 1:
        t = rest[0]
        if t['type'] == 'list':
          return True
    if t['type'] == 'ref':
      payload = t['id']
      continue
    return False
  return False


def _emit_total_check(w, paged_name: str, total_expr: str):
  w.line(f'const totalRaw = {total_expr}')
  w.line('const total = totalRaw === undefined || totalRaw === null ? null : Number(totalRaw)')
  with w.block('if (total === null || Number.isNaN(total) || (totalSeen !== null && total !== totalSeen)) {'):
    w.line(
      f'throw new LogicError(`\\`{paged_name}\\` needs a \\`total\\` on every page. The API omitted it '
      'here, or reported a value (${totalRaw}) that disagrees with an earlier page of this same '
      'walk (${totalSeen}); retry the whole walk from the start.`)'
    )
  w.line('totalSeen = total')


def _total_done(done: dict, state: str, start: int, size: str | None, size_unknown: str | None) -> str:
  pages = f'({state} - {start} + 1)'
  if done.get('counts') == 'pages':
    return f'{pages} >= total'
  if size is None:
    return 'rows.length === 0'
  if size_unknown:
    return f'({size_unknown} !== undefined && {pages} * {size} >= total) || rows.length === 0'
  return f'{pages} * {size} >= total'


__all__ = [
  'META_FILE', 'POLICY_FILE', 'RAW_DOC', 'RAW_OPTIONS', 'EndpointModule', 'Method', 'Param', 'Token',
  'channel_expr', 'core_interface', 'emit_signatures', 'endpoint_file', 'is_dual', 'meta_type_name', 'options_type',
  'raw_returns', 'render_endpoint', 'render_implementation_return', 'render_params', 'render_return', 'render_stream',
  'render_type',
]
