"""One endpoint module: its types, and a struct holding the core behind the contract
trait with the method, its `_raw` twin, and the `<method>_paged` walker when pagination
is declared.

The struct holds its core as an `Arc<dyn HttpEndpoint<Meta>>` (`CommandEndpoint<Meta>` for
an `rpc` endpoint reached over a WebSocket, a combined trait from `contract.rs` for one
declaring both transports) and calls exactly one verb on it per call. The typed method dumps the request, hands the core an `HttpCall`, and `decode`s the
reply; the `_raw` twin returns the `serde_json::Value` the core returned, which is the Rust
form of `validate: false` (`docs/rust.md`: a second method, since no overload can return a
different type). The request struct is the wire object under `snake_case` names, and the
options are the runtime's `CallOptions`.
"""
import re
from contextlib import contextmanager
from dataclasses import dataclass, field

from truewire.codegen.policy import POLICY_MODULE
from truewire.codegen.seek import plan_exclusive_sentences
from truewire.plan.model import EndpointPlan, PackagePlan, PaginationPlan
from truewire.plan.types import Type, is_optional, seek_cursor_format, strip_null
from truewire.spec.endpoint import seek_move_prose

from .contract import Contracts
from .meta import META_FILE, MetaShape
from .names import json_expr, pascal_case, snake_ident, string, unique
from .printer import BANNER, INDENT, MAX_WIDTH, Writer, long_tuple
from .types import CORE, EXTRA_FIELD, Field, Module, scope_module, visible_scopes

PAGED_SUFFIX = '_paged'
RAW_SUFFIX = '_raw'


def raw_name(method: str) -> str:
  """The `_raw` twin of `method`. A keyword escaped with a trailing `_` (`type_`) drops the
  escape first, since `type_raw` is already not a keyword and `type__raw` is not snake_case."""
  from truewire.codegen.rust.names import RESERVED
  if method.endswith('_') and method[:-1] in RESERVED:
    method = method[:-1]
  return f'{method}{RAW_SUFFIX}'

Tokens = list[tuple[str, str]]
"""A type in a method signature, kept symbolic so a router can render it qualified:
`('local', 'Request')` for a name the endpoint module defines, `('shared',
'crate::types::Label')` for one a shared scope defines, `('core', 'CallOptions')` for a
runtime name, `('std', 'HashMap')`, `('json', 'serde_json::Value')`, and `('text', '<')`
for everything else."""

_CORE_NAMES = frozenset((
  'DecimalString', 'IntegerString', 'BooleanString', 'TimestampSeconds', 'TimestampMillis',
  'TimestampMicros', 'TimestampNanos', 'TimestampIso', 'DateIso', 'TimestampSecondsString',
  'TimestampMillisString', 'TimestampMicrosString', 'TimestampNanosString', 'TimestampSecondsFloat',
  'TimestampMillisFloat', 'TimestampMicrosFloat', 'TimestampNanosFloat',
))
_TOKEN = re.compile(r'serde_json::[A-Za-z]+|[A-Za-z_][A-Za-z0-9_]*|[^A-Za-z_]+')
_SEGMENT = re.compile(r'\[(-?\d+)\]|([^.\[\]]+)')
_PLACEHOLDER = re.compile(r'\{([^{}]+)\}')
_TUPLE = re.compile(r'\(\s*[A-Za-z_]')

_STATE_TYPES = {'integer': 'i64', 'number': 'f64', 'string': 'String', 'boolean': 'bool'}
"""Cursor types the runtime's `cursor_or_done` accepts (`CursorLike`)."""


@dataclass(frozen=True)
class Method:
  """One method of the endpoint struct, as a router needs it to delegate."""
  name: str
  params: list[tuple[str, Tokens]]
  """(parameter name, its type) after `&self`."""
  returns: Tokens
  is_async: bool
  doc: list[str | None] = field(default_factory=list)
  deprecated: bool = False
  complex_type: bool = False
  """A signature holding an inline tuple row (`PaginatedResponse<(A, B, ...), SeekState<K, (A, B, ...)>>`):
  clippy's `type_complexity` fires on it, and the plan offers no name to factor it into."""


@dataclass(frozen=True)
class EndpointModule:
  file: str
  struct_name: str
  bound: str
  """The contract the struct's core satisfies: `HttpEndpoint<DefaultMeta>`, `HttpEndpoint`,
  or several joined by ` + ` (`CommandEndpoint + HttpEndpoint`) for a dual-transport one."""
  meta_type: str | None
  methods: list[Method]
  source: str
  kind: str = 'rpc'
  """`rpc` or `stream`: whether `dispatch.rs` reaches it through `call` or `subscribe`."""
  main: str = ''
  """The typed method's name."""
  raw: str | None = None
  """The `_raw` twin's name, or `None` when the endpoint returns nothing."""
  takes_request: bool = False
  """Whether the methods take a request (or parameters) before `options`."""
  typed_reply: bool = False
  """A stream whose `reply` is typed (ADR 0014): its typed method's `Stream` carries that type."""
  aliases: dict[str, str] = field(default_factory=dict)
  """Method names the parent router exposes under another name, because a sibling endpoint's
  own method already has it (`orders_by_instrument`'s `_raw` twin beside an
  `orders_by_instrument_raw` endpoint becomes `orders_by_instrument_raw2`, as in Go)."""
  unit_response: bool = False
  """An `rpc` whose declared response renders as `()` (a `null` body): the typed method has
  nothing to hand back, so `dispatch.rs` answers `null` rather than binding and dumping it."""


def endpoint_file(path: list[str]) -> str:
  """`repos.list_commits` -> `repos/list_commits.rs`: the spec directory, as a file."""
  return '/'.join([*(snake_ident(p, fallback='router') for p in path[:-1]), f'{snake_ident(path[-1], fallback="endpoint")}.rs'])


def tokenize(module: Module, expr: str) -> Tokens:
  """A rendered type expression as `Tokens`, from what the module knows each name is."""
  out: Tokens = []
  for word in _TOKEN.findall(expr):
    if word.startswith('serde_json::'):
      out.append(('json', word))
    elif word in module.names:
      out.append(('local', word))
    elif (scope := module.shared_scope(word)) is not None:
      out.append(('shared', f'{scope_module(scope)}::{word}'))
    elif word in _CORE_NAMES:
      out.append(('core', word))
    elif word == 'HashMap':
      out.append(('std', word))
    elif out and out[-1][0] == 'text':
      out[-1] = ('text', out[-1][1] + word)
    else:
      out.append(('text', word))
  return out


def render_tokens(module: Module, tokens: Tokens, *, qualifier: str | None = None) -> str:
  """`tokens` as the module's own text, importing what they need; a router passes the
  endpoint module's name as `qualifier` (`list::Request`)."""
  parts: list[str] = []
  for kind, text in tokens:
    if kind == 'core':
      text = module.core(text)
    elif kind == 'std':
      module.imports.add('std::collections', text)
    elif kind == 'json':
      module.serde_json()
    elif kind == 'shared':
      path, _, text = text.rpartition('::')
      module.imports.add(path, text)
    elif kind == 'local' and qualifier is not None:
      text = f'{qualifier}::{text}'
    parts.append(text)
  return ''.join(parts)


def core_tokens(*names: str) -> Tokens:
  return [('core', name) for name in names]


POLICY_FILE = f'{POLICY_MODULE}.rs'
"""`RefusedByPolicy` and `refuse`, written when `[policy].refuse` names an endpoint (W15)."""


def method_docs(endpoint: EndpointPlan) -> list[str | None]:
  docs = [endpoint.docs.description]
  if endpoint.refused:
    docs.append('Refused by `[policy].refuse`: fails with `RefusedByPolicy` before any request is made.')
  if endpoint.docs.url:
    docs.append(f'See <{endpoint.docs.url}>.')
  return docs


def refusal(module: Module, endpoint: EndpointPlan) -> list[str]:
  """`refuse("<function>")?;`, the first statement of a body that reaches the core, when
  `[policy].refuse` names the endpoint: the method returns the refusal before any request.
  Every other method of the struct reaches the core through that body. A `?` on a call
  that always fails, rather than a `return Err`, keeps the rest of the body reachable, so
  it compiles warning-free."""
  if not endpoint.refused:
    return []
  module.imports.add(f'crate::{POLICY_MODULE}', 'refuse')
  return [f'refuse({string(endpoint.function)})?;']


