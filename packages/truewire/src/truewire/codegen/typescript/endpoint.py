"""One endpoint module: its types and codecs, and a class holding the method (plus the
`<method>Paged` walker when pagination is declared).

The class takes its core as a constructor argument typed by the `@truewire/core` contract
(`HttpEndpoint<Meta>` for HTTP, `CommandEndpoint<Meta>` for a WebSocket command) and calls
exactly one verb on it. The request object is the wire object: the method's first
parameter is the endpoint's `Request` interface, whose keys are the API's own names, and
its second is the `CallOptions` object (`validate`, `signal`).
"""
import re
from dataclasses import dataclass, field

from typing_extensions import Literal

from truewire.plan.model import EndpointPlan, PackagePlan, PaginationPlan
from truewire.plan.types import Type

from .names import camel_case, is_binding, literal, member_access, pascal_case, property_key, string
from .printer import BANNER, relative_specifier
from .types import Module, visible_scopes

META_FILE = 'meta.ts'
"""Where `meta_module` writes the per-core `meta` interfaces."""

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
  """`('promise', (T,))`, `('void', ())`, `('paginated', (row, state))` or `('generator', (T,))`."""
  doc: list[str | None] = field(default_factory=list)
  tags: list[str] = field(default_factory=list)
  is_async: bool = True
  generator: bool = False


@dataclass(frozen=True)
class EndpointModule:
  file: str
  class_name: str
  core_type: str
  """The core interface this class takes: `HttpEndpoint` or `CommandEndpoint`."""
  meta_type: str | None
  """The `meta.ts` interface the core is parameterised by, or `None` for a core with no schema."""
  types: list[str]
  """Names the module defines, for a router to qualify."""
  methods: list[Method]
  source: str


def endpoint_file(path: list[str]) -> str:
  """`repos.list_commits` -> `repos/list_commits.ts`: the spec directory, as a file."""
  return '/'.join([*path[:-1], f'{path[-1]}.ts'])


def meta_type_name(core: str) -> str:
  return f'{pascal_case(core)}Meta'


def core_interface(endpoint: EndpointPlan) -> str:
  """Which contract interface the endpoint's transport calls for."""
  return 'HttpEndpoint' if 'http' in endpoint.transports else 'CommandEndpoint'


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
  return f'AsyncGenerator<{render_type(module, tokens[0])}, void, undefined>'


def render_params(module: Module, params: list[Param]) -> str:
  return ', '.join(
    f'{param.name}{"?" if param.optional else ""}: {render_type(module, param.type)}'
    for param in params
  )


def raw_returns(returns: tuple[str, tuple[Token, ...]]) -> tuple[str, tuple[Token, ...]] | None:
  """`returns` as a `validate: false` call leaves it: the value (a promise's, a walker's
  rows, a generator's pages) is the body as the wire sent it, so `unknown` takes the
  declared type's place while a walker's state type stays. `None` for a method that
  returns nothing, which has no overload to make."""
  shape, tokens = returns
  if shape == 'void':
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
  raw = raw_returns(method.returns)
  if raw is None:
    w.jsdoc(*method.doc, tags=method.tags)
    return
  name = property_key(method.name)
  request = [Param(p.name, p.type, optional=False) for p in method.params if p.name != 'options']
  head = render_params(module, request)
  module.core('CallOptions', type_only=True)
  w.jsdoc(RAW_DOC)
  w.line(f'{name}({head + ", " if head else ""}options: {RAW_OPTIONS}): {render_return(module, raw)}')
  w.jsdoc(*method.doc, tags=method.tags)
  w.line(f'{name}({render_params(module, method.params)}): {render_return(module, method.returns)}')


def method_docs(endpoint: EndpointPlan) -> tuple[list[str | None], list[str]]:
  tags: list[str] = []
  if endpoint.docs.url:
    tags.append(f'@see {endpoint.docs.url}')
  if endpoint.deprecated:
    tags.append('@deprecated')
  return [endpoint.docs.description], tags


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


def render_endpoint(plan: PackagePlan, endpoint: EndpointPlan, *, class_name: str) -> EndpointModule:
  """Render one `rpc` endpoint's module."""
  file = endpoint_file(endpoint.path)
  module = Module(plan, file, local=endpoint.types, visible=visible_scopes(plan, endpoint.path))
  w = module.writer
  module.define_all(endpoint.types)

  core_type = core_interface(endpoint)
  core_plan = plan.cores.get(endpoint.core)
  meta_type = meta_type_name(endpoint.core) if core_plan is not None and core_plan.meta is not None else None
  module.core(core_type, type_only=True)
  module.core('CallOptions', type_only=True)
  if meta_type is not None:
    module.imports.add(relative_specifier(file, META_FILE), meta_type, type_only=True)
  core_param = f'{core_type}<{meta_type}>' if meta_type is not None else core_type

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
  main_params.append(Param('options', ('core', 'CallOptions'), optional=True))
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
      if fixed:
        fills = ', '.join(
          f'{property_key(f.wire)}: {member_access("request", f.wire, optional=False)} ?? {literal(f.fixed)}'
          for f in fixed
        )
        w.line(f'const wire: {request_type} = {{ ...request, {fills} }}')
      call = 'await ' if payload is None else 'return '
      with w.block(f'{call}this.core.request({{', '})'):
        if core_type == 'HttpEndpoint':
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

  defined = list(endpoint.types)
  if fixed:
    defined.append(args_type)
  if paged is not None:
    defined.append(str(paged.params[0].type[1]))
  return EndpointModule(
    file=file, class_name=class_name, core_type=core_type, meta_type=meta_type,
    types=defined, methods=methods, source=module.render(BANNER),
  )


