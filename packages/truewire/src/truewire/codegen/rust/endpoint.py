"""One endpoint module: its types, and a struct holding the core behind the contract
trait with the method, its `_raw` twin, and the `<method>_paged` walker when pagination
is declared.

The struct holds its core as an `Arc<dyn HttpEndpoint<Meta>>` and calls exactly one verb
on it. The typed method dumps the request, hands the core an `HttpCall`, and `decode`s the
reply; the `_raw` twin returns the `serde_json::Value` the core returned, which is the Rust
form of `validate: false` (`docs/rust.md`: a second method, since no overload can return a
different type). The request struct is the wire object under `snake_case` names, and the
options are the runtime's `CallOptions`.
"""
import re
from dataclasses import dataclass, field

from truewire.plan.model import EndpointPlan, PackagePlan, PaginationPlan
from truewire.plan.types import Type, is_optional, strip_null

from .meta import META_FILE, MetaShape
from .names import json_expr, pascal_case, snake_ident, string
from .printer import BANNER, Writer
from .types import CORE, EXTRA_FIELD, Field, Module, scope_module, visible_scopes

PAGED_SUFFIX = '_paged'
RAW_SUFFIX = '_raw'

Tokens = list[tuple[str, str]]
"""A type in a method signature, kept symbolic so a router can render it qualified:
`('local', 'Request')` for a name the endpoint module defines, `('shared',
'crate::types::Label')` for one a shared scope defines, `('core', 'CallOptions')` for a
runtime name, `('std', 'HashMap')`, `('json', 'serde_json::Value')`, and `('text', '<')`
for everything else."""

_CORE_NAMES = frozenset((
  'DecimalString', 'IntegerString', 'BooleanString', 'TimestampSeconds', 'TimestampMillis',
  'TimestampMicros', 'TimestampNanos', 'TimestampIso', 'DateIso',
))
_TOKEN = re.compile(r'serde_json::[A-Za-z]+|[A-Za-z_][A-Za-z0-9_]*|[^A-Za-z_]+')
_SEGMENT = re.compile(r'\[(-?\d+)\]|([^.\[\]]+)')

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


@dataclass(frozen=True)
class EndpointModule:
  file: str
  struct_name: str
  bound: str
  """The contract the struct's core satisfies: `HttpEndpoint<DefaultMeta>`, `HttpEndpoint`."""
  meta_type: str | None
  methods: list[Method]
  source: str


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
      module.core(text)
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


def method_docs(endpoint: EndpointPlan) -> list[str | None]:
  docs = [endpoint.docs.description]
  if endpoint.docs.url:
    docs.append(f'See <{endpoint.docs.url}>.')
  return docs


def emit_method(module: Module, method: Method, *, qualifier: str | None, body: list[str] | None = None):
  """`method`'s docs, attributes and signature; `body` lines inside it, or a delegating
  call when `body` is `None` (a router)."""
  w = module.writer
  w.doc(*method.doc)
  if method.deprecated:
    w.line('#[deprecated]')
    if body is None:
      w.line('#[allow(deprecated)]')
  params = ['&self', *(f'{name}: {render_tokens(module, tokens, qualifier=qualifier)}' for name, tokens in method.params)]
  head = f'pub {"async " if method.is_async else ""}fn {method.name}'
  returns = render_tokens(module, method.returns, qualifier=qualifier)
  w.signature(head, params, f' -> {returns} {{')
  with w.indented():
    if body is None:
      args = ', '.join(name for name, _ in method.params)
      elements = [f'.{qualifier}', f'.{method.name}({args})']
      if method.is_async:
        elements.append('.await')
      w.chain('', 'self', elements)
    else:
      for line in body:
        w.line(line)
  w.line('}')


# -- the module -------------------------------------------------------------------------


@dataclass
class _Skipped(Exception):
  """Raised inside a walker renderer for a declaration this backend has no walker for."""
  reason: str