def renders_unit(module: Module, name: str | None) -> bool:
  """Whether the type `name` (following aliases of aliases) renders as `()`."""
  seen: set[str] = set()
  t = module.lookup(name) if name else None
  while t is not None and t['type'] == 'ref' and t['id'] not in seen:
    seen.add(t['id'])
    t = module.lookup(t['id'])
  return t is not None and t['type'] == 'scalar' and t.get('base') == 'null'


def emit_method(
  module: Module, method: Method, *, qualifier: str | None, body: list[str] | None = None, delegate: str | None = None,
):
  """`method`'s docs, attributes and signature; `body` lines inside it, or a delegating
  call when `body` is `None` (a router)."""
  w = module.writer
  w.doc(*method.doc)
  params = ['&self', *(f'{name}: {render_tokens(module, tokens, qualifier=qualifier)}' for name, tokens in method.params)]
  head = f'pub {"async " if method.is_async else ""}fn {method.name}'
  returns = render_tokens(module, method.returns, qualifier=qualifier)
  if method.deprecated:
    w.line('#[deprecated]')
    # A deprecated method's body reaches a deprecated sibling: a router delegates, a typed
    # method calls its `_raw` twin, a walker calls the typed method.
    w.line('#[allow(deprecated)]')
  if method.complex_type or _TUPLE.search(' '.join(params) + returns):
    # A wire tuple (a candle row) inside a walker's `PaginatedResponse<Row, SeekState<_, Row>>`
    # is a positional type the plan gives no name to; naming it would invent API.
    w.line('#[allow(clippy::type_complexity)]')
  w.signature(head, params, f' -> {returns} {{')
  with w.indented():
    if body is None:
      args = ', '.join(name for name, _ in method.params)
      elements = [f'.{qualifier}', f'.{delegate or method.name}({args})']
      if method.is_async:
        elements.append('.await')
      w.chain('', 'self', elements)
    else:
      for line in body:
        w.line(line)
  w.line('}')


# -- the module -------------------------------------------------------------------------


@contextmanager
def if_block(w: Writer, condition: str, *, offset: int = 0):
  """`if condition { ... }` laid out as `rustfmt` does: on one line when it fits; the brace
  on its own line when only the brace overflows; otherwise one `||` operand per line (a
  long method-chain operand broken at its `.`, or, when even that overflows, the body of
  its closing closure in a block), the brace again on its own line.

  `offset` is how much deeper than `w.column` the lines finally sit, for a body rendered
  in its own writer and re-indented when it is placed."""
  column = w.column + offset
  one = f'if {condition} {{'
  if column + len(one) <= MAX_WIDTH:
    w.line(one)
  elif column + len(f'if {condition}') <= MAX_WIDTH:
    w.line(f'if {condition}')
    w.line('{')
  else:
    first, *rest = _split_or(condition)
    w.line(f'if {first}')
    with w.indented():
      for operand in rest:
        dot = operand.find('.')
        paren = operand.find('(')
        chained = dot > 0 and (paren < 0 or dot < paren) and operand[:dot].isidentifier()
        inner = w.column + offset
        head, bar, body = operand.partition('| ')
        # Broken at its `.`, a method call still too wide for the line keeps the chain whole
        # and puts the body of its closing one-parameter closure in a block instead.
        closure = (
          bar and operand.endswith(')') and inner + len(f'|| {operand}') > MAX_WIDTH
          and (not chained or inner + len(INDENT) + len(operand[dot:]) > MAX_WIDTH)
        )
        if closure:
          w.line(f'|| {head}| {{')
          with w.indented():
            w.line(body[:-1])
          w.line('})')
        elif inner + len(f'|| {operand}') <= MAX_WIDTH:
          # Conditions use the available width, not the narrower statement chain width.
          w.line(f'|| {operand}')
        elif chained:
          w.chain('|| ', operand[:dot], [operand[dot:]])
        else:
          w.line(f'|| {operand}')
    w.line('{')
  with w.indented():
    yield
  w.line('}')


def _split_or(condition: str) -> list[str]:
  """`condition`'s top-level `||` operands."""
  parts: list[str] = []
  depth = 0
  start = 0
  i = 0
  while i < len(condition):
    ch = condition[i]
    if ch in '([{':
      depth += 1
    elif ch in ')]}':
      depth -= 1
    elif depth == 0 and condition.startswith(' || ', i):
      parts.append(condition[start:i])
      i += 4
      start = i
      continue
    i += 1
  parts.append(condition[start:])
  return parts

@dataclass
class _Skipped(Exception):
  """Raised inside a walker renderer for a declaration this backend has no walker for."""
  reason: str


def render_stream_endpoint(
  plan: PackagePlan, endpoint: EndpointPlan, *, struct_name: str, meta: MetaShape | None,
  contracts: Contracts | None = None,
) -> tuple[EndpointModule, list[str]]:
  """Render one `stream` endpoint's module: `subscribe`, typed and raw.

  The shape mirrors the HTTP path -- a typed method that decodes each pushed frame and a
  `_raw` twin that hands them over as they came -- because a subscription is the same
  contract as a call, spread over time. `Stream::map` is what turns one into the other.
  """
  file = endpoint_file(endpoint.path)
  request = endpoint.request
  local = dict(endpoint.types)
  # A stream whose parameters only fill its channel template (`tickers.{symbol}`) has no
  # request type in the plan: the method takes a `Parameters` struct of those fields, fills
  # the channel itself and hands the core `parameters: None`, as the other backends do.
  template = request.type is None and bool(request.fields)
  parameters_type = request.type
  if template:
    parameters_type = next(name for name in ('Parameters', 'ChannelParameters', 'StreamParameters') if name not in local)
    local[parameters_type] = {  # type: ignore[assignment]
      'type': 'record', 'id': parameters_type,
      'docstring': f'What `{endpoint.wire.channel}` is filled from.',
      'fields': {
        f.wire: {'type': f.type, 'required': f.required, 'docstring': f.description} for f in request.fields
      },
    }
  module = Module(plan, file, local=local, visible=visible_scopes(plan, endpoint.path))
  module.define_all(local)
  w = module.writer
  notes: list[str] = []

  channel = endpoint.wire.channel or ''

  meta_type = meta.name if meta is not None else None
  bound = f'StreamEndpoint<{meta_type}>' if meta_type is not None else 'StreamEndpoint'
  module.core('StreamEndpoint')
  module.core('SubscribeCall')
  module.core('CallOptions')
  module.core('Result')
  module.core('Stream')
  module.core('decode')
  module.serde_json()
  module.imports.add('std::sync', 'Arc')
  if meta_type is not None:
    module.imports.add(f'crate::{META_FILE[:-3]}', meta_type)
  if parameters_type is not None:
    module.core('dump')
  if template:
    module.imports.add(f'{CORE}::http', 'query_value')

  method = snake_ident(endpoint.path[-1], fallback='subscribe')
  message = endpoint.response.payload
  reply = endpoint.stream.reply if endpoint.stream is not None else None
  docs = method_docs(endpoint)

  params: list[tuple[str, Tokens]] = []
  if parameters_type is not None:
    params.append(('parameters', tokenize(module, parameters_type)))
  params.append(('options', core_tokens('CallOptions')))
  message_tokens: Tokens = tokenize(module, message) if message else [('json', 'serde_json::Value')]
  reply_tokens: Tokens = [('text', ', '), *tokenize(module, reply)] if reply else []
  returns: Tokens = [
    ('core', 'Result'), ('text', '<'), ('core', 'Stream'), ('text', '<'), *message_tokens, *reply_tokens, ('text', '>>'),
  ]
  raw_returns: Tokens = [
    ('core', 'Result'), ('text', '<'), ('core', 'Stream'), ('text', '<'),
    ('json', 'serde_json::Value'), ('text', '>>'),
  ]
  main = Method(method, params, returns, is_async=True, doc=docs, deprecated=endpoint.deprecated)
  raw = Method(
    raw_name(method), params, raw_returns, is_async=True,
    doc=[f'`{method}` without validation: the frames as they came.'], deprecated=endpoint.deprecated,
  )
  typed = message is not None or reply is not None
  methods = [main, raw] if typed else [main]

  w.blank()
  w.doc(endpoint.docs.description)
  w.line('#[derive(Clone)]')
  with w.block(f'pub struct {struct_name} {{'):
    w.line(f'core: Arc<dyn {bound}>,')
  w.blank()
  with w.block(f'impl {struct_name} {{'):
    w.line(f'pub fn new(core: Arc<dyn {bound}>) -> Self {{')
    with w.indented():
      w.line('Self { core }')
    w.line('}')
    args = ', '.join(name for name, _ in params)
    if typed:
      w.blank()
      mapped = 'stream' + ('.map(decode)' if message is not None else '') + ('.map_reply(decode)' if reply else '')
      # As `rustfmt` lays the chain out: past its chain width, one element per line.
      head = Writer(2)
      head.chain('let stream = ', 'self', [f'.{raw.name}({args})', '.await?'], ';')
      emit_method(module, main, qualifier=None, body=[
        *(line[8:] for line in head.render().rstrip('\n').split('\n')),
        mapped if reply else f'Ok({mapped})',
      ])
      w.blank()
      emit_method(module, raw, qualifier=None, body=_subscribe_body(module, endpoint, meta, channel))
    else:
      w.blank()
      emit_method(module, main, qualifier=None, body=_subscribe_body(module, endpoint, meta, channel))

  return EndpointModule(
    file=file, struct_name=struct_name, bound=bound, meta_type=meta_type, methods=methods,
    source=module.render(BANNER), kind='stream', main=main.name,
    raw=raw.name if typed else None, takes_request=parameters_type is not None, typed_reply=reply is not None,
  ), notes


