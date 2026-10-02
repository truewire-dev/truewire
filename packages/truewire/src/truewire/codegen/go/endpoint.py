"""One endpoint package: its types, and an `Endpoint` struct holding the core behind its
contract interface, with the method, its `Raw` twin, and the `Paged` walker when
pagination is declared.

Every endpoint is its own Go package (`repos/get`), so its types keep the plan's own names
(`get.Request`, `get.Repository`) without colliding with a sibling's. The typed method
dumps the request, hands the core one call and decodes the reply; the `Raw` twin returns
the `json.RawMessage` the core returned, the Go form of `validate: false` (a second
method, since Go has no overloads). Every method takes a `context.Context` first and
`...truewire.CallOption` last.
"""
import re
from dataclasses import dataclass, field

from truewire.codegen.seek import plan_exclusive_sentences
from truewire.plan.model import EndpointPlan, PackagePlan, PaginationPlan
from truewire.plan.types import Type, is_optional, strip_null

from .meta import META_DIR, MetaShape
from .names import camel_ident, json_value, package_ident, pascal_ident, string, unique
from .printer import Writer
from .types import CORE, FORMATS, Field, Module, Package, scope_alias, visible_scopes

PAGED_SUFFIX = 'Paged'
RAW_SUFFIX = 'Raw'

RESERVED_ROOT_DIRS = frozenset(('core', 'meta', 'protos', 'replay', 'types'))
"""Directories the root package holds for itself: the hand-written core, `meta`, the replay
table and the shared types. A root-level spec segment with one of these names gets an
`api` suffix."""

Tokens = list[tuple[str, str]]
"""A type in a method signature, kept symbolic so a router can render it qualified:
`('local', 'Request')` for a name the endpoint package defines, `('shared', '<scope>|Name')`
for a shared scope's, `('core', 'CallOption')`, `('json', 'RawMessage')`, `('text', '[]')`."""

_TOKEN = re.compile(r'[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?|[^A-Za-z_]+')
_SEGMENT = re.compile(r'\[(-?\d+)\]|([^.\[\]]+)')

_TIME_UNITS = {'us': 'time.Microsecond', 'ms': 'time.Millisecond', 's': 'time.Second'}
_EPOCH_CONVERTERS = {
  'TimestampSeconds': 'EpochSeconds', 'TimestampMillis': 'EpochMillis',
  'TimestampMicros': 'EpochMicros', 'TimestampNanos': 'EpochNanos',
}


def endpoint_dirs(path: list[str]) -> list[str]:
  """`repos.list_commits` -> `['repos', 'listcommits']`: one package directory per segment."""
  dirs = [package_ident(p, fallback='router') for p in path]
  if dirs and dirs[0] in RESERVED_ROOT_DIRS:
    dirs[0] = f'{dirs[0]}api'
  return dirs


@dataclass(frozen=True)
class Method:
  """One method of the `Endpoint` struct, as a router needs it to delegate."""
  name: str
  params: list[tuple[str, Tokens]]
  """(parameter name, its type) between the context and the options."""
  returns: list[Tokens]
  context: bool = True
  doc: list[str | None] = field(default_factory=list)
  deprecated: bool = False
  kind: str = 'call'
  """`call`, `raw` or `paged`: what a replay table may pair up."""


@dataclass(frozen=True)
class EndpointModule:
  file: str
  dirs: list[str]
  package: str
  """The Go package name."""
  import_path: str
  struct_name: str
  constructor: str
  contract: str
  """The `truewire` interface the core satisfies: `HttpEndpoint`, `CommandEndpoint`,
  `RpcEndpoint` (both transports) or `StreamEndpoint`."""
  transport: str
  """`http`, `ws` or `stream`: for an endpoint declaring both, the first declared (the one
  a call without `WithTransport` goes over)."""
  methods: list[Method]
  source: str


def tokenize(module: Module, expr: str) -> Tokens:
  """A rendered type expression as `Tokens`, from what the module knows each name is."""
  aliases = {scope_alias(scope): scope for scope in module.plan.schemas}
  out: Tokens = []
  for word in _TOKEN.findall(expr):
    if '.' in word and word[0].isalpha():
      qualifier, name = word.split('.', 1)
      if qualifier == CORE:
        out.append(('core', name))
        continue
      if qualifier == 'json':
        out.append(('json', name))
        continue
      if qualifier in aliases:
        out.append(('shared', f'{aliases[qualifier]}|{name}'))
        continue
    if word in module.names:
      out.append(('local', word))
    elif out and out[-1][0] == 'text':
      out[-1] = ('text', out[-1][1] + word)
    else:
      out.append(('text', word))
  return out


def render_tokens(module: Module, tokens: Tokens, *, qualifier: str | None = None) -> str:
  """`tokens` as this module's own text, importing what they need; a router passes the
  endpoint package's import name as `qualifier` (`get.Request`)."""
  parts: list[str] = []
  for kind, text in tokens:
    if kind == 'core':
      parts.append(module.core(text))
    elif kind == 'json':
      module.std('encoding/json')
      parts.append(f'json.{text}')
    elif kind == 'shared':
      scope, name = text.split('|', 1)
      parts.append(f'{module.import_scope(scope)}.{name}')
    elif kind == 'local' and qualifier is not None:
      parts.append(f'{qualifier}.{text}')
    else:
      parts.append(text)
  return ''.join(parts)


def method_docs(endpoint: EndpointPlan) -> list[str | None]:
  docs = [endpoint.docs.description]
  if endpoint.docs.url:
    docs.append(f'See {endpoint.docs.url}.')
  return docs


def emit_method(
  module: Module, method: Method, *, receiver: str, qualifier: str | None,
  body: list[str] | None = None, target: str | None = None, name: str | None = None,
):
  """`method`'s doc and signature on `receiver` (`e *Endpoint`); `body` lines inside it,
  or a call delegating to `target` (`r.get`) when `body` is `None` (a router). `name` is
  the method's name on the receiver when a router renames a colliding twin."""
  w = module.writer
  doc = [*method.doc]
  outer = name or method.name
  if outer != method.name and doc and doc[0] and doc[0].startswith(method.name):
    doc[0] = outer + doc[0][len(method.name):]
  if method.deprecated:
    doc.append('Deprecated: the API marks this endpoint deprecated.')
  w.doc(*doc)
  params: list[str] = []
  if method.context:
    module.std('context')
    params.append('ctx context.Context')
  params.extend(f'{name} {render_tokens(module, tokens, qualifier=qualifier)}' for name, tokens in method.params)
  params.append(f'opts ...{module.core("CallOption")}')
  returns = [render_tokens(module, tokens, qualifier=qualifier) for tokens in method.returns]
  result = returns[0] if len(returns) == 1 else f'({", ".join(returns)})'
  w.line(f'func ({receiver}) {outer}({", ".join(params)}) {result} {{')
  with w.indented():
    if body is None:
      args = [*(['ctx'] if method.context else []), *(n for n, _ in method.params), 'opts...']
      w.line(f'return {target}.{method.name}({", ".join(args)})')
    else:
      for line in body:
        w.line(line)
  w.line('}')


class _Skipped(Exception):
  def __init__(self, reason: str):
    super().__init__(reason)
    self.reason = reason


def _save(module: Module) -> tuple:
  """What a renderer that may be skipped part-way can leave behind: written lines, imports
  and declared names."""
  imports = module.imports
  return len(module.writer._lines), set(imports._std), dict(imports._other), set(module.names)


def _restore(module: Module, saved: tuple):
  """Undo everything since `_save`, so a skipped walker leaves no unused import or type."""
  lines, std, other, names = saved
  del module.writer._lines[lines:]
  module.imports._std = std
  module.imports._other = other
  module.names = names


def _new_module(plan: PackagePlan, package: Package, endpoint: EndpointPlan, local) -> tuple[Module, list[str], str]:
  dirs = endpoint_dirs(endpoint.path)
  file = '/'.join([*dirs, f'{dirs[-1]}.go'])
  module = Module(plan, file, package=package, name=dirs[-1], local=local, visible=visible_scopes(plan, endpoint.path))
  return module, dirs, file