# -- pagination -------------------------------------------------------------------------


def _size_expr(pagination: PaginationPlan, endpoint: EndpointPlan) -> tuple[str | None, bool]:
  """The page-size expression the walk measures a page against, and whether it can be
  `undefined` (an optional size with no documented default)."""
  if pagination.size is None:
    return None, False
  size = member_access('request', pagination.size, optional=False)
  field = next((f for f in endpoint.request.fields if f.wire == pagination.size), None)
  if field is not None and field.required:
    return size, False
  if pagination.size_default is not None:
    return f'({size} ?? {pagination.size_default})', False
  return size, True


def _exhausted(kind: str, size: str | None, size_unknown: bool) -> str:
  """The condition under which a page ends the walk, for `short_page`/`empty`."""
  if kind == 'empty' or size is None:
    return 'rows.length === 0'
  if size_unknown:
    return f'rows.length === 0 || ({size} !== undefined && rows.length < {size})'
  return f'rows.length === 0 || rows.length < {size}'


def _zero(t: Type) -> str:
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
  strategy, done = pagination.strategy, pagination.done.get('kind')
  unsupported = None
  if strategy == 'window':
    unsupported = 'a `window` walk'
  elif strategy == 'seek' and pagination.overlap is not None:
    unsupported = 'a `seek` walk with `overlap`'
  elif done == 'unchanged':
    unsupported = 'an `unchanged` terminator'
  elif pagination.walker == 'generator' and strategy == 'seek':
    unsupported = 'a `seek` walk with no resumable state'
  if unsupported is not None:
    return None, (
      f'truewire: {unsupported} has no TypeScript walker yet; call `{method}` per page.'
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
    Param('options', ('core', 'CallOptions'), optional=True),
  ]
  if pagination.walker == 'paginated':
    assert pagination.row_type is not None and pagination.state_type is not None
    doc = [*doc, f'Paged variant of `{method}`: awaitable (flattens every page) or async-iterable (one page at a time).']
    return Method(
      f'{method}{PAGED_SUFFIX}', params,
      ('paginated', (('plan', pagination.row_type), ('plan', pagination.state_type))),
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
  w = module.writer
  emit_signatures(module, paged)
  request_param, options_param = paged.params
  request_text = f'request: {render_type(module, request_param.type)}'
  if request_param.optional:
    request_text += ' = {}'
  params = f'{request_text}, options?: {render_type(module, options_param.type)}'
  returns = render_return(module, paged.returns)
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
  zero_is_absent = pagination.strategy in ('token', 'seek') and not pagination.driver_required

  def call(value: str) -> str:
    entry = value if value == key else f'{key}: {value}'
    return f'await this.{main.name}({{ ...request, {entry} }}, options)'

  if pagination.walker == 'paginated':
    assert pagination.row_type is not None and pagination.state_type is not None
    row = module.type_expr(pagination.row_type)
    state_type = module.type_expr(pagination.state_type)
    module.core('PaginatedResponse')
    with w.block(f'{paged.name}({params}): {returns} {{'):
      if pagination.strategy == 'page' and kind == 'total':
        w.line('let totalSeen: number | null = null')
        module.core('LogicError')
      with w.block(f'const next = async ({state}: {state_type}): Promise<[{row}[], {state_type} | null]> => {{'):
        w.line(f'const response = {call(f"{state} || undefined" if zero_is_absent else state)}')
        w.line(f'const rows = {rows_expr}')
        if pagination.strategy == 'page':
          start = pagination.start if pagination.start is not None else 1
          if kind == 'total':
            _emit_total_check(w, paged.name, read_path('response', str(done.get('path', '')), optional=optional))
            w.line(f'if ({_total_done(done, state, start, size, size_unknown)}) return [rows, null]')
          else:
            w.line(f'if ({_exhausted(str(kind), size, size_unknown)}) return [rows, null]')
          w.line(f'return [rows, {state} + 1]')
        elif pagination.strategy == 'token':
          cursor = read_path('response', pagination.cursor_from or '', optional=optional)
          w.line(f'const following = {cursor} ?? null')
          w.line('return [rows, following || null]')
        else:  # seek
          w.line(f'if ({_exhausted(str(kind), size, size_unknown)}) return [rows, null]')
          cursor = read_path('rows', pagination.cursor_from or '', optional=False)
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
    with w.block('while (true) {'):
      w.line(f'const response = {call(state)}')
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


def _total_done(done: dict, state: str, start: int, size: str | None, size_unknown: bool) -> str:
  pages = f'({state} - {start} + 1)'
  if done.get('counts') == 'pages':
    return f'{pages} >= total'
  if size is None:
    return 'rows.length === 0'
  if size_unknown:
    return f'({size} !== undefined && {pages} * {size} >= total) || rows.length === 0'
  return f'{pages} * {size} >= total'


__all__ = [
  'META_FILE', 'RAW_DOC', 'RAW_OPTIONS', 'EndpointModule', 'Method', 'Param', 'Token',
  'core_interface', 'emit_signatures', 'endpoint_file', 'meta_type_name', 'raw_returns',
  'render_endpoint', 'render_params', 'render_return', 'render_type',
]