def _subscribe_body(module: Module, endpoint: EndpointPlan, meta: MetaShape | None, channel: str) -> list[str]:
  """The `SubscribeCall` one subscription is made from."""
  w = Writer(1)
  meta_expr = '&()'
  if meta is not None:
    w.struct_literal('let meta = ', meta.name, meta.literal_fields(endpoint.meta), ';')
    meta_expr = '&meta'
  template = endpoint.request.type is None and bool(endpoint.request.fields)
  channel_expr = string(channel)
  if template:
    # Each placeholder as its query text: a string verbatim, a number or boolean as JSON.
    names = _PLACEHOLDER.findall(channel)
    pattern = _PLACEHOLDER.sub('\x00', channel).replace('{', '{{').replace('}', '}}').replace('\x00', '{}')
    w.line('let values = dump(&parameters)?;')
    if pattern == '{}':
      # The channel is the one value (binance's listenKey): `format!("{}", x)` is a clippy
      # `useless_format`, and `query_value` already returns the `String`.
      w.chain('let channel = ', f'query_value(&values[{string(names[0])}])', ['.unwrap_or_default()'], ';')
    else:
      w.call('let channel = format!', [string(pattern), *(f'query_value(&values[{string(name)}]).unwrap_or_default()' for name in names)], ';')
    channel_expr = '&channel'
  parameters = 'Some(dump(&parameters)?)' if endpoint.request.type is not None else 'None'
  w.struct_literal('let call = ', 'SubscribeCall', [
    f'channel: {channel_expr}',
    f'parameters: {parameters}',
    f'meta: {meta_expr}',
    'options',
  ], ';')
  w.line('self.core.subscribe(call).await')
  return [*refusal(module, endpoint), *(line[4:] if line else line for line in w.render().rstrip('\n').split('\n'))]


def render_endpoint(
  plan: PackagePlan, endpoint: EndpointPlan, *, struct_name: str, meta: MetaShape | None,
  contracts: Contracts | None = None,
) -> tuple[EndpointModule, list[str]]:
  """Render one `rpc` endpoint's module; the second value lists what was skipped.

  Over HTTP the core receives an `HttpCall`, over a WebSocket a `CommandCall` whose `path`
  is the wire method name. An endpoint declaring both transports holds a core satisfying
  both and picks one per call from `CallOptions::transport`, defaulting to the transport
  its spec lists first -- the Rust form of the Python backend's `transport=` keyword.
  """
  contracts = contracts if contracts is not None else Contracts()
  file = endpoint_file(endpoint.path)
  request = endpoint.request
  fixed = [f for f in request.fields if f.fixed is not None] if request.shape == 'fields' else []
  local = dict(endpoint.types)
  if fixed and request.type in local:
    # A fixed field is wire plumbing the method fills in, not something the caller sets.
    record = local[request.type]
    local[request.type] = {**record, 'fields': {k: v for k, v in record['fields'].items() if k not in {f.wire for f in fixed}}}  # type: ignore[typeddict-item]
  module = Module(plan, file, local=local, visible=visible_scopes(plan, endpoint.path))
  module.define_all(local)
  w = module.writer
  notes: list[str] = []

  meta_type = meta.name if meta is not None else None
  generic = f'<{meta_type}>' if meta_type is not None else ''
  transports = [t for t in dict.fromkeys(endpoint.transports) if t in ('http', 'ws')] or ['http']
  traits = {'http': f'HttpEndpoint{generic}', 'ws': f'CommandEndpoint{generic}'}
  bound = ' + '.join(sorted(traits[t] for t in transports))
  holder = contracts.holder(module, [traits[t] for t in transports])
  if len(transports) > 1:
    contracts.bound(module, [traits[t] for t in transports])
    module.core('Transport')
  module.core('CallOptions')
  if 'http' in transports:
    module.core('HttpCall')
  if 'ws' in transports:
    module.core('CommandCall')
  module.core('Result')
  module.imports.add('std::sync', 'Arc')
  if meta_type is not None:
    module.imports.add(f'crate::{META_FILE[:-3]}', meta_type)

  method = snake_ident(endpoint.path[-1], fallback='call')
  request_type = request.type
  payload = endpoint.response.payload
  docs = method_docs(endpoint)

  params: list[tuple[str, Tokens]] = []
  if request_type is not None:
    params.append(('request', tokenize(module, request_type)))
  params.append(('options', core_tokens('CallOptions')))
  returns: Tokens = [('core', 'Result'), ('text', '<'), *tokenize(module, payload), ('text', '>')] if payload else [('core', 'Result'), ('text', '<()>')]
  main = Method(method, params, returns, is_async=True, doc=docs, deprecated=endpoint.deprecated)
  methods: list[Method] = []

  paged: Method | None = None
  paged_body: list[str] = []
  pagination = endpoint.pagination
  if pagination is not None and pagination.walker != 'none':
    state = module.checkpoint()
    try:
      paged, paged_body = _paged(module, endpoint, pagination, method=method, struct_name=struct_name, request_type=request_type)
    except _Skipped as skipped:
      # Nothing of a walker given up on part-way may stay behind: its request type and
      # imports would be dead code, which `clippy -D warnings` refuses.
      module.restore(state)
      notes.append(f'{endpoint.function}: {skipped.reason}')
  if paged is not None:
    methods.append(paged)
  methods.append(main)
  raw: Method | None = None
  if payload is not None:
    raw = Method(
      raw_name(method), params, [('core', 'Result'), ('text', '<'), ('json', 'serde_json::Value'), ('text', '>')],
      is_async=True, doc=[f'`{method}` without validation: the wire body as it came.'], deprecated=endpoint.deprecated,
    )
    methods.append(raw)
    module.core('decode')
    module.serde_json()
  if request_type is not None:
    module.core('dump')

  w.blank()
  w.doc(endpoint.docs.description)
  w.line('#[derive(Clone)]')
  with w.block(f'pub struct {struct_name} {{'):
    w.line(f'core: Arc<dyn {holder}>,')
  w.blank()
  with w.block(f'impl {struct_name} {{'):
    w.line(f'pub fn new(core: Arc<dyn {holder}>) -> Self {{')
    with w.indented():
      w.line('Self { core }')
    w.line('}')
    if paged is not None:
      w.blank()
      emit_method(module, paged, qualifier=None, body=paged_body)
    w.blank()
    if raw is not None:
      args = ', '.join(name for name, _ in params)
      fetch = Writer()
      fetch.chain('let raw = ', 'self', [f'.{raw.name}({args})', '.await?'], ';')
      emit_method(module, main, qualifier=None, body=[*fetch.render().rstrip('\n').split('\n'), 'decode(raw)'])
      w.blank()
      body = _call_body(module, endpoint, meta, fixed=fixed, request_type=request_type, transports=transports, returns=True)
      emit_method(module, raw, qualifier=None, body=body)
    else:
      body = _call_body(module, endpoint, meta, fixed=fixed, request_type=request_type, transports=transports, returns=False)
      emit_method(module, main, qualifier=None, body=body)

  source = module.render(BANNER)
  return EndpointModule(
    file=file, struct_name=struct_name, bound=bound, meta_type=meta_type, methods=methods,
    source=source, kind='rpc', main=main.name, raw=raw.name if raw is not None else None,
    takes_request=request_type is not None, unit_response=renders_unit(module, payload),
  ), notes