def _meta_field(module: Module, meta: MetaShape | None, endpoint: EndpointPlan) -> str | None:
  if meta is None:
    return None
  text, needs_json = meta.literal(endpoint.meta, CORE)
  if f'{CORE}.' in text:
    module.core('Ptr')
  if needs_json:
    module.std('encoding/json')
  module.imports.add(module.package.path(META_DIR))
  return f'Meta: {META_DIR}.{text}'


def _declare_struct(module: Module) -> tuple[str, str]:
  struct = unique('Endpoint', module.names)
  constructor = unique('New', module.names)
  return struct, constructor


def _header(module: Module, struct: str, constructor: str, contract: str, endpoint: EndpointPlan):
  w = module.writer
  w.blank()
  w.doc(f'{struct} is the `{endpoint.function}` endpoint.', endpoint.docs.description)
  w.struct(f'type {struct} struct {{', [('core', module.core(contract), None)])
  w.blank()
  w.doc(f'{constructor} returns the endpoint over core.')
  with w.block(f'func {constructor}(core {module.core(contract)}) *{struct} {{'):
    w.line(f'return &{struct}{{core: core}}')


def render_endpoint(
  plan: PackagePlan, endpoint: EndpointPlan, *, package: Package, meta: MetaShape | None,
) -> tuple[EndpointModule, list[str]]:
  """Render one `rpc` endpoint's package, over HTTP, as a WebSocket command, or (declaring
  both) over either behind `truewire.WithTransport`, the first declared by default; the
  second value lists what was skipped."""
  request = endpoint.request
  fixed = [f for f in request.fields if f.fixed is not None] if request.shape == 'fields' else []
  local = dict(endpoint.types)
  if fixed and request.type in local:
    record = local[request.type]
    local[request.type] = {**record, 'fields': {k: v for k, v in record['fields'].items() if k not in {f.wire for f in fixed}}}  # type: ignore[typeddict-item]
  module, dirs, file = _new_module(plan, package, endpoint, local)
  struct, constructor = _declare_struct(module)
  module.define_all(local)
  notes: list[str] = []
  over_http = 'http' in endpoint.transports
  dual = over_http and 'ws' in endpoint.transports
  primary = endpoint.transports[0] if dual else ('http' if over_http else 'ws')
  contract = 'RpcEndpoint' if dual else 'HttpEndpoint' if over_http else 'CommandEndpoint'
  if request.type is None and request.fields:
    raise _Skipped('a request with fields but no rendered type has no Go rendering')

  name = pascal_ident(endpoint.path[-1], fallback='Call')
  payload = endpoint.response.payload
  params: list[tuple[str, Tokens]] = []
  if request.type is not None:
    params.append(('request', tokenize(module, module.ref(request.type))))
  docs = method_docs(endpoint)
  if dual:
    other = 'ws' if primary == 'http' else 'http'
    wire = {'http': 'HTTP', 'ws': 'the WebSocket'}
    docs = [*docs, f'Sent over {wire[primary]}; pass truewire.WithTransport(truewire.Transport{other.upper() if other == "ws" else "HTTP"}) to send it over {wire[other]}.']
  json_tokens: Tokens = [('json', 'RawMessage')]
  error: Tokens = [('text', 'error')]
  methods: list[Method] = []

  paged: tuple[Method, list[str]] | None = None
  pagination = endpoint.pagination
  if pagination is not None and payload is not None:
    saved = _save(module)
    try:
      paged = _paged(module, endpoint, pagination, method=name, request_type=request.type)
    except _Skipped as skipped:
      _restore(module, saved)
      notes.append(f'{endpoint.function}: {skipped.reason}')

  call_lines, body_var = _dump_lines(module, request.type is not None, fixed, 'request', error_return='nil, err' if payload else 'err')
  meta_field = _meta_field(module, meta, endpoint)
  common = [*([f'Request: {body_var}'] if body_var else []), *([meta_field] if meta_field else []), f'Options: {module.core("Options")}(opts...)']

  def verb_call(transport: str) -> str:
    if transport == 'http':
      fields = [*([f'Method: {string(endpoint.wire.method)}'] if endpoint.wire.method else []), f'Path: {string(endpoint.wire.path or "")}']
      call_type, verb = 'HttpCall', 'Request'
    else:
      fields = [f'Path: {string(endpoint.wire.path or "")}']
      call_type, verb = 'CommandCall', 'Command'
    return f'e.core.{verb}(ctx, {module.core(call_type)}{{{", ".join([*fields, *common])}}})'

  call = verb_call(primary)
  # The other half of a dual-transport endpoint: taken when the caller's option names it.
  alternate: tuple[str, str] | None = None
  if dual:
    other = 'ws' if primary == 'http' else 'http'
    alternate = (f'{module.core("Options")}(opts...).Transport == {module.core("Transport" + ("WS" if other == "ws" else "HTTP"))}', verb_call(other))

  _header(module, struct, constructor, contract, endpoint)
  w = module.writer
  receiver = f'e *{struct}'
  args = ', '.join(['ctx', *(n for n, _ in params), 'opts...'])
  if payload is not None:
    payload_tokens = tokenize(module, module.ref(payload))
    main = Method(name, params, [payload_tokens, error], doc=docs, deprecated=endpoint.deprecated)
    raw = Method(
      f'{name}{RAW_SUFFIX}', params, [json_tokens, error],
      doc=[f'{name}{RAW_SUFFIX} is {name} without validation: the wire body as it came.'],
      deprecated=endpoint.deprecated, kind='raw',
    )
    if paged is not None:
      methods.append(paged[0])
    methods.extend((main, raw))
    if paged is not None:
      w.blank()
      emit_method(module, paged[0], receiver=receiver, qualifier=None, body=paged[1])
    w.blank()
    decoded = render_tokens(module, payload_tokens)
    emit_method(module, main, receiver=receiver, qualifier=None, body=[
      f'return {module.core("DecodeResult")}[{decoded}](e.{raw.name}({args}))',
    ])
    w.blank()
    branch = [f'if {alternate[0]} {{', f'\treturn {alternate[1]}', '}'] if alternate else []
    emit_method(module, raw, receiver=receiver, qualifier=None, body=[*call_lines, *branch, f'return {call}'])
  else:
    main = Method(name, params, [error], doc=docs, deprecated=endpoint.deprecated)
    methods.append(main)
    w.blank()
    assign = '_, err = ' if call_lines else '_, err := '
    branch = [f'if {alternate[0]} {{', f'\t{assign}{alternate[1]}', '\treturn err', '}'] if alternate else []
    emit_method(module, main, receiver=receiver, qualifier=None, body=[*call_lines, *branch, f'{assign}{call}', 'return err'])

  return EndpointModule(
    file=file, dirs=dirs, package=module.name, import_path=package.path(*dirs), struct_name=struct,
    constructor=constructor, contract=contract, transport=primary, methods=methods,
    source=module.render(doc=f'Package {module.name} is the `{endpoint.function}` endpoint.'),
  ), notes


def _dump_lines(module: Module, has_request: bool, fixed, var: str, *, error_return: str) -> tuple[list[str], str | None]:
  """The statements dumping the request into `body`, and the name holding it."""
  if not has_request:
    return [], None
  if fixed:
    entries = ', '.join(f'{string(f.wire)}: {json_value(f.fixed)}' for f in fixed)
    if 'json.RawMessage(' in entries:
      module.std('encoding/json')
    head = f'body, err := {module.core("DumpWith")}({var}, map[string]any{{{entries}}})'
  else:
    head = f'body, err := {module.core("Dump")}({var})'
  return [head, 'if err != nil {', f'\treturn {error_return}', '}'], 'body'