def render_stream_endpoint(
  plan: PackagePlan, endpoint: EndpointPlan, *, struct_name: str, meta: MetaShape | None,
) -> tuple[EndpointModule, list[str]]:
  """Render one `stream` endpoint's module: `subscribe`, typed and raw.

  The shape mirrors the HTTP path -- a typed method that decodes each pushed frame and a
  `_raw` twin that hands them over as they came -- because a subscription is the same
  contract as a call, spread over time. `Stream::map` is what turns one into the other.
  """
  file = endpoint_file(endpoint.path)
  request = endpoint.request
  module = Module(plan, file, local=dict(endpoint.types), visible=visible_scopes(plan, endpoint.path))
  module.define_all(endpoint.types)
  w = module.writer
  notes: list[str] = []

  channel = endpoint.wire.channel or ''
  if request.type is None and request.fields:
    raise _Skipped('a stream whose parameters only fill its channel template has no Rust rendering yet')

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
  if request.type is not None:
    module.core('dump')

  method = snake_ident(endpoint.path[-1], fallback='subscribe')
  message = endpoint.response.payload
  docs = method_docs(endpoint)

  params: list[tuple[str, Tokens]] = []
  if request.type is not None:
    params.append(('parameters', tokenize(module, request.type)))
  params.append(('options', core_tokens('CallOptions')))
  message_tokens: Tokens = tokenize(module, message) if message else [('json', 'serde_json::Value')]
  returns: Tokens = [
    ('core', 'Result'), ('text', '<'), ('core', 'Stream'), ('text', '<'), *message_tokens, ('text', '>>'),
  ]
  raw_returns: Tokens = [
    ('core', 'Result'), ('text', '<'), ('core', 'Stream'), ('text', '<'),
    ('json', 'serde_json::Value'), ('text', '>>'),
  ]
  main = Method(method, params, returns, is_async=True, doc=docs, deprecated=endpoint.deprecated)
  raw = Method(
    f'{method}{RAW_SUFFIX}', params, raw_returns, is_async=True,
    doc=[f'`{method}` without validation: the frames as they came.'], deprecated=endpoint.deprecated,
  )
  methods = [main] if message is None else [main, raw]

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
    if message is not None:
      w.blank()
      emit_method(module, main, qualifier=None, body=[
        f'let stream = self.{raw.name}({args}).await?;',
        'Ok(stream.map(decode))',
      ])
      w.blank()
      emit_method(module, raw, qualifier=None, body=_subscribe_body(module, endpoint, meta, channel))
    else:
      w.blank()
      emit_method(module, main, qualifier=None, body=_subscribe_body(module, endpoint, meta, channel))

  return EndpointModule(
    file=file, struct_name=struct_name, bound=bound, meta_type=meta_type, methods=methods,
    source=module.render(BANNER),
  ), notes


def _subscribe_body(module: Module, endpoint: EndpointPlan, meta: MetaShape | None, channel: str) -> list[str]:
  """The `SubscribeCall` one subscription is made from."""
  w = Writer(1)
  meta_expr = '&()'
  if meta is not None:
    w.struct_literal('let meta = ', meta.name, meta.literal_fields(endpoint.meta), ';')
    meta_expr = '&meta'
  parameters = 'Some(dump(&parameters)?)' if endpoint.request.type is not None else 'None'
  w.struct_literal('let call = ', 'SubscribeCall', [
    f'channel: {string(channel)}',
    f'parameters: {parameters}',
    f'meta: {meta_expr}',
    'options',
  ], ';')
  w.line('self.core.subscribe(call).await')
  return [line[4:] if line else line for line in w.render().rstrip('\n').split('\n')]