def _call_body(
  module: Module, endpoint: EndpointPlan, meta: MetaShape | None, *, fixed, request_type: str | None,
  transports: list[str], returns: bool,
) -> list[str]:
  """The statements of the method that talks to the core: the dumped request, the `meta`
  literal, the call for each transport, and the call itself -- its value returned when
  `returns`, else discarded and `Ok(())`."""
  w = Writer(1)
  if request_type is None:
    request_expr = 'None'
  elif fixed:
    w.line('let mut request = dump(&request)?;')
    with w.block('if let Some(object) = request.as_object_mut() {'):
      for f in fixed:
        w.call('object.insert', [f'{string(f.wire)}.to_string()', json_expr(f.fixed)], ';')
    request_expr = 'Some(request)'
  else:
    request_expr = 'Some(dump(&request)?)'
  if meta is not None:
    w.struct_literal('let meta = ', meta.name, meta.literal_fields(endpoint.meta), ';')
    meta_expr = '&meta'
  else:
    meta_expr = '&()'
  method = endpoint.wire.method
  path = f'path: {string(endpoint.wire.path or "")}'
  calls = {
    'http': ('HttpCall', [f'method: {f"Some({string(method)})" if method else "None"}', path]),
    'ws': ('CommandCall', [path]),
  }
  trait = {'http': 'HttpEndpoint', 'ws': 'CommandEndpoint'}
  variant = {'http': 'Http', 'ws': 'Ws'}
  tail = '' if returns else '?;'

  def one(transport: str, invoke: str):
    name, head = calls[transport]
    w.struct_literal('let call = ', name, [*head, f'request: {request_expr}', f'meta: {meta_expr}', 'options'], ';')
    w.line(f'{invoke}.await{tail}')

  if len(transports) == 1:
    one(transports[0], 'self.core.request(call)')
  else:
    w.line(f'let transport = options.transport.unwrap_or(Transport::{variant[transports[0]]});')
    with w.block('match transport {'):
      for transport in ('http', 'ws'):
        with w.block(f'Transport::{variant[transport]} => {{'):
          one(transport, f'{trait[transport]}::request(&*self.core, call)')
  if not returns:
    w.line('Ok(())')
  return [*refusal(module, endpoint), *(line[4:] if line else line for line in w.render().rstrip('\n').split('\n'))]


# -- response paths ---------------------------------------------------------------------


@dataclass(frozen=True)
class Read:
  """An expression reading a response path, as a method chain (`root` then `.field`,
  `.map(..)` elements), whether it yields an `Option`, and the tree of the value it yields
  (null stripped)."""
  root: str
  elements: list[str]
  optional: bool
  type: Type | None


def read_path(module: Module, subject: str, subject_type: Type, path: str, *, borrow: bool) -> Read:
  """An expression reading a dotted/indexed response path (`data.rows`, `[-1].id`) off
  `subject`, a value of `subject_type`.

  Owned (`borrow=False`): every hop moves out of `subject`, so this must be its last use.
  Borrowed: every hop goes through `.as_ref()`/`.map` and the leaf is cloned or copied,
  so `subject` stays whole for the reads after it.
  """
  elements: list[str] = []
  state = 'place'
  """`place`: an owned value; `opt_place`: an owned `Option`; `opt_ref`: an `Option<&T>`."""
  t, optional = _unwrap(module, subject_type)
  if optional:
    state = 'opt_place'
  for index, key in _SEGMENT.findall(path):
    if state == 'opt_place' and borrow:
      elements.append('.as_ref()')
      state = 'opt_ref'
    if index:
      hop = '.last()' if int(index) == -1 else f'.get({int(index)})'
      if int(index) < -1:
        raise _Skipped(f'a response path indexing `[{index}]` has no Rust reading yet')
      if state == 'place':
        elements.append(hop)
      elif state == 'opt_place':
        elements.append(f'.and_then(|value| value{hop}.cloned())')
      else:
        elements.append(f'.and_then(|value| value{hop})')
      state = 'opt_ref' if state != 'opt_place' else 'opt_place'
      t = _item_type(module, t)
      continue
    record, fields = _record_fields(module, t)
    if record is None or key not in record['fields']:
      raise _Skipped(f'a response path through `{key}` has no field to read in Rust')
    plan_field = record['fields'][key]
    rendered = next(f for f in fields if f.wire == key)
    inner, nullable = _unwrap(module, plan_field['type'])
    optional = rendered.optional or nullable
    double = rendered.optional and nullable
    if state == 'place':
      elements.append(f'.{rendered.ident}')
      if double:
        elements.append('.flatten()')
      state = 'opt_place' if optional else 'place'
    elif state == 'opt_place':
      access = f'value.{rendered.ident}' + ('.flatten()' if double else '')
      elements.append(f'.and_then(|value| {access})' if optional else f'.map(|value| {access})')
    else:
      access = f'value.{rendered.ident}'
      if double:
        elements.extend((f'.and_then(|value| {access}.as_ref())', '.and_then(|value| value.as_ref())'))
      elif optional:
        elements.append(f'.and_then(|value| {access}.as_ref())')
      else:
        elements.append(f'.map(|value| &{access})')
    t = inner
  if borrow and t is not None:
    copy = module.copyable(t)
    if state == 'opt_ref':
      elements.append('.copied()' if copy else '.cloned()')
    elif not copy:
      elements.append('.clone()')
  return Read(subject, elements, state != 'place', t)


def _resolved(module: Module, t: Type | None) -> Type | None:
  seen: set[str] = set()
  while t is not None and t['type'] == 'ref' and t['id'] not in seen:
    seen.add(t['id'])
    t = module.lookup(t['id'])
  return t


def _record_fields(module: Module, t: Type | None) -> tuple[Type | None, list[Field]]:
  """The record `t` names and its fields as rendered (in this module, or in the shared
  scope that defines it), or `(None, [])`."""
  resolved = _resolved(module, t)
  if resolved is None or resolved['type'] != 'record':
    return None, []
  return resolved, module.fields.get(resolved['id']) or module.field_layout(resolved)


def _unwrap(module: Module, t: Type | None) -> tuple[Type | None, bool]:
  """`t` through every alias and `null` variant, and whether one was nullable."""
  optional = False
  seen: set[str] = set()
  while t is not None:
    if is_optional(t):
      optional = True
      t = strip_null(t)
    elif t['type'] == 'ref' and t['id'] not in seen:
      seen.add(t['id'])
      t = module.lookup(t['id'])
    else:
      break
  return t, optional


def _item_type(module: Module, t: Type | None) -> Type | None:
  resolved = _resolved(module, t)
  if resolved is None or resolved['type'] != 'list':
    raise _Skipped('a response path indexes into something that is not a list')
  return resolved['item']


# -- pagination -------------------------------------------------------------------------