def render_stream_endpoint(
  plan: PackagePlan, endpoint: EndpointPlan, *, package: Package, meta: MetaShape | None,
) -> tuple[EndpointModule, list[str]]:
  """Render one `stream` endpoint's package: the subscription, typed and raw.

  A stream whose parameters are exactly its channel's placeholders (`direct_channel`, or
  a `connect_only` push) takes them as plain arguments and fills the channel itself; any
  other stream takes its `Parameters` value and hands it to the core.
  """
  request = endpoint.request
  module, dirs, file = _new_module(plan, package, endpoint, dict(endpoint.types))
  struct, constructor = _declare_struct(module)
  module.define_all(endpoint.types)
  notes: list[str] = []
  channel = endpoint.wire.channel or ''
  name = pascal_ident(endpoint.path[-1], fallback='Subscribe')
  message = endpoint.response.payload
  reply = endpoint.stream.reply if endpoint.stream is not None else None

  params: list[tuple[str, Tokens]] = []
  pre: list[str] = []
  body_var: str | None = None
  channel_expr = string(channel)
  if request.type is not None:
    params.append(('parameters', tokenize(module, module.ref(request.type))))
    pre, body_var = _dump_lines(module, True, [], 'parameters', error_return='nil, err')
  elif request.fields:
    values: list[str] = []
    taken = {'ctx', 'opts', 'e', 'body', 'err'}
    for f in request.fields:
      ident = unique(camel_ident(f.wire, fallback='value'), taken)
      tree = strip_null(f.type)
      params.append((ident, tokenize(module, module.type_expr(tree, path=[pascal_ident(f.wire)]))))
      values.append(f'{string(f.wire)}: {ident}')
    channel_expr = f'{module.core("FillTemplate")}({string(channel)}, map[string]any{{{", ".join(values)}}})'

  fields = [f'Channel: {channel_expr}']
  if body_var:
    fields.append(f'Parameters: {body_var}')
  meta_field = _meta_field(module, meta, endpoint)
  if meta_field:
    fields.append(meta_field)
  fields.append(f'Options: {module.core("Options")}(opts...)')
  call = f'e.core.Subscribe(ctx, {module.core("SubscribeCall")}{{{", ".join(fields)}}})'

  _header(module, struct, constructor, 'StreamEndpoint', endpoint)
  w = module.writer
  receiver = f'e *{struct}'
  docs = method_docs(endpoint)
  raw_stream: Tokens = [('text', '*'), ('core', 'Stream'), ('text', '['), ('json', 'RawMessage'), ('text', ']')]
  error: Tokens = [('text', 'error')]
  args = ', '.join(['ctx', *(n for n, _ in params), 'opts...'])
  methods: list[Method] = []
  if message is not None:
    message_tokens = tokenize(module, module.ref(message))
    if reply is not None:
      reply_tokens = tokenize(module, module.ref(reply))
      typed: Tokens = [('text', '*'), ('core', 'Subscription'), ('text', '['), *message_tokens, ('text', ', '), *reply_tokens, ('text', ']')]
      helper = f'{module.core("Subscribed")}[{render_tokens(module, message_tokens)}, {render_tokens(module, reply_tokens)}]'
    else:
      typed = [('text', '*'), ('core', 'Stream'), ('text', '['), *message_tokens, ('text', ']')]
      helper = f'{module.core("Streamed")}[{render_tokens(module, message_tokens)}]'
    main = Method(name, params, [typed, error], doc=docs, deprecated=endpoint.deprecated)
    raw = Method(
      f'{name}{RAW_SUFFIX}', params, [raw_stream, error],
      doc=[f'{name}{RAW_SUFFIX} is {name} without validation: the messages as they came.'],
      deprecated=endpoint.deprecated, kind='raw',
    )
    methods.extend((main, raw))
    w.blank()
    emit_method(module, main, receiver=receiver, qualifier=None, body=[f'return {helper}(e.{raw.name}({args}))'])
    w.blank()
    emit_method(module, raw, receiver=receiver, qualifier=None, body=[*pre, f'return {call}'])
  else:
    main = Method(name, params, [raw_stream, error], doc=docs, deprecated=endpoint.deprecated)
    methods.append(main)
    w.blank()
    emit_method(module, main, receiver=receiver, qualifier=None, body=[*pre, f'return {call}'])

  return EndpointModule(
    file=file, dirs=dirs, package=module.name, import_path=package.path(*dirs), struct_name=struct,
    constructor=constructor, contract='StreamEndpoint', transport='stream', methods=methods,
    source=module.render(doc=f'Package {module.name} is the `{endpoint.function}` stream.'),
  ), notes


# -- response paths ---------------------------------------------------------------------


@dataclass
class Reader:
  """The statements reading a response path, ending with the value in `var` of Go type
  `go_type` (tree `tree`); `early` when a statement returns `zero, false`."""
  lines: list[str]
  var: str
  tree: Type | None
  early: bool
  go_type: str | None = None
  """The Go type of `var` when the last hop was a record field of known rendering."""


def _record_field(module: Module, record: Type, key: str) -> tuple[Field | None, dict | None]:
  """A record's field as rendered (in this module, or laid out as its defining module
  renders it) without rendering anything here."""
  if key not in record['fields']:
    return None, None
  plan_field = record['fields'][key]
  ident = dict(module.field_layout(record))[key]
  rendered = next((f for f in module.fields.get(record['id'], []) if f.wire == key), None)
  if rendered is not None:
    return rendered, plan_field
  t = plan_field['type']
  optional = not plan_field['required']
  nullable = module.nullable(t)
  boxed = False
  target = module.resolved(t)
  if t['type'] == 'ref' and target is not None and target['type'] == 'record':
    boxed = t['id'] == record['id'] or module.reaches(t['id'], record['id'])
  if optional and nullable:
    shape = 'optional'
  elif (optional and not nullable and not module.nilable(t)) or (boxed and not nullable):
    shape = 'pointer'
  else:
    shape = 'value'
  return Field(key, ident, '', optional, nullable, shape, ''), plan_field


def read_path(module: Module, subject: str, subject_type: Type, path: str, *, counter: list[int]) -> Reader:
  """Statements reading a dotted/indexed path (`data.rows`, `[-1].id`, `[0]`) off `subject`.

  Every hop that may be absent (a nil pointer, an unset optional, a short list) returns
  `zero, false`, so the caller wraps the lines in a function returning `(T, bool)`.
  """
  lines: list[str] = []
  state: dict = {'early': False, 'go_type': None}

  def fresh(expr: str) -> str:
    counter[0] += 1
    var = f'v{counter[0]}'
    lines.append(f'{var} := {expr}')
    return var

  def bail(condition: str):
    state['early'] = True
    lines.extend((f'if {condition} {{', '\treturn zero, false', '}'))

  def normalize(var: str, t: Type | None, *, pointer: bool = False) -> tuple[str, Type | None]:
    """Unwrap `var` through the pointers its tree's nullability put there."""
    if pointer:
      bail(f'{var} == nil')
      var = fresh(f'*{var}')
    seen: set[str] = set()
    unwrapped = pointer
    while t is not None:
      if is_optional(t):
        inner = strip_null(t)
        # Go holds every layer of nullability (`X | null` through a nullable alias) in one
        # pointer, so only the first layer is checked and dereferenced.
        if not unwrapped:
          bail(f'{var} == nil')
          if not module.nilable(inner) and inner['type'] != 'scalar' or (inner['type'] == 'scalar' and not module.nilable(inner)):
            var = fresh(f'*{var}')
          unwrapped = True
        t = inner
      elif t['type'] == 'ref' and t['id'] not in seen:
        seen.add(t['id'])
        target = module.lookup(t['id'])
        if target is None or target['type'] == 'record':
          return var, t
        if target['type'] in ('literal', 'tuple') or (target['type'] == 'union' and not is_optional(target)):
          return var, t
        t = target
      else:
        break
    return var, t

  var = fresh(subject)
  var, t = normalize(var, subject_type)
  for index, key in _SEGMENT.findall(path):
    resolved = module.resolved(t)
    state['go_type'] = None
    if index:
      i = int(index)
      if resolved is not None and resolved['type'] == 'tuple':
        if not 0 <= i < len(resolved['items']):
          raise _Skipped(f'a response path indexes `[{i}]` outside a tuple')
        var, t = normalize(fresh(f'{var}.V{i}'), resolved['items'][i])
        continue
      if resolved is None or resolved['type'] != 'list':
        raise _Skipped('a response path indexes into something that is not a list')
      if i == -1:
        bail(f'len({var}) == 0')
        var = fresh(f'{var}[len({var})-1]')
      elif i >= 0:
        bail(f'len({var}) <= {i}')
        var = fresh(f'{var}[{i}]')
      else:
        raise _Skipped(f'a response path indexing `[{i}]` has no Go reading yet')
      var, t = normalize(var, resolved['item'])
      continue
    if resolved is None or resolved['type'] != 'record':
      raise _Skipped(f'a response path through `{key}` has no field to read in Go')
    rendered, plan_field = _record_field(module, resolved, key)
    if rendered is None or plan_field is None:
      raise _Skipped(f'a response path through `{key}` has no field to read in Go')
    access = f'{var}.{rendered.ident}'
    if rendered.shape != 'optional' and rendered.type and not is_optional(plan_field['type']):
      state['go_type'] = rendered.type[1:] if rendered.shape == 'pointer' and rendered.type.startswith('*') else rendered.type
    if rendered.shape == 'optional':
      bail(f'!{access}.Set')
      var, t = normalize(fresh(f'{access}.Value'), plan_field['type'])
    else:
      var, t = normalize(fresh(access), plan_field['type'], pointer=rendered.shape == 'pointer')
  return Reader(lines, var, t, state['early'], state['go_type'])