def render_endpoint(
  plan: PackagePlan, endpoint: EndpointPlan, *, struct_name: str, meta: MetaShape | None,
) -> tuple[EndpointModule, list[str]]:
  """Render one HTTP `rpc` endpoint's module; the second value lists what was skipped."""
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
  bound = f'HttpEndpoint<{meta_type}>' if meta_type is not None else 'HttpEndpoint'
  module.core('HttpEndpoint')
  module.core('CallOptions')
  module.core('HttpCall')
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
    try:
      paged, paged_body = _paged(module, endpoint, pagination, method=method, struct_name=struct_name, request_type=request_type)
    except _Skipped as skipped:
      notes.append(f'{endpoint.function}: {skipped.reason}')
  if paged is not None:
    methods.append(paged)
  methods.append(main)
  raw: Method | None = None
  if payload is not None:
    raw = Method(
      f'{method}{RAW_SUFFIX}', params, [('core', 'Result'), ('text', '<'), ('json', 'serde_json::Value'), ('text', '>')],
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
    w.line(f'core: Arc<dyn {bound}>,')
  w.blank()
  with w.block(f'impl {struct_name} {{'):
    w.line(f'pub fn new(core: Arc<dyn {bound}>) -> Self {{')
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
      body = _call_body(module, endpoint, meta, fixed=fixed, request_type=request_type)
      body.append('self.core.request(call).await')
      emit_method(module, raw, qualifier=None, body=body)
    else:
      body = _call_body(module, endpoint, meta, fixed=fixed, request_type=request_type)
      body.extend(('self.core.request(call).await?;', 'Ok(())'))
      emit_method(module, main, qualifier=None, body=body)

  return EndpointModule(
    file=file, struct_name=struct_name, bound=bound, meta_type=meta_type, methods=methods,
    source=module.render(BANNER),
  ), notes


def _call_body(module: Module, endpoint: EndpointPlan, meta: MetaShape | None, *, fixed, request_type: str | None) -> list[str]:
  """The statements building the `HttpCall`, up to and including `let call = ...;`."""
  w = Writer(1)
  if request_type is None:
    request_expr = 'None'
  elif fixed:
    w.line('let mut request = dump(&request)?;')
    with w.block('if let Some(object) = request.as_object_mut() {'):
      for f in fixed:
        w.line(f'object.insert({string(f.wire)}.to_string(), {json_expr(f.fixed)});')
    request_expr = 'Some(request)'
  else:
    request_expr = 'Some(dump(&request)?)'
  if meta is not None:
    w.struct_literal('let meta = ', meta.name, meta.literal_fields(endpoint.meta), ';')
    meta_expr = '&meta'
  else:
    meta_expr = '&()'
  method = endpoint.wire.method
  fields = [
    f'method: {f"Some({string(method)})" if method else "None"}',
    f'path: {string(endpoint.wire.path or "")}',
    f'request: {request_expr}',
    f'meta: {meta_expr}',
    'options',
  ]
  w.struct_literal('let call = ', 'HttpCall', fields, ';')
  return [line[4:] if line else line for line in w.render().rstrip('\n').split('\n')]


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
  if strategy == 'window':
    raise _Skipped('a `window` walk has no Rust walker yet; call the method per page')
  if strategy == 'seek' and pagination.overlap is not None:
    raise _Skipped('a `seek` walk with `overlap` has no Rust walker yet; call the method per page')
  if done == 'unchanged':
    raise _Skipped('an `unchanged` terminator has no Rust walker yet; call the method per page')
  if pagination.walker != 'paginated':
    raise _Skipped(f'an `{strategy}` walk with no resumable state has no Rust walker yet; call the method per page')
  if request_type is None or endpoint.request.shape != 'fields' or request_type not in module.fields:
    raise _Skipped('a walk needs a flat request to advance; this one has none')
  state_tree = pagination.state_type
  state_type = _state_type(state_tree)
  if state_type is None or pagination.row_type is None or endpoint.response.payload is None:
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

  row = module.type_expr(pagination.row_type, path=[paged_type, 'Row'])
  returns: Tokens = [('core', 'PaginatedResponse'), ('text', '<'), *tokenize(module, row), ('text', f', {state_type}>')]
  docs = [*method_docs(endpoint)[:1], f'Paged variant of `{method}`: await it for every row, or walk `rows()`/`pages()` one page at a time.', *method_docs(endpoint)[1:]]
  signature = Method(
    paged_name, [('request', [('local', paged_type)]), ('options', core_tokens('CallOptions'))], returns,
    is_async=False, doc=docs, deprecated=endpoint.deprecated,
  )

  # The body.
  payload_type: Type = {'type': 'ref', 'id': endpoint.response.payload}
  if endpoint.response.optional:
    payload_type = {'type': 'union', 'variants': [{'type': payload_type}, {'type': {'type': 'scalar', 'base': 'null'}}]}
  kind = str(done)
  zero_is_absent = strategy in ('token', 'seek') and not pagination.driver_required
  w.line('let endpoint = self.clone();')
  # Only the `page` and `seek` terminators read the page size; a `token` walk stops on the
  # cursor the response carries, so binding one there is dead code and a warning on
  # every clean build of every cursor-paged client.
  size = (
    _size(w, module, endpoint, pagination, request_fields, plan_fields)
    if strategy in ('page', 'seek')
    else 'None'
  )
  total_walk = strategy == 'page' and kind == 'total'
  if total_walk:
    module.imports.add('std::sync', 'Arc')
    module.imports.add('std::sync', 'Mutex')
    module.imports.add(f'{CORE}::paging', 'TotalSeen')
    w.line(f'let walker = {string(paged_name)};')
    w.line('let total_seen = Arc::new(Mutex::new(TotalSeen::new()));')
  with w.block(f'let next = move |{driver.ident}: {state_type}| {{', '};'):
    w.line('let endpoint = endpoint.clone();')
    w.line('let request = request.clone();')
    w.line('let options = options.clone();')
    if total_walk:
      w.line('let total_seen = total_seen.clone();')
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
        total = read_path(module, 'response', payload_type, str(pagination.done.get('path', '')), borrow=True)
        w.chain('let total = ', total.root, _as_total(module, total), ';')
        w.line('let total = {')
        with w.indented():
          w.chain('let mut seen = ', 'total_seen', ['.lock()', '.unwrap_or_else(|poisoned| poisoned.into_inner())'], ';')
          w.line(f'seen.check(walker, {"total" if total.optional else "Some(total)"})?')
        w.line('};')
      if strategy == 'token':
        module.imports.add(f'{CORE}::paging', 'cursor_or_done')
        cursor = read_path(module, 'response', payload_type, pagination.cursor_from or '', borrow=True)
        w.chain('let cursor = ', cursor.root, cursor.elements, ';')
      rows = read_path(module, 'response', payload_type, pagination.rows or '', borrow=False)
      w.chain('let rows = ', rows.root, [*rows.elements, *(['.unwrap_or_default()'] if rows.optional else [])], ';')
      if strategy == 'page':
        start = pagination.start if pagination.start is not None else 1
        if total_walk:
          module.imports.add(f'{CORE}::paging', 'total_reached')
          counts_pages = 'true' if pagination.done.get('counts') == 'pages' else 'false'
          condition = f'total_reached({counts_pages}, {driver.ident}, {start}, {size}, rows.len(), total)'
        else:
          module.imports.add(f'{CORE}::paging', 'exhausted')
          condition = f'exhausted(rows.len(), {size})'
        with w.block(f'if {condition} {{'):
          w.line('return Ok((rows, None));')
        w.line(f'Ok((rows, Some({driver.ident} + 1)))')
      elif strategy == 'token':
        w.line(f'Ok((rows, cursor_or_done({"cursor" if cursor.optional else "Some(cursor)"})))')
      else:  # seek: the cursor is read off the page's last row
        module.imports.add(f'{CORE}::paging', 'exhausted')
        module.imports.add(f'{CORE}::paging', 'cursor_or_done')
        with w.block(f'if exhausted(rows.len(), {size}) {{'):
          w.line('return Ok((rows, None));')
        cursor = _seek_cursor(module, pagination, rows.type)
        w.chain('let cursor = ', cursor.root, cursor.elements, ';')
        w.line(f'Ok((rows, cursor_or_done({"cursor" if cursor.optional else "Some(cursor)"})))')
  if strategy == 'page':
    seed = str(pagination.start if pagination.start is not None else 1)
  elif pagination.driver_required:
    seed = f'request.{driver.ident}' if module.copyable(plan_fields[driver.wire]['type']) else f'request.{driver.ident}.clone()'
  else:
    seed = {'i64': '0', 'f64': '0.0', 'String': 'String::new()', 'bool': 'false'}[state_type]
  w.line(f'PaginatedResponse::new({seed}, next)')
  module.core('PaginatedResponse')
  return signature, [line[4:] if line else line for line in w.render().rstrip('\n').split('\n')]


def _state_type(t: Type | None) -> str | None:
  """The Rust cursor type, when the runtime's `CursorLike` covers it."""
  if t is None or t['type'] != 'scalar':
    return None
  fmt = t.get('format')
  if fmt is not None and fmt not in ('uuid', 'hostname', 'uri'):
    return None
  return _STATE_TYPES.get(t['base'])


def _size(w: Writer, module: Module, endpoint: EndpointPlan, pagination: PaginationPlan, request_fields: list[Field], plan_fields) -> str:
  """Bind `size` (an `Option<usize>`) from the request's page-size field and return the
  expression the terminator checks read it by."""
  if pagination.size is None:
    return 'None'
  rendered = next((f for f in request_fields if f.wire == pagination.size), None)
  if rendered is None:
    return 'None'
  tree = strip_null(plan_fields[pagination.size]['type'])
  if tree['type'] != 'scalar' or tree['base'] != 'integer' or tree.get('format') is not None:
    raise _Skipped(f'a page size of type other than an integer (`{pagination.size}`) has no Rust walker yet')
  flatten = ['.flatten()'] if rendered.optional and rendered.nullable else []
  if not rendered.optional and not rendered.nullable:
    w.line(f'let size = Some(request.{rendered.ident} as usize);')
  elif pagination.size_default is not None:
    w.chain('let size = ', 'request', [f'.{rendered.ident}', *flatten, f'.unwrap_or({pagination.size_default})'], ';')
    w.line('let size = Some(size as usize);')
  else:
    w.chain('let size = ', 'request', [f'.{rendered.ident}', *flatten, '.map(|size| size as usize)'], ';')
  return 'size'


def _as_total(module: Module, read: Read) -> list[str]:
  """The chain elements reading `total` as the `i64` (or `Option<i64>`) `TotalSeen::check` takes."""
  t = _resolved(module, read.type)
  if t is None or t['type'] != 'scalar' or t['base'] != 'integer':
    raise _Skipped('a `total` that is not an integer has no Rust walker yet')
  elements = list(read.elements)
  if t.get('format') == 'integer-string':
    elements.append('.map(|total| total.0)' if read.optional else '.0')
  return elements


def _seek_cursor(module: Module, pagination: PaginationPlan, rows_type: Type | None) -> Read:
  """The next cursor of a `seek` walk, read off the last row (`cursor.from` is
  `[-1].<field>`), borrowed so `rows` can still be returned."""
  list_tree: Type = {'type': 'list', 'item': _item_type(module, rows_type)}
  return read_path(module, 'rows', list_tree, pagination.cursor_from or '', borrow=True)


__all__ = [
  'PAGED_SUFFIX', 'RAW_SUFFIX', 'EndpointModule', 'Method', 'Read', 'Tokens', 'core_tokens',
  'emit_method', 'endpoint_file', 'method_docs', 'read_path', 'render_endpoint',
  'render_stream_endpoint', 'render_tokens', 'tokenize',
]