def _paged(
  module: Module, endpoint: EndpointPlan, pagination: PaginationPlan, *, method: str,
  struct_name: str, request_type: str | None,
) -> tuple[Method, list[str]]:
  """The walker's signature and body, or `_Skipped` for a declaration this backend has
  no walker for."""
  strategy, done = pagination.strategy, pagination.done.get('kind')
  if request_type is None or endpoint.request.shape != 'fields' or request_type not in module.fields:
    raise _Skipped('a walk needs a flat request to advance; this one has none')
  if endpoint.response.payload is None:
    raise _Skipped('a walk needs rows to read, and this endpoint returns nothing')
  payload_type: Type = {'type': 'ref', 'id': endpoint.response.payload}
  if endpoint.response.optional:
    payload_type = {'type': 'union', 'variants': [{'type': payload_type}, {'type': {'type': 'scalar', 'base': 'null'}}]}
  if strategy == 'seek':
    return _paged_seek(
      module, endpoint, pagination, method=method, struct_name=struct_name, request_type=request_type,
      payload_type=payload_type,
    )
  row_tree = pagination.row_type
  if strategy == 'offset' and pagination.walker != 'none' and row_tree is None:
    # The plan names a row type only for the walks it calls resumable; an offset walk is
    # resumable in Rust too (its state is the offset), so the row is read off `rows`.
    row_tree = _item_type(module, read_path(module, 'response', payload_type, pagination.rows or '', borrow=False).type)
  if pagination.walker == 'none' or (pagination.walker != 'paginated' and strategy != 'offset'):
    raise _Skipped(f'an `{strategy}` walk with no resumable state has no Rust walker yet; call the method per page')
  union = _union_pages(module, endpoint, pagination)
  if union is not None:
    row_tree = union.row
  state_type = _state_type(pagination.state_type)
  if state_type is None or row_tree is None:
    raise _Skipped('a cursor of this type has no Rust walker yet; call the method per page')

  w = Writer(1)
  paged_name = f'{method}{PAGED_SUFFIX}'
  paged_type = f'{struct_name}{pascal_case(PAGED_SUFFIX)}Request'
  request_fields = module.fields[request_type]
  driver = next((f for f in request_fields if f.wire == pagination.driver), None)
  if driver is None:
    raise _Skipped(f'the driver `{pagination.driver}` is not a field of `{request_type}`')
  plan_fields = module.local[request_type]['fields']

  # The walker's request type.
  module.declare(paged_type)
  if pagination.driver_required:
    module.writer.doc(f'`{paged_name}`\'s request: `{request_type}`, whose `{pagination.driver}` seeds the walk.')
    module.writer.line(f'pub type {paged_type} = {request_type};')
    module.writer.blank()
  else:
    fields = [f for f in request_fields if f.wire != pagination.driver]
    module.writer.doc(f'`{paged_name}`\'s request: `{request_type}` without `{pagination.driver}`, which the walk advances.')
    derives = ['Debug', 'Clone', 'PartialEq']
    if module.defaultable({'type': 'record', 'id': paged_type, 'fields': {k: v for k, v in plan_fields.items() if k != pagination.driver}}):
      derives.append('Default')
    module.writer.line(f'#[derive({", ".join(derives)})]')
    with module.writer.block(f'pub struct {paged_type} {{'):
      for f in fields:
        module.writer.doc(plan_fields[f.wire].get('docstring'))
        module.writer.line(f'pub {f.ident}: {f.type},')
      module.writer.doc('Keys the spec does not document, sent as they are.')
      module.writer.line(f'pub {EXTRA_FIELD}: serde_json::Map<String, serde_json::Value>,')
    module.writer.blank()
    with module.writer.block(f'impl {paged_type} {{'):
      module.writer.doc(f'The `{request_type}` for one page of the walk.')
      module.writer.line(f'fn at(&self, {driver.ident}: {driver.type}) -> {request_type} {{')
      with module.writer.indented():
        entries: list[str] = []
        for f in request_fields:
          if f.wire == pagination.driver:
            entries.append(f.ident)
          elif module.copyable(plan_fields[f.wire]['type']) and 'Box<' not in f.type:
            entries.append(f'{f.ident}: self.{f.ident}')
          else:
            entries.append(f'{f.ident}: self.{f.ident}.clone()')
        entries.append(f'{EXTRA_FIELD}: self.{EXTRA_FIELD}.clone()')
        module.writer.struct_literal('', request_type, entries)
      module.writer.line('}')
    module.writer.blank()

  row = module.type_expr(row_tree, path=[paged_type, 'Row'])
  row_labels: list[str | None] = [None] * len(union.items) if union is not None else []
  if union is not None and len(union.items) > 1:
    taken: set[str] = set()
    row_labels = [unique(module.variant_name(item), taken) for item in union.items]
  returns: Tokens = [('core', 'PaginatedResponse'), ('text', '<'), *tokenize(module, row), ('text', f', {state_type}>')]
  docs = [*method_docs(endpoint)[:1], f'Paged variant of `{method}`: await it for every row, or walk `rows()`/`pages()` one page at a time.', *method_docs(endpoint)[1:]]
  signature = Method(
    paged_name, [('request', [('local', paged_type)]), ('options', core_tokens('CallOptions'))], returns,
    is_async=False, doc=docs, deprecated=endpoint.deprecated, complex_type=long_tuple(row),
  )

  # The body.
  kind = str(done)
  zero_is_absent = strategy == 'token' and not pagination.driver_required
  w.line('let endpoint = self.clone();')
  # Only the indexed terminators read the page size; a `token` walk stops on the cursor the
  # response carries, so binding one there is dead code and a warning on every clean build
  # of every cursor-paged client. An `offset` walk that is not ended by `short_page` or a
  # page-counting `total` never reads it either.
  # A walk ended by `empty` stops on a page with no rows, whatever its size.
  reads_size = (strategy == 'page' and kind != 'empty') or (
    strategy == 'offset' and (kind == 'short_page' or (kind == 'total' and pagination.done.get('counts') == 'pages'))
  )
  size = _size(w, module, endpoint, pagination, request_fields, plan_fields) if reads_size else 'None'
  # ADR 0013: `total` only decides when to stop. A missing or moving total is not an error,
  # and no state outside `next`'s argument is kept, so every page stays a pure call.
  total_walk = strategy in ('page', 'offset') and kind == 'total'
  with w.block(f'let next = move |{driver.ident}: {state_type}| {{', '};'):
    w.line('let endpoint = endpoint.clone();')
    w.line('let request = request.clone();')
    w.line('let options = options.clone();')
    with w.block('async move {'):
      if pagination.driver_required:
        w.struct_literal('let request = ', request_type, [driver.ident, '..request'], ';')
      elif zero_is_absent:
        module.imports.add(f'{CORE}::paging', 'cursor_or_done')
        w.line(f'let {driver.ident} = cursor_or_done(Some({driver.ident}));')
        w.line(f'let request = request.at({driver.ident});')
      elif driver.optional:
        w.line(f'let request = request.at(Some({driver.ident}));')
      else:
        w.line(f'let request = request.at({driver.ident});')
      w.chain('let response = ', 'endpoint', [f'.{method}(request, options)', '.await?'], ';')
      if total_walk:
        total = _as_total(module, read_path(module, 'response', payload_type, str(pagination.done.get('path', '')), borrow=True))
        w.chain('let total = ', total.root, total.elements, ';')
      if union is not None:
        if strategy == 'token':
          # Only a cursor walk reads the cursor; a page or offset walk would leave it unused.
          module.imports.add(f'{CORE}::paging', 'cursor_or_done')
        _union_page_reads(w, module, endpoint, pagination, union, row, row_labels)
        cursor = Read('cursor', [], True, None)
      else:
        if strategy == 'token':
          module.imports.add(f'{CORE}::paging', 'cursor_or_done')
          cursor = read_path(module, 'response', payload_type, pagination.cursor_from or '', borrow=True)
          w.chain('let cursor = ', cursor.root, cursor.elements, ';')
        rows = read_path(module, 'response', payload_type, pagination.rows or '', borrow=False)
        w.chain('let rows = ', rows.root, [*rows.elements, *(['.unwrap_or_default()'] if rows.optional else [])], ';')
      if strategy in ('page', 'offset'):
        start = (pagination.start if pagination.start is not None else 1) if strategy == 'page' else 0
        counts_pages = pagination.done.get('counts') == 'pages'
        if total_walk:
          if strategy == 'page':
            module.imports.add(f'{CORE}::paging', 'total_reached')
            reached = f'total_reached({"true" if counts_pages else "false"}, {driver.ident}, {start}, {size}, rows.len(), total)'
          elif counts_pages:
            # An unaligned page can enter the last page's range before fetching its
            # last row. Only its starting offset can establish the final page.
            reached = (
              f'size.is_some_and(|size| {driver.ident} / size as i64 + 1 >= total)'
              if size != 'None' else 'false'
            )
          else:
            reached = f'{driver.ident} + rows.len() as i64 >= total'
          condition = f'rows.is_empty() || total.is_some_and(|total| {reached})' if total.optional else f'rows.is_empty() || {reached}'
        elif kind == 'empty':
          # `empty` ends on the first page with no rows only; a short page may still be
          # followed by more (the spec, Python, TypeScript and Go render the same rule).
          condition = 'rows.is_empty()'
        elif strategy == 'page' or kind == 'short_page':
          module.imports.add(f'{CORE}::paging', 'exhausted')
          condition = f'exhausted(rows.len(), {size})'
        else:
          condition = 'rows.is_empty()'
        if strategy == 'offset':
          # Read before `rows` moves into the returned tuple.
          w.line(f'let following = {driver.ident} + rows.len() as i64;')
        # This body is rendered one level shallower than the method it lands in.
        with if_block(w, condition, offset=len(INDENT)):
          w.line('return Ok((rows, None));')
        w.line(f'Ok((rows, Some({f"{driver.ident} + 1" if strategy == "page" else "following"})))')
      else:  # token; a `seek` walk is refused above
        if kind == 'empty':
          # `done: empty`: an empty page ends the walk whatever cursor it carries.
          with w.block('if rows.is_empty() {'):
            w.line('return Ok((rows, None));')
        w.line(f'Ok((rows, cursor_or_done({"cursor" if cursor.optional else "Some(cursor)"})))')
  if strategy == 'page':
    seed = str(pagination.start if pagination.start is not None else 1)
  elif strategy == 'offset':
    seed = '0'
  elif pagination.driver_required:
    seed = f'request.{driver.ident}' if module.copyable(plan_fields[driver.wire]['type']) else f'request.{driver.ident}.clone()'
  else:
    seed = {'i64': '0', 'f64': '0.0', 'String': 'String::new()', 'bool': 'false'}[state_type]
  w.line(f'PaginatedResponse::new({seed}, next)')
  module.core('PaginatedResponse')
  return signature, [line[4:] if line else line for line in w.render().rstrip('\n').split('\n')]