def _reader_function(module: Module, reader: Reader, go_type: str, *, result: str | None = None) -> list[str]:
  """The body of a `func() (T, bool)` over a reader."""
  body = [*reader.lines, f'return {result or reader.var}, true']
  if reader.early:
    body.insert(0, f'var zero {go_type}')
  return body


def _invoke(target: str, go_type: str, body: list[str]) -> list[str]:
  """`target := func() (T, bool) { body }()` as lines."""
  return [f'{target} := func() ({go_type}, bool) {{', *(f'\t{line}' for line in body), '}()']


def _leaf_type(module: Module, t: Type | None) -> str:
  if t is None:
    raise _Skipped('a response path leads to a value with no Go type')
  kind = t['type']
  if kind in ('scalar', 'ref'):
    return module.type_expr(t)
  if kind in ('list', 'dict'):
    return module.type_expr(t, path=['Row'])
  # An inline tuple, union or string literal is only nameable as the definition this file
  # already hoisted it under (a list payload's `Item`); hoisting a twin would not convert.
  hoisted = module.hoisted_name(t)
  if hoisted is not None:
    return hoisted
  raise _Skipped('a response path leads to an inline value with no nameable Go type')


def _row_type(module: Module, row_type: Type | None, rows: Reader) -> str:
  """The Go type of one row: a tuple row (bybit's klines) has no name of its own, so it is
  the item type the rows' record field was rendered with (`[]KlineResultListItem`)."""
  if row_type is not None and row_type['type'] == 'tuple' and rows.go_type and rows.go_type.startswith('[]'):
    return rows.go_type[2:]
  return _leaf_type(module, row_type)


def _cursor_result(module: Module, cursor: Reader, cursor_type: str, state: str) -> str:
  """The expression turning a read cursor (Go type `cursor_type`) into the walk's state."""
  if cursor_type == state:
    return cursor.var
  if state == f'{CORE}.IntegerString' and cursor_type == 'int64':
    return f'{module.core("IntegerStringOf")}({cursor.var})'
  if f'{CORE}.IntegerString' in (state, cursor_type) and cursor_type != 'string':
    raise _Skipped(f'a cursor of Go type `{cursor_type}` cannot be read as a `{state}` state')
  return f'{state}({cursor.var})'


@dataclass
class _UnionRows:
  """A token walk over a union payload: per variant (its field label on the payload struct),
  the reader of its rows and their Go item type, and the row type the walk yields."""
  variants: list[tuple[str, Type, Reader, str, str | None]]
  row: str


def _union_variants(module: Module, endpoint: EndpointPlan) -> list[tuple[str, Type]] | None:
  """The payload's variants with the field label its union struct gives each, or `None`
  when the payload is not a union of more than one variant."""
  if endpoint.response.payload is None or endpoint.response.optional:
    return None
  t = module.resolved({'type': 'ref', 'id': endpoint.response.payload})  # type: ignore[typeddict-item]
  if t is None or t['type'] != 'union' or is_optional(t) or len(t['variants']) < 2:
    return None
  taken: set[str] = set()
  return [(unique(module.variant_name(v['type']), taken), v['type']) for v in t['variants']]


def _union_rows(module: Module, pagination: PaginationPlan, variants: list[tuple[str, Type]], counter: list[int]) -> _UnionRows:
  """Every variant's rows, joined into one row type: the item type itself when every variant
  holds the same one, else a hoisted `PagedRow` union with one pointer per item type, as the
  Python backend joins them (bybit's `market.instruments`: one variant per category)."""
  # A `page`/`offset` walk joins the rows the same way, unless a `total` must be read too.
  indexed_ok = pagination.strategy in ('page', 'offset') and pagination.done.get('kind') != 'total'
  if (pagination.strategy != 'token' and not indexed_ok) or not pagination.rows:
    raise _Skipped(f'a `{pagination.strategy}` walk over a union payload has no Go walker yet')
  read: list[tuple[str, Type, Reader, str, Type]] = []
  items: list[Type] = []
  for label, variant in variants:
    reader = read_path(module, f'*response.{label}', variant, pagination.rows, counter=counter)
    tree = module.resolved(reader.tree)
    if tree is None or tree['type'] != 'list':
      raise _Skipped('a union variant whose rows are not a list has no Go walker')
    item = tree['item']
    item_go = reader.go_type[2:] if reader.go_type and reader.go_type.startswith('[]') else _leaf_type(module, item)
    read.append((label, variant, reader, item_go, item))
    if item not in items:
      items.append(item)
  if len(items) == 1:
    return _UnionRows([(label, variant, reader, item_go, None) for label, variant, reader, item_go, _ in read], read[0][3])
  row = module.type_expr({'type': 'union', 'variants': [{'type': item} for item in items]}, path=[f'{PAGED_SUFFIX}Row'])  # type: ignore[typeddict-item]
  taken: set[str] = set()
  labels = [unique(module.variant_name(item), taken) for item in items]
  return _UnionRows([(label, variant, reader, item_go, labels[items.index(item)]) for label, variant, reader, item_go, item in read], row)


def _is_absent_ok(lines: list[str]) -> list[str]:
  return lines


# -- pagination -------------------------------------------------------------------------


def _field_setter(rendered: Field, value_ptr: str) -> str:
  """The expression a request field of `rendered`'s shape takes from a pointer `value_ptr`."""
  if rendered.shape == 'optional':
    return f'{CORE}.Some({value_ptr})'
  if rendered.shape in ('pointer',) or rendered.type.startswith('*'):
    return value_ptr
  return f'*{value_ptr}'