@dataclass(frozen=True)
class _UnionPages:
  """A token walk over a union payload (bybit's `market.instruments`, one variant per
  category): each variant with its enum label, the item type of its rows, and the row type
  the walk yields -- that item type when every variant shares it, else their union."""
  payload: str
  variants: list[tuple[str, Type, Type]]
  items: list[Type]
  row: Type


def _union_pages(module: Module, endpoint: EndpointPlan, pagination: PaginationPlan) -> _UnionPages | None:
  """The variants of a union payload a token walk reads its rows and cursor from, or `None`
  when the payload is not such a union. Python and the other backends walk it by joining
  every variant's rows; a variant without the cursor ends the walk. A `page` or `offset` walk
  not ended by a `total` joins the rows the same way, with no cursor to carry (binance's
  broker `sub_account_futures_summary_v2`, one variant per `futuresType`)."""
  token = pagination.strategy == 'token'
  indexed = pagination.strategy in ('page', 'offset') and pagination.done.get('kind') != 'total'
  if not (token or indexed) or not pagination.rows or (token and not pagination.cursor_from):
    return None
  if endpoint.response.payload is None or endpoint.response.optional:
    return None
  t = _resolved(module, {'type': 'ref', 'id': endpoint.response.payload})  # type: ignore[typeddict-item]
  if t is None or t['type'] != 'union' or len(t['variants']) < 2:
    return None
  if is_optional(t):
    raise _Skipped('a walk over a nullable union payload has no Rust walker yet')
  taken: set[str] = set()
  variants: list[tuple[str, Type, Type]] = []
  items: list[Type] = []
  carried = False
  for variant in t['variants']:
    label = unique(module.variant_name(variant['type']), taken)
    rows = read_path(module, 'page', variant['type'], pagination.rows, borrow=False)
    item = _item_type(module, rows.type)
    if item is None:
      raise _Skipped('a union variant whose rows have no item type has no Rust walker')
    variants.append((label, variant['type'], item))
    if item not in items:
      items.append(item)
    if not token:
      continue
    try:
      read_path(module, 'page', variant['type'], pagination.cursor_from or '', borrow=True)
      carried = True
    except _Skipped:
      pass
  if token and not carried:
    raise _Skipped('no variant of the union payload carries the cursor')
  row: Type = items[0] if len(items) == 1 else {'type': 'union', 'variants': [{'type': item} for item in items]}  # type: ignore[typeddict-item]
  return _UnionPages(endpoint.response.payload, variants, items, row)


def _union_page_reads(
  w: Writer, module: Module, endpoint: EndpointPlan, pagination: PaginationPlan, union: _UnionPages,
  row: str, row_labels: list[str | None],
):
  """`let (rows, cursor) = match response { ... };`: each variant's rows mapped into the
  walk's row type, and its cursor (`None` for a variant that carries none). An indexed walk
  carries no cursor: `let rows = match response { ... };`."""
  payload = render_tokens(module, tokenize(module, union.payload))
  token = pagination.strategy == 'token'
  with w.block('let (rows, cursor) = match response {' if token else 'let rows = match response {', '};'):
    for label, variant, item in union.variants:
      if not token:
        # One expression per arm: `rustfmt` drops the block and breaks the chain under the pattern.
        rows = read_path(module, 'page', variant, pagination.rows or '', borrow=False)
        elements = [*rows.elements, *(['.unwrap_or_default()'] if rows.optional else [])]
        label_of_row = row_labels[union.items.index(item)]
        if label_of_row is not None:
          elements.extend(('.into_iter()', f'.map({row}::{label_of_row})', '.collect::<Vec<_>>()'))
        w.chain(f'{payload}::{label}(page) => ', rows.root, elements, ',')
        continue
      with w.block(f'{payload}::{label}(page) => {{'):
        try:
          # Owned: a move out of one field of `page` leaves the rows field still readable.
          cursor = read_path(module, 'page', variant, pagination.cursor_from or '', borrow=False)
        except _Skipped:
          cursor = None
        if cursor is not None:
          w.chain('let cursor = ', cursor.root, cursor.elements, ';')
        rows = read_path(module, 'page', variant, pagination.rows or '', borrow=False)
        elements = [*rows.elements, *(['.unwrap_or_default()'] if rows.optional else [])]
        label_of_row = row_labels[union.items.index(item)]
        if label_of_row is not None:
          elements.extend(('.into_iter()', f'.map({row}::{label_of_row})', '.collect::<Vec<_>>()'))
        w.chain('let rows = ', rows.root, elements, ';')
        if cursor is None:
          w.line('(rows, None)')
        else:
          w.line('(rows, cursor)' if cursor.optional else '(rows, Some(cursor))')


_SEEK_KEY_FORMATS = frozenset((
  None, 'epoch-seconds', 'epoch-millis', 'epoch-micros', 'epoch-nanos', 'date-time', 'date', 'integer-string', 'int64',
))
"""Bound formats the runtime's `SeekKey` covers."""

_SPAN_UNITS = {'us': 'Micros', 'ms': 'Millis', 's': 'Seconds'}


def _paged_seek(
  module: Module, endpoint: EndpointPlan, pagination: PaginationPlan, *, method: str,
  struct_name: str, request_type: str, payload_type: Type,
) -> tuple[Method, list[str]]:
  """A `seek` walk (ADR 0013): the request is the single call's own, its moving bound sent
  from the walk's `SeekState`, and every page folded back through the runtime's `Seek`,
  which owns the algorithm (dedup, the moving bound, span chunks, the `LogicError`s)."""
  seek = pagination.seek or {}
  if pagination.row_type is None:
    raise _Skipped('a `seek` walk whose rows have no nameable type has no Rust walker yet; call the method per page')
  request_fields = module.fields[request_type]
  plan_fields = module.local[request_type]['fields']
  by_wire = {f.wire: f for f in request_fields}
  moving_wire, far_wire = seek.get('moving') or '', seek.get('far')
  moving = by_wire.get(moving_wire)
  far = by_wire.get(far_wire) if far_wire else None
  if moving is None:
    raise _Skipped(f'the seek bound `{moving_wire}` is not a field of `{request_type}`')
  if far_wire and far is None:
    raise _Skipped(f'the seek bound `{far_wire}` is not a field of `{request_type}`')
  if any(f is not None and f.optional and f.nullable for f in (moving, far)):
    raise _Skipped('a seek bound both optional and nullable has no Rust walker yet; call the method per page')
  key_tree = strip_null(plan_fields[moving.wire]['type'])
  if key_tree['type'] != 'scalar' or key_tree['base'] not in ('integer', 'number', 'string') or key_tree.get('format') not in _SEEK_KEY_FORMATS:
    raise _Skipped('a seek bound of this type has no Rust walker yet; call the method per page')
  span = seek.get('span')
  if span is not None and far is None:
    raise _Skipped('a seek `span` needs both bounds on the request')
  copy = module.copyable(key_tree)
  key_type = module.type_expr(key_tree, path=[struct_name, 'SeekKey'])
  clone = '' if copy else '.clone()'
  exclusive: dict = seek.get('exclusive') or {}
  firsts = []
  until = None
  until_field = ''
  until_clone = ''
  if exclusive:
    if not (moving.optional or moving.nullable):
      raise _Skipped(f'the exclusive parameters are refused beside `{moving_wire}`, which is required, so they could never be sent')
    for name in exclusive['parameters']:
      first = by_wire.get(name)
      if first is None:
        raise _Skipped(f'the exclusive parameter `{name}` is not a field of `{request_type}`')
      if not (first.optional or first.nullable) or (first.optional and first.nullable):
        raise _Skipped(f'the exclusive parameter `{name}` is not a plain optional field, so the walk cannot leave it out')
      firsts.append(first)
    if exclusive.get('far'):
      until = by_wire[exclusive['far']['parameter']]
      until_tree = strip_null(plan_fields[until.wire]['type'])
      if until_tree['type'] != 'scalar' or until_tree['base'] not in ('integer', 'number', 'string') or until_tree.get('format') not in _SEEK_KEY_FORMATS:
        raise _Skipped(f'the exclusive far bound `{until.wire}` has a type the walk cannot compare rows with')
      until_clone = '' if module.copyable(until_tree) else '.clone()'
      until_field = exclusive['far']['field']

  w = Writer(1)
  paged_name = f'{method}{PAGED_SUFFIX}'
  paged_type = f'{struct_name}{pascal_case(PAGED_SUFFIX)}Request'
  module.declare(paged_type)
  caps = f' and whose `{far_wire}` caps it' if far is not None else ''
  module.writer.doc(f'`{paged_name}`\'s request: `{request_type}`, whose `{moving_wire}` the walk moves{caps}.')
  module.writer.line(f'pub type {paged_type} = {request_type};')
  module.writer.blank()

  row = module.type_expr(pagination.row_type, path=[paged_type, 'Row'])
  row_tokens = tokenize(module, row)
  key_tokens = tokenize(module, key_type)
  returns: Tokens = [
    ('core', 'PaginatedResponse'), ('text', '<'), *row_tokens, ('text', ', '), ('core', 'SeekState'),
    ('text', '<'), *key_tokens, ('text', ', '), *row_tokens, ('text', '>>'),
  ]
  direction = 'backwards' if seek.get('descending') else 'forwards'
  field = seek['cursor']['field']
  move = seek_move_prose(
    field, descending=bool(seek.get('descending')), ordered=key_type != 'String',
    cap=_seek_cap_known(pagination, request_fields, seek), span=span is not None,
  )
  walk = (
    f'Walks {direction} by moving `{moving.ident}` {move}'
    + (f', never past the caller\'s own `{far.ident}`' if far is not None else '')
    + '; a page re-serving rows already yielded is deduplicated.'
  )
  clamp = _seek_size(pagination, request_fields, plan_fields)
  if clamp is not None:
    walk += f' {_seek_size_rule(pagination)}'
  walk += ''.join(f' {sentence}' for sentence in plan_exclusive_sentences(exclusive, moving=moving_wire))
  docs = [*method_docs(endpoint)[:1], f'Paged variant of [`Self::{method}`]: await it for every row, or walk `rows()`/`pages()` one page at a time. {walk}', *method_docs(endpoint)[1:]]
  signature = Method(
    paged_name, [('request', [('local', paged_type)]), ('options', core_tokens('CallOptions'))], returns,
    is_async=False, doc=docs, deprecated=endpoint.deprecated, complex_type=long_tuple(row),
  )
  module.core('PaginatedResponse')
  module.core('Seek')
  module.core('SeekState')

  w.line('let endpoint = self.clone();')
  if clamp is not None:
    w.line('let mut request = request;')
    w.chain(*clamp, ';')
  size = _size(w, module, endpoint, pagination, request_fields, plan_fields, clamped=clamp is not None)
  cap = seek.get('cap')
  if size != 'None':
    cap_expr = f'size.or(Some({cap}))' if cap is not None else 'size'
  else:
    cap_expr = f'Some({cap})' if cap is not None else None
  elements = [f'.cap({cap_expr})'] if cap_expr is not None else []
  if span is not None:
    module.core('SpanUnit')
    elements.append(f'.span({int(span["default"])}, SpanUnit::{_SPAN_UNITS[span["unit"]]})')
  if seek_cursor_format(pagination.cursor_type, key_tree) is not None:
    # The runtime reads the cursor back through its wire form: a row in another timestamp
    # format than the bound's (epoch seconds under a millisecond bound) is read as its own
    # type, then converted (TRU-197).
    assert pagination.cursor_type is not None
    cursor_type = module.type_expr(pagination.cursor_type, path=[paged_type, 'Cursor'])
    elements.append(f'.cursor::<{cursor_type}>()')
  if until is not None:
    elements.append(f'.until({string(until_field)})')
  unique = 'true' if seek.get('cursor', {}).get('unique') else 'false'
  descending = 'true' if seek.get('descending') else 'false'
  # Two statements rather than one chain: `rustfmt` measures a chain on a call root
  # differently from one on a short binding, and the binding is the rule the printer knows.
  # The declared field, which is unambiguous; the runtime prints it relative to one row.
  w.call('let seek = Seek::new', [string(paged_name), string(field), unique, descending], ';')
  if elements:
    w.chain('let seek = ', 'seek', elements, ';')
  moving_optional = moving.optional or moving.nullable
  init = f'request.{moving.ident}{clone}'
  w.line(f'let init = SeekState::new({init if moving_optional else f"Some({init})"});')
  # This body is rendered one level shallower than the method it lands in.
  with w.closure_binding(
    'let next', 'move |state: ', f'SeekState<{key_type}, {row}>', '| {', offset=len(INDENT),
  ):
    w.line('let endpoint = endpoint.clone();')
    w.line('let request = request.clone();')
    w.line('let options = options.clone();')
    w.line('let seek = seek.clone();')
    with w.block('async move {'):
      if far is None:
        w.line(f'let far: Option<{key_type}> = None;')
      elif far.optional or far.nullable:
        w.line(f'let far = request.{far.ident}{clone};')
      else:
        w.line(f'let far = Some(request.{far.ident}{clone});')
      if span is not None:
        w.chain('let edge = ', 'seek', ['.edge(state.pos.as_ref(), far.as_ref())?'], ';')
      if firsts:
        _exclusive_checks(w, module, exclusive, paged_name=paged_name, moving=moving, firsts=firsts, until=until)
      if until is not None:
        w.line(f'let until = request.{until.ident}{until_clone};')
      w.line('let mut request = request;')
      if moving_optional:
        w.line(f'request.{moving.ident} = state.pos{clone};')
      else:
        with w.block(f'if let Some(pos) = state.pos{clone} {{'):
          w.line(f'request.{moving.ident} = pos;')
      if firsts:
        # Only while the walk has no position of its own: the venue refuses them beside it.
        with w.block('if state.pos.is_some() {'):
          for first in firsts:
            w.line(f'request.{first.ident} = None;')
      if span is not None and far is not None:
        if far.optional or far.nullable:
          w.line(f'request.{far.ident} = edge{clone};')
        else:
          with w.block(f'if let Some(edge) = edge{clone} {{'):
            w.line(f'request.{far.ident} = edge;')
      w.chain('let response = ', 'endpoint', [f'.{method}(request, options)', '.await?'], ';')
      rows = read_path(module, 'response', payload_type, pagination.rows or '', borrow=False)
      w.chain('let rows = ', rows.root, [*rows.elements, *(['.unwrap_or_default()'] if rows.optional else [])], ';')
      edge_arg = 'edge.as_ref()' if span is not None else 'None'
      if until is not None:
        w.line(f'seek.step_until(&state, rows, {edge_arg}, far.as_ref(), until.as_ref())')
      else:
        w.line(f'seek.step(&state, rows, {edge_arg}, far.as_ref())')
  w.line('PaginatedResponse::new(init, next)')
  return signature, [line[4:] if line else line for line in w.render().rstrip('\n').split('\n')]


def _exclusive_checks(
  w: Writer, module: Module, exclusive: dict, *, paged_name: str, moving: Field, firsts: list[Field],
  until: Field | None,
):
  """The walk's refusal of the moving bound beside a non-far exclusive parameter, and of
  neither the moving bound nor `first`, before any request (as a `LogicError`, like a span
  walk missing a bound). The far one is allowed beside the moving bound: it is then never
  sent, and kept on the rows all the same. Rendered one level shallower than it lands, as
  the walk is."""
  refused_fields = [first for first in firsts if until is None or first.wire != until.wire]
  if refused_fields or exclusive.get('first'):
    module.core('Error')
  if refused_fields:
    operands = [f'request.{first.ident}.is_some()' for first in refused_fields]
    head = 'let given = '
    if w.column + len(INDENT) + len(head) + len(' || '.join(operands)) + 1 <= MAX_WIDTH:
      w.line(f'{head}{" || ".join(operands)};')
    else:
      w.line(f'{head}{operands[0]}')
      with w.indented():
        for operand in operands[1:-1]:
          w.line(f'|| {operand}')
        w.line(f'|| {operands[-1]};')
    listed = '/'.join(f'`{first.wire}`' for first in refused_fields)
    refused = f'`{paged_name}` walks by `{moving.wire}`, which the venue refuses alongside {listed}: pass one or the other'
    with if_block(w, f'request.{moving.ident}.is_some() && given', offset=len(INDENT)):
      w.call('return Err(Error::logic', [string(refused)], ');')
  first_wire = exclusive.get('first')
  if first_wire:
    first = next(f for f in firsts if f.wire == first_wire)
    missing = (
      f'`{paged_name}` needs `{moving.wire}` or `{first.wire}` to start from: without either the venue '
      f'answers from the wrong end of the range'
    )
    with if_block(w, f'request.{moving.ident}.is_none() && request.{first.ident}.is_none()', offset=len(INDENT)):
      w.call('return Err(Error::logic', [string(missing)], ');')