def _paged(
  module: Module, endpoint: EndpointPlan, pagination: PaginationPlan, *, method: str, request_type: str | None,
) -> tuple[Method, list[str]]:
  """The walker's signature and body, or `_Skipped`."""
  strategy = pagination.strategy
  # An `offset` walk is walked by the rows each page held, so it only needs their type.
  if pagination.walker != 'paginated' and not (strategy == 'offset' and pagination.row_type is not None):
    raise _Skipped(f'an `{strategy}` walk with no resumable state has no Go walker; call the method per page')
  if request_type is None or endpoint.request.shape != 'fields' or request_type not in module.fields:
    raise _Skipped('a walk needs a flat request to advance; this one has none')
  if pagination.row_type is None or endpoint.response.payload is None:
    raise _Skipped('a walk whose rows have no type has no Go walker; call the method per page')
  if strategy == 'seek':
    return _paged_seek(module, endpoint, pagination, method=method, request_type=request_type)
  if strategy not in ('page', 'token', 'offset'):
    raise _Skipped(f'an `{strategy}` walk has no Go walker yet; call the method per page')

  fields = module.fields[request_type]
  driver = next((f for f in fields if f.wire == pagination.driver), None)
  if driver is None:
    raise _Skipped(f'the driver `{pagination.driver}` is not a field of `{request_type}`')
  state_tree = pagination.state_type
  if state_tree is None or state_tree['type'] != 'scalar' or state_tree['base'] not in ('integer', 'number', 'string', 'boolean'):
    raise _Skipped('a cursor of this type has no Go walker yet; call the method per page')
  # A `token` cursor may be a timestamp (deribit's `continuation`, an `epoch-millis` sent
  # back as `end_timestamp`): its zero time is the absent cursor.
  timed = strategy == 'token' and module.type_expr(state_tree).startswith(f'{CORE}.Timestamp')
  if state_tree.get('format') in FORMATS and state_tree.get('format') not in ('integer-string', 'decimal-string', 'boolean-string') and not timed:
    raise _Skipped('a cursor of this type has no Go walker yet; call the method per page')
  indexed = strategy in ('page', 'offset')
  if indexed and state_tree['base'] != 'integer':
    raise _Skipped(f'a `{strategy}` index that is not an integer has no Go walker')
  state = module.type_expr(state_tree)
  plan_fields = module.local[request_type]['fields']
  paged_name = f'{method}{PAGED_SUFFIX}'
  counter = [0]
  payload_type: Type = {'type': 'ref', 'id': endpoint.response.payload}  # type: ignore[typeddict-item]
  if endpoint.response.optional:
    payload_type = {'type': 'union', 'variants': [{'type': payload_type}, {'type': {'type': 'scalar', 'base': 'null'}}]}
  variants = _union_variants(module, endpoint)
  union = _union_rows(module, pagination, variants, counter) if variants is not None else None
  if union is not None:
    row = union.row
  else:
    row = _row_type(module, pagination.row_type, read_path(module, 'response', payload_type, pagination.rows or '', counter=[0]))

  # The walker's request type: `Request` without the driver, unless the caller seeds it.
  own_request = indexed or not pagination.driver_required
  paged_type = request_type
  if own_request:
    paged_type = unique(f'{PAGED_SUFFIX}Request', module.names)
    others = [f for f in fields if f.wire != pagination.driver]
    w = module.writer
    w.blank()
    w.doc(f'{paged_type} is {request_type} without `{pagination.driver}`, which {paged_name} advances.')
    w.struct(f'type {paged_type} struct {{', [
      *((f.ident, f.type, plan_fields[f.wire].get('docstring')) for f in others),
      ('Extra', f'map[string]{module.raw_json()}', 'Extra holds keys the spec does not document, sent as they are.'),
    ])
    w.blank()
    entries = ', '.join([*(f'{f.ident}: p.{f.ident}' for f in others), f'{driver.ident}: {driver.ident[0].lower()}{driver.ident[1:]}Value', 'Extra: p.Extra'])
    w.doc(f'at is the {request_type} for one page of the walk.')
    value_name = f'{driver.ident[0].lower()}{driver.ident[1:]}Value'
    with w.block(f'func (p {paged_type}) at({value_name} {driver.type}) {request_type} {{'):
      w.line(f'return {request_type}{{{entries}}}')

  body: list[str] = []
  size_var = '-1'
  # An `offset` walk ended by a `total` of items never measures the page size.
  counts_items = strategy == 'offset' and pagination.done.get('kind') == 'total' and pagination.done.get('counts') != 'pages'
  # An `empty` walk ends on a page with no rows, whatever its size.
  if indexed and not counts_items and pagination.done.get('kind') != 'empty':
    size_var = _size(body, module, pagination, fields, plan_fields)
  cursor_var = 'state'
  body.append(f'next := func(ctx context.Context, {cursor_var} {state}) ([]{row}, *{state}, error) {{')
  inner: list[str] = []
  if own_request:
    zero_is_absent = strategy == 'token'
    if driver.shape == 'optional':
      value = f'{CORE}.Some(&{cursor_var})'
    elif driver.shape == 'pointer' or driver.type.startswith('*'):
      value = f'&{cursor_var}'
    else:
      value = cursor_var
    if zero_is_absent and value != cursor_var:
      zero = {'string': '""', 'boolean': 'false'}.get(state_tree['base'], '0')
      present = f'!{cursor_var}.IsZero()' if timed else f'{cursor_var} != {zero}'
      inner.extend((f'var at {driver.type}', f'if {present} {{', f'\tat = {value}', '}'))
      value = 'at'
    inner.append(f'response, err := e.{method}(ctx, request.at({value}), opts...)')
  else:
    inner.extend(('request := request', f'request.{driver.ident} = {cursor_var}', f'response, err := e.{method}(ctx, request, opts...)'))
  inner.extend(('if err != nil {', '\treturn nil, nil, err', '}'))
  if union is not None:
    inner.extend((f'rows := func() []{row} {{', f'\tvar out []{row}'))
    for label, _, reader, item_go, row_label in union.variants:
      inner.append(f'\tif response.{label} != nil {{')
      inner.extend(f'\t\t{line}' for line in _invoke('variantRows, _', f'[]{item_go}', _reader_function(module, reader, f'[]{item_go}')))
      if row_label is None:
        inner.append('\t\tout = append(out, variantRows...)')
      else:
        inner.extend(('\t\tfor i := range variantRows {', f'\t\t\tout = append(out, {row}{{{row_label}: &variantRows[i]}})', '\t\t}'))
      inner.append('\t}')
    inner.extend(('\treturn out', '}()'))
  else:
    rows = read_path(module, 'response', payload_type, pagination.rows or '', counter=counter)
    if not rows.lines[1:] and not rows.early:
      inner.append(f'rows := {rows.var if rows.var != "v1" else "response"}')
    else:
      inner.extend(_invoke('rows, _', f'[]{row}', _reader_function(module, rows, f'[]{row}')))
  if indexed:
    # A page number starts at `index.start` and steps by one; a row offset starts at 0 and
    # steps by the rows the page held.
    start = (pagination.start if pagination.start is not None else 1) if strategy == 'page' else 0
    done = pagination.done.get('kind')
    if done == 'total':
      total = read_path(module, 'response', payload_type, str(pagination.done.get('path', '')), counter=counter)
      total_type = _leaf_type(module, total.tree)
      # A `number` total (deribit's `records_total`) counts whole rows; `int64(total)` reads it.
      if total_type not in ('int64', 'float64', f'{CORE}.IntegerString', 'string'):
        raise _Skipped('a `total` that is not an integer has no Go walker yet')
      inner.extend(_invoke('total, found', total_type, _reader_function(module, total, total_type)))
      total_n = 'int64(total)'
      if total_type == 'string':
        # An integer sent as a string (bybit's `lastPage`); one that is not a number never ends the walk.
        module.std('strconv')
        inner.extend(('totalN, parseErr := strconv.ParseInt(total, 10, 64)', 'found = found && parseErr == nil'))
        total_n = 'totalN'
      if total_type == f'{CORE}.IntegerString':
        # Exact on the wire; a total beyond int64 cannot be reached, so it never ends the walk.
        inner.extend(('totalN, fits := total.Int64()', 'found = found && fits'))
        total_n = 'totalN'
      pages = pagination.done.get('counts') == 'pages'
      if strategy == 'page':
        condition = f'len(rows) == 0 || (found && {CORE}.TotalReached({"true" if pages else "false"}, int64({cursor_var}), {start}, {size_var}, len(rows), {total_n}))'
      elif not pages:
        condition = f'len(rows) == 0 || (found && int64({cursor_var})+int64(len(rows)) >= {total_n})'
      elif size_var != '-1':
        # The starting offset proves this is the final page even after unaligned resume.
        condition = f'len(rows) == 0 || (found && {size_var} > 0 && int64({cursor_var})/int64({size_var})+1 >= {total_n})'
      else:
        condition = 'len(rows) == 0'
    elif done == 'empty':
      # `empty` ends on the first page with no rows only; a short page may still be followed
      # by more (Python and TypeScript render the same rule).
      condition = 'len(rows) == 0'
    else:
      condition = f'{CORE}.Exhausted(len(rows), {size_var})'
    step = '1' if strategy == 'page' else f'{state}(len(rows))'
    inner.extend((f'if {condition} {{', '\treturn rows, nil, nil', '}', f'following := {cursor_var} + {step}', 'return rows, &following, nil'))
    seed = f'{state}({start})'
  else:
    if pagination.done.get('kind') == 'empty':
      # An `empty` token walk ends on the first page with no rows, whatever cursor it carries.
      inner.extend(('if len(rows) == 0 {', '\treturn rows, nil, nil', '}'))
    if union is not None:
      # Read off whichever variant is set; a variant without the cursor ends the walk.
      reading = [f'following, found := func() ({state}, bool) {{', f'\tvar zero {state}']
      carried = False
      for label, variant, _, _, _ in union.variants:
        try:
          cursor = read_path(module, f'*response.{label}', variant, pagination.cursor_from or '', counter=counter)
        except _Skipped:
          continue
        result = _cursor_result(module, cursor, _leaf_type(module, cursor.tree), state)
        carried = True
        reading.append(f'\tif response.{label} != nil {{')
        reading.extend(f'\t\t{line}' for line in cursor.lines)
        reading.extend((f'\t\treturn {result}, true', '\t}'))
      if not carried:
        raise _Skipped('no variant of the union payload carries the cursor')
      inner.extend((*reading, '\treturn zero, false', '}()'))
    else:
      cursor = read_path(module, 'response', payload_type, pagination.cursor_from or '', counter=counter)
      result = _cursor_result(module, cursor, _leaf_type(module, cursor.tree), state)
      inner.extend(_invoke('following, found', state, _reader_function(module, cursor, state, result=result)))
    if timed:
      inner.extend(('if !found || following.IsZero() {', '\treturn rows, nil, nil', '}', 'return rows, &following, nil'))
    else:
      inner.extend(('if !found {', '\treturn rows, nil, nil', '}', f'return rows, {CORE}.CursorOrDone(&following), nil'))
    if pagination.driver_required:
      seed = f'request.{driver.ident}'
    else:
      zero = {'string': '""', 'boolean': 'false'}.get(state_tree['base'], '0')
      seed = f'{state}{{}}' if timed else f'{state}({zero})'
  body.extend(f'\t{line}' for line in inner)
  body.append('}')
  body.append(f'return {CORE}.NewPaginatedResponse({seed}, next)')
  module.core('NewPaginatedResponse')
  module.std('context')
  returns: Tokens = [('text', '*'), ('core', 'PaginatedResponse'), ('text', '['), *tokenize(module, row), ('text', ', '), *tokenize(module, state), ('text', ']')]
  docs = [*method_docs(endpoint)[:1], f'{paged_name} is the paged variant of {method}: All for every row, or range over Rows/Pages one page at a time.', *method_docs(endpoint)[1:]]
  signature = Method(paged_name, [('request', [('local', paged_type)])], [returns], context=False, doc=docs, deprecated=endpoint.deprecated, kind='paged')
  return signature, body