def _state_type(t: Type | None) -> str | None:
  """The Rust cursor type, when the runtime's `CursorLike` covers it."""
  if t is None or t['type'] != 'scalar':
    return None
  fmt = t.get('format')
  # `int64` renders the base integer (`i64`), so it walks as one.
  if fmt is not None and fmt not in ('uuid', 'hostname', 'uri', 'int64'):
    return None
  return _STATE_TYPES.get(t['base'])


def _seek_size(pagination: PaginationPlan, request_fields: list[Field], plan_fields) -> tuple[str, str, list[str]] | None:
  """The chain (`Writer.chain`'s prefix, root, elements) that clamps the request's integer
  page size in place before a `seek` walk starts, so every page sends the clamped value and
  `_size` reads it as the cap: at most the schema's `maximum`, and at least 2 (a page of 1
  cannot advance past an inclusive moving bound: it re-serves the boundary row alone). A
  size the caller omits stays unset. `None` when there is no integer size to clamp."""
  rendered = next((f for f in request_fields if f.wire == pagination.size), None) if pagination.size else None
  if rendered is None:
    return None
  tree = strip_null(plan_fields[rendered.wire]['type'])
  if tree['type'] != 'scalar' or tree['base'] != 'integer' or tree.get('format') not in (None, 'int64'):
    return None
  maximum = pagination.size_maximum
  # `clamp` (clippy's `manual_clamp`, and `.max(2).min(1)` is its deny-level `min_max`),
  # floored at `min(2, maximum)` so that it cannot panic: a maximum below 2 is the page size.
  clamp = '.max(2)' if maximum is None else f'.clamp({min(2, maximum)}, {maximum})'
  if rendered.optional and rendered.nullable:
    elements = [f'.map(|size| size.map(|size| size{clamp}))']
  elif rendered.optional or rendered.nullable:
    elements = [f'.map(|size| size{clamp})']
  else:
    elements = [clamp]
  return f'request.{rendered.ident} = ', 'request', [f'.{rendered.ident}', *elements]


def _seek_cap_known(pagination: PaginationPlan, request_fields: list[Field], seek: dict) -> bool | str:
  """Whether the cap `Seek::cap` gets always resolves (`True`), never does (`False`), or only
  while the caller sets the optional size field it names, as `_size` binds it."""
  if seek.get('cap') is not None:
    return True
  rendered = next((f for f in request_fields if f.wire == pagination.size), None) if pagination.size else None
  if rendered is None:
    return False
  if not rendered.optional and not rendered.nullable:
    return True
  default = pagination.size_default
  return True if default is not None and str(default).isdigit() else rendered.ident


def _seek_size_rule(pagination: PaginationPlan) -> str:
  """The doc sentence stating `_seek_size`'s clamp. A maximum below 2 leaves no room for the
  floor, so the sentence then claims none."""
  maximum = pagination.size_maximum
  if maximum is not None and maximum < 2:
    return f'The walk requests pages of {maximum} row{"" if maximum == 1 else "s"}.'
  bounds = f'at least 2 rows and at most {pagination.size_maximum}' if pagination.size_maximum is not None else 'at least 2 rows'
  return f'The walk requests pages of {bounds}: a page must hold one new row beside the one it re-reads.'


def _size(
  w: Writer, module: Module, endpoint: EndpointPlan, pagination: PaginationPlan, request_fields: list[Field], plan_fields,
  *, clamped: bool = False,
) -> str:
  """Bind `size` (an `Option<usize>`) from the request's page-size field and return the
  expression the terminator checks read it by. `clamped`: `_seek_size` has already clamped
  the field in the request, so it is read as is. A size of 0 or less is unknown (`None`), as
  an unset one is: a 0 would divide by zero in a page-counting `total`, and a negative one
  cast to `usize` would read every page as short."""
  if pagination.size is None:
    return 'None'
  rendered = next((f for f in request_fields if f.wire == pagination.size), None)
  if rendered is None:
    return 'None'
  tree = strip_null(plan_fields[pagination.size]['type'])
  positive = '.filter(|&size| size > 0)'
  if tree['type'] == 'scalar' and tree['base'] == 'string' and tree.get('format') is None:
    # A size sent as a string (bybit's `size`): its number, or unknown when it is not one.
    parse = '.parse::<usize>().ok()'
    default = [f'.or(Some({int(pagination.size_default)}))'] if str(pagination.size_default).isdigit() else []
    if not rendered.optional and not rendered.nullable:
      w.chain('let size = ', 'request', [f'.{rendered.ident}', parse, *default, positive], ';')
    else:
      hops = ['.as_ref()', '.and_then(|size| size.as_deref())'] if rendered.optional and rendered.nullable else ['.as_deref()']
      w.chain('let size = ', 'request', [f'.{rendered.ident}', *hops, f'.and_then(|size| size{parse})', *default, positive], ';')
    return 'size'
  # `int64` renders the base integer (`i64`), so it sizes a page as one.
  if tree['type'] != 'scalar' or tree['base'] != 'integer' or tree.get('format') not in (None, 'int64'):
    raise _Skipped(f'a page size of type other than an integer (`{pagination.size}`) has no Rust walker yet')
  flatten = ['.flatten()'] if rendered.optional and rendered.nullable else []
  # The caller's size as the API serves it: at most the schema's `maximum`, so a page full
  # at the maximum is not read as a short last page (or, for `seek`, as not full).
  clamp = f'.min({pagination.size_maximum})' if pagination.size_maximum is not None and not clamped else ''
  if not rendered.optional and not rendered.nullable:
    w.chain('let size = ', f'usize::try_from(request.{rendered.ident}{clamp})', ['.ok()', positive], ';')
  elif pagination.size_default is not None:
    w.chain('let size = ', 'request', [f'.{rendered.ident}', *flatten, f'.unwrap_or({pagination.size_default})', *([clamp] if clamp else [])], ';')
    w.chain('let size = ', 'usize::try_from(size)', ['.ok()', positive], ';')
  else:
    w.chain('let size = ', 'request', [f'.{rendered.ident}', *flatten, f'.and_then(|size| usize::try_from(size{clamp}).ok())', positive], ';')
  return 'size'


def _as_total(module: Module, read: Read) -> Read:
  """`read` as the `i64` (or `Option<i64>`) the `total` checks take."""
  t = _resolved(module, read.type)
  elements = list(read.elements)
  if t is not None and t['type'] == 'scalar' and t['base'] == 'string' and t.get('format') is None:
    # An integer sent as a string (bybit's `lastPage`); one that is not a number never ends the walk.
    if elements and elements[-1] in ('.clone()', '.cloned()'):
      elements.pop()
    parse = '.parse::<i64>().ok()'
    elements.extend((f'.and_then(|total| total{parse})' if read.optional else parse, '.unwrap_or(i64::MAX)'))
    return Read(read.root, elements, False, t)
  # An `integer-string` is an integer whatever its JSON base (dYdX Comet's `total_count` is a
  # `string`), and renders `IntegerString` either way.
  integer = t is not None and t['type'] == 'scalar' and (t['base'] == 'integer' or t.get('format') == 'integer-string')
  if not integer:
    raise _Skipped('a `total` that is not an integer has no Rust walker yet')
  if t.get('format') == 'integer-string':
    # An arbitrary-precision integer, clamped: no page walk counts past `i64::MAX` rows.
    elements.append('.map(|total| total.saturating_i64())' if read.optional else '.saturating_i64()')
  return Read(read.root, elements, read.optional, t)


__all__ = [
  'PAGED_SUFFIX', 'RAW_SUFFIX', 'raw_name', 'EndpointModule', 'Method', 'Read', 'Tokens', 'core_tokens',
  'emit_method', 'endpoint_file', 'method_docs', 'read_path', 'render_endpoint',
  'render_stream_endpoint', 'render_tokens', 'tokenize',
]