def _size(body: list[str], module: Module, pagination: PaginationPlan, fields: list[Field], plan_fields) -> str:
  """Bind `size` (an `int`, -1 when unknown) from the page-size field; returns its name."""
  if pagination.size is None:
    return '-1'
  rendered = next((f for f in fields if f.wire == pagination.size), None)
  if rendered is None:
    return '-1'
  tree = strip_null(plan_fields[pagination.size]['type'])
  if tree['type'] == 'scalar' and tree['base'] == 'string' and tree.get('format') not in FORMATS:
    # A size sent as a string (bybit's `size`): its number, or unknown when it is not one.
    module.std('strconv')
    default = str(pagination.size_default) if str(pagination.size_default).isdigit() else '-1'
    if rendered.shape == 'value':
      guard, access = None, f'request.{rendered.ident}'
    elif rendered.shape == 'optional':
      guard, access = f'request.{rendered.ident}.Set && request.{rendered.ident}.Value != nil', f'*request.{rendered.ident}.Value'
    else:
      guard, access = f'request.{rendered.ident} != nil', f'*request.{rendered.ident}'
    parse = [f'if n, err := strconv.Atoi({access}); err == nil {{', f'\tsize = {_clamped(pagination, "n")}', '}']
    body.append(f'size := {default}')
    if guard is None:
      body.extend(parse)
    else:
      body.extend((f'if {guard} {{', *(f'\t{line}' for line in parse), '}'))
    return 'size'
  if tree['type'] != 'scalar' or tree['base'] != 'integer' or tree.get('format') in FORMATS:
    raise _Skipped(f'a page size of type other than an integer (`{pagination.size}`) has no Go walker yet')
  default = pagination.size_default if pagination.size_default is not None else -1
  if rendered.shape == 'value':
    body.append(f'size := {_clamped(pagination, f"int(request.{rendered.ident})")}')
  elif rendered.shape == 'optional':
    body.extend((f'size := {default}', f'if request.{rendered.ident}.Set && request.{rendered.ident}.Value != nil {{', f'\tsize = {_clamped(pagination, f"int(*request.{rendered.ident}.Value)")}', '}'))
  else:
    body.extend((f'size := {default}', f'if request.{rendered.ident} != nil {{', f'\tsize = {_clamped(pagination, f"int(*request.{rendered.ident})")}', '}'))
  return 'size'


def _seek_size(pagination: PaginationPlan, expr: str) -> str:
  """The page size a `seek` walk sends for the caller's `expr`: at most the schema's
  `maximum`, and at least 2 (a page of 1 cannot advance past an inclusive moving bound)."""
  return f'min(max({expr}, 2), {pagination.size_maximum})' if pagination.size_maximum is not None else f'max({expr}, 2)'


def _seek_size_rule(pagination: PaginationPlan) -> str:
  """The doc sentence stating `_seek_size`'s clamp. A maximum below 2 leaves no room for the
  floor, so the sentence then claims none."""
  maximum = pagination.size_maximum
  if maximum is not None and maximum < 2:
    return f'The walk requests pages of {maximum} row{"" if maximum == 1 else "s"}.'
  bounds = f'at least 2 rows and at most {pagination.size_maximum}' if pagination.size_maximum is not None else 'at least 2 rows'
  return f'The walk requests pages of {bounds}: a page must hold one new row beside the one it re-reads.'


def _clamped(pagination: PaginationPlan, expr: str) -> str:
  """The caller's page size as the API serves it: at most the schema's `maximum`, so a page
  full at the maximum is not read as short (or, for `seek`, as not full)."""
  return f'min({expr}, {pagination.size_maximum})' if pagination.size_maximum is not None else expr


def _key_kind(key_type: str) -> str | None:
  if key_type in ('int64', 'float64'):
    return 'ordered'
  if key_type == 'string':
    return 'string'
  if key_type in (f'{CORE}.Decimal', f'{CORE}.IntegerString'):
    return 'decimal'
  if key_type.startswith(f'{CORE}.Timestamp') or key_type == f'{CORE}.DateIso':
    return 'time'
  return None


def _convert_key(module: Module, var: str, leaf: str, key: str) -> tuple[list[str], str] | None:
  """Statements and the expression turning a row's cursor value (Go type `leaf`) into the
  moving bound's type `key`, or `None` when no conversion is known."""
  if leaf == key:
    return [], var
  core = f'{CORE}.'
  key_kind, leaf_kind = _key_kind(key), _key_kind(leaf)
  if key_kind == 'time':
    name = key[len(core):]
    if leaf_kind == 'time':
      return [], f'{key}{{Time: {var}.Time}}'
    converter = _EPOCH_CONVERTERS.get(name)
    if converter is not None and leaf in ('int64', 'float64'):
      return [], f'{key}{{Time: {module.core(converter)}.FromEpoch(int64({var}))}}'
    if converter is not None and leaf == f'{CORE}.IntegerString':
      return [f'parsed, ok := {var}.Int64()', 'if !ok {', '\treturn zero, false', '}'], f'{key}{{Time: {module.core(converter)}.FromEpoch(parsed)}}'
    if converter is not None and leaf in ('string', f'{CORE}.Decimal'):
      return [f'parsed, err := {module.core(converter)}.Parse(string({var}))', 'if err != nil {', '\treturn zero, false', '}'], f'{key}{{Time: parsed}}'
    if name == 'TimestampIso' and leaf == 'string':
      return [f'parsed, err := {module.core("ParseDateTime")}({var})', 'if err != nil {', '\treturn zero, false', '}'], f'{key}{{Time: parsed}}'
    return None
  if key == f'{CORE}.IntegerString':
    if leaf == 'int64':
      return [], f'{module.core("IntegerStringOf")}({var})'
    if leaf == 'string':
      return [f'parsed, err := {module.core("ParseIntegerString")}({var})', 'if err != nil {', '\treturn zero, false', '}'], 'parsed'
    return None
  if key == 'int64':
    if leaf in ('int64', 'float64'):
      return [], f'{key}({var})'
    if leaf == f'{CORE}.IntegerString':
      return [f'parsed, ok := {var}.Int64()', 'if !ok {', '\treturn zero, false', '}'], 'parsed'
    if leaf == 'string':
      module.std('strconv')
      return [f'parsed, err := strconv.ParseInt({var}, 10, 64)', 'if err != nil {', '\treturn zero, false', '}'], f'{key}(parsed)'
    return None
  if key == 'float64':
    if leaf == 'int64':
      return [], f'float64({var})'
    if leaf in ('string', f'{CORE}.Decimal', f'{CORE}.IntegerString'):
      module.std('strconv')
      return [f'parsed, err := strconv.ParseFloat(string({var}), 64)', 'if err != nil {', '\treturn zero, false', '}'], 'parsed'
    return None
  if key == 'string' and leaf == f'{CORE}.Decimal':
    return [], f'string({var})'
  if key == 'string' and leaf == 'int64':
    module.std('strconv')
    return [], f'strconv.FormatInt({var}, 10)'
  return None


def _paged_seek(
  module: Module, endpoint: EndpointPlan, pagination: PaginationPlan, *, method: str, request_type: str,
) -> tuple[Method, list[str]]:
  """A `seek` walk (ADR 0013), rendered over `truewire.Seek`."""
  seek = pagination.seek or {}
  fields = module.fields[request_type]
  plan_fields = module.local[request_type]['fields']
  moving = next((f for f in fields if f.wire == seek.get('moving')), None)
  far = next((f for f in fields if f.wire == seek.get('far')), None) if seek.get('far') else None
  if moving is None:
    raise _Skipped(f'the seek bound `{seek.get("moving")}` is not a field of `{request_type}`')
  if seek.get('far') and far is None:
    raise _Skipped(f'the seek bound `{seek.get("far")}` is not a field of `{request_type}`')
  key_type = module.type_expr(strip_null(plan_fields[moving.wire]['type']))
  kind = _key_kind(key_type)
  if kind is None:
    raise _Skipped(f'a seek bound of Go type `{key_type}` has no Go walker yet')
  paged_name = f'{method}{PAGED_SUFFIX}'
  exclusive = seek.get('exclusive')
  firsts: list[Field] = []
  if exclusive:
    if moving.shape != 'pointer':
      raise _Skipped(f'the exclusive parameters are refused beside `{moving.wire}`, which is not an optional field, so they could never be sent')
    for name in exclusive['parameters']:
      first = next((f for f in fields if f.wire == name), None)
      if first is None:
        raise _Skipped(f'the exclusive parameter `{name}` is not a field of `{request_type}`')
      if first.shape != 'pointer':
        raise _Skipped(f'the exclusive parameter `{name}` is not a plain optional field, so the walk cannot leave it out')
      firsts.append(first)
  counter = [0]
  payload_type: Type = {'type': 'ref', 'id': endpoint.response.payload}  # type: ignore[typeddict-item]
  if endpoint.response.optional:
    payload_type = {'type': 'union', 'variants': [{'type': payload_type}, {'type': {'type': 'scalar', 'base': 'null'}}]}
  row = _row_type(module, pagination.row_type, read_path(module, 'response', payload_type, pagination.rows or '', counter=[0]))
  cursor = seek.get('cursor') or {}
  field_path = str(cursor.get('field', ''))
  if not field_path.startswith('[-1]'):
    raise _Skipped('a seek cursor field that is not relative to the last row has no Go walker')
  row_path = field_path[len('[-1]'):]
  core = module.core
  body: list[str] = [f'var walk {core("Seek")}[{key_type}, {row}]', f'walk.Walker = {string(paged_name)}']
  if cursor.get('unique'):
    body.append('walk.Unique = true')
  if seek.get('descending'):
    body.append('walk.Descending = true')
  if kind == 'ordered':
    module.std('cmp')
    body.extend(('walk.Ordered = true', f'walk.Compare = cmp.Compare[{key_type}]'))
  elif kind == 'string':
    module.std('strings')
    body.append('walk.Compare = strings.Compare')
  elif kind == 'decimal':
    body.extend(('walk.Ordered = true', f'walk.Compare = func(a, b {key_type}) int {{ return a.Compare(b) }}'))
  else:
    body.extend(('walk.Ordered = true', f'walk.Compare = func(a, b {key_type}) int {{ return a.Compare(b.Time) }}'))

  # The cap a full page is measured against: the caller's size, else the declared cap,
  # else the size parameter's documented default, else unknown. A given size is clamped
  # once, into the request every page sends: at most the schema's `maximum`, and at least
  # 2, since a page must hold one new row beside the boundary row it re-reads.
  fallback = seek.get('cap') if seek.get('cap') is not None else pagination.size_default
  body.append(f'walk.Cap = {fallback if fallback is not None else -1}')
  size_field = next((f for f in fields if f.wire == pagination.size), None) if pagination.size else None
  clamps_size = False
  if size_field is not None:
    size_tree = strip_null(plan_fields[size_field.wire]['type'])
    if size_tree['type'] == 'scalar' and size_tree['base'] == 'integer' and size_tree.get('format') not in FORMATS:
      clamps_size = True
      if size_field.shape == 'value':
        # A required size left at Go's zero value is unset: it goes on the wire as it is and
        # the cap keeps the fallback, rather than becoming a walk of 2-row pages.
        body.extend((
          f'if request.{size_field.ident} != 0 {{',
          f'\trequest.{size_field.ident} = {_seek_size(pagination, f"request.{size_field.ident}")}',
          f'\twalk.Cap = int(request.{size_field.ident})', '}',
        ))
      else:
        # A new value, not a write through the pointer, which is the caller's own.
        given = f'request.{size_field.ident}.Value' if size_field.shape == 'optional' else f'request.{size_field.ident}'
        guard = f'request.{size_field.ident}.Set && {given} != nil' if size_field.shape == 'optional' else f'{given} != nil'
        body.extend((
          f'if {guard} {{', f'\tsize := {_seek_size(pagination, f"*{given}")}', f'\t{given} = &size',
          '\twalk.Cap = int(size)', '}',
        ))

  def pointer_to(f: Field) -> tuple[list[str], str]:
    """Statements and a `*K` expression holding the caller's own value of bound `f`."""
    if f.shape == 'optional':
      return [], f'request.{f.ident}.Value'
    if f.shape == 'pointer' or f.type.startswith('*'):
      return [], f'request.{f.ident}'
    return [], f'&request.{f.ident}'

  if far is not None:
    lines, expr = pointer_to(far)
    body.extend(lines)
    body.append(f'walk.Far = {expr}')
  span = seek.get('span')
  if span:
    if far is None:
      raise _Skipped('a seek span needs both bounds declared')
    body.append(f'span := int64({int(span["default"])})')
    body.extend((f'if options := {core("Options")}(opts...); options.Span != 0 {{', '\tspan = options.Span', '}'))
    sign = '-' if seek.get('descending') else '+'
    if kind == 'time':
      module.std('time')
      unit = _TIME_UNITS[span.get('unit', 'ms')]
      if seek.get('descending'):
        edge = f'{key_type}{{Time: pos.Add(-time.Duration(span) * {unit})}}'
      else:
        edge = f'{key_type}{{Time: pos.Add(time.Duration(span) * {unit})}}'
    elif kind == 'ordered':
      edge = f'pos {sign} {key_type}(span)'
    elif key_type == f'{CORE}.IntegerString':
      edge = f'pos.Add({"-" if seek.get("descending") else ""}span)'
    else:
      raise _Skipped('a seek span over a bound that is neither a number nor a timestamp has no Go walker')
    body.append(f'walk.Edge = func(pos {key_type}) {key_type} {{ return {edge} }}')

  row_tree: Type = pagination.row_type  # type: ignore[assignment]

  def row_reader(path: str, target: str, what: str) -> list[str]:
    """The body of a `func(row) (target, bool)` reading one row's field at `path` (relative
    to the row) as the parameter type `target` it is compared with."""
    reader = read_path(module, 'row', row_tree, path, counter=counter)
    union = module.resolved(reader.tree)
    if union is not None and union['type'] == 'union' and not is_optional(union) and all(v['type']['type'] == 'scalar' for v in union['variants']):
      # A field that is one of several scalars (mexc's trade `id`, an integer or a string):
      # read whichever variant is set, each converted to the parameter's type.
      lines = [*reader.lines]
      taken: set[str] = set()
      for variant in union['variants']:
        label = unique(module.variant_name(variant['type']), taken)
        value = f'(*{reader.var}.{label})'
        converted = _convert_key(module, value, module.type_expr(variant['type']), target)
        if converted is None:
          raise _Skipped(f'a seek {what} variant `{label}` cannot be read as `{target}`')
        conversion, result = converted
        lines.extend((f'if {reader.var}.{label} != nil {{', *(f'\t{line}' for line in conversion), f'\treturn {result}, true', '}'))
      lines.extend((f'return zero, false',))
      lines.insert(0, f'var zero {target}')
      return lines
    leaf = _leaf_type(module, reader.tree)
    converted = _convert_key(module, reader.var, leaf, target)
    if converted is None:
      raise _Skipped(f'a seek {what} of Go type `{leaf}` cannot be read as `{target}`')
    conversion, result = converted
    lines = [*reader.lines, *conversion, f'return {result}, true']
    if reader.early or conversion:
      lines.insert(0, f'var zero {target}')
    return lines

  # Key: one row's cursor field, as the moving bound's type.
  body.append(f'walk.Key = func(row {row}) ({key_type}, bool) {{')
  body.extend(f'\t{line}' for line in row_reader(row_path, key_type, 'cursor'))
  body.append('}')

  # Past: a far bound the venue refuses beside the moving one, kept on the rows instead.
  far_exclusive = exclusive.get('far') if exclusive else None
  if far_exclusive:
    until = next(f for f in firsts if f.wire == far_exclusive['parameter'])
    until_type = module.type_expr(strip_null(plan_fields[until.wire]['type']))
    until_kind = _key_kind(until_type)
    beyond = '<' if seek.get('descending') else '>'
    if until_kind == 'ordered':
      past = f'value {beyond} until'
    elif until_kind == 'time':
      past = f'value.Compare(until.Time) {beyond} 0'
    elif until_kind == 'decimal':
      past = f'value.Compare(until) {beyond} 0'
    else:
      raise _Skipped(f'the exclusive far bound `{until.wire}` of Go type `{until_type}` cannot be compared with a row')
    until_path = str(far_exclusive['field'])[len('[-1]'):]
    body.append(f'if request.{until.ident} != nil {{')
    body.append(f'\tuntil := *request.{until.ident}')
    body.append(f'\tat := func(row {row}) ({until_type}, bool) {{')
    body.extend(f'\t\t{line}' for line in row_reader(until_path, until_type, 'far bound field'))
    body.append('\t}')
    body.append(f'\twalk.Past = func(row {row}) bool {{')
    body.append('\t\tvalue, ok := at(row)')
    body.append(f'\t\treturn ok && {past}')
    body.append('\t}')
    body.append('}')

  # Fetch: the request with the moving bound at pos and, with a span, the far bound at edge.
  module.std('context')
  body.append(f'walk.Fetch = func(ctx context.Context, pos *{key_type}, edge *{key_type}) ([]{row}, error) {{')
  inner: list[str] = []
  if firsts:
    # The caller may give the moving bound or the non-far exclusive parameters, never both.
    # The far one is allowed beside it: it is then never sent, and kept on the rows.
    refused_fields = [first for first in firsts if not far_exclusive or first.wire != far_exclusive['parameter']]
    if refused_fields:
      given = ' || '.join(f'request.{first.ident} != nil' for first in refused_fields)
      given = f'({given})' if len(refused_fields) > 1 else given
      listed = '/'.join(f'`{first.wire}`' for first in refused_fields)
      refused = f'`{paged_name}` walks by `{moving.wire}`, which the venue refuses alongside {listed}: pass one or the other'
      inner.extend((
        f'if request.{moving.ident} != nil && {given} {{',
        f'\treturn nil, {core("LogicError")}({string(refused)})', '}',
      ))
    first = next((f for f in firsts if f.wire == exclusive.get('first')), None) if exclusive else None
    if first is not None:
      missing = (
        f'`{paged_name}` needs `{moving.wire}` or `{first.wire}` to start from: without either the venue '
        f'answers from the wrong end of the range'
      )
      inner.extend((
        f'if request.{moving.ident} == nil && request.{first.ident} == nil {{',
        f'\treturn nil, {core("LogicError")}({string(missing)})', '}',
      ))
  inner.append('request := request')

  def assign(f: Field, ptr: str) -> list[str]:
    if f.shape == 'optional':
      return [f'request.{f.ident} = {CORE}.Some({ptr})']
    if f.shape == 'pointer' or f.type.startswith('*'):
      return [f'request.{f.ident} = {ptr}']
    return [f'if {ptr} != nil {{', f'\trequest.{f.ident} = *{ptr}', '}']

  inner.extend(assign(moving, 'pos'))
  if span and far is not None:
    inner.extend(assign(far, 'edge'))
  if firsts:
    # Only while the walk has no position of its own: the venue refuses them beside it.
    inner.append('if pos != nil {')
    inner.extend(f'\trequest.{first.ident} = nil' for first in firsts)
    inner.append('}')
  inner.extend((f'response, err := e.{method}(ctx, request, opts...)', 'if err != nil {', '\treturn nil, err', '}'))
  rows = read_path(module, 'response', payload_type, pagination.rows or '', counter=counter)
  if not rows.lines[1:] and not rows.early:
    inner.append('return response, nil')
  else:
    inner.extend(_invoke('rows, _', f'[]{row}', _reader_function(module, rows, f'[]{row}')))
    inner.append('return rows, nil')
  body.extend(f'\t{line}' for line in inner)
  body.append('}')
  lines, start = pointer_to(moving)
  body.extend(lines)
  body.append(f'return walk.Response({start})')

  state: Tokens = [('core', 'SeekState'), ('text', '['), *tokenize(module, key_type), ('text', ', '), *tokenize(module, row), ('text', ']')]
  returns: Tokens = [('text', '*'), ('core', 'PaginatedResponse'), ('text', '['), *tokenize(module, row), ('text', ', '), *state, ('text', ']')]
  docs = [
    *method_docs(endpoint)[:1],
    f'{paged_name} is the paged variant of {method}, a `seek` walk: All for every row, or range over Rows/Pages one page at a time. '
    f'The walk moves `{moving.wire}`{" newest-first" if seek.get("descending") else ""} and never requests outside the range the request gives.'
    + (f' {_seek_size_rule(pagination)}' if clamps_size else '')
    + ''.join(f' {sentence}' for sentence in plan_exclusive_sentences(exclusive, moving=moving.wire)),
    *method_docs(endpoint)[1:],
  ]
  signature = Method(paged_name, [('request', [('local', request_type)])], [returns], context=False, doc=docs, deprecated=endpoint.deprecated, kind='paged')
  return signature, body


__all__ = [
  'EndpointModule', 'Method', 'PAGED_SUFFIX', 'RAW_SUFFIX', 'Reader', 'Tokens', 'emit_method',
  'endpoint_dirs', 'method_docs', 'read_path', 'render_endpoint', 'render_stream_endpoint',
  'render_tokens', 'tokenize',
]
