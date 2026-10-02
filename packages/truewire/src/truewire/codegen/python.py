"""The universal Python codegen backend.

Renders a project's spec tree -- endpoint modules, shared schema types, router groupings
and `_paged` iterators -- into a typed package. Every position in the function tree is
generated the same way; a project's optional `[python].backend` module subclasses
`Generator` to override only the hooks it needs.
"""
from typing_extensions import (
  Any, Container, Iterable, Literal, Mapping, NotRequired, TypedDict, get_args, get_origin,
  get_type_hints,
)
import dataclasses
import enum
import inspect
import keyword
import re
import sys
import textwrap
from importlib import import_module
from pathlib import Path
from types import UnionType

from truewire.generation.openapi import BODY_KEY, PARAMETER_KEY, RESPONSE_KEY, normalize_schemas
from truewire.generation.python import TypeGenerator, Parser, Renderer
from truewire.generation.python.types.parser import TIMESTAMP_FORMATS as TIMESTAMP_ALIASES, TYPES_PACKAGE
from truewire.generation.python.code import Docstring, Function, HttpRequest, self_shadowing_alias
from truewire.generation.python.code.functions import group_lines
from truewire.generation.python.code.imports import Imports as ImportsRenderer
from truewire.generation.python.util import safe_identifier
from truewire.generation.types import ExternalReference, Imports, RenderedTypes, merge_imports
from truewire.generation.schema import Operation, Reference, ResolutionError, Schema, ensure_nonref
from truewire.generation.util import indent, snake_case

from truewire.codegen.layout import group_class_name
from truewire.codegen.policy import POLICY_MODULE, http_policy_lines
from truewire.codegen.seek import exclusive_sentences
from truewire.plan.build import (
  PlanBuilder, channel_direct_params, connect_channel_param, direct_channel_scalar,
)
from truewire.plan.model import EndpointPlan, PackagePlan
from truewire.plan.types import Type as PlanType, seek_cursor_format
from truewire.project import Project
from truewire.spec import (
  Endpoint, GrpcEndpointSpec, Pagination, RouterDoc, RpcEndpointSpec, RpcEnvelopeSpec,
  SeekExclusive, StreamEndpointSpec, last_row_field,
  load_router, load_shared_schemas, path_segments, select_schema,
)
from truewire.spec.codegen_toml import CodegenConfig, PythonCoreConfig
from truewire.spec.endpoint import last_row_field_prose, seek_move_prose
from truewire.spec.request import PLACEHOLDER


def raw_paged_return_type(return_type: str) -> str:
  """Swap a `PaginatedResponse[Rows, State]` annotation's row type for `Any`.

  The `validate=False` overload of a `_paged` walker yields raw rows. The row type can
  itself hold a top-level comma inside brackets (`dict[str, Any]`), so the split is
  bracket-aware rather than at the first `, `.

  Args:
    return_type: The walker's rendered return annotation.
  """
  prefix = 'PaginatedResponse['
  if not return_type.startswith(prefix):
    return return_type
  depth = 0
  for index in range(len(prefix), len(return_type)):
    char = return_type[index]
    if char == '[':
      depth += 1
    elif char == ']':
      depth -= 1
    elif char == ',' and depth == 0:
      return prefix + 'Any' + return_type[index:]
  return return_type

PAGED_SUFFIX = '_paged'
"""Suffix separating a generated page iterator from the single-request method it drives."""
PAGED_CALL_NAME = 'paged'
"""Name a page iterator takes when the method it drives is its class's `__call__`."""
PAGED_IMPORTS: Mapping[str, set[str]] = {
  'truewire_core': {'PaginatedResponse'}, 'typing_extensions': {'Sequence'},
}
"""Imports a generated module needs once it carries a page method (ADR 0021: every one is
`PaginatedResponse`-shaped)."""
PAGED_LOGIC_ERROR_IMPORTS: Mapping[str, set[str]] = {'truewire_core.exceptions': {'LogicError'}}
"""Imports a generated module needs once a `seek` walk carries its full-page-one-key or
carried-row-missing raises."""
VALIDATE_PARAM = 'validate'
"""Keyword every request/reply method takes to override the client's response validation."""
VALIDATE_OVERLOAD_IMPORTS: Mapping[str, set[str]] = {
  'typing_extensions': {'Any', 'Literal', 'overload'},
}
"""Imports a generated module needs once a method carries `validate_overloads`."""
PAGED_DOC_WIDTH = 84
"""Wrap width for a page iterator's docstring, leaving room for a method's indentation."""
WS_REPLY_CODE = 'reply'
"""Response code naming the one-shot subscribe acknowledgement, per `docs/spec/spec.md`."""
WS_MESSAGE_CODE = 'message'
"""Response code naming the repeated stream message, per `docs/spec/spec.md`."""
JSON_CONTENT_TYPE = 'application/json'
"""Content type whose schema is the readable typed contract for a stream message."""
CHANNEL_PLACEHOLDER = PLACEHOLDER
"""Placeholder in a channel template, filled from an `in: 'path'` parameter."""
STREAM_PAYLOAD_KEY = '$stream/payload'
"""Reference id under which a subscription's payload schema is rendered."""
_SCALAR_ZERO_VALUES: Mapping[str, str] = {
  'bytes': "b''", 'str': "''", 'int': '0', 'bool': 'False', 'float': '0.0',
}
"""A bare (non-`X | None`) scalar type's own zero value, for constructing a nested
message field whose real constructor -- unlike a typical optional wrapper parameter --
does not accept `None` (betterproto2 fields use `default_factory`, not `Optional`, so
omitting a field entirely is the only way to get its zero value; passing `None`
explicitly is a `TypeError` at the constructor). Cosmos SDK's own pagination already
treats a zero `key`/`limit` as "start"/"use the default", so substituting one where the
loop-local is `None` is not a guessed value -- it is the declared zero value standing in
for its own absence, the same relationship `key: bytes = b''` already has in the
hand-written `page_request()` helper this replaces."""


def validate_overloads(
  header: Function, *, raw_return_type: str, generator: bool = False,
) -> list[Function]:
  """The `@overload` stubs that make a method's return type honest about `validate`.

  A generated method returns the parsed record -- `datetime`s, `Decimal`s, `Literal`s --
  only when the reply was validated; `validate=False` hands back the body as the wire
  sent it, and the single annotation `-> Commits` lies for that call. The cheapest honest
  typing is two stubs: `validate: Literal[False]` returns `raw_return_type` (`Any`, or
  `PaginatedResponse[Any, ...]`/`AsyncIterator[Any]` for a walker), and
  `validate: bool | None = None` -- the default `None` leaves the decision to the client
  -- returns the declared type. The implementation keeps the same header, and the
  runtime is untouched.

  The second stub is deliberately `bool | None`, not `Literal[True] | None`: a caller
  forwarding a `bool` variable -- the generated walkers do exactly that
  (`validate=validate`) -- gets the declared type. Only a literal `False` at the call
  site is knowably raw; a flag decided elsewhere is the caller's own decision, like
  `None`. (Pyright would otherwise expand such a `bool` over `Literal[True]`/
  `Literal[False]` within a budget it spends left to right, which a few
  `Literal[...] | None` parameters ahead of `validate` exhaust; mypy never expands it.)
  Two stubs is also the least a signature can be repeated, which matters in generated
  code people read.

  Args:
    header: The implementation's header, its `kwargs` final. Returned empty when it has
      no `validate` keyword or no return type, so a method that never validates (a gRPC
      call, a reply-less command) renders exactly as before.
    raw_return_type: The annotation of the `validate=False` overload.
    generator: Whether the implementation is an async generator (`async def` yielding,
      annotated `AsyncIterator[...]`). Its stubs are plain `def`s: a stub has no `yield`
      to mark it a generator, so an `async def` stub would read as a coroutine *returning*
      the iterator, which is not what calling the implementation gives.

  Returns:
    The stubs, in the order they must be declared: `Literal[False]` first, since a
    checker picks the first match and the wider stubs would otherwise shadow it.
  """
  validate = next((param for param in header.kwargs if param.name == VALIDATE_PARAM), None)
  if validate is None or header.return_type is None:
    return []

  def variant(validate_type: str, *, default: str | None, return_type: str) -> Function:
    kwargs = [
      Function.Param(name=param.name, type=validate_type, required=True, default=default)
      if param.name == VALIDATE_PARAM else param
      for param in header.kwargs
    ]
    return Function(
      name=header.name, asyn=header.asyn and not generator, method=header.method,
      args=list(header.args), kwargs=kwargs, return_type=return_type,
      decorators=['@overload'],
    )

  return [
    variant('Literal[False]', default=None, return_type=raw_return_type),
    variant('bool | None', default='None', return_type=header.return_type),
  ]


def _nested_pagination_ref(parameter: str) -> tuple[str, str] | None:
  """Split a declared pagination parameter name into `(outer, inner)` when it is a
  dotted path into a single nested request-message parameter, or None for the ordinary
  flat top-level case.

  Args:
    parameter: `pagination.cursor.parameter` or `pagination.size.parameter`, as declared.
  """
  if '.' not in parameter:
    return None
  outer, inner = parameter.split('.', 1)
  return outer, inner

class File(TypedDict):
  path: str
  content: str

class RouterChild(TypedDict):
  """One child a generated router composes: an endpoint class or a sub-router."""
  import_path: str
  """Relative import the router writes to reach the child."""
  class_name: str
  """Name of the child's generated class."""
  attr_name: str
  """Attribute the child is reached through, e.g. `market` in `client.market`."""
  kind: Literal['endpoint', 'router']
  """Whether the child is a leaf endpoint class or a sub-router package."""
  transport: Literal['http', 'ws', 'mixed']
  """Which transports the child needs, so a router can compose both under one root.

  A leaf is `http` or `ws`; a sub-router is whichever its own leaves are, or `mixed`
  when it carries both. A root whose children are not all `http` owns a socket as well
  as an HTTP client, so it composes onto the client core's `Client` rather than its
  `Router` — that is the whole reason a channel no longer needs its own `output_base`
  and its own top-level class.
  """
  mixin: NotRequired[str]
  """Client-core base the child's own class was built on, when the backend tracks one."""
  doc: NotRequired['RouterDoc | None']
  """A `kind: 'router'` child's own declared `router.json`, pre-loaded by the CLI (`None`
  when it has none, or when `kind` is `'endpoint'` -- a leaf has no `router.json` of its
  own). Lets a backend give a `cached_property` child its own real docstring via
  `router_docstring(attr_name, child['doc'])`, the same content its own class docstring
  carries, without the backend having to resolve the child's `spec/endpoints/` path itself."""
  spec_dir: NotRequired[Path]
  """A `kind: 'router'` child's own `spec/endpoints/<...>` directory, when the caller
  knows it -- lets `_router_child_new_method` resolve the child's own core
  and check whether it needs `.new()`-based construction, without the backend having to
  resolve that path itself. Unset degrades gracefully to today's already-shipped
  behavior (no `.new()` ever attempted) -- an existing hand-built `RouterChild` dict (a
  unit test, say) that never sets it is completely unaffected."""

def router_docstring(section: str, doc: RouterDoc | None) -> str:
  """
  Render a router class's docstring: declared `router.json` content when present, the
  generic fallback every backend already emits otherwise.

  Args:
    section: Router section name (e.g. `classic/mix`), used in the generic fallback.
    doc: This section's loaded `router.json`, or `None`.
  """
  if doc is None:
    return f'"""`{section}` endpoints."""'
  return (
    f'"""{doc.description}\n'
    f'\n'
    f'References:\n'
    f'  - [Upstream docs]({doc.upstream})\n'
    f'"""'
  )

class SurfaceGroupMember(TypedDict):
  """One already-generated `SURFACES` root a `SurfaceGroup` composes as a named field."""
  attr_name: str
  """Attribute the member is reached through, e.g. `http` in `client.spot.http`."""
  class_name: str
  """Name of the member's generated root class (e.g. `Http`)."""
  import_path: str
  """Relative import the surface group writes to reach the member."""
  base: str
  """The member's own `output_base` (e.g. `spot/http`), to load its own `router.json`."""

class SurfaceGroup(TypedDict):
  """A hand-composed node above the `SURFACES` level, gathering several differently-typed
  transport roots (`Http`/`Streams`/`Ws`/...) into one plain dataclass -- a `Spot`
  composing `spot/http` + `spot/streams` + `spot/ws` is the motivating case.

  Deliberately not folded into `router()`'s own tree: a `RouterChild` composes siblings
  that all share one `self.client` (the `cached_property` shape S11 requires); a
  `SurfaceGroup`'s members each need their own, differently-typed and differently-
  constructed client (an HTTP client, a stream socket, an RPC socket, ...), so this
  renders a bare dataclass of named fields instead -- gathered on `__aenter__`/
  `__aexit__`, construction left entirely to the client's hand-written root, since
  URLs/credentials are never a spec-level fact.
  """
  path: str
  """Output path, relative to the client's package root (e.g. `spot/__init__.py`)."""
  spec_dir: str
  """`spec/endpoints/` directory this group's own `router.json` is loaded from."""
  class_name: str
  """Name of the generated dataclass (e.g. `Spot`)."""
  members: list[SurfaceGroupMember]
  """Every transport root this group composes, in declaration order."""

def surface_group_module(
  group: SurfaceGroup, doc: RouterDoc | None, member_docs: Mapping[str, RouterDoc | None],
) -> str:
  """
  Render one `SurfaceGroup`'s module: import lines, a plain `@dataclass(kw_only=True)`
  gathering its members as named fields -- never `cached_property`, since each member
  needs its own already-constructed client rather than a shared `self.client` -- and
  `__aenter__`/`__aexit__` gathering every member concurrently.

  Each member field's own docstring comes from that member's *own* `router.json`
  (`member_docs`), never a repeat of the group's -- `spot.http`'s hover text is
  `spot/http`'s own declared description, the same text `Http`'s own class docstring
  already carries, not `Spot`'s.

  Args:
    group: The surface group to render.
    doc: This group's own loaded `router.json`, or `None`.
    member_docs: Each member's own loaded `router.json`, keyed by `attr_name`.
  """
  members = group['members']
  lines = [
    'from dataclasses import dataclass', 'import asyncio', '',
    *(f'from {m["import_path"]} import {m["class_name"]}' for m in members),
    '', '',
    '@dataclass(kw_only=True)',
    f'class {group["class_name"]}:',
  ]
  lines.append(indent(router_docstring(group['spec_dir'], doc), '  '))
  for member in members:
    lines.append('')
    lines.append(indent(f'{member["attr_name"]}: {member["class_name"]}', '  '))
    member_doc = router_docstring(member['base'], member_docs.get(member['attr_name']))
    lines.append(indent(member_doc, '  '))
  lines.append('')
  lines.append('  async def __aenter__(self):')
  lines.append('    await asyncio.gather(')
  lines.extend(f'      self.{m["attr_name"]}.__aenter__(),' for m in members)
  lines.append('    )')
  lines.append('    return self')
  lines.append('')
  lines.append('  async def __aexit__(self, exc_type, exc_value, traceback):')
  lines.append('    await asyncio.gather(')
  lines.extend(
    f'      self.{m["attr_name"]}.__aexit__(exc_type, exc_value, traceback),'
    for m in members
  )
  lines.append('    )')
  return '\n'.join(lines)

class ExternalSchemas(TypedDict):
  files: Iterable[File]
  references: Mapping[str, ExternalReference]

class SubscriptionParameter(TypedDict):
  """One input a caller supplies when opening a subscription."""
  name: str
  """Name as the spec writes it, before conversion to a Python identifier."""
  id: str
  """Reference id of this parameter's schema inside `Subscription.schemas`."""
  required: bool
  """Whether the API requires the parameter."""
  description: str | None
  """Prose for the generated `Args:` bullet."""

class Subscription(TypedDict):
  """A WebSocket subscription, read out of the adapted-OpenAPI convention.

  `docs/spec/spec.md` §"WebSocket Subscriptions" defines the convention this decodes:
  `in: 'path'` parameters interpolate the channel, `requestBody` carries the
  subscription parameters, and `responses` splits into a one-shot `reply` and a
  repeated `message`.
  """
  channel: str
  """Channel template, with `{name}` placeholders for `channel_parameters`."""
  channel_parameters: list[SubscriptionParameter]
  """Every `in: 'path'` parameter, template order first and declaration order after.

  An API that interpolates them names them all in the template, so the two agree.
  Not every API does — one declares `id` and `batched` on a channel called `v4_candles`
  and sends them as sibling fields of the subscribe frame — so ordering by the
  template alone would drop them.
  """
  parameters: list[SubscriptionParameter]
  """Subscription parameters expanded from the `requestBody` record, in spec order."""
  operation: Operation
  """The operation with every schema replaced by a reference."""
  schemas: dict[str, Schema | Reference]
  """Every schema the subscription refers to, keyed by reference id."""
  body: str | None
  """Reference id of the whole `requestBody` schema, when one is declared."""
  reply: str | None
  """Reference id of the subscribe acknowledgement, when one is declared."""
  message: str | None
  """Reference id of the repeated stream message, when one is declared."""
  binary: bool
  """Whether the stream message declares a content type other than JSON.

  A binary stream is decoded by API-specific machinery — a Protobuf push, say — so
  its generated payload type would not be the JSON schema's
  `TypedDict`. Backends use this to refuse rather than to emit something wrong.
  """

def endpoint_transport(endpoint: Endpoint) -> Literal['http', 'ws']:
  """Name the transport one endpoint needs, so a router knows what to compose it onto.

  Args:
    endpoint: The endpoint whose module is about to be generated.
  """
  return 'ws' if 'ws' in endpoint.transports else 'http'

def subtree_transport(
  transports: Mapping[tuple[str, tuple[str, ...]], str], base: str, node: tuple[str, ...],
) -> Literal['http', 'ws', 'mixed']:
  """Name the transports a whole sub-router needs, from the leaves beneath it.

  `mixed` is a real answer rather than a failure: a section may serve HTTP endpoints and
  WebSocket channels alike, and a root that composes both is exactly what lets one client
  expose `client.market.time()` and `client.ws.ticker()` side by side instead of shipping
  a second top-level class per socket.

  Args:
    transports: Transport of every leaf, keyed by its `(output base, function-tree node)`.
    base: Output base the sub-router lives under.
    node: Function-tree node naming the sub-router.
  """
  kinds = {
    transport
    for (leaf_base, leaf), transport in transports.items()
    if leaf_base == base and leaf[:len(node)] == node
  }
  if kinds == {'http'}:
    return 'http'
  if kinds == {'ws'}:
    return 'ws'
  return 'mixed'

TYPE_CAST_ALIAS_RE = re.compile(r'^\w+\s*=\s*(Literal\[|Any\s*$)')

def needs_type_cast(defn: str | None) -> bool:
  """Whether a rendered type definition is a bare `Literal[...]`/`Any` alias -- not a
  real class -- which pyright refuses to accept where a `type[T] | UnionType | None`
  argument is expected: `type[Literal[...]]`/`type[Any]` matches neither `type[T]` nor
  `UnionType` when the module only ever binds it to a plain module-level `{name} = ...`
  assignment (rather than, say, a genuine class definition). A genuine union of real
  classes/records (`A | B`) is unaffected -- the `UnionType` branch already accepts that
  fine -- so this only fires for these two narrower alias shapes.

  Confirmed live: bybit's `spot_margin.fixedborrow.renew`
  (`Response = Literal['Success', 'Failure']`) and kucoin's `spot.orders_hf.
  cancel_all_by_symbol`/`margin.orders_hf.cancel_all_by_symbol` (`Response =
  Literal['success']`) for the `Literal` case; kucoin's `streams.futures_private.
  order`/`all_orders` (`Payload = Any`, an undocumented push payload) for the `Any`
  case -- both hit this once `response_type=Response`/`response_type=Payload` is passed
  straight through to `core.request()`/`core.subscribe()` (design §7's own "generated
  code passes bare types" rule). The generated call wraps the value in `cast(type,
  ...)` at the call site instead of a `# type: ignore` comment -- `group_lines` packs
  multiple call arguments onto one physical line, and a trailing comment would silently
  swallow whatever else shares that line.

  Args:
    defn: The rendered definition text for this type's own id (`types.definitions.get
      (id)`), or `None` when it resolved externally (an already-rendered shared schema,
      or a bare `$ref` response) -- never a bare-Literal/Any alias either way.
  """
  return defn is not None and bool(TYPE_CAST_ALIAS_RE.match(defn.strip()))


def _fstring_literal(text: str) -> str:
  """Escape one literal (non-placeholder) segment of a `channel` template for splicing
  into a single-quoted f-string source (`Generator.channel_expr`'s default rendering) --
  a backslash or a single quote is the only thing that can't appear verbatim there.
  """
  return text.replace('\\', '\\\\').replace("'", "\\'")


class Generator:
  core_package: str | None = None
  """Package the project's hand-written core lives in, e.g. `petstore.core`.

  Set by the CLI after loading the backend, from the same package directory it already
  computes for output — never hardcoded here, since nothing in this module knows a
  project's name. Passed to the type parser so an epoch-formatted field has somewhere to
  import `Timestamp` from; `None` is fine for a project whose spec declares no epoch field.
  """

  project: Project | None = None
  """The project being generated (`truewire.project.Project`), set by the CLI after loading
  the backend -- same pattern as `core_package`. `None` when a `Generator` is constructed
  outside the CLI (a unit test, say), in which case `router_doc` always returns `None`.
  """

  shared_schemas: Mapping[str, Schema] = {}
  """`spec/schemas.json`'s raw schemas, keyed by their own `$ref` id -- set by the CLI
  right after `load_schemas`, the same point `core_package`/`project` are set, and
  before `self.schemas(...)` (whose own contract is to return rendered *types*, not the
  raw schema map a `Resolver` needs). Lets a backend build a
  `truewire.generation.schema.LocalResolver(schemas=self.shared_schemas)` and pass it to
  `HttpRequest.parse`'s `resolver` parameter so a request body that's a bare `$ref` into
  `schemas.json` (or an `anyOf`/array-items of one) still gets its `decimal-string`/
  timestamp-formatted properties converted to wire shape. Empty for a `Generator` built
  outside the CLI loop (a unit test, say) or for a project with no `schemas.json`.
  """

  router_context: tuple[str, tuple[str, ...]] | None = None
  """`(base, node)` for the router call about to run -- set by the CLI immediately before
  every `skip_router`/`router` call, the same point `skip_router` itself already receives
  this exact context. `None` only for a `Generator` built outside the CLI loop (a unit
  test, say), or before the loop's first iteration.

  This exists so `router_doc`'s default resolution (below) can build a router section's
  real `spec/endpoints/` directory from the lossless `(base, node)` pair instead of the
  lossy collapsed `section` string (`node[-1] if node else base`,
  `truewire.cli.codegen`) -- a bare last-segment name alone can't tell a nested grouping
  apart from an unrelated one sharing its name (a `streams.bitcoin` vs. a base-root
  `bitcoin` product group both collapse to `section == "bitcoin"`). `(base,
  node)` never collides this way: it's exactly the router's own position in the function
  tree, so `Path(base, *node)` is a deterministic, ambiguity-free candidate path.

  Several projects' own codegen backends independently reinvented this exact stash --
  overriding `skip_router` purely to capture `(base, node)` as a side effect, never to
  skip anything -- because `router()`/`router_doc()` had no other way to reach it. This
  formalizes that pattern once, centrally, instead of leaving it as divergent
  per-project hacks.
  """

  codegen_config: CodegenConfig | None = None
  """This project's loaded `truewire.toml` -- set by the CLI after loading the
  backend, same pattern as `core_package`/`project`/`shared_schemas`. `rpc_endpoint`
  passes it straight through to `_resolve_core` alongside a per-call `endpoint_dir`, so
  that method needs no constructor of its own to learn a fact that's really per-project
  rather than per-call. `None` for a `Generator` built outside the CLI loop (a unit test,
  say), in which case `rpc_endpoint` cannot resolve a core class and raises.
  """

  schemas_scope: tuple[str, ...] | None = None
  """Directory segments (relative to `spec/endpoints/`) that the `schemas.json` file about
  to be rendered by the next `schemas()` call lives under -- the empty tuple `()` for the
  project root's own `spec/schemas.json` (the final fallback). Set by the CLI
  immediately before every `schemas()` call, the identical `router_context`-style stash
  this class already uses for `router()` (see that attribute's own docstring for the full
  reasoning) -- `schemas()`'s own signature stays exactly `schemas(self, schemas)`,
  unchanged from the contract a legacy backend overrides it with, so this is how the
  base universal implementation learns
  which scope's own module/package path to render a given call's file into, without
  breaking any of those already-hand-rolled overrides' call signature. `None` only for a
  `Generator` built outside the CLI loop (a unit test, say) or before the loop's first
  `schemas()` call -- the base implementation then falls back to root scope (`()`).
  """

  refusing: str | None = None
  """The function path of the endpoint `endpoint()` is rendering when `[policy].refuse`
  names it (W15), else `None`: set around each `rpc_endpoint`/`stream_endpoint`/
  `grpc_endpoint` call, the `router_context`-style stash, so each one puts `refusal()`'s
  decorator above its class without a new parameter a legacy override would not accept."""

  plan: PackagePlan | None = None
  """The whole package's plan (`truewire.plan.build.build_plan`), attached by the CLI once
  per run. `rpc_endpoint`/`stream_endpoint` read their endpoint's decisions from it --
  request fields, the returned type's nullability, whether a type is a bare alias, and
  every pagination fact -- instead of re-deriving them from rendered strings. `None` for a
  `Generator` built outside the CLI (a unit test, say): `endpoint_plan` then computes the
  one endpoint's plan on demand from `project`."""

  def refusal(self) -> tuple[list[str], Imports]:
    """`@refused(...)` for the class of the endpoint being rendered, and its import, when
    `[policy].refuse` names it (`refusing`); nothing otherwise. The decorator makes every
    public method raise the package's `RefusedByPolicy` before any request."""
    if self.refusing is None or self.project is None:
      return [], {}
    return [f'@refused({self.refusing!r})'], {f'{self.project.package_name}.{POLICY_MODULE}': {'refused'}}

  def endpoint_plan(self, endpoint: Endpoint, endpoint_dir: Path) -> EndpointPlan:
    """This endpoint's plan: looked up on the attached `plan` by function path, or
    computed on its own when none is attached (or the path is not on it, as when a
    backend's `output_function` renames it).

    Args:
      endpoint: The endpoint being generated -- planned as given, so a caller passing a
        modified copy gets a plan of that copy.
      endpoint_dir: Its `spec/endpoints/` directory.

    Raises:
      ValueError: When the endpoint is a shape the plan does not cover (OpenAPI-shaped
        or gRPC), which the callers here have already refused anyway.
    """
    assert self.project is not None, 'endpoint_plan needs project (set by the CLI)'
    function = endpoint.resolved_function(endpoint_dir / 'endpoint.json', self.project.spec_dir)
    if self.plan is not None:
      found = self.plan.endpoint(function)
      if found is not None:
        return found
    planned = PlanBuilder(self.project).endpoint(endpoint, endpoint_dir)
    if planned is None:
      raise ValueError(f'{function}: not a request/response-shaped endpoint; nothing to plan')
    return planned

  def plan_type_code(self, type: PlanType) -> str:
    """Render one plan type tree to the Python type expression this backend writes for it
    -- the same renderer every `Request`/`Response` field goes through, so a cursor's
    seed type reads exactly as its parameter's annotation does."""
    return self.type_generator().render.code(type).iden

  def router_doc(self, section: str) -> RouterDoc | None:
    """
    Load `section`'s declared `router.json`, if any.

    `section` wins whenever it already looks like an explicit path (contains `/`) --
    this is how a subclass override delegates a position that genuinely differs from
    whatever `router_context` currently holds (a remapped/aliased base, say), by passing
    that real path straight through rather than relying on ambient state the caller
    can't see. A bare `section` (no `/`) can only mean "resolve whatever `router()` is
    being called for right now", so it falls through to `router_context` -- `(base,
    node)` for the call in flight, set by the CLI -- and resolves `Path(base, *node)`,
    the router's real `spec/endpoints/` directory at any depth, not just a base-root
    section. `None` when neither applies: `project` unset, or `router_context` unset
    and `section` bare (a `Generator` constructed outside the CLI loop, a unit test say).

    This priority is deliberate, not incidental: an explicit argument should never be
    silently overridden by instance state the caller didn't pass. A real backend override
    hit exactly the bug this ordering prevents -- delegating via
    `super().router_doc('ws/trading')` to resolve a remapped path, only to have the
    remap silently discarded in favor of `router_context`'s still-unremapped position,
    under an earlier version of this method that checked `router_context` first.

    A backend whose `output_base` returns a value with no real `spec/endpoints/`
    counterpart (a uniform `'http'` artifact, or a value that needs stripping/remapping
    like `chain/modules/<module>`) still needs its own
    override -- this default can't invent a mapping that isn't literally the real
    directory path, and correctly returns `None` (via `load_router`'s own file-existence
    check) rather than guess when `Path(base, *node)` doesn't exist.

    Args:
      section: Router section name, as passed to `router()`. Pass an explicit `/`-joined
        path here (rather than mutating `router_context`) when delegating a resolution
        that differs from the position `router_context` holds -- it always wins.
    """
    if self.project is None:
      return None
    if '/' in section:
      return load_router(self.project.endpoints_dir / section)
    if self.router_context is not None:
      base, node = self.router_context
      return load_router(self.project.endpoints_dir / Path(base, *node))
    return None

  def _resolve_core_name(self, endpoint_dir: Path, spec_root: Path) -> str:
    """
    Walk `endpoint_dir` up through its `spec/endpoints/` ancestors (including
    `endpoint_dir` itself), returning the nearest ancestor `router.json`'s own declared
    `core` -- the bare symbolic name, before either `[python.cores]` (`_resolve_core`) or
    the top-level `[cores.<name>]` (`_resolve_meta_schema`) look it up. Factored out so
    both lookups share one walk rather than keeping two copies of it in sync.

    No implicit fallback -- every project's root `router.json` must declare
    one, enforced separately by `check_router_core` at spec-test time; this raises rather
    than silently defaulting if that invariant is somehow violated when codegen actually
    runs.

    Args:
      endpoint_dir: Directory (endpoint leaf or router grouping) to resolve a core for.
      spec_root: The client's `spec/` directory -- the walk stops here.
    """
    endpoints_root = spec_root / 'endpoints'
    current = endpoint_dir
    while True:
      doc = load_router(current)
      if doc is not None and doc.core is not None:
        return doc.core
      if current == endpoints_root:
        raise ValueError(f'{endpoint_dir}: no ancestor router.json declares `core`')
      current = current.parent

  def _resolve_core(
    self, endpoint_dir: Path, spec_root: Path, config: CodegenConfig,
  ) -> PythonCoreConfig:
    """
    Resolve the `PythonCoreConfig` a generated class at `endpoint_dir`
    should subclass -- its `base` (`module.path:ClassName`) and, for a base that
    genuinely composes more than one distinctly-based child, its `children` mapping.

    Resolves `endpoint_dir`'s symbolic core name via `_resolve_core_name`'s ancestor
    walk, then looks that name up in `truewire.toml`'s `[python.cores]` table. No implicit
    fallback -- see `_resolve_core_name`'s own docstring.

    Called identically for every position in the tree, root included --
    there is no separate mechanism for the root's own base; it is simply whichever
    `[python.cores]` entry its own `spec/endpoints/router.json` declares.

    Args:
      endpoint_dir: Directory (endpoint leaf or router grouping) to resolve a core for.
      spec_root: The client's `spec/` directory -- the walk stops here.
      config: This client's loaded `truewire.toml`.
    """
    if config.python is None:
      raise ValueError(f'{spec_root}: truewire.toml has no [python] section')
    name = self._resolve_core_name(endpoint_dir, spec_root)
    resolved = config.python.cores.get(name)
    if resolved is None:
      raise ValueError(
        f'{endpoint_dir}: router.json declares core={name!r}, not in truewire.toml [python.cores]'
      )
    return resolved

  def _resolve_meta_schema(
    self, endpoint_dir: Path, spec_root: Path, config: CodegenConfig,
  ) -> dict[str, Any] | None:
    """
    Resolve the declared JSON Schema for `meta`'s shape at `endpoint_dir`'s
    resolved core, or `None` when that core declares no `meta` schema at all -- meaning
    every endpoint resolving to it must declare its own `meta: {}`.

    Resolves `endpoint_dir`'s symbolic core name via `_resolve_core_name`'s ancestor
    walk (the identical walk `_resolve_core` itself uses), then looks that name up in
    `truewire.toml`'s top-level `[cores.<name>]` table -- a separate, language-neutral
    table from `[python.cores]`, since `meta`'s shape is an API fact, not a Python
    one. Unlike `_resolve_core`, a name absent from `[cores.<name>]` -- or `config.cores`
    being unset entirely -- is not an error: most cores need no `meta` schema at all
    (§6), so this returns `None` rather than raising.

    Args:
      endpoint_dir: Directory (endpoint leaf or router grouping) to resolve for.
      spec_root: The client's `spec/` directory -- the walk stops here.
      config: This client's loaded `truewire.toml`.
    """
    if config.cores is None:
      return None
    name = self._resolve_core_name(endpoint_dir, spec_root)
    core = config.cores.get(name)
    if core is None:
      return None
    return core.meta

  def _resolve_schemas(
    self, endpoint_dir: Path, spec_root: Path,
    schemas_references: Mapping[Path, Mapping[str, ExternalReference]],
  ) -> Mapping[str, ExternalReference]:
    """
    Merge every `schemas.json` scope visible from `endpoint_dir`, for the
    `references` map `class_name`/`endpoint` need to resolve a `$ref` into an already-
    rendered shared type rather than re-emit it.

    Walks `endpoint_dir` up through its `spec/endpoints/` ancestors (including
    `endpoint_dir` itself), the identical nearest-ancestor shape `_resolve_core` walks
    -- but unlike that walk, this one does not stop at the first hit: a leaf
    automatically inherits visibility into every ancestor scope, not just the nearest, so
    every ancestor `schemas.json` found along the way is merged in, and the project root's
    own `spec/schemas.json` (the final fallback -- the walk always reaches it last) is
    folded in last. Since no id may be declared at two scopes on one path to root (no
    shadowing, `check_schemas_no_shadowing`
    at spec-test time), the merge order never actually matters for a clean spec -- this
    still raises rather than silently resolving by nearer-wins precedence if that
    invariant is somehow violated when codegen actually runs, the identical backstop
    `_resolve_core` itself already keeps even though `check_router_core` also exists.

    Args:
      endpoint_dir: Directory (endpoint leaf or router grouping) resolving references for.
      spec_root: The client's `spec/` directory -- the walk stops here, then checks
        `spec_root / 'schemas.json'` once more as the final fallback.
      schemas_references: Every discovered `schemas.json`'s own already-rendered
        `references` map (`Generator.schemas(...)['references']`), keyed by that file's
        real path -- `discover_schemas_files`' own inventory, pre-rendered by the CLI
        before any endpoint is generated.
    """
    def merge(into: dict[str, ExternalReference], refs: Mapping[str, ExternalReference]):
      for id, ref in refs.items():
        existing = into.get(id)
        if existing is not None and existing != ref:
          raise ValueError(
            f'schema id {id!r} is declared by more than one schemas.json scope visible '
            f'from {endpoint_dir} -- shadowing is refused rather than resolved '
            'by nearer-wins precedence'
          )
        into[id] = ref

    endpoints_root = spec_root / 'endpoints'
    merged: dict[str, ExternalReference] = {}
    current = endpoint_dir
    while True:
      refs = schemas_references.get(current / 'schemas.json')
      if refs:
        merge(merged, refs)
      if current == endpoints_root:
        break
      current = current.parent
    root_refs = schemas_references.get(spec_root / 'schemas.json')
    if root_refs:
      merge(merged, root_refs)
    return merged

  def surface_groups(self) -> list[SurfaceGroup]:
    """
    Declare every `SurfaceGroup` this backend hand-composes above the `SURFACES` level
    (a `Spot` gathering `spot/http` + `spot/streams` + `spot/ws` is the motivating
    case). Empty by default -- a backend with no such node (most of them: a
    single-product project's root composite is the only thing above `SURFACES`, and it
    needs real construction logic no spec encodes, so it stays entirely hand-written)
    declares nothing and this step is a no-op.

    Each declared group is rendered by `surface_group_module` and generated into the
    manifest exactly like any other file -- fully codegen-owned, never hand-edited.
    """
    return []

  def schemas(self, schemas: Mapping[str, Schema]) -> ExternalSchemas:
    """
    Render one `schemas.json` scope's shared shapes into its own types module (design
    §5b), reusing `type_generator` the identical way `rpc_endpoint`/`stream_endpoint`
    already do for a single endpoint's own schemas -- this generalizes that same
    mechanism to a scope shared by more than one endpoint, rather than inventing a new
    rendering convention. A legacy hand-rolled backend implements exactly this for its
    own root `spec/schemas.json` alone; this base implementation additionally supports a
    nested scope (`schemas_scope`, set by the CLI once per discovered file), so it renders
    correctly whether called for the project root or for a subtree like
    `futures/`.

    `schemas_scope` (`()` for the client root, or unset -- both mean the same thing here)
    names the file `schemas.py` and the package `{package_root}.schemas`; a nested scope
    (`('futures',)`, say) names them `futures/schemas.py` and
    `{package_root}.futures.schemas` instead -- `{package_root}` is `core_package` with
    its own trailing `.core` segment stripped, since the CLI always sets `core_package`
    to exactly `f'{output_root.name}.core'` (that attribute's own docstring).

    Empty input renders nothing, the same "no shared schemas to emit" answer the CLI
    already relies on for a project with no root `spec/schemas.json` at all.

    Args:
      schemas: One `schemas.json` file's decoded contents, id to schema -- never more
        than one scope's worth; a project with several scopes gets one `schemas()` call
        per discovered file (`discover_schemas_files`), each with its own `schemas_scope`
        set immediately before the call.
    """
    if not schemas:
      return ExternalSchemas(files=[], references={})
    if self.core_package is None:
      raise ValueError(
        'Generator.schemas needs core_package set (by the CLI, same point as '
        'project/codegen_config) to name the shared-schemas module it renders'
      )
    package_root = self.core_package.rsplit('.', 1)[0]
    scope = self.schemas_scope or ()
    module_parts = (*scope, 'schemas')
    path = '/'.join(module_parts) + '.py'
    package = '.'.join((package_root, *module_parts))
    scope_label = f'`{"/".join(scope)}/`' if scope else 'the client root'
    source_label = (
      f'spec/endpoints/{"/".join(scope)}/schemas.json' if scope else 'spec/schemas.json'
    )

    rendered = self.type_generator()(schemas, inline=True)
    imports = dict(rendered.imports)
    lines: list[str] = [
      f'"""Shapes shared by two or more endpoints under {scope_label}, generated from '
      f'`{source_label}`."""',
      '',
    ]
    typing_names = imports.pop('typing_extensions', None)
    if typing_names:
      lines.append(f'from typing_extensions import {", ".join(sorted(typing_names))}')
    for pkg in sorted(imports):
      if imports[pkg]:
        lines.append(f'from {pkg} import {", ".join(sorted(imports[pkg]))}')
      else:
        lines.append(f'import {pkg}')
    lines.append('')
    lines.append('')
    for def_id in rendered.generation_order:
      lines.append(rendered.definitions[def_id])
      lines.append('')
      lines.append('')
    content = '\n'.join(lines).rstrip('\n') + '\n'

    references = {
      schema_id: ExternalReference(name=rendered.identifiers[schema_id], package=package)
      for schema_id in schemas
    }
    return ExternalSchemas(files=[File(path=path, content=content)], references=references)

  def type_generator(
    self, external_references: Mapping[str, ExternalReference] = {},
  ) -> TypeGenerator:
    """Build the type backend used for this client's schemas.

    Override to plug in a custom parser or renderer; `type_names` uses the same
    backend, so a project's endpoint class names stay aligned with the types it emits.
    """
    return TypeGenerator(
      external_references=external_references,
      render=Renderer(parser=Parser()),
    )

  def subscription(self, endpoint: Endpoint) -> Subscription:
    """Decode one WebSocket endpoint into the parts a generator needs.

    The stream message is read from its JSON schema even when the endpoint also
    declares a binary content type: `docs/spec/spec.md` makes the decoded JSON the
    readable typed contract and the wire frames an additive sidecar. `binary`
    reports the sidecar so a backend can decline the endpoint.

    Args:
      endpoint: The endpoint whose module is about to be generated.

    Raises:
      TypeError: When the endpoint does not use the WebSocket transport.
    """
    if endpoint.channel is None:
      raise TypeError(f'{endpoint.function}: not a WebSocket endpoint')
    channel = endpoint.channel
    operation = endpoint.openapi.model_copy(deep=True)
    message = operation.responses.get(WS_MESSAGE_CODE)
    binary = False
    if message is not None:
      message = ensure_nonref(message)
      content = message.content or {}
      binary = bool(content) and set(content) != {JSON_CONTENT_TYPE}
      if JSON_CONTENT_TYPE in content:
        message.content = {JSON_CONTENT_TYPE: content[JSON_CONTENT_TYPE]}
      operation.responses[WS_MESSAGE_CODE] = message

    operation, schemas = normalize_schemas(operation)
    declared = {
      parameter.name: parameter
      for parameter in (ensure_nonref(p) for p in operation.parameters or [])
      if parameter.in_ == 'path'
    }
    interpolated = [name for name in CHANNEL_PLACEHOLDER.findall(channel) if name in declared]
    ordered = interpolated + [name for name in declared if name not in interpolated]
    channel_parameters = [
      SubscriptionParameter(
        name=name,
        id=PARAMETER_KEY(declared[name]),
        required=bool(declared[name].required),
        description=declared[name].description,
      )
      for name in ordered
    ]

    body = BODY_KEY if BODY_KEY in schemas else None
    parameters: list[SubscriptionParameter] = []
    body_schema = schemas.get(BODY_KEY)
    if isinstance(body_schema, Schema) and body_schema.properties:
      required = set(body_schema.required or [])
      for name, property in body_schema.properties.items():
        id = f'{BODY_KEY}/{name}'
        schemas[id] = property
        parameters.append(SubscriptionParameter(
          name=name,
          id=id,
          required=name in required,
          description=getattr(property, 'description', None),
        ))

    return Subscription(
      channel=channel,
      channel_parameters=channel_parameters,
      parameters=parameters,
      operation=operation,
      schemas=schemas,
      body=body,
      reply=RESPONSE_KEY(WS_REPLY_CODE) if WS_REPLY_CODE in operation.responses else None,
      message=RESPONSE_KEY(WS_MESSAGE_CODE) if WS_MESSAGE_CODE in operation.responses else None,
      binary=binary,
    )

  def stream_payload_schema(
    self, subscription: Subscription, *, field: str | None = None,
  ) -> Schema | Reference | None:
    """Return the schema of the value a subscriber receives, or None when unusable.

    APIs wrap every push in an envelope and the client core unwraps it before the
    payload reaches a caller — forwarding `data` out of `{channel, data, ts}`, say — so
    the generated payload type is that field's schema, not the envelope's. `field` names
    it; omit it for an API whose push *is* the payload.

    Args:
      subscription: The decoded subscription.
      field: Envelope property holding the payload, when the API wraps its pushes.
    """
    if subscription['message'] is None:
      return None
    message = subscription['schemas'].get(subscription['message'])
    if field is None:
      return message
    if not isinstance(message, Schema) or not message.properties:
      return None
    return message.properties.get(field)

  def stream_payload_is_typed(
    self, subscription: Subscription, *,
    field: str | None = None,
    references: Mapping[str, ExternalReference] = {},
  ) -> bool:
    """Report whether the push payload renders to a type worth generating.

    A payload the spec leaves as a bare `object`, or as a union of them, renders to
    `Any`. Generating it trades a typed surface for an untyped one — the same trade
    `test_generated_type_names.py` already refuses for REST.

    Args:
      subscription: The decoded subscription.
      field: Envelope property holding the payload, when the API wraps its pushes.
      references: Shared schemas the payload may `$ref`. A payload that names one and
        cannot resolve it raises rather than reporting an answer, so a project whose
        subscriptions share schemas with its REST tree — an `OrderSide` enum — has
        to pass them.
    """
    schema = self.stream_payload_schema(subscription, field=field)
    if schema is None:
      return False
    types = self.type_generator(references)({STREAM_PAYLOAD_KEY: schema})
    return 'Any' not in re.split(r'\W+', types.identifiers[STREAM_PAYLOAD_KEY])

  def raw_shared_schemas(self) -> Mapping[str, Any]:
    """`spec/schemas.json`'s schemas as plain JSON, keyed by id, for walking a `$ref` met
    on an `envelope.payload` path (`select_schema`). Read off the project when the CLI
    set one, else dumped from `shared_schemas` -- the same map, one representation over."""
    if self.project is not None:
      cached = getattr(self, '_raw_shared_schemas', None)
      if cached is None:
        cached = load_shared_schemas(self.project)
        self._raw_shared_schemas = cached
      return cached
    return {
      key: value.model_dump(by_alias=True, exclude_none=True)
      for key, value in self.shared_schemas.items()
    }

  def returned_response_schema(self, endpoint: Endpoint) -> dict[str, Any] | None:
    """The schema of the value an rpc method returns: `spec.response` itself, or the node
    a declared `envelope.payload` selects inside it.

    The response schema describes the whole wire frame and `envelope.payload` is a
    selector into it (ADR 0010, `docs/spec/authoring.md` rule 6); the wrapper's own
    fields are never rendered, and the core still hands back the unwrapped value, so
    the return type is the selected node's. A `$ref` on the way is walked through
    `raw_shared_schemas`; the selected node is returned as written, a `$ref` node
    included, so the caller renders it as the reference it is.

    Raises:
      ValueError: When the path does not resolve inside the schema, naming the segment
        -- a schema still written for the unwrapped value (`truewire migrate` rewrites
        it), or a path into an `anyOf`/unresolvable `$ref`.
    """
    spec = endpoint.spec
    if not isinstance(spec, RpcEndpointSpec) or spec.response is None:
      return None
    envelope = endpoint.envelope
    if not isinstance(envelope, RpcEnvelopeSpec) or envelope.payload == '':
      return spec.response
    try:
      return select_schema(spec.response, envelope.payload, shared=self.raw_shared_schemas())
    except LookupError as exc:
      raise ValueError(
        f'{spec.path}: envelope.payload {envelope.payload!r} does not resolve inside the '
        f'response schema ({exc}); the schema describes the whole wire frame and the path '
        f'selects the returned value (ADR 0010) -- `truewire migrate` rewrites a schema '
        f'written for the unwrapped value'
      ) from exc

  def endpoint_schemas(self, endpoint: Endpoint) -> Mapping[str, Schema | Reference]:
    """Return every schema one endpoint's module renders, keyed by reference id.

    A new-shape endpoint (`request`/`response` on `RpcEndpointSpec`,
    `parameters`/`payload` on `StreamEndpointSpec`) is handled first, mirroring
    `rpc_endpoint`/`stream_endpoint`'s own `$request`/`$response`/`$parameters`/
    `$payload` id convention exactly -- this method exists so `type_names`/`class_name`
    (collision-avoidance naming, run by the CLI *before* `rpc_endpoint`/`stream_endpoint`
    are called at all) see the same schemas those two methods will actually render.
    Before this branch existed, every new-shape endpoint crashed here with
    `AttributeError: 'NoneType' object has no attribute 'model_copy'` -- `endpoint.openapi`
    is `None` for the new shape, and the old branches below unconditionally read it. No
    unit test constructed a `Generator.class_name`/`type_names` call against a real
    new-shape fixture endpoint before the end-to-end smoke test did.

    `$request`/`$parameters` have their own `title` stripped before being recorded here,
    exactly like `rpc_endpoint`/`stream_endpoint`'s own `request_schema.model_copy(update=
    {'title': None})` -- both of those methods force the type they actually render to the
    fixed name `Request`/`Parameters` regardless of the schema's own title (the fixed-name
    contract), so a caller of *this* method has to see that same fixed-name shape,
    not the schema's original title, or `type_names` reports an occupied name
    (`OrderbookRequest`, say) that nothing in the generated module actually uses, while the
    name genuinely used (`Request`) goes unreported -- a real, if latent, hole in the exact
    collision-avoidance guarantee this method exists to provide. Found and fixed after an
    initial version of this branch validated the schema as-is, title intact; confirmed via
    a reproduction against `market/orderbook` showing `$request`'s title read
    `'OrderbookRequest'` here while the endpoint's own generated code renders `class
    Request(TypedDict):`. `$response`/`$payload` keep their own title unchanged, matching
    `rpc_endpoint`/`stream_endpoint`'s identical treatment of `response`/`payload`.

    Args:
      endpoint: The endpoint whose module is about to be generated.
    """
    spec = endpoint.spec
    if isinstance(spec, RpcEndpointSpec) and (spec.request is not None or spec.response is not None):
      schemas: dict[str, Schema | Reference] = {}
      if spec.request is not None:
        schemas['$request'] = Schema.model_validate(spec.request).model_copy(update={'title': None})
      # The returned value's own schema (`envelope.payload` selected inside the wire-body
      # `response`, ADR 0010) -- the type this leaf actually renders and could collide
      # with. A path that fails to resolve falls back to the whole response here, since
      # naming is all this method feeds; `rpc_endpoint` raises the real error.
      try:
        response = self.returned_response_schema(endpoint)
      except ValueError:
        response = spec.response
      if response is not None:
        # A response that is, in its entirety, one bare `{"$ref": "..."}` needs the
        # same treatment `rpc_endpoint` itself already gives it (see that method's own
        # docstring: `Schema.model_validate` can't represent a bare `$ref` at all --
        # `Schema` declares no `ref` field and silently accepts it as an ignored extra
        # key, producing an empty, untyped schema). Before this branch existed, that
        # meant `type_names`/`class_name` (collision-avoidance naming, run by the CLI
        # *before* `rpc_endpoint` itself resolves the real imported name) saw nothing
        # at all for a bare-$ref response -- invisible to the exact collision it exists
        # to catch. Confirmed live: a `nft.metadata.collection_metadata` endpoint
        # generates a leaf class literally named `CollectionMetadata` (directory-derived)
        # that also imports a same-named `CollectionMetadata` response type from the
        # shared schemas module -- a real name collision `class_name` never saw
        # coming, silently producing an endpoint class shadowing its own response type
        # in the same module.
        schemas['$response'] = (
          Reference.model_validate({'$ref': response['$ref']})
          if isinstance(response.get('$ref'), str)
          else Schema.model_validate(response)
        )
      return schemas
    if isinstance(spec, StreamEndpointSpec):
      parameters_raw = spec.parameters if spec.parameters is not None else spec.request
      if spec.new_shape:
        schemas = {}
        if parameters_raw is not None:
          schemas['$parameters'] = Schema.model_validate(parameters_raw).model_copy(update={'title': None})
        if spec.payload is not None:
          schemas['$payload'] = Schema.model_validate(spec.payload)
        if spec.reply is not None:
          # A bare `$ref` stays a reference (ADR 0022): `Schema.model_validate` would read
          # it as an empty, untyped schema, invisible to the collision check this feeds.
          schemas['$reply'] = (
            Reference.model_validate(spec.reply)
            if isinstance(spec.reply, dict) and set(spec.reply) == {'$ref'}
            else Schema.model_validate(spec.reply)
          )
        return schemas
    # A dual-transport rpc (`transports: ['http', 'ws']`) is representable, but not yet
    # handled here: the first matching branch returns, so an http-and-ws endpoint only
    # ever reaches the http-derived schemas and `subscription` (the ws-derived source)
    # is never consulted.
    if 'http' in endpoint.transports:
      _, schemas = normalize_schemas(endpoint.openapi)
      return schemas
    if 'ws' in endpoint.transports:
      return self.subscription(endpoint)['schemas']
    return {}

  def type_names(
    self, endpoint: Endpoint, references: Mapping[str, ExternalReference],
  ) -> set[str]:
    """Report the module-level names the endpoint's generated types will occupy.

    Covers both the definitions written into the module and the names imported into
    it, since either one is shadowed by a class of the same name.

    A record with a field named after a Python keyword — a wallet transaction's
    `origin.class` — makes `Renderer` emit an extra `<Record>Keywords` base carrying
    that field. Those bases are appended to `generation_order` keyed by the class name
    they already are, with no `identifiers` entry, so the id doubles as the name.

    Args:
      endpoint: The endpoint whose module is about to be generated.
      references: Shared schemas already emitted elsewhere in the package.
    """
    schemas = self.endpoint_schemas(endpoint)
    if not schemas:
      return set()
    rendered = self.type_generator(references)(schemas)
    names = {rendered.identifiers.get(id, id) for id in rendered.generation_order}
    return names.union(*rendered.imports.values()) if rendered.imports else names

  def class_name(
    self, endpoint: Endpoint, references: Mapping[str, ExternalReference], *,
    name: str,
  ) -> str:
    """Choose the generated class name for a leaf endpoint.

    The class name is derived from `endpoint.function` and the response type name is
    authored in the spec, so on a collision the derived name is the one that yields.

    Args:
      endpoint: The endpoint whose module is about to be generated.
      references: Shared schemas already emitted elsewhere in the package.
      name: The name derived from the endpoint's position in the function tree.
    """
    taken = self.type_names(endpoint, references)
    while name in taken:
      name += 'Endpoint'
    return name

  def method_name(
    self,
    endpoint: Endpoint,
    *,
    parent: tuple[str, ...],
    child_name: str,
    is_aggregate_parent: bool,
  ) -> str:
    """Choose the generated method name for a leaf endpoint.

    Always the real name, regardless of `is_aggregate_parent` (S29, `docs/production_
    standards.md`): `router()` inherits every endpoint-kind child directly as
    a base at any depth -- a leaf under a mixed node (e.g. a `Portfolio` whose
    `nft_contracts`/`nfts`/`token_balances`/`tokens` leaves sit beside its own
    `transactions` sub-router, so `is_aggregate_parent` is `False` for all four) resolves
    to a real, distinctly named method exactly like a leaf under a pure aggregate does --
    there is no depth at which `__call__` is actually needed, and generating one here
    fails `truewire standards --only no-call` outright, the same S29-violating shape
    a legacy backend once had. `is_aggregate_parent` is accepted only so a per-project
    override can still read it if
    it ever needs to (none currently do); this default never branches on it.
    """
    return child_name

  def identifier(self, name: str) -> str:
    """Convert a spec parameter name to the identifier the generated method gives it.

    A project that renames parameters — turning `pageSize` into `page_size` and `type`
    into `type_` — overrides this, so that machinery reading the spec by name, such as
    `paged_method`, addresses the same parameter the header declares.

    Args:
      name: Parameter name exactly as the operation declares it.
    """
    return safe_identifier(name)

  def paged_name(self, method_name: str) -> str:
    """Name the page iterator that drives `method_name`.

    A leaf endpoint hanging off a router as a field is called through the attribute it is
    annotated under — `client.v1.trading.candles(...)` — so `method_name` is `__call__`,
    and `__call___paged` is not an attribute anyone can reach: two leading underscores
    and no trailing one is exactly the form Python mangles inside a class body, so the
    iterator would be bound as `_Candles__call___paged` and the name the docstring
    promises would raise `AttributeError`. Such an endpoint gets `paged` instead.

    Args:
      method_name: Name of the single-request method the iterator drives.
    """
    if method_name == '__call__':
      return PAGED_CALL_NAME
    return f'{method_name}{PAGED_SUFFIX}'

  def paged_local(self, name: str, taken: Container[str]) -> str:
    """Return a loop-local name that cannot shadow a parameter of the generated method.

    Args:
      name: Preferred name.
      taken: Names the generated signature already binds.
    """
    while name in taken:
      name += '_'
    return name

  def pagination_driver(self, pagination: Pagination) -> str:
    """Return the spec name of the request parameter a paginated walk advances.

    Args:
      pagination: Declaration carried by the endpoint.
    """
    if pagination.strategy == 'page':
      return pagination.index.parameter
    if pagination.strategy == 'token':
      return pagination.cursor.parameter
    if pagination.strategy == 'seek':
      return pagination.moving
    return pagination.offset.parameter

  def pagination_driver_required(
    self, header: 'Function', pagination: Pagination,
  ) -> bool:
    """Whether a `token` walk's cursor parameter is required on the single-request
    method (no server-side default), the way `paged_response_token` decides it --
    factored out here so a dispatch site deciding whether an endpoint
    is `PaginatedResponse`-*seedable* (`rpc_endpoint`/`grpc_endpoint`'s own `seedable`
    gate) can ask the identical question before ever calling into either.

    `False` for every other strategy: `page`/`offset` seed from `pagination.index.start`
    or `0`, never ambiguous with "done" the way an absent `token`/`seek` cursor is, so
    neither needs this question asked at all. deribit's `market_data.
    get_volatility_index_data` (`token`, `end_timestamp` required) and its
    `get_mark_price_history`/`get_funding_rate_history` (`seek`, `start_timestamp`
    required) are the real, motivating cases.

    Args:
      header: Rendered header of the single-request method the walk drives, *before* any
        nested-pagination flattening -- matching every real caller today, none of which
        declares a required nested cursor (`docs/pagination.md`/`flatten_nested_pagination`'s
        own scope note).
      pagination: Declaration carried by the endpoint.
    """
    if pagination.strategy != 'token':
      return False
    driver = self.identifier(self.pagination_driver(pagination))
    driver_param = next(
      (param for param in (*header.args, *header.kwargs) if param.name == driver), None,
    )
    return driver_param is not None and driver_param.required

  def paged_state_type(
    self, header: 'Function', pagination: Pagination,
    nested_fields: Mapping[str, Mapping[str, str]] | None = None,
  ) -> str:
    """Resolve the bare (non-`| None`) rendered type of a paginated endpoint's cursor
    parameter, for `paged_response_method`'s `state_type` -- a `token`/`seek` cursor is
    not always a string: kucoin's `spot.orders_hf.get_trade_history`/`margin.orders_hf.
    get_trade_history` (`lastId`) is a genuine `integer` cursor, confirmed against a
    real generation failure (the caller's own correctly-`int`-typed `last_id` keyword
    rejected by a `_paged` loop hardcoded to `state: str = ...`). Falls back to `'str'`
    when the driver parameter can't be resolved on `header` at all (a stream-derived or
    otherwise unusual shape) -- the same default every already-migrated client's own
    cursor happened to already be, so this generalizes the hardcoded case rather than
    changing it.

    `nested_fields` resolves the type directly from its own declared table
    (`flatten_nested_pagination`'s own shape) when the cursor is a dotted reference into
    a single nested request parameter (`_nested_pagination_ref`) -- `header` never carries
    a real parameter named `pagination.key`, only the outer `pagination` group's own
    (wrapper-typed) parameter, which is the wrong type entirely for the inner field. dYdX's
    gRPC `token`/`absent_cursor` endpoints are the motivating case: `key` is a `bytes`
    field nested inside `cosmos.base.query.v1beta1.PageRequest`, not the group's own
    `PageRequest | None` type `header` would otherwise resolve to.

    Args:
      header: The generated method's own header, before pagination-only params are
        stripped -- still carries the cursor parameter's own resolved type.
      pagination: This endpoint's declared pagination.
      nested_fields: See `flatten_nested_pagination`. `None` for the ordinary flat case,
        same as every caller before this parameter existed.
    """
    driver = self.pagination_driver(pagination)
    nested = _nested_pagination_ref(driver)
    if nested is not None and nested_fields is not None:
      outer, inner = nested
      inner_type = nested_fields.get(outer, {}).get(inner)
      if inner_type is not None:
        return inner_type
    driver = self.identifier(driver)
    param = next((p for p in (*header.args, *header.kwargs) if p.name == driver), None)
    if param is None or param.type is None:
      return 'str'
    return param.type.removesuffix(' | None')

  def paged_window_bound(
    self, expression: str, *, unit: str, param: 'Function.Param',
  ) -> tuple[str, bool]:
    """Return the expression that reduces a `seek` bound to the number the venue takes,
    and whether that expression is still a `datetime`.

    A `seek` walk declaring a `span` adds the span to its moving bound, so a client whose
    timestamp parameters also accept a `datetime` overrides this to normalise them first
    (`datetime + int` is not arithmetic). The unit is passed because a venue that bounds
    in seconds and one that bounds in milliseconds need different conversions.

    The second element of the return is not decoration: it is the one place a backend
    states what the loop variable ends up being, and `paged_response_seek` renders the
    span as a `timedelta(...)` or a bare count from it rather than re-deriving it from
    `param.type`. A bound normalised to an integer here and a span still rendered as
    `timedelta(...)` is a `TypeError` at runtime, and the two cannot say different
    things because there is only one place either of them is said. **An override that
    normalises a `datetime` bound to a number must return `False`, not the declared
    type.**

    Args:
      expression: Expression holding the bound as the caller passed it.
      unit: Unit the declaration states the bounds in.
      param: Header parameter carrying the bound.

    Returns:
      The expression to bind the loop-local bound to, and whether that expression is
      still a `datetime` once bound.
    """
    return expression, param.type in HttpRequest.TIMESTAMP_HELPERS

  _TIMEDELTA_UNITS = {'us': 'microseconds', 'ms': 'milliseconds', 's': 'seconds'}
  """`pagination.step.unit` code -> the `timedelta` keyword it names."""

  def paged_size(
    self, pagination: Pagination, parameters: list['Function.Param'],
  ) -> 'Function.Param | None':
    """Return the header parameter carrying the declared page size, when there is one.

    A declaration naming a size the operation does not take yields None rather than
    raising: the terminators that cannot proceed without one say so themselves, and the
    ones that can are unaffected.

    Args:
      pagination: Declaration carried by the endpoint.
      parameters: Header parameters of the single-request method.
    """
    if pagination.size is None:
      return None
    name = self.identifier(pagination.size.parameter)
    return next((param for param in parameters if param.name == name), None)

  def paged_size_default(self, endpoint: Endpoint) -> int | None:
    """Return the row cap the venue applies when the caller sends no page size.

    Declared, not inferred, like everything else here: a venue documenting a default page
    size states it as `default` on the size parameter's own schema. Prose does not reach
    the generator — bybit writes "defaults to 200" in five descriptions and the walk that
    read only the parameter could not see any of them.

    A migrated (design §7) endpoint carries this on `endpoint.request`'s own flat
    `properties[pagination.size.parameter]` rather than `endpoint.openapi.parameters` --
    mirrors every other dual-shape gate in this module (`_one_shape`'s own pattern), just
    never extended here, since no migrated client declared an `offset`/`total`-terminated
    pagination needing a size default before kraken's own `spot.account.trades_history`
    (Task 34) -- without this, `paged_step` raised `ValueError` for every migrated
    endpoint whose walk genuinely needs one, even when the spec correctly declares it.

    Args:
      endpoint: The endpoint whose module is being generated.
    """
    pagination = endpoint.pagination
    if pagination is None or pagination.size is None:
      return None
    request = endpoint.request
    if request is not None:
      properties = request.get('properties') if isinstance(request, dict) else None
      prop = properties.get(pagination.size.parameter) if isinstance(properties, dict) else None
      default = prop.get('default') if isinstance(prop, dict) else None
      return default if isinstance(default, int) else None
    operation = endpoint.openapi
    if operation is None:
      return None
    for parameter in operation.parameters or []:
      if isinstance(parameter, Reference) or parameter.name != pagination.size.parameter:
        continue
      schema = parameter.schema_
      if isinstance(schema, Schema) and isinstance(schema.default, int):
        return schema.default
      return None
    return None

  def paged_size_maximum(self, endpoint: Endpoint) -> int | None:
    """Return the largest page size the venue honours, when the size parameter's schema
    declares a `maximum`.

    A caller may ask for more than the venue serves (`limit=5000` against a 1000-row
    clamp); the page that comes back is then full at the venue's maximum, not short at the
    caller's number, and a walk reading it as short would stop with rows left. Same
    dual-shape lookup as `paged_size_default`.

    Args:
      endpoint: The endpoint whose module is being generated.
    """
    pagination = endpoint.pagination
    if pagination is None or pagination.size is None:
      return None
    request = endpoint.request
    if request is not None:
      properties = request.get('properties') if isinstance(request, dict) else None
      prop = properties.get(pagination.size.parameter) if isinstance(properties, dict) else None
      maximum = prop.get('maximum') if isinstance(prop, dict) else None
      return int(maximum) if isinstance(maximum, (int, float)) and maximum == int(maximum) else None
    operation = endpoint.openapi
    if operation is None:
      return None
    for parameter in operation.parameters or []:
      if isinstance(parameter, Reference) or parameter.name != pagination.size.parameter:
        continue
      schema = parameter.schema_
      # `Schema.maximum` parses as a float (JSON Schema allows a non-integer bound); a
      # page size's maximum is a whole number of rows.
      maximum = schema.maximum if isinstance(schema, Schema) else None
      if isinstance(maximum, (int, float)) and maximum == int(maximum):
        return int(maximum)
      return None
    return None

  def paged_size_given(self, endpoint: Endpoint, size: 'Function.Param') -> str:
    """Return the expression for the page size the venue honours when the caller passes
    one: the caller's own value, clamped to the schema's `maximum` when declared.

    Args:
      endpoint: The endpoint whose module is being generated.
      size: Header parameter carrying the page size.
    """
    maximum = self.paged_size_maximum(endpoint)
    return f'min({size.name}, {maximum})' if maximum is not None else size.name

  def paged_seek_size(self, endpoint: Endpoint, size: 'Function.Param | None') -> str | None:
    """Return the statement that clamps a `seek` walk's page size before the walk starts,
    or `None` when there is no integer size to clamp.

    The walk sends the clamped value on every request and measures a full page against
    it (ADR 0013): at most the schema's `maximum`, when declared, and at least 2. A page
    of 1 cannot advance past a venue whose moving bound is inclusive: it re-serves the
    boundary row and nothing else, so every page is full and shares one key. A size the
    caller omits stays omitted.

    Args:
      endpoint: The endpoint whose module is being generated.
      size: Header parameter carrying the page size, when one is declared.
    """
    if size is None or (size.type or '').removesuffix(' | None') != 'int':
      return None
    maximum = self.paged_size_maximum(endpoint)
    clamped = f'max({size.name}, 2)'
    if maximum is not None:
      clamped = f'min({clamped}, {maximum})'
    if size.required and not (size.type or '').endswith('| None'):
      return f'{size.name} = {clamped}'
    return f'{size.name} = {clamped} if {size.name} is not None else None'

  def paged_seek_size_rule(self, endpoint: Endpoint) -> str:
    """Return the docstring sentence stating the page sizes `paged_seek_size` lets a `seek`
    walk request.

    A `maximum` below 2 leaves no room for the floor: the walk requests pages of exactly
    that many rows, and the sentence makes no claim of a floor.

    Args:
      endpoint: The endpoint whose module is being generated.
    """
    maximum = self.paged_size_maximum(endpoint)
    if maximum is not None and maximum < 2:
      return f'The walk requests pages of {maximum} row{"" if maximum == 1 else "s"}.'
    bounds = f'at least 2 rows and at most {maximum}' if maximum is not None else 'at least 2 rows'
    return (
      f'The walk requests pages of {bounds}: a page must hold one new row beside the one '
      f'it re-reads.'
    )

  def paged_page_size(
    self, endpoint: Endpoint, size: 'Function.Param | None', *, fallback: str | None = None,
  ) -> str | None:
    """Return the expression for the number of rows the venue serves on one call: the
    caller's own size, clamped to the venue's declared `maximum` when there is one, or
    `fallback` (a declared default or fixed cap) when the caller omits an optional size.

    Args:
      endpoint: The endpoint whose module is being generated.
      size: Header parameter carrying the page size, when one is declared.
      fallback: Expression for the venue's own page size when the caller sends none, or
        `None` when nothing declares it.

    Returns:
      The expression, or `None` when no size parameter is declared and there is no
      fallback either.
    """
    if size is None:
      return fallback
    given = self.paged_size_given(endpoint, size)
    if self.paged_always_set(size) or (fallback is None and given == size.name):
      return given
    return f'({given} if {size.name} is not None else {fallback})'

  def paged_cap(self, endpoint: Endpoint, size: 'Function.Param | None') -> str | None:
    """Return the row count a full page is full relative to, when the spec settles one.

    The cap is what the venue would have returned had it more to give: the caller's own
    size where the method always sends one, and the venue's declared default where the
    caller may omit it. `None` means the spec settles neither — the size is optional and
    no default is declared — and then no guard is generated at all. A guard reading
    `size is not None` would be silent on precisely the call that loses rows, the one that
    passes bounds and no size, so it would buy a promise the docstring could not keep. The
    fix for such an endpoint is to declare the default the venue documents.

    Args:
      endpoint: The endpoint whose module is being generated.
      size: Header parameter carrying the page size, when one is declared.
    """
    if size is None:
      return None
    if self.paged_always_set(size):
      return size.name
    default = self.paged_size_default(endpoint)
    if default is None:
      return None
    return f'({size.name} if {size.name} is not None else {default})'

  def request_imports(self, request: HttpRequest) -> Mapping[str, set[str]]:
    """Return the imports the request an endpoint generates needs from the client core.

    `HttpRequest` emits `timestamp_millis.dump(...)` (or the sibling helper matching a
    parameter's declared render id -- see `HttpRequest.TIMESTAMP_HELPERS`) for a timestamp
    parameter and no import for the helper it names; the helpers live in
    `truewire_core.types` beside the aliases, so the import is added here. A module
    generated without it raises `NameError` on first import, which gates 1, 2 and 3 never
    reach, and gate 4 reports as a bare undefined name.

    Args:
      request: The request rendered for this endpoint's method.
    """
    helpers = request.helpers
    if not helpers:
      return {}
    return {TYPES_PACKAGE: set(helpers)}

  def read_path(
    self, path: str, *, subject: str, name: str, accessor: Literal['dict', 'attr'] = 'dict',
    subject_optional: bool = True,
  ) -> list[str]:
    """Emit the statements binding `name` to the value a dotted response path names.

    Every step is guarded, including the first, unless told otherwise: the audit settles
    that a path exists in the schema, not that a given payload carries it, and a
    paginated walk's own terminator can be the reason `subject` itself is `None` — a
    `token` walk whose API signals exhaustion with a bare `null` page reads its cursor
    from exactly that page.

    Args:
      path: Plain dotted key, as `ResponsePath` validates it.
      subject: Expression holding the payload the path is read from.
      name: Name the final value is bound to.
      accessor: How to read one key off the payload -- `'dict'` (`.get(key)`) for every
        JSON-shaped HTTP/WS response, the default every existing client relies on;
        `'attr'` (`getattr(x, key, None)`) for a real typed object, which is what a gRPC
        response actually is (a betterproto2 dataclass, never a dict).
      subject_optional: Whether `subject` itself (the *first* step only -- every deeper
        one stays guarded regardless, since a betterproto2 message-typed field can
        legitimately be declared `Optional` even when the response object holding it
        isn't) can genuinely be `None`. `True` for a JSON-shaped response read off a
        payload that can itself be a bare `null` (the `null`-page case above); `False`
        when `subject` is known non-`None` by construction -- a gRPC response object,
        which a generated method either returns for real or never returns at all
        (raising instead), or a JSON-shaped response that is likewise a generated
        method's own always-present return value -- guarding it anyway only costs
        pyright a spurious `X | None` it can't prove away, for a case that cannot happen.
    """
    keys = path.split('.')
    lines: list[str] = []
    source = subject
    for index, key in enumerate(keys):
      target = name if index == len(keys) - 1 else f'{name}_{index}'
      if index == 0 and not subject_optional:
        if accessor == 'attr':
          # Direct attribute access, not `getattr(x, key, None)`: `subject` is known
          # non-`None`, and unlike `getattr` with a string-literal key -- which pyright
          # always types `Any | None`, unable to resolve it statically -- `source.key`
          # keeps the field's real declared type (`list[Coin]`, say), not an erased one.
          lines.append(f'{target} = {source}.{key}')
        else:
          # `.get(key)` without the `if source is not None` ternary: `subject` is known
          # non-`None`, so the guard only costs pyright a spurious `X | None` it can't
          # prove away for a case that cannot happen. Still `.get(...)`, not `[key]` --
          # the audit settles that the path exists in the schema, not in this payload.
          lines.append(f'{target} = {source}.get({key!r})')
      else:
        read = f'{source}.get({key!r})' if accessor == 'dict' else f'getattr({source}, {key!r}, None)'
        lines.append(f'{target} = {read} if {source} is not None else None')
      source = target
    return lines

  def row_field_read(self, row: str, field: str, name: str) -> list[str]:
    """Return statements binding `name` to a dotted-key/bracket-index field read off one
    row, tolerant of a missing key or an out-of-range index at any level -- the same
    tolerance `read_path` gives a top-level response path.

    A dict-key segment emits `.get(key)`; a bracket-index segment emits a bounds-checked
    `[index]`, since indexing a list out of range raises `IndexError` where a dict's
    `.get` merely returns `None` -- most candle rows are positional tuples (the open time
    at a fixed array index), which is exactly what needs this (`docs/pagination.md` §3).

    Each level but the last binds its own local (`<name>_0`, `<name>_1`, ...), which the
    next level's `is not None` guard then narrows. One nested expression per level would
    repeat the whole parent read inside the guard, and pyright never narrows a repeated
    call expression: weather.gov's `[-1].properties.timestamp` failed standard mode with
    `"get" is not a known attribute of "None"`.

    Binds the raw wire value. Normalizing it for comparison (parsing an epoch number or
    numeral string through the row field's own converter when validation is off, casting a
    numeric id) is `paged_response_seek`'s nested `key_of` helper's job, which narrows
    `name` past `None` before converting.

    Args:
      row: Expression holding one row of a `seek` walk's own row collection.
      field: A `SeekCursor.field`, its `[-1]` prefix already stripped
        (`last_row_field`) -- everything relative to one row.
      name: Local the read is bound to; intermediate levels take it as their prefix.
    """
    segments = path_segments(field)
    if not segments:
      # A bare `[-1]`: the row itself is the cursor (rows that are plain epoch numbers).
      return [f'{name} = {row}']
    lines: list[str] = []
    source = row
    for index, (kind, key) in enumerate(segments):
      target = name if index == len(segments) - 1 else f'{name}_{index}'
      if kind == 'index':
        bound = f'len({source}) > {key}' if key >= 0 else f'len({source}) >= {-key}'
        read = f'({source}[{key}] if {source} is not None and {bound} else None)'
      else:
        read = f'({source}.get({key!r}) if {source} is not None else None)'
      lines.append(f'{target} = {read}')
      source = target
    return lines

  def paged_step(
    self, size: 'Function.Param | None', *, rows: str | None, default: int | None = None,
  ) -> str:
    """Return the expression an `offset` walk advances its offset by.

    Row count first, because it is what an offset walk means and it is right whether or not
    the caller asked for a page size. A size parameter the method leaves optional cannot be
    added to an integer when it is omitted, which is why this is not simply the size --
    unless the venue documents what it defaults to, the same `default` a `seek` walk's
    cap already falls back to (`paged_cap`); the two mirror each other because both are
    "what the venue actually used when the caller left it unset".

    Args:
      size: Header parameter carrying the page size, when one is declared.
      rows: Expression holding the page's rows, when the terminator measures them.
      default: The venue's documented default for `size`, when declared on its schema
        (`paged_size_default`). `None` when the venue documents none.

    Raises:
      ValueError: When none is available — a total-terminated `offset` walk whose size
        the caller may omit, with no documented default, leaves nothing to step by.
    """
    if rows is not None:
      return f'len({rows})'
    if size is not None and self.paged_always_set(size):
      return size.name
    if size is not None and default is not None:
      return f'({size.name} if {size.name} is not None else {default})'
    raise ValueError(
      'an `offset` walk needs either rows to count, a page size the method always has, '
      'or a documented default for an optional one'
    )

  def paged_always_set(self, size: 'Function.Param') -> bool:
    """Report whether a page-size parameter is guaranteed non-`None` inside the loop.

    Args:
      size: Header parameter carrying the page size.
    """
    return size.required and size.default is None

  def paged_rows(
    self, path: str | None, *, response: str, taken: Container[str], lines: list[str],
    response_accessor: Literal['dict', 'attr'] = 'dict',
  ) -> str:
    """Return the expression holding a page's rows, appending any read it needs.

    `read_path`'s own `subject_optional=False`: `response` is always `await
    self.{method_name}(...)`'s own return value -- either a real value or a raise, the
    same always-present-return-value reasoning `paged_response_method`'s own identical
    read already relies on -- so guarding it as `X | None` here only costs pyright a
    spurious optional it can't prove away, and (once `path` resolves through it, `rows`'
    own use unconditionally indexed/iterated by `paged_response_seek`) fails pyright for
    real: `reportOptionalSubscript`/`reportOptionalIterable`/
    `len()`-on-`Sized` errors, confirmed against a real generation failure (bybit's
    enveloped `market.kline` family, the first real caller to exercise this path with
    `done.rows` actually set).

    Args:
      path: Declared response path of the collection, or None when the payload is it.
      response: Name holding the page just yielded.
      taken: Names the generated signature already binds.
      lines: Statement list the read is appended to.
      response_accessor: How the response payload's fields are read -- see `read_path`.
    """
    if path is None:
      return response
    rows = self.paged_local('rows', taken)
    lines.extend(
      self.read_path(
        path, subject=response, name=rows, accessor=response_accessor,
        subject_optional=False,
      )
    )
    return rows

  def paged_summary(
    self, method_name: str, *, walk: str | None = None, ends: str | None = None,
    note: str | None = None, body: str | None = None,
    docstring: 'Docstring | None' = None, outer: 'Function | None' = None,
    extra_params: 'list[Docstring.Param] | None' = None,
  ) -> str:
    """Assemble a page iterator's docstring from its walk and its terminator.

    When the single-request sibling's own `Docstring` (built once in `rpc_endpoint`) and
    the page wrapper's own real, already-filtered signature (`outer`) are both given, this
    reuses the sibling's own description/`Args:`/`References:` instead of the bare
    structural text below — a caller reading `<method>_paged`'s own docstring otherwise
    has to already know the sibling's parameters and what they mean, since the paged
    variant never repeated any of it. `Args:` is filtered to exactly `outer`'s own real
    parameter names, so a parameter the walk manages internally (a dropped pagination
    driver — `last_id`, `cursor`, ...) never appears for a variant that does not accept
    it; `extra_params` (a `span`) are appended for what the paged variant adds that the
    sibling never had. Falls back to the original bare text when
    either is omitted — a caller that has not threaded the sibling's `Docstring` through
    yet (a not-yet-updated per-client backend) keeps exactly today's rendering.

    Args:
      method_name: Name of the single-request method the iterator drives.
      walk: Prose naming the parameter the loop advances. Required unless `body` is given.
      ends: Prose naming what ends the loop. Required unless `body` is given.
      note: Prose stating what the walk guarantees or raises on, when there is anything
        beyond the terminator worth a caller's attention.
      body: A complete summary paragraph, verbatim, replacing the `walk`/`ends`-built one
        — the `PaginatedResponse`-shaped wrappers' own "awaitable or async-iterable" fact,
        which isn't a walk/terminator statement at all.
      docstring: The single-request sibling's own `Docstring`, when available.
      outer: The page wrapper's own real, rendered signature, when available.
      extra_params: `Args:` entries the paged variant adds beyond the sibling's own
        parameters (a `span`).
    """
    if body is None:
      assert walk is not None and ends is not None
      body = f'{walk} and {ends}. Awaitable (flattens every page) or async-iterable (one page at a time).'
    # An endpoint reached by calling its attribute has no name to quote at the reader:
    # `__call__` is how the method is spelled in the class, never how it is invoked.
    subject = 'this endpoint' if method_name == '__call__' else f'`{method_name}`'
    if docstring is not None and outer is not None:
      kept_names = {param.name for param in outer.args} | {
        param.name for param in outer.kwargs
      }
      params = [param for param in docstring.params if param.name in kept_names]
      seen = {param.name for param in params}
      for param in extra_params or []:
        if param.name not in seen:
          params.append(param)
          seen.add(param.name)
      paragraphs = [
        (docstring.description or '').strip(),
        f'Paged variant of {subject}: {body}',
        *([note] if note else []),
      ]
      enriched = Docstring(
        description='\n\n'.join(p for p in paragraphs if p),
        docs_url=docstring.docs_url, docs_label=docstring.docs_label, params=params,
      )
      return enriched.code()
    return '\n'.join([
      f'"""Yield successive pages of {subject}.',
      '',
      *textwrap.wrap(body, width=PAGED_DOC_WIDTH),
      *(
        line for paragraph in (note or '').split('\n\n') if paragraph
        for line in ('', *textwrap.wrap(paragraph, width=PAGED_DOC_WIDTH))
      ),
      '"""',
    ])

  _NESTED_DRIVER_FIELD: Mapping[str, str] = {
    'page': 'index', 'offset': 'offset', 'token': 'cursor',
  }
  """Pagination-model attribute holding the request-parameter-carrying sub-model
  (`PageIndex`/`PaginationParameter`/`Cursor`), per strategy -- every strategy but `page`
  names it the same as the strategy itself; `page`'s is `index`. `seek` has no entry: it
  moves one of two declared bounds, not one driver parameter, and no real endpoint nests
  its bounds inside a message, so there is nothing here for a nested shape to name."""

  def flatten_nested_pagination(
    self, pagination: Pagination, header: Function,
    nested_fields: Mapping[str, Mapping[str, str]] | None,
  ) -> tuple[
    Pagination, Function,
    tuple[str, str, str, str | None, Mapping[str, str]] | None,
  ]:
    """Rewrite a `page`/`offset`/`token` pagination whose cursor/size are dotted paths
    into one nested request-message parameter into an equivalent flat declaration +
    synthetic header, so the rest of the page renderers' flat-parameter-name logic runs
    completely unchanged.

    A gRPC unary request that wraps Cosmos-SDK-style pagination in one message field
    (`pagination: PageRequest | None`, itself carrying `key`/`limit`/`reverse`) is the
    motivating case: `pagination.cursor.parameter` is declared `"pagination.key"`, which
    names no flat parameter of the single-request method at all -- `"pagination"` does,
    but `"pagination.key"` doesn't, and never can, since a message field is not a
    parameter. A REST venue whose page-driven listing endpoints bundle `pageNo`/`pageSize`
    inside one POST body object rather than exposing them as flat query-role parameters
    (bitget's `classic.broker.agent_customer_*`) hits the identical shape one level down
    -- a JSON object field instead of a protobuf message field, same "the driver isn't a
    flat parameter" problem. Scoped to one level of nesting: nothing in this codebase's
    pagination declarations needs more, and a generalized nested-path grammar is exactly
    the expressiveness `docs/spec/authoring.md` rule 7 already refuses admitting for
    response paths, for the same reason.

    Args:
      pagination: Declaration carried by the endpoint.
      header: Rendered header of the single-request method, before flattening.
      nested_fields: The outer parameter's own field names and rendered types (`bytes`,
        `int`, ... or `X | None` when the message itself declares the field optional),
        keyed by outer parameter name -- supplied by a backend that knows the nested
        message type via its own introspection, since this method has no way to
        discover it. `None`, or a cursor/size that isn't dotted, is the ordinary flat
        case: returned unchanged.

    Returns:
      `(pagination, header)`, rewritten if flattening applied, and either `None` or
      `(outer_parameter, outer_type, inner_cursor, inner_size, fields)` recording what
      to reconstruct at call time -- `paged_call` reads this after building the call
      to the single-request method, merging the flat driver/size entries back into one
      constructed message argument, coercing one that isn't itself declared optional
      (a betterproto2 field's constructor never accepts `None` -- see
      `_SCALAR_ZERO_VALUES`) to its zero value rather than passing `None` through.

    Raises:
      ValueError: A dotted cursor/size names an outer parameter or nested field
        `nested_fields` doesn't declare, or cursor and size disagree about which outer
        parameter they nest into.
    """
    driver_field = self._NESTED_DRIVER_FIELD.get(pagination.strategy)
    if nested_fields is None or driver_field is None:
      return pagination, header, None
    driver = getattr(pagination, driver_field)
    cursor_ref = _nested_pagination_ref(driver.parameter)
    if cursor_ref is None:
      return pagination, header, None
    outer_name, inner_cursor = cursor_ref
    outer_identifier = self.identifier(outer_name)
    outer_param = next(
      (p for p in [*header.args, *header.kwargs] if p.name == outer_identifier), None,
    )
    fields = nested_fields.get(outer_identifier)
    if outer_param is None or fields is None:
      raise ValueError(
        f'pagination.{driver_field}.parameter {driver.parameter!r} names no method '
        f'parameter {outer_identifier!r} with declared nested_fields'
      )
    if inner_cursor not in fields:
      raise ValueError(
        f'{inner_cursor!r} is not a declared field of {outer_identifier!r} '
        f'({sorted(fields)})'
      )
    inner_size: str | None = None
    if pagination.size is not None:
      size_ref = _nested_pagination_ref(pagination.size.parameter)
      if size_ref is not None:
        size_outer, inner_size = size_ref
        if self.identifier(size_outer) != outer_identifier:
          raise ValueError(
            f'pagination.cursor and pagination.size nest into different parameters '
            f'({outer_identifier!r} vs {self.identifier(size_outer)!r}) -- not supported'
          )
        if inner_size not in fields:
          raise ValueError(
            f'{inner_size!r} is not a declared field of {outer_identifier!r} '
            f'({sorted(fields)})'
          )

    flat_cursor = self.identifier(inner_cursor)
    flat_size = self.identifier(inner_size) if inner_size is not None else None

    def flatten(params: list['Function.Param']) -> list['Function.Param']:
      """Replace the outer nested parameter with its flattened driver/size fields."""
      out: list[Function.Param] = []
      for param in params:
        if param.name != outer_identifier:
          out.append(param)
          continue
        # `required=False`: a nested cursor field is never required on the first call --
        # proto3's own zero-value semantics mean omitting it starts from the beginning,
        # the same convention `token` walks already assume for a flat cursor. A venue
        # that genuinely requires a starting value on every call (deribit's
        # `end_timestamp`) has no nested-message shape to be declaring here at all.
        # `.removesuffix(' | None')`: `Param(required=False)` already appends its own
        # ` | None`, so a field the message itself already declares optional would
        # otherwise double up.
        out.append(Function.Param(
          name=flat_cursor, type=fields[inner_cursor].removesuffix(' | None'), required=False,
        ))
        if inner_size is not None and flat_size is not None:
          out.append(Function.Param(
            name=flat_size, type=fields[inner_size].removesuffix(' | None'), required=False,
          ))
      return out

    flat_header = Function(
      name=header.name, asyn=header.asyn, method=header.method,
      args=flatten(header.args), kwargs=flatten(header.kwargs),
      return_type=header.return_type, return_type_alias=header.return_type_alias,
      decorators=header.decorators,
    )
    flat_pagination = pagination.model_copy(update={
      driver_field: driver.model_copy(update={'parameter': flat_cursor}),
      'size': (
        pagination.size.model_copy(update={'parameter': flat_size})
        if pagination.size is not None and flat_size is not None
        else pagination.size
      ),
    })
    return (
      flat_pagination, flat_header,
      (
        outer_identifier, (outer_param.type or '').removesuffix(' | None'),
        inner_cursor, inner_size, fields,
      ),
    )

  def pagination_rows_path(self, pagination: Pagination) -> str:
    """Return the declared response path of a paginated endpoint's row collection, `''`
    when the payload is itself the collection.

    Args:
      pagination: Declaration carried by the endpoint.
    """
    rows = pagination.rows if pagination.strategy == 'seek' else pagination.done.rows
    return rows or ''

  def seek_key_kind(
    self, param: 'Function.Param', *, cursor_format: str | None = None,
  ) -> tuple[str, str | None]:
    """Classify how a `seek` walk normalizes and orders its cursor keys, from the moving
    bound parameter's own rendered type.

    A timestamp-typed bound (`TimestampMillis`, ...) compares `datetime`s. A response field
    is only a real `datetime` when validation is on (S8), and a raw `int`/numeral `str`
    otherwise, so the walk parses a raw value through the *row field's* own converter:
    `cursor_format` when the row declares an instant format other than the bound's
    (lighter's `epoch-seconds` fundings under an `epoch-millis` bound, TRU-197), else the
    bound's, which is the same format. Only the request encodes the next value in the
    bound's format. A row field with no timestamp format under a timestamp bound would
    leave the unit to a guess; `truewire check` refuses it. An `int`/`float` bound
    casts the row value, so a venue that serializes a numeric id as a string (kucoin's
    ledger `id`, dYdX's block heights) still compares numerically. Anything else -- a
    plain string id (bitget's `idLessThan`, mexc's `fromId`) -- is compared by equality
    only, and the walk takes the *last* row's key in wire order as its extreme rather
    than `max`/`min`, which `check_seek` only permits for a `unique` cursor.

    Args:
      param: The moving bound's header parameter.
      cursor_format: `seek_cursor_format` of the endpoint's plan: the row field's own
        instant format when it differs from the bound's, else `None`.

    Returns:
      `('timestamp', <converter helper name>)`, `('int', None)`, `('float', None)` or
      `('str', None)`.
    """
    base = (param.type or 'str').removesuffix(' | None')
    helper = HttpRequest.TIMESTAMP_HELPERS.get(base)
    if helper is not None:
      if cursor_format is not None:
        helper = HttpRequest.TIMESTAMP_HELPERS[TIMESTAMP_ALIASES[cursor_format]]
      return 'timestamp', helper
    if base in ('int', 'float'):
      return base, None
    return 'str', None

  def paged_imports(
    self, endpoint: Endpoint, *, header: Function, cursor_format: str | None = None,
  ) -> Mapping[str, set[str]]:
    """Return the imports the `PaginatedResponse`-shaped page method this endpoint
    generates needs, beyond the row type's own (which the caller resolves).

    Every strategy needs `PaginatedResponse`. A `seek` walk also raises `LogicError` (a
    full page sharing one key, or a carried-over row vanishing), parses timestamp keys
    through the row field's converter (`datetime`, `cast`, and the runtime's helper; see
    `seek_key_kind`), and an exclusive far bound's own converter, and steps a `datetime`
    bound by a `timedelta` when a `span` is declared.

    Args:
      endpoint: The endpoint whose module is being generated.
      header: Rendered header of the single-request method the walk drives.
      cursor_format: See `seek_key_kind`.
    """
    pagination = endpoint.pagination
    if pagination is None:
      return {}
    if pagination.strategy != 'seek':
      return PAGED_IMPORTS
    imports: Mapping[str, set[str]] = {**PAGED_IMPORTS, **PAGED_LOGIC_ERROR_IMPORTS}
    moving = self.identifier(pagination.moving)
    param = next((p for p in (*header.args, *header.kwargs) if p.name == moving), None)
    if param is None:
      return imports
    helpers = [self.seek_key_kind(param, cursor_format=cursor_format)]
    exclusive = pagination.exclusive
    if exclusive is not None and exclusive.far is not None:
      far = self.identifier(exclusive.far.parameter)
      far_param = next((p for p in (*header.args, *header.kwargs) if p.name == far), None)
      if far_param is not None:
        helpers.append(self.seek_key_kind(far_param))
    timestamps = {helper for kind, helper in helpers if kind == 'timestamp' and helper}
    if not timestamps:
      return imports
    imports = merge_imports([
      imports, {'datetime': {'datetime'}}, {'typing_extensions': {'cast'}},
      {TYPES_PACKAGE: timestamps},
    ])
    if pagination.span is not None and self.seek_key_kind(param)[0] == 'timestamp':
      imports = merge_imports([imports, {'datetime': {'timedelta'}}])
    return imports

  def paged_exclusive_note(self, exclusive: SeekExclusive, moving: str) -> str:
    """The docstring paragraph for a `seek.exclusive` walk (ADR 0013): the far bound kept
    on the rows, the parameters sent on the first request only, and what a caller may
    pass beside the moving bound.

    Args:
      exclusive: The declaration.
      moving: The moving bound's identifier.
    """
    return ' '.join(exclusive_sentences(
      [self.identifier(name) for name in exclusive.parameters], moving=moving,
      first=self.identifier(exclusive.first) if exclusive.first is not None else None,
      far_parameter=self.identifier(exclusive.far.parameter) if exclusive.far is not None else None,
      far_field=last_row_field_prose(exclusive.far.field) if exclusive.far is not None else None,
    ))

  def paged_docstring(
    self, pagination: Pagination, *,
    method_name: str, size: str | None, step: str | None, cap: bool | str = True,
    ordered: bool = True,
    docstring: 'Docstring | None' = None, outer: 'Function | None' = None,
    extra_params: 'list[Docstring.Param] | None' = None, size_rule: str | None = None,
  ) -> str:
    """Describe the walk a generated page method performs, and how it ends.

    The terminator is named rather than implied: a caller who has to know whether a walk
    trusts a total, a short page or an absent token is reading the venue's docs, which is
    the reading the declaration exists to have done once.

    Args:
      pagination: Declaration carried by the endpoint.
      method_name: Name of the single-request method the walk drives.
      size: Name of the page-size parameter, when one is declared.
      step: Expression an `offset` walk advances by, as `paged_step` chose it.
      cap: `seek` only -- whether a row cap resolves, so a short page ends the walk: `True`,
        `False`, or the size parameter it resolves from only while the caller sets it.
      ordered: `seek` only -- whether keys compare by order (`False`: a plain string id,
        taken from the last row).
      size_rule: `seek` only -- the sentence stating the page sizes the walk requests
        (`paged_seek_size_rule`), when it clamps one.
      docstring: See `paged_summary`.
      outer: See `paged_summary`.
      extra_params: See `paged_summary`.
    """
    note: str | None = None
    if pagination.strategy == 'token':
      walk = f"Passes each page's token back as `{pagination.cursor.parameter}`"
      ends = (
        f'stops when a response carries no `{pagination.cursor.from_}`'
        if pagination.done.kind == 'absent_cursor'
        else 'stops on the first empty page'
      )
    elif pagination.strategy == 'seek':
      field = last_row_field_prose(pagination.cursor.field)
      key = last_row_field_prose(pagination.cursor.field, value=True)
      moving = self.identifier(pagination.moving)
      far = self.identifier(pagination.far) if pagination.far is not None else None
      direction = 'backwards' if pagination.descending else 'forwards'
      move = seek_move_prose(
        pagination.cursor.field, descending=pagination.descending, ordered=ordered, cap=cap,
        span=pagination.span is not None,
      )
      walk = (
        f'Walks {direction} by moving `{moving}` {move}'
        + (f', never past the caller\'s own `{far}`' if far is not None else '')
      )
      exclusive = pagination.exclusive
      if pagination.span is not None:
        ends = (
          f'covers the range in `{self.identifier(pagination.span.parameter)}`-wide requests, '
          f'stopping at `{far}`'
        )
      elif cap is True:
        ends = 'stops on the first page shorter than the venue\'s row cap'
      elif cap is False:
        ends = 'stops on the first page that brings nothing new'
      else:
        ends = (
          f'stops on the first page shorter than `{cap}`, or, while it is unset, on the first '
          f'page that brings nothing new'
        )
      if pagination.cursor.unique:
        note = (
          f'The boundary row a venue re-serves is dropped by its {key}, so no '
          f'row is duplicated or skipped whichever way the venue bounds its ranges.'
        )
      else:
        note = (
          f'Rows sharing the boundary {key} are re-fetched and dropped by '
          f'content, so a value shared by more than one row is never duplicated or '
          f'skipped. Raises `LogicError` if a row already yielded is genuinely missing '
          f'from the next page (not merely reordered), or if a full page shares one '
          f'{key}, since the rest of it would then be unreachable.'
        )
      if size_rule is not None:
        note += ' ' + size_rule
      if exclusive is not None:
        note += '\n\n' + self.paged_exclusive_note(exclusive, moving)
    else:
      if pagination.strategy == 'page':
        walk = f'Requests `{pagination.index.parameter}` from {pagination.index.start} upwards'
      else:
        walk = f'Advances `{pagination.offset.parameter}` by `{step}`'
      done = pagination.done
      if done.kind == 'total':
        unit = 'pages' if done.counts == 'pages' else 'items'
        ends = (
          f'stops once it has covered the `{done.path}` {unit} the response reports, or on '
          f'an empty page'
        )
      elif done.kind == 'short_page':
        ends = f'stops on the first page shorter than `{size}`'
      else:
        ends = 'stops on the first empty page'
    return self.paged_summary(
      method_name, walk=walk, ends=ends, note=note,
      docstring=docstring, outer=outer, extra_params=extra_params,
    )

  def paged_signature(
    self, header: Function, *, drop: set[str], extra: 'list[Function.Param] | None' = None,
  ) -> tuple[list['Function.Param'], list['Function.Param'], 'Function.Param | None']:
    """Split the single-request header into the page method's own positional/keyword
    parameters and its `validate` keyword, dropping the parameters the walk manages.

    Args:
      header: Rendered header of the single-request method.
      drop: Parameter names the walk supplies itself (a page index, a token cursor).
      extra: Keywords the page method adds beyond the sibling's own (a `span`).

    Returns:
      `(positional, keyword, validate)`; `validate` is `None` when the sibling has none.
    """
    positional = [param for param in header.args if param.name not in drop]
    keyword = [
      param for param in header.kwargs if param.name not in drop and param.name != 'validate'
    ]
    validate = next((param for param in header.kwargs if param.name == 'validate'), None)
    return positional, [*keyword, *(extra or [])], validate

  def paged_call(
    self, positional: list['Function.Param'], keyword: list['Function.Param'],
    validate: 'Function.Param | None', *, overrides: Mapping[str, str],
    supplied: Mapping[str, str], nested: 'tuple[str, str, str, str | None, Mapping[str, str]] | None',
  ) -> str:
    """Render the argument list of one call to the single-request method from inside the
    page method's `next`.

    Args:
      positional: The page method's own positional parameters.
      keyword: Its keyword parameters (excluding `validate` and any page-method-only
        keyword such as a `span`, which `supplied`/`overrides` never name).
      validate: Its `validate` keyword, when the sibling has one.
      overrides: Parameter name -> expression to pass instead of the parameter itself
        (a `seek` bound replaced by the walk's own position).
      supplied: Parameter name -> expression, for parameters the page method does not
        take at all and the walk supplies (a page index, a token cursor).
      nested: `flatten_nested_pagination`'s reconstruction record, or `None` for the flat
        case -- the flattened driver/size entries are merged back into one constructed
        message argument, coercing a non-optional field's `None` to its zero value.
    """
    call = [overrides.get(param.name, param.name) for param in positional]
    call.extend(f'{param.name}={overrides.get(param.name, param.name)}' for param in keyword)
    call.extend(f'{name}={expr}' for name, expr in supplied.items())
    if validate is not None:
      call.append(f'{validate.name}={validate.name}')
    if nested is None:
      return ', '.join(call)
    outer_name, outer_type, inner_cursor, inner_size, fields = nested

    def merged_arg(inner: str, *, coerce: bool) -> str | None:
      """`inner=<expr>` for the constructed message, read straight off `call`."""
      flat = self.identifier(inner)
      entry = next((c for c in call if c == flat or c.startswith(f'{flat}=')), None)
      if entry is None:
        return None
      expr = entry.split('=', 1)[1] if '=' in entry else entry
      field_type = fields[inner]
      if not coerce or field_type.endswith(' | None'):
        return f'{inner}={expr}'
      bare = field_type.removesuffix(' | None')
      if bare not in _SCALAR_ZERO_VALUES:
        raise ValueError(
          f'nested pagination field {inner!r} of {outer_name!r} has type {field_type!r}, '
          f'which is not one of {sorted(_SCALAR_ZERO_VALUES)} -- no zero value to '
          f'substitute for the loop-local when it is None on the first call'
        )
      return f'{inner}=({expr} if {expr} is not None else {_SCALAR_ZERO_VALUES[bare]})'

    construct_args = [
      arg for inner, coerce in ((inner_cursor, False), (inner_size, True))
      if inner is not None and (arg := merged_arg(inner, coerce=coerce)) is not None
    ]
    flat_names = {self.identifier(inner) for inner in (inner_cursor, inner_size) if inner}
    call = [
      c for c in call
      if c not in flat_names and not any(c.startswith(f'{flat}=') for flat in flat_names)
    ]
    call.append(f'{outer_name}={outer_type}({", ".join(construct_args)})')
    return ', '.join(call)

  def paged_response_method(
    self, endpoint: Endpoint, *,
    method_name: str, header: Function, rows_type: str, state_type: str | None = None,
    zero_value_is_wire_absent: bool = True,
    nested_fields: Mapping[str, Mapping[str, str]] | None = None,
    response_accessor: Literal['dict', 'attr'] = 'dict',
    response_optional: bool = False,
    docstring: 'Docstring | None' = None,
    cursor_format: str | None = None,
  ) -> str | None:
    """Generate the `truewire_core.util.paging.PaginatedResponse`-shaped page method an
    endpoint's `pagination` declaration describes -- awaitable (flattens every page) and
    async-iterable (one page at a time), with every page one pure `next(state)` call so a
    caller can retry or resume a single page (ADR 0021).

    Dispatches on strategy: `page`/`offset` to `paged_response_indexed`, `token` to
    `paged_response_token`, `seek` to `paged_response_seek`. Each renderer keeps the whole
    of the walk's state in `PaginatedResponse`'s own `S` -- never in a closure -- which is
    the purity `truewire_core.util.paging`'s contract requires.

    Args:
      endpoint: The endpoint whose module is being generated.
      method_name: Name of the single-request method the wrapper drives.
      header: Rendered header of that method, after any client-specific renaming.
      rows_type: Rendered type of one row (`PaginatedResponse`'s own `T`) -- the element
        type of the declared row collection, or of the response itself when the payload
        is the collection.
      state_type: `token` only -- rendered (bare, no ` | None`) type of the cursor. Must be
        one of `_SCALAR_ZERO_VALUES` unless the cursor parameter is required on the
        single-request method, since the seed is that type's zero value (`None` means
        "done" to `PaginatedResponse`, so it can't be the seed).
      zero_value_is_wire_absent: `token` only -- whether the seed is itself a valid,
        wire-correct way to say "no cursor" (`True` for a proto3 field, whose zero value
        is indistinguishable from absence; `False` for a REST/JSON-RPC parameter, where
        the seed is coerced back to `None` so the first call omits it).
      nested_fields: See `flatten_nested_pagination`.
      response_accessor: How the response payload's fields are read -- `'dict'`
        (`.get(key)`) for every JSON-shaped response, `'attr'` for a gRPC dataclass.
      response_optional: Whether the single-request method's own declared return type is
        itself nullable (an `anyOf`-wrapped response whose non-null branch carries the
        rows) -- kucoin's `spot.orders_hf.get_closed_orders` is the real case.
      docstring: The single-request sibling's own `Docstring`, reused for the page
        method's description/`Args:`/`References:` -- see `paged_summary`.
      cursor_format: `seek` only -- see `seek_key_kind`.

    Returns:
      Source for the `<method_name>_paged` method, or `None` when nothing is declared.
    """
    pagination = endpoint.pagination
    if pagination is None:
      return None
    if pagination.strategy == 'seek':
      return self.paged_response_seek(
        endpoint, method_name=method_name, header=header, rows_type=rows_type,
        response_accessor=response_accessor, response_optional=response_optional,
        docstring=docstring, cursor_format=cursor_format,
      )
    if pagination.strategy == 'token':
      return self.paged_response_token(
        endpoint, method_name=method_name, header=header, rows_type=rows_type,
        state_type=state_type, zero_value_is_wire_absent=zero_value_is_wire_absent,
        nested_fields=nested_fields, response_accessor=response_accessor,
        response_optional=response_optional, docstring=docstring,
      )
    return self.paged_response_indexed(
      endpoint, method_name=method_name, header=header, rows_type=rows_type,
      nested_fields=nested_fields, response_accessor=response_accessor,
      response_optional=response_optional, docstring=docstring,
    )

  def paged_rows_read(
    self, rows_path: str, *, response: str, rows: str, response_accessor: Literal['dict', 'attr'],
    response_optional: bool,
  ) -> list[str]:
    """Emit the statements binding `rows` to the page's row collection, never `None`.

    `read_path`'s emitted `.get(key)` always types a `TypedDict` field as `X | None`
    regardless of whether it is declared `Required` (typeshed's `TypedDict.get` never
    narrows on Required-ness), so the read is coerced to `[]` on a structurally-impossible
    `None`. When `rows_path` is `''` the payload is the collection and `rows` is bound to
    the response itself.

    Args:
      rows_path: Declared response path of the row collection, `''` for the payload.
      response: Name holding the single-request method's return value.
      rows: Name to bind the rows to.
      response_accessor: See `paged_response_method`.
      response_optional: See `paged_response_method`.
    """
    if not rows_path:
      if response_optional:
        return [f'{rows} = {response} if {response} is not None else []']
      return [f'{rows} = {response}']
    return [
      *self.read_path(
        rows_path, subject=response, name=rows, accessor=response_accessor,
        subject_optional=response_optional,
      ),
      f'{rows} = {rows} if {rows} is not None else []',
    ]

  def paged_response_indexed(
    self, endpoint: Endpoint, *,
    method_name: str, header: Function, rows_type: str,
    nested_fields: Mapping[str, Mapping[str, str]] | None = None,
    response_accessor: Literal['dict', 'attr'] = 'dict',
    response_optional: bool = False,
    docstring: 'Docstring | None' = None,
  ) -> str:
    """Generate the `PaginatedResponse`-shaped page method for `page`/`offset` pagination
    -- `paged_response_method`'s dispatch target, not meant to be called directly.

    The state is the index itself: a page number seeded from `index.start` and stepped by
    one, or a row offset seeded from `0` and stepped by the rows the previous page held
    (`paged_step`). An empty page always ends the walk; a declared `total` or a short page
    ends it earlier when the declaration can decide that (`docs/pagination.md` §2.1). A
    missing or moving `total` is not an error (ADR 0021): the walk simply keeps going
    until the venue's own rows run out.

    Args:
      endpoint: The endpoint whose module is being generated.
      method_name: Name of the single-request method the wrapper drives.
      header: Rendered header of that method, after any client-specific renaming.
      rows_type: Rendered type of one row.
      nested_fields: See `flatten_nested_pagination`.
      response_accessor: See `paged_response_method`.
      response_optional: See `paged_response_method`.
      docstring: See `paged_response_method`.
    """
    pagination = endpoint.pagination
    assert pagination is not None and pagination.strategy in ('page', 'offset')
    done = pagination.done
    rows_path = self.pagination_rows_path(pagination)
    total_path = done.path if done.kind == 'total' else None
    pagination, header, nested = self.flatten_nested_pagination(
      pagination, header, nested_fields,
    )
    assert pagination.strategy in ('page', 'offset')
    parameters = [*header.args, *header.kwargs]
    driver = self.identifier(self.pagination_driver(pagination))
    size = self.paged_size(pagination, parameters)
    size_default = self.paged_size_default(endpoint)
    start = pagination.index.start if pagination.strategy == 'page' else 0

    positional, keyword, validate = self.paged_signature(header, drop={driver})
    taken = {
      'self', 'next', driver,
      *(param.name for param in positional), *(param.name for param in keyword),
      *({validate.name} if validate is not None else set()),
    }
    response = self.paged_local('response', taken)
    rows = self.paged_local('rows', taken)
    total = self.paged_local('total', taken)
    call = self.paged_call(
      positional, keyword, validate, overrides={}, supplied={driver: driver}, nested=nested,
    )

    size_expr: str | None = None
    if size is not None and (self.paged_always_set(size) or size_default is not None):
      size_expr = self.paged_page_size(
        endpoint, size, fallback=str(size_default) if size_default is not None else None,
      )
    # What the walk has covered once this page is in: a page count for `page`, a row
    # count for `offset` -- compared against the declared total in its own unit, so an
    # `offset` walk never multiplies a row count by a page size, and a `page` walk only
    # does when the total counts items.
    if pagination.strategy == 'page':
      pages_covered = f'({driver} - {start} + 1)'
      items_covered = f'{pages_covered} * {size_expr}' if size_expr is not None else None
    else:
      items_covered = f'({driver} + len({rows}))'
      # Use the page's start: rounding its ending offset up can lose rows on resume.
      pages_covered = f'{driver} // {size_expr} + 1' if size_expr is not None else None
    body: list[str] = [
      f'{response} = await self.{method_name}({call})',
      *self.paged_rows_read(
        rows_path, response=response, rows=rows, response_accessor=response_accessor,
        response_optional=response_optional,
      ),
    ]
    done_terms = [f'not {rows}']
    if done.kind == 'total':
      assert total_path is not None
      body.extend(self.read_path(
        total_path, subject=response, name=total, accessor=response_accessor,
        subject_optional=response_optional,
      ))
      # A venue can serialize a total as a numeral string (binance's `bfusd`/`rwusd`
      # rate_history: `total: NotRequired[str]`) -- coerced once here.
      body.append(f'{total} = int({total}) if {total} is not None else None')
      covered = pages_covered if done.counts == 'pages' else items_covered
      if covered is not None:
        done_terms.append(f'({total} is not None and {covered} >= {total})')
      elif size is not None:
        # Optional size, no documented default: only decide by the arithmetic when the
        # caller actually supplied a size; otherwise the empty page is the terminator.
        given = self.paged_size_given(endpoint, size)
        unit_covered = (
          f'({driver} - {start} + 1) * {given}' if pagination.strategy == 'page'
          else f'{driver} // {given} + 1'
        )
        done_terms.append(
          f'({total} is not None and {size.name} is not None and {unit_covered} >= {total})'
        )
    elif done.kind == 'short_page':
      if size_expr is not None:
        done_terms.append(f'len({rows}) < {size_expr}')
      elif size is not None:
        given = self.paged_size_given(endpoint, size)
        done_terms.append(f'({size.name} is not None and len({rows}) < {given})')
    if pagination.strategy == 'page':
      step = '1'
      advance = f'{driver} + 1'
    else:
      step = self.paged_step(size, rows=rows, default=size_default)
      advance = f'{driver} + {step}'
    body.extend([
      f'if {" or ".join(done_terms)}:',
      f'  return {rows}, None',
      f'return {rows}, {advance}',
    ])
    inner = Function(
      name='next', asyn=True, method=False,
      args=[Function.Param(name=driver, type='int')],
      return_type=f'tuple[Sequence[{rows_type}], int | None]',
    )
    outer = Function(
      name=self.paged_name(method_name), asyn=False, method=True,
      args=list(positional), kwargs=list(keyword),
      return_type=f'PaginatedResponse[{rows_type}, int]',
    )
    if validate is not None:
      outer.kwargs.append(validate)
    outer_doc = self.paged_docstring(
      pagination, method_name=method_name, size=size.name if size is not None else None,
      step=step, docstring=docstring, outer=outer,
    )
    inner_code = inner.code() + '\n' + indent('\n'.join(body))
    outer_body = '\n'.join([outer_doc, inner_code, f'return PaginatedResponse({start}, {inner.name})'])
    outer.overloads = validate_overloads(
      outer, raw_return_type=raw_paged_return_type(outer.return_type or ''),
    )
    return outer.code() + '\n' + indent(outer_body)

  def paged_response_token(
    self, endpoint: Endpoint, *,
    method_name: str, header: Function, rows_type: str, state_type: str | None,
    zero_value_is_wire_absent: bool,
    nested_fields: Mapping[str, Mapping[str, str]] | None = None,
    response_accessor: Literal['dict', 'attr'] = 'dict',
    response_optional: bool = False,
    docstring: 'Docstring | None' = None,
  ) -> str:
    """Generate the `PaginatedResponse`-shaped page method for `token` pagination --
    `paged_response_method`'s dispatch target, not meant to be called directly.

    The state is the cursor. `PaginatedResponse`'s contract (`next: state -> (rows,
    next_state | None)`) maps directly onto a token walk: the next cursor is read off a
    declared response path and relayed unchanged; an absent one (or, for `empty`
    termination, an empty page) ends the walk. The first call has no cursor yet, and
    `None` would mean "done", so the seed is the cursor type's zero value (`''`, `0`,
    `b''`), coerced back to `None` before it reaches the wire unless a zero-value field is
    itself wire-absent (proto3) -- or the caller's own real argument when the venue
    requires a cursor on every call (deribit's `get_volatility_index_data`).

    Args:
      endpoint: The endpoint whose module is being generated.
      method_name: Name of the single-request method the wrapper drives.
      header: Rendered header of that method, after any client-specific renaming.
      rows_type: Rendered type of one row.
      state_type: See `paged_response_method`.
      zero_value_is_wire_absent: See `paged_response_method`.
      nested_fields: See `flatten_nested_pagination`.
      response_accessor: See `paged_response_method`.
      response_optional: See `paged_response_method`.
      docstring: See `paged_response_method`.

    Raises:
      ValueError: No `state_type`, no declared `done.rows`, or a cursor type with no zero
        value to seed from while the cursor is optional on the single-request method.
    """
    pagination = endpoint.pagination
    assert pagination is not None and pagination.strategy == 'token'
    if state_type is None:
      raise ValueError(
        f'{endpoint.function}: paged_response_method needs state_type for token-strategy '
        f'pagination'
      )
    if pagination.done.rows is None:
      raise ValueError(
        f'{endpoint.function}: token pagination needs `done.rows` declared -- a token comes '
        f'off a response field, so the payload is never itself the row collection'
      )
    rows_path: str = pagination.done.rows
    cursor_from: str = pagination.cursor.from_
    empty_ends = pagination.done.kind == 'empty'

    pagination, header, nested = self.flatten_nested_pagination(
      pagination, header, nested_fields,
    )
    parameters = [*header.args, *header.kwargs]
    driver = self.identifier(self.pagination_driver(pagination))
    driver_param = next((param for param in parameters if param.name == driver), None)
    driver_required = driver_param is not None and driver_param.required
    if driver_required:
      seed = driver
    else:
      if state_type not in _SCALAR_ZERO_VALUES:
        raise ValueError(
          f'{endpoint.function}: cursor type {state_type!r} is not one of '
          f'{sorted(_SCALAR_ZERO_VALUES)} -- no zero value to seed PaginatedResponse with'
        )
      seed = _SCALAR_ZERO_VALUES[state_type]

    positional, keyword, validate = self.paged_signature(
      header, drop=set() if driver_required else {driver},
    )
    taken = {
      'self', 'next', driver,
      *(param.name for param in positional), *(param.name for param in keyword),
      *({validate.name} if validate is not None else set()),
    }
    response = self.paged_local('response', taken)
    rows_local = self.paged_local('rows', taken)
    state_local = self.paged_local('state', taken)
    driver_arg = driver if zero_value_is_wire_absent else f'({driver} or None)'
    call = self.paged_call(
      positional, keyword, validate,
      overrides={driver: driver_arg} if driver_required else {},
      supplied={} if driver_required else {driver: driver_arg}, nested=nested,
    )
    rows_read = self.paged_rows_read(
      rows_path, response=response, rows=rows_local, response_accessor=response_accessor,
      response_optional=response_optional,
    )
    state_read = self.read_path(
      cursor_from, subject=response, name=state_local, accessor=response_accessor,
      subject_optional=response_optional,
    )
    inner = Function(
      name='next', asyn=True, method=False,
      args=[Function.Param(name=driver, type=state_type)],
      return_type=f'tuple[Sequence[{rows_type}], {state_type} | None]',
    )
    following = f'({state_local} or None) if {rows_local} else None' if empty_ends else f'{state_local} or None'
    inner_body = '\n'.join([
      f'{response} = await self.{method_name}({call})',
      *rows_read,
      *state_read,
      f'return {rows_local}, {following}',
    ])
    inner_code = inner.code() + '\n' + indent(inner_body)
    outer = Function(
      name=self.paged_name(method_name), asyn=False, method=True,
      args=list(positional), kwargs=list(keyword),
      return_type=f'PaginatedResponse[{rows_type}, {state_type}]',
    )
    if validate is not None:
      outer.kwargs.append(validate)
    outer_doc = self.paged_docstring(
      pagination, method_name=method_name, size=None, step=None,
      docstring=docstring, outer=outer,
    )
    outer_body = '\n'.join([outer_doc, inner_code, f'return PaginatedResponse({seed}, {inner.name})'])
    outer.overloads = validate_overloads(
      outer, raw_return_type=raw_paged_return_type(outer.return_type or ''),
    )
    return outer.code() + '\n' + indent(outer_body)

  def paged_response_seek(
    self, endpoint: Endpoint, *,
    method_name: str, header: Function, rows_type: str,
    response_accessor: Literal['dict', 'attr'] = 'dict',
    response_optional: bool = False,
    docstring: 'Docstring | None' = None,
    cursor_format: str | None = None,
  ) -> str:
    """Generate the `PaginatedResponse`-shaped page method for `seek` pagination --
    `paged_response_method`'s dispatch target, not meant to be called directly.

    ADR 0021's one cursor-from-rows walk, `docs/pagination.md` §2.4. The state is
    `(pos, carried)`: the value the moving bound is sent as (the caller's own bound at
    first, then the extreme cursor key of each full page), and the rows already yielded
    that share that key, so the venue re-serving them on the next page costs nothing. A
    request always spans from `pos` to the caller's far bound (or the span edge), so
    nothing is ever fetched outside the caller's own range. A full page moves `pos`; a
    short page (when a cap resolves) or a page with nothing fresh (when none does) ends
    the walk, or advances to the next span-wide chunk when a `span` is declared.

    Dedup is by key for a `unique` cursor and by content otherwise, per
    `SeekCursor.unique`. Keys are normalized per `seek_key_kind`; a plain-string key is
    compared by equality only and the page's last row in wire order stands in for its
    extreme.

    Args:
      endpoint: The endpoint whose module is being generated.
      method_name: Name of the single-request method the wrapper drives.
      header: Rendered header of that method, after any client-specific renaming.
      rows_type: Rendered type of one row.
      response_accessor: See `paged_response_method`.
      response_optional: See `paged_response_method`.
      docstring: See `paged_response_method`.
      cursor_format: See `seek_key_kind`.

    Raises:
      ValueError: A declared bound is not a parameter of the generated method.
    """
    pagination = endpoint.pagination
    assert pagination is not None and pagination.strategy == 'seek'
    parameters = [*header.args, *header.kwargs]
    moving = self.identifier(pagination.moving)
    far = self.identifier(pagination.far) if pagination.far is not None else None
    moving_param = next((param for param in parameters if param.name == moving), None)
    if moving_param is None:
      raise ValueError(
        f'the seek bound `{moving}` is not a parameter of `{method_name}`, so the walk has '
        f'nothing to move'
      )
    if far is not None and not any(param.name == far for param in parameters):
      raise ValueError(
        f'the seek bound `{far}` is not a parameter of `{method_name}`, so the walk has '
        f'nothing to stop at'
      )
    exclusive = pagination.exclusive
    exclusives = [self.identifier(name) for name in exclusive.parameters] if exclusive is not None else []
    for name in exclusives:
      if not any(param.name == name for param in parameters):
        raise ValueError(
          f'the exclusive parameter `{name}` is not a parameter of `{method_name}`, so the '
          f'walk has nothing to send on its first request'
        )
    descending = pagination.descending
    size = self.paged_size(pagination, parameters)
    # The cap a full page is measured against: the caller's own `size` whenever one is
    # given (a caller asking for 2 rows gets pages of 2, whatever the venue's own cap),
    # else the venue's declared default or fixed `cap`, else unknown (`None`).
    fallback = str(pagination.cap) if pagination.cap is not None else None
    if fallback is None and size is not None:
      default = self.paged_size_default(endpoint)
      fallback = str(default) if default is not None else None
    # A given size is clamped once, before the walk, and the clamped value is both what
    # every request sends and the cap: at most the schema's `maximum`, and at least 2,
    # since a page must hold one new row beside the boundary row it re-reads.
    size_clamp = self.paged_seek_size(endpoint, size)
    if size_clamp is None:
      # A size `paged_seek_size` leaves alone (not `int`) still measures a full page
      # against the venue's `maximum`.
      cap_expr = self.paged_page_size(endpoint, size, fallback=fallback)
    else:
      assert size is not None
      cap_expr = (
        size.name if self.paged_always_set(size) or fallback is None
        else f'({size.name} if {size.name} is not None else {fallback})'
      )
    # What the docstring says of the cap: always known, never, or only while the caller
    # sets an optional size nothing else defaults.
    cap_known: bool | str = (
      False if cap_expr is None
      else size.name if size is not None and not self.paged_always_set(size) and fallback is None
      else True
    )
    kind, helper = self.seek_key_kind(moving_param, cursor_format=cursor_format)
    bound_type = (moving_param.type or 'str').removesuffix(' | None')
    field = last_row_field(pagination.cursor.field)
    rows_path = self.pagination_rows_path(pagination)
    span = pagination.span

    span_param: Function.Param | None = None
    if span is not None:
      span_param = Function.Param(
        name=self.identifier(span.parameter), type='int', default=str(span.default),
      )
    positional, keyword, validate = self.paged_signature(
      header, drop=set(), extra=[span_param] if span_param is not None else None,
    )
    taken = {
      'self', 'next',
      *(param.name for param in positional), *(param.name for param in keyword),
      *({validate.name} if validate is not None else set()),
    }
    response = self.paged_local('response', taken)
    rows = self.paged_local('rows', taken)
    state = self.paged_local('state', taken)
    pos = self.paged_local('pos', taken)
    carried = self.paged_local('carried', taken)
    keys = self.paged_local('keys', taken)
    key = self.paged_local('key', taken)
    item = self.paged_local('item', taken)
    fresh = self.paged_local('fresh', taken)
    values = self.paged_local('values', taken)
    extreme = self.paged_local('extreme', taken)
    cap = self.paged_local('cap', taken)
    edge = self.paged_local('edge', taken)
    carried_keys = self.paged_local('carried_keys', taken)
    remaining = self.paged_local('remaining', taken)

    # `key(item)` reads one row's cursor field, normalized to compare against `pos`: a
    # nested function rather than an inline expression, so the raw read is bound once and
    # pyright narrows it past `is not None` before the cast (a repeated expression never
    # narrows), and so one normalization serves both the page and the carried rows.
    key_fn = self.paged_local('key_of', taken)
    raw = self.paged_local('raw', taken)

    def normalized(kind: str, helper: str | None) -> str:
      """`raw` (a row value already narrowed past `None`) as the parameter it is compared
      with takes it."""
      if kind == 'timestamp':
        assert helper is not None
        # `cast(...)` only steers pyright: the converter accepts the raw wire value, an
        # epoch number (or numeral string) for the epoch helpers, an ISO string otherwise.
        wire = 'str' if helper in ('timestamp_iso', 'date_iso') else 'int'
        return f'{helper}.parse(cast({wire}, {raw})) if not isinstance({raw}, datetime) else {raw}'
      return f'{kind}({raw})'

    key_doc = (
      f"row's {last_row_field_prose(pagination.cursor.field)}" if field else 'row, as a key'
    )
    key_def = [
      f'def {key_fn}({item}: {rows_type}):',
      f'  """One {key_doc}, normalized to compare against the moving bound."""',
      *(f'  {line}' for line in self.row_field_read(item, field, raw)),
      f'  return None if {raw} is None else {normalized(kind, helper)}',
    ]
    key_expr = f'{key_fn}({item})'
    # An exclusive far bound (`exclusive.far`) is sent on the first request at most, so the
    # walk enforces it on the rows: the caller's value is normalized once up front the way
    # the row values are, `past(item)` says whether one row lies beyond it, and a page
    # holding such a row yields the rest and ends the walk.
    far_setup: list[str] = []
    past_def: list[str] = []
    ended: list[str] = []
    if exclusive is not None and exclusive.far is not None:
      far_limit = self.identifier(exclusive.far.parameter)
      far_param = next(param for param in parameters if param.name == far_limit)
      far_kind, far_helper = self.seek_key_kind(far_param)
      far_field = last_row_field(exclusive.far.field)
      far_key = self.paged_local('far_key', taken)
      past_fn = self.paged_local('past', taken)
      far_value = (
        f'{far_helper}.parse({far_helper}.dump({far_limit}))' if far_kind == 'timestamp'
        else f'{far_kind}({far_limit})'
      )
      far_setup = [f'{far_key} = None if {far_limit} is None else {far_value}']
      past_def = [
        f'def {past_fn}({item}: {rows_type}) -> bool:',
        f'  """Whether one row\'s {last_row_field_prose(exclusive.far.field)} lies past the caller\'s own '
        f'`{far_limit}`."""',
        *(f'  {line}' for line in self.row_field_read(item, far_field, raw)),
        f'  return {far_key} is not None and {raw} is not None and '
        f'({normalized(far_kind, far_helper)}) {"<" if descending else ">"} {far_key}',
      ]
      ended = [
        f'if any({past_fn}({item}) for {item} in {rows}):',
        f'  return [{item} for {item} in {fresh} if not {past_fn}({item})], None',
      ]

    # `pos` starts as the caller's own moving bound: `None`-able only when the caller may
    # omit it (the venue then starts from its own default) and no `span` forces both
    # bounds to be given -- a required bound (hyperliquid's `start_time`, coinbase's
    # `start`/`end`) is never `None`, and typing it so would fail the call it feeds.
    pos_optional = span is None and not moving_param.required
    pos_type = f'{bound_type} | None' if pos_optional else bound_type
    state_type = f'tuple[{pos_type}, list[{rows_type}]]'
    call_keyword = [param for param in keyword if span_param is None or param.name != span_param.name]
    overrides = {moving: pos}
    if span is not None:
      overrides[far or ''] = edge
    for name in exclusives:
      overrides[name] = f'{name} if {pos} is None else None'
    call = self.paged_call(
      positional, call_keyword, validate, overrides=overrides, supplied={}, nested=None,
    )

    setup: list[str] = []
    if span is not None:
      assert far is not None and span_param is not None
      setup.append(f'if {moving} is None or {far} is None:')
      setup.append(
        f'  raise ValueError('
        f'{self.paged_name(method_name) + " walks a bounded range in spans: pass both `" + moving + "` and `" + far + "`"!r})'
      )
      _, is_datetime = self.paged_window_bound(moving, unit=span.unit, param=moving_param)
      span_expr = (
        f'timedelta({self._TIMEDELTA_UNITS[span.unit]}={span_param.name})' if is_datetime
        else span_param.name
      )
      edge_expr = (
        f'max({pos} - {span_expr}, {far})' if descending else f'min({pos} + {span_expr}, {far})'
      )
    else:
      edge_expr = None
    if exclusive is not None:
      # Beside a caller's moving bound only the non-far parameters are refused: the far one
      # is then never sent, and kept on the rows all the same.
      far_name = self.identifier(exclusive.far.parameter) if exclusive.far is not None else None
      refused = [name for name in exclusives if name != far_name]
      if refused:
        given = ' or '.join(f'{name} is not None' for name in refused)
        given = f'({given})' if len(refused) > 1 else given
        listed = '/'.join(f'`{name}`' for name in refused)
        setup.append(f'if {moving} is not None and {given}:')
        setup.append(
          f'  raise ValueError('
          f'{self.paged_name(method_name) + " walks by `" + moving + "`, which the venue refuses alongside " + listed + ": pass one or the other"!r})'
        )
      if exclusive.first is not None and pos_optional:
        first = self.identifier(exclusive.first)
        setup.append(f'if {moving} is None and {first} is None:')
        setup.append(
          f'  raise ValueError('
          f'{self.paged_name(method_name) + " needs `" + moving + "` or `" + first + "` to start from: without either the venue answers from the wrong end of the range"!r})'
        )
    setup.extend(far_setup)
    if size_clamp is not None:
      setup.append(size_clamp)
    if cap_expr is not None:
      setup.append(f'{cap}: int | None = {cap_expr}')
    else:
      setup.append(f'{cap}: int | None = None')

    # The cursor field as messages print it: relative to one row, "row" for the row itself.
    named = f'`{field}`' if field else 'row'
    at = lambda value: f'[{item} for {item}, {key} in zip({rows}, {keys}) if {key} == {value}]'  # noqa: E731
    if pagination.cursor.unique:
      dedup = [
        f'{carried_keys} = [{key_expr} for {item} in {carried}]',
        f'{fresh} = [{item} for {item}, {key} in zip({rows}, {keys}) if {key} not in {carried_keys}]',
      ]
    else:
      missing = (
        f'`{self.paged_name(method_name)}` requested from {{{pos}}} and the venue no longer '
        f'returned one or more rows it had already returned for that {named}; row content '
        f'was expected to stay available across requests, so the walk stopped instead of '
        f'silently dropping or duplicating rows.'
      )
      dedup = [
        f'{remaining} = list({carried})',
        f'{fresh} = []',
        f'for {item} in {rows}:',
        f'  if {item} in {remaining}:',
        f'    {remaining}.remove({item})',
        '  else:',
        f'    {fresh}.append({item})',
        f'if {remaining}:',
        f'  raise LogicError(f{missing!r})',
      ]
    stuck = (
      f'`{self.paged_name(method_name)}` requested from {{{pos}}} and the venue returned a '
      f'full page of {{len({rows})}} rows all sharing one {named} value; the rest of that '
      f'value is unreachable and advancing would drop it.'
    )
    if kind == 'str':
      extreme_expr = f'{values}[-1] if {values} else None'
    else:
      extreme_expr = f'{"min" if descending else "max"}({values}) if {values} else None'
    body: list[str] = [
      *key_def,
      *past_def,
      f'{pos}, {carried} = {state}',
      *([f'{edge} = {edge_expr}'] if edge_expr is not None else []),
      f'{response} = await self.{method_name}({call})',
      *self.paged_rows_read(
        rows_path, response=response, rows=rows, response_accessor=response_accessor,
        response_optional=response_optional,
      ),
      f'{keys} = [{key_expr} for {item} in {rows}]',
      *dedup,
      *ended,
      f'{values} = [{key} for {key} in {keys} if {key} is not None]',
      f'{extreme} = {extreme_expr}',
      f'if {cap} is not None and len({rows}) >= {cap}:',
      f'  if {extreme} is None or {extreme} == {pos}:',
      f'    raise LogicError(f{stuck!r})',
      f'  return {fresh}, ({extreme}, {at(extreme)})',
      f'if {cap} is None and {extreme} is not None and {extreme} != {pos}:',
      f'  return {fresh}, ({extreme}, {at(extreme)})',
    ]
    if edge_expr is not None:
      body.extend([
        f'if {edge} {"<=" if descending else ">="} {far}:',
        f'  return {fresh}, None',
        f'return {fresh}, ({edge}, {at(edge)})',
      ])
    else:
      body.append(f'return {fresh}, None')

    inner = Function(
      name='next', asyn=True, method=False,
      args=[Function.Param(name=state, type=state_type)],
      return_type=f'tuple[Sequence[{rows_type}], {state_type} | None]',
    )
    outer = Function(
      name=self.paged_name(method_name), asyn=False, method=True,
      args=list(positional), kwargs=list(keyword),
      return_type=f'PaginatedResponse[{rows_type}, {state_type}]',
    )
    if validate is not None:
      outer.kwargs.append(validate)
    extra_params: list[Docstring.Param] = []
    if span is not None and span_param is not None:
      extra_params.append(Docstring.Param(
        name=span_param.name, required=False,
        docstring=(
          f'Widest range one request covers, in `{span.unit}`. Defaults to `{span.default}`, '
          f'the venue\'s own documented maximum.'
        ),
      ))
    outer_doc = self.paged_docstring(
      pagination, method_name=method_name, size=size.name if size is not None else None,
      step=None, cap=cap_known, ordered=kind != 'str', docstring=docstring, outer=outer,
      extra_params=extra_params,
      size_rule=self.paged_seek_size_rule(endpoint) if size_clamp is not None else None,
    )
    inner_code = inner.code() + '\n' + indent('\n'.join(body))
    outer_body = '\n'.join([
      outer_doc, *setup, inner_code,
      f'return PaginatedResponse(({moving}, []), {inner.name})',
    ])
    outer.overloads = validate_overloads(
      outer, raw_return_type=raw_paged_return_type(outer.return_type or ''),
    )
    return outer.code() + '\n' + indent(outer_body)

  def paged_response_rows_type(
    self, types: RenderedTypes, rows_path: str, *,
    response_ref: str | None = None, references: Mapping[str, ExternalReference] = {},
    imports: dict[str, set[str]] | None = None,
  ) -> str | None:
    """Resolve the rendered element type of a declared `pagination.done.rows` field, for
    a `PaginatedResponse`-shaped `_paged` wrapper's own `T` (design's request/response
    shape; ported from the equivalent per-client helper every hand-rolled backend needing
    this used to carry its own copy of -- alchemy's own pre-migration `codegen/python.py`
    named it `_paged_rows_type`).

    `Unnest` gives every titled-record schema nested inside `$response` a stable
    identifier keyed by its own path plus a trailing `item` step for an array's element
    schema (`$response/{dotted-path-with-'/'}/item`) -- tried first, and returned as-is
    when the row schema is a titled record (or `$ref`, resolved into `$response`'s own
    definition the identical way any other nested reference already is).

    Not every row schema is a titled record, though: a bare `additionalProperties: true`
    map (rule 1: "a map, not a record, needs no title") or a plain scalar array
    (alchemy's own `nft.get_owners_for_nft`, `owners: list[str]`) gets no identifier from
    `Unnest` at all. For either, the type is read straight off the parent record's own
    rendered field instead -- regex-extracted from `types.definitions`, stripping the
    enclosing `NotRequired[...]`/`| None`/`list[...]` -- rather than left unresolved,
    which would silently downgrade an endpoint from a `PaginatedResponse`-shaped `_paged`
    method to a plain async generator even though the original, already-published
    behavior (S24) was the richer shape.

    Neither of those two resolutions works when the *whole* response is a bare top-level
    `{"$ref": "..."}` (`rpc_endpoint`'s own `response_ref` special case, its docstring's
    "a bare top-level $ref response" note): `$response` is then never added to `schemas`
    at all, so `types` carries no `$response/...` entries whatsoever -- `response_ref`/
    `references` exist so this method has a third path for exactly that shape, reading
    the referenced schema directly out of `self.shared_schemas` (`spec/schemas.json`'s
    raw, parsed schemas) and resolving its own row-items `$ref` against `references` the
    same way an ordinary flat request property already does. Found live on moralis's
    `streams.evm.all_streams` (and 6 more `streams.*` endpoints): each response is a bare
    `$ref` to a shared `*StreamsResponse`/`*History`/... schema whose own `result` (or
    equivalent) property's items are themselves `$ref`'d into `spec/schemas.json` --
    exactly the shape a `PaginatedResponse`-shaped `_paged` method needs to resolve a row
    type for, previously silently downgrading all 7 to a plain async generator.

    Nor does either resolution work directly when the response itself is `anyOf`-wrapped
    (docs/spec/authoring.md rule 5's nullable-record shape: `anyOf: [RealShape, {"type":
    "null"}]`) -- `Unnest` never keys the branch's own fields under `$response/...`, only
    under `$response/anyOf/{i}/...` (one path segment per union member, `MapReduce`'s own
    `path + ('anyOf', str(i))` convention), so a direct `$response`-rooted lookup finds
    nothing and `$response`'s own rendered definition is a union alias
    (`HfClosedOrdersPage | None`), not a record with the `rows_path` field on it. Retried
    against each `$response/anyOf/{i}` `Unnest` actually extracted (a plain scalar/null
    branch is never extracted at all -- `Unnest.unnest` only fires for an object schema
    with `properties`, so only a real record variant ever gets its own path -- there is
    nothing to try a resolution against for the `null` branch, and no false match to
    filter out). kucoin's `spot.orders_hf.get_closed_orders`/`margin.orders_hf.
    get_closed_orders` are the real, motivating case: both declare `pagination.done.rows:
    "items"`, `"token"`/`"absent_cursor"` (an S24-eligible strategy/terminator), and a
    response shaped `anyOf: [HfClosedOrdersPage, {"type": "null"}]` -- identical to their
    sibling `get_trade_history` endpoints' own `pagination` declaration, which render
    `PaginatedResponse`-shaped correctly because their response is a plain, non-wrapped
    record. Before this, both `get_closed_orders` silently fell back to a plain async-
    generator `_paged` method despite qualifying under S24, for no reason visible in
    either endpoint's own spec.

    Args:
      types: The `RenderedTypes` result of the one `type_generator(schemas, inline=True)`
        call that registered `$request`/`$response` (and every schema nested inside
        them) -- `rpc_endpoint`'s own already-computed value, reused rather than
        re-derived.
      rows_path: `pagination.done.rows`, a dotted path into the response -- or `''` when
        `done.rows` is unset because the response itself *is* the row collection (a bare
        top-level array).
      response_ref: `spec.response`'s own bare `$ref` id, when the whole response is one
        (`rpc_endpoint`'s `response_ref`) -- `None` for an ordinary inline response.
      references: Shared schemas already emitted elsewhere in the package
        (`rpc_endpoint`'s own `references` parameter), consulted only for the
        `response_ref` resolution path, to turn a nested `$ref`'s id into its rendered
        Python name.
      imports: Mutated in place with the row type's own import, when the `response_ref`
        resolution path is what resolved it -- unlike the other two paths (whose row type
        is always something `types.imports` already covers, since it's derived from
        `types` itself), a `response_ref`-resolved row type is never seen by
        `type_generator` at all (the whole point of the bare-`$ref`-response special
        case), so nothing else registers its import. `None` (the default) skips this --
        the caller only passes a real dict when it intends to fold the result into its
        own merged imports.

    Returns:
      The rendered element type, or `None` when none of the three resolutions work (an
      endpoint whose declared `pagination` this backend still can't render
      `PaginatedResponse`-shaped -- the caller falls back to a plain async generator).
    """
    def resolve_from(root: str) -> str | None:
      """Try the two `Unnest`-identifier/`types.definitions`-regex resolutions rooted at
      `root` -- `$response` on the first, direct call, and (only if that finds nothing)
      each `$response/anyOf/{i}` branch `Unnest` actually extracted, on retry. Identical
      logic either way; only the root path prefix differs, which is exactly what
      `Unnest`'s own `path + (...)` convention keys everything off of.
      """
      # `rows_path` is empty when the response *is* the row collection -- a bare
      # top-level array, no wrapper object at all (`docs/spec/authoring.md` rule 8:
      # "short_page and empty also name the collection they measure... omit it only when
      # the payload is itself the collection"). kucoin's `account.hf_ledgers`/`account.
      # margin_hf_ledgers` (`seek` strategy, no declared `done.rows`) are the real,
      # motivating case -- `Unnest` keys a bare top-level array's own item type
      # `$response/item` (no extra path segment to insert), confirmed empirically
      # against that schema directly.
      if not rows_path:
        item_type = types.identifiers.get(f'{root}/item')
        if isinstance(item_type, str):
          return item_type
        response_def = types.definitions.get(root)
        if isinstance(response_def, str):
          match = re.match(r'^\w+\s*=\s*list\[(.+)\]$', response_def.strip())
          if match is not None:
            return match.group(1)
        return None

      segments = rows_path.split('/') if '/' in rows_path else rows_path.split('.')
      item_key = f'{root}/' + '/'.join(segments) + '/item'
      item_type = types.identifiers.get(item_key)
      if isinstance(item_type, str):
        return item_type

      parent_key = root if len(segments) == 1 else f'{root}/' + '/'.join(segments[:-1])
      field = segments[-1]
      definition = types.definitions.get(parent_key)
      if isinstance(definition, str):
        match = re.search(rf'^\s*{re.escape(field)}: (.+)$', definition, re.MULTILINE)
        if match is not None:
          field_type = match.group(1).strip()
          if field_type.startswith('NotRequired[') and field_type.endswith(']'):
            field_type = field_type[len('NotRequired[') : -1]
          if field_type.endswith(' | None'):
            field_type = field_type[: -len(' | None')]
          match = re.fullmatch(r'(?:builtins\.)?list\[(.+)\]', field_type)
          if match:
            return match.group(1)
          # A row collection the schema leaves untyped (etherscan's generic
          # `EtherscanResponse.result: Any`, shared by every endpoint of that shape):
          # the rows are `Any` too, which is honest, not a resolution failure.
          if field_type == 'Any':
            return 'Any'
      return None

    direct = resolve_from('$response')
    if direct is not None:
      return direct

    # `$response` itself resolved nothing -- retry against every `$response/anyOf/{i}`
    # branch `Unnest` extracted its own record for (see this method's own docstring, the
    # kucoin `get_closed_orders` case). A plain scalar/`null` branch is never extracted
    # at all (`Unnest.unnest` requires an object schema with `properties`), so this never
    # tries, or wrongly matches against, a branch with no row field to find -- `sorted`
    # by branch index keeps resolution order deterministic and matching declaration order
    # for the (currently hypothetical) case of more than one real record branch.
    # Every branch's own row type, joined into one union: bybit's `market.instruments` is
    # one `anyOf` per product category, each with its own `list` element type, and a
    # `PaginatedResponse[SpotInstrument, str]` would be a lie for a linear category. An
    # array branch (kucoin's `affiliate.commission`: `anyOf: [[...], null]`, no wrapper
    # record at all) has no `$response/anyOf/{i}` definition, only an
    # `$response/anyOf/{i}/item` identifier -- both spellings are gathered here.
    branch_root = re.compile(r'^\$response/anyOf/(\d+)$')
    item_root = re.compile(r'^\$response/anyOf/(\d+)/item$')
    roots = {key for key in types.definitions if branch_root.match(key)}
    roots |= {key[: -len('/item')] for key in types.identifiers if item_root.match(key)}
    branch_types: list[str] = []
    for root in sorted(roots, key=lambda key: int(branch_root.match(key).group(1))):  # type: ignore[union-attr]
      branched = resolve_from(root)
      if branched is not None and branched not in branch_types:
        branch_types.append(branched)
    if branch_types:
      return ' | '.join(branch_types)

    # The bare-top-level-array shape (`not rows_path`) has no `segments` to walk against
    # `self.shared_schemas` below -- `resolve_from`'s own empty-`rows_path` branch is the
    # only resolution that shape gets, same as before this method grew a second root to
    # try. `response_ref` (moralis's bare-`$ref`-response shape, this method's own
    # docstring) is never itself the empty-`rows_path` case in practice -- `done.rows`
    # unset only ever pairs with a bare top-level array response, and a bare `$ref`
    # response is a record, not an array -- so this is not a real loss of coverage.
    if not rows_path or response_ref is None:
      return None
    segments = rows_path.split('/') if '/' in rows_path else rows_path.split('.')
    node: Schema | Reference | None = self.shared_schemas.get(response_ref)
    for segment in segments:
      if isinstance(node, Reference):
        node = self.shared_schemas.get(node.ref)
      if not isinstance(node, Schema) or not node.properties:
        return None
      node = node.properties.get(segment)
    if isinstance(node, Reference):
      node = self.shared_schemas.get(node.ref)
    if not isinstance(node, Schema) or node.items is None:
      return None
    item = node.items
    if isinstance(item, Reference):
      external = references.get(item.ref)
      if external is None:
        return None
      if imports is not None:
        imports.setdefault(external['package'], set()).add(external['name'])
      return external['name']
    if isinstance(item, Schema) and item.title:
      return item.title
    return None

  def endpoint(
    self,
    endpoint: Endpoint,
    references: Mapping[str, ExternalReference],
    *,
    class_name: str,
    method_name: str,
    endpoint_dir: Path | None = None,
  ) -> str:
    """Generate Python code for a single endpoint module, dispatching on call pattern.

    A backend implements one hook per call pattern (`rpc`/`stream`/`grpc`) rather than
    one hook that re-discriminates the spec at runtime, mirroring the type-level split
    `spec.kind` already makes (ADR 0005). Adding grpc support to a project that already
    generates rpc/stream endpoints does not touch either existing path.

    `endpoint_dir` is only forwarded to the resolved `rpc_endpoint`/`stream_endpoint` when
    that callable actually declares the parameter (`inspect.signature`, not a hardcoded
    `isinstance`/subclass check) -- the base `Generator`'s own implementations need it
    (`_resolve_core`), but a legacy hand-rolled backend may override
    `rpc_endpoint`/`stream_endpoint` with the older, `endpoint_dir`-less signature, and
    unconditionally passing an unrecognized keyword would break it immediately with
    `TypeError`, regardless of the value.

    Args:
      endpoint: The endpoint whose module is about to be generated.
      references: Shared schemas already emitted elsewhere in the package.
      class_name: Name chosen for the generated class.
      method_name: Name chosen for the generated method.
      endpoint_dir: This endpoint's own `spec/endpoints/` directory -- see
        `rpc_endpoint`/`stream_endpoint`'s own parameter of the same name.
    """
    if endpoint.spec.kind == 'stream':
      target = self.stream_endpoint
    elif endpoint.spec.kind == 'grpc':
      target = self.grpc_endpoint
    else:
      target = self.rpc_endpoint
    kwargs = {'class_name': class_name, 'method_name': method_name}
    if 'endpoint_dir' in inspect.signature(target).parameters:
      kwargs['endpoint_dir'] = endpoint_dir
    refused = self.project.policy.refuse if self.project is not None else ()
    function = (
      endpoint.resolved_function(endpoint_dir / 'endpoint.json', self.project.spec_dir)
      if refused and endpoint_dir is not None and self.project is not None else None
    )
    self.refusing = function if function in refused else None
    try:
      return target(endpoint, references, **kwargs)
    finally:
      self.refusing = None

  def _flat_request_kwargs(
    self, field_params: list['Function.Param'],
  ) -> tuple[list['Function.Param'], list['Function.Param']]:
    """Split a flat request's own field params into positional-or-keyword vs keyword-only.

    The house rule "force same-typed parameters to be kwargs", inverted: only the very
    first declared field can ever stay positional-or-keyword, and only when no sibling
    shares its rendered type. Originally a gRPC backend's own `first_type_is_unique`,
    promoted to a shared `Generator` rule used everywhere. `symbol:
    str` alone (orderbook) stays positional; `symbol: str, quantity: Decimal`
    (place_order) still only lets `symbol` go positional -- every field but the first is
    always keyword-only, regardless of whether *its own* type happens to be unique too.

    Args:
      field_params: One `Function.Param` per top-level `request` property, in the
        schema's own declared order.
    """
    first = field_params[0]
    unique = sum(1 for p in field_params if p.type == first.type) == 1
    if unique:
      return [first], field_params[1:]
    return [], field_params

  RESERVED_CALL_KWARGS = {'validate'}
  """Generated identifiers no flat `request`/`parameters` field may resolve to, because
  every generated `rpc`/`stream` method already reserves them for its own kwargs
  (`validate`, S8's response/payload-validation override -- the only one today).
  `_avoid_reserved_collision` renames the *local* Python identifier a colliding wire
  field resolves to; the wire key itself (the `Request`/`Parameters` TypedDict's own
  field name) is untouched, matching `docs/spec/authoring.md` rule 10's own guidance
  that a real wire collision is fixed by the codegen backend, never by renaming the spec.
  """

  def _avoid_reserved_collision(self, identifier: str) -> str:
    """Rename a flat field's generated Python identifier when it would otherwise
    collide with a reserved kwarg every generated `rpc`/`stream` method carries
    (`RESERVED_CALL_KWARGS`) -- a trailing underscore, the same mechanical convention
    already used for a Python-keyword collision (`type` -> `type_`).

    Without this, a flat request/parameters property literally named `validate` (the
    real, motivating case: an `AddOrderBatch`/`EditOrder` pair, both transports, each
    declaring a genuine wire field named `validate`, flat at the top level of the request
    body) would
    produce a `Function.Param` also named `validate`, duplicating the `validate: bool |
    None = None` kwarg `rpc_endpoint`/`stream_endpoint` always append -- a real
    `SyntaxError` (`def f(self, *, validate: bool = ..., validate: bool | None = None)`)
    at import time, not just an ugly signature.

    Args:
      identifier: The Python identifier `self.identifier(name)` already produced.
    """
    return f'{identifier}_' if identifier in self.RESERVED_CALL_KWARGS else identifier

  def _typed_dict_constructor(
    self, class_name: str, parts: list[tuple[str, str]],
  ) -> tuple[str, bool]:
    """Render every required `Request`/`Parameters` field into the expression that
    builds one, and whether that expression needs an explicit `: {class_name}`
    annotation on the statement assigning it (`request: Request = ...`) to type-check.

    Ordinarily a real constructor call, `{class_name}(name=identifier, ...)` -- every
    field name is already a valid Python identifier and not a keyword, so this is both
    valid syntax and, since it's a genuine call with individually-typed keyword
    arguments, checks each field's value against its own declared type correctly.

    A wire field name that is a Python keyword (an `account_transfer` declaring a
    genuine, required `from` field) makes
    `{class_name}(from=...)` a flat `SyntaxError` regardless of what
    `_avoid_reserved_collision`/`self.identifier` renamed the *local* variable to --
    Python refuses a keyword as a keyword-argument name even when the callee (a
    `TypedDict`, structurally a plain `dict` at runtime) would accept it as a key. The
    obvious next attempt, `{class_name}(**{{'from': from_}}, other=other)`, is valid
    syntax but a real pyright false-positive: mixing `**mapping` unpacking with ordinary
    keyword arguments in one `TypedDict(...)` call makes pyright check the *whole*
    unpacked mapping's inferred value type against every other field's own type,
    including ones the mapping doesn't even touch -- confirmed empirically (a
    `Literal[...]`-typed sibling field falsely flagged `str` is not assignable, purely
    because the *unrelated* `'from': from_` entry widened the mapping's own inferred
    value type to plain `str`). A plain dict *literal*, checked against an explicit
    `: {class_name}` variable annotation rather than passed through a constructor call,
    sidesteps both problems at once -- confirmed clean under pyright for the identical
    case: dict-literal syntax accepts any string key (no Python keyword restriction),
    and each entry is checked individually against the annotated TypedDict's own field
    type (no unpacking-inference widening).

    Args:
      class_name: `'Request'` or `'Parameters'`.
      parts: `(wire name, local identifier)` per required field, in declaration order.

    Returns:
      The constructor expression, and whether it's a bare dict literal needing an
      explicit type annotation on whatever statement assigns it (`False` for the
      ordinary call-syntax case, which type-checks fine as a bare expression on its own).
    """
    if any(not name.isidentifier() or keyword.iskeyword(name) for name, _ in parts):
      entries = ', '.join(f'{name!r}: {identifier}' for name, identifier in parts)
      return f'{{{entries}}}', True
    call_args = ', '.join(f'{name}={identifier}' for name, identifier in parts)
    return f'{class_name}({call_args})', False

  def skip_endpoint(self, endpoint: Endpoint) -> bool:
    """
    Whether this endpoint's own module is hand-written (or has no callable at all) and
    should not be regenerated -- the default behavior for any project generating through
    the bare universal `Generator` with no per-project subclass override.

    Consults the already-established `endpoint.surface` mechanism
    (`HandwrittenSurface`/`AbsentSurface`):

    - `handwritten`: an endpoint whose real return type the mechanized
      `rpc_endpoint`/`stream_endpoint` shape cannot express at all -- a `retrieve_export`
      whose 2xx response is a raw binary export file, not
      a JSON-schema-describable one, so there is no `response` schema to derive a `bytes`
      return annotation from -- declares `surface: {"kind": "handwritten", ...}` and keeps
      a real, hand-written module at the conventional generated path instead; `router()`'s
      own aggregate/composite branches already include such an endpoint as a base/child by
      class name (`cli/codegen.py`'s endpoint loop keeps a `handwritten` node in the tree
      precisely so a router sharing its directory can still compose it).
    - `absent`: an endpoint with no callable at all -- WS notification types with no
      per-type subscribe protocol (reached through a hand-written dispatch-by-`type`
      iterator instead), login commands that are internal auth handshakes, or raw
      `subscribe`/`unsubscribe` commands deliberately reachable only through
      `StreamEndpoint.subscribe`, never as their own raw-command callables.
      `cli/codegen.py`'s own endpoint loop already filters these out of the generated
      function tree entirely, up front, before this hook is ever consulted for them --
      but `truewire.surface.check`'s reconciliation walks every endpoint and calls this
      hook directly, with no such pre-filter, so this hook's default previously covering
      only `handwritten` meant every `absent` declaration read as stale the moment a
      project migrated onto the bare `Generator` -- a false positive ("declared absent,
      but the backend now generates it") unable to tell a genuinely, deliberately
      excluded endpoint from one whose exclusion has quietly gone stale.

    Args:
      endpoint: The endpoint whose module is about to be generated.
    """
    return endpoint.surface is not None and endpoint.surface.kind in ('handwritten', 'absent')

  def _nested_prop_type(
    self, prop: 'Reference | Schema', *, prefix: str,
    type_generator: TypeGenerator, identifiers: Mapping[str, str],
  ) -> str:
    """Resolve one flat property's own standalone type expression -- the parameter type
    a generated method signature needs for it, distinct from (but matching) whatever
    type `Request`/`Parameters` itself already rendered that property's field as.

    Consults `identifiers` (`types.identifiers`, from the one registered
    `type_generator(schemas, inline=True)` call the caller already ran) at every
    position a nested schema *could* have been unnested into its own named type by
    `Unnest.records()` -- a record, an array of one, a tuple element, or an `anyOf`
    variant -- recursing structurally instead of special-casing each shape combination
    one at a time. Re-deriving any of these via a single un-normalized
    `renderer.parser(prop, id=None)` call on the *whole* property hits `Parser`'s own
    refusal to inline a record type nested anywhere inside an array/tuple/union
    (`ValueError('Found nested record type')`) -- confirmed against three real,
    independently-shaped cases: a `get_nft_metadata_batch` (`tokens:
    list[NftMetadataBatchToken]`, array-of-record), an `order.grouping`
    (`Literal[...] | OrderPriorityGrouping`, a property-level union with a record
    variant) and a `deployerFees` (`list[tuple[str,
    DeployerFee]]`, an array of tuples with a record element) -- rather than three
    separate patches for the same root cause.

    A schema that reaches the bottom of this recursion with no nested nesting left
    (a plain scalar, an enum, or an array/union of only scalars) falls through to the
    original single `renderer.parser`/`renderer.code` call, unchanged from before any of
    these fixes existed -- that path was never broken; only a record reachable *through*
    an array/tuple/union was.

    Args:
      prop: The property's own schema (or a `$ref` into a shared one).
      prefix: This property's own `Unnest` path prefix (`$request/{name}`,
        `$parameters/{name}`, ...), extended with `/anyOf/{i}`, `/item`, or `/prefix/{i}`
        at each recursive step to match the identical convention `Unnest.records()`
        (`truewire.generation.types.unnest`) builds while walking the same schema tree for
        `Request`/`Parameters` itself.
      type_generator: The same `TypeGenerator` instance used to render `Request`/
        `Parameters`/the response type, so a resolved type here always matches what a
        sibling occurrence of the identical schema rendered elsewhere.
      identifiers: `types.identifiers`, the same registered-call result naming every
        schema `Unnest.records()` actually extracted.
    """
    if isinstance(prop, Reference):
      external = type_generator.external_references.get(prop.ref)
      if external is None:
        raise ResolutionError(prop.ref)
      return external['name']
    if (nested := identifiers.get(prefix)) is not None:
      return nested
    if prop.anyOf:
      return ' | '.join(
        self._nested_prop_type(
          variant, prefix=f'{prefix}/anyOf/{i}',
          type_generator=type_generator, identifiers=identifiers,
        )
        for i, variant in enumerate(prop.anyOf)
      )
    if prop.prefixItems:
      items = ', '.join(
        self._nested_prop_type(
          item, prefix=f'{prefix}/prefix/{i}',
          type_generator=type_generator, identifiers=identifiers,
        )
        for i, item in enumerate(prop.prefixItems)
      )
      return f'tuple[{items}]'
    if prop.type == 'array' and prop.items is not None:
      item_type = self._nested_prop_type(
        prop.items, prefix=f'{prefix}/item',
        type_generator=type_generator, identifiers=identifiers,
      )
      return f'list[{item_type}]'
    renderer = type_generator.render
    return renderer.code(renderer.parser(prop, id=None)).iden

  def _rpc_request_params(
    self, request_schema: Schema | None, *, type_generator: TypeGenerator, request_type: str | None,
    identifiers: Mapping[str, str] = {},
  ) -> tuple[
    list['Function.Param'], list['Function.Param'], list[str], str, list['Docstring.Param'],
  ]:
    """Derive a flat request's own generated parameters from `endpoint.spec.request`.

    Three shapes: a flat object's `properties` become named kwargs (as
    `openapi.parameters` already did) and the returned `request_value_expr` builds a
    `Request(...)` instance from them; a titled `anyOf` becomes one union-typed
    parameter instead, and `request_value_expr` is just that parameter's own name --
    the same mechanism `requestBody` already used for an order-placing `buy`/`sell`, not
    a special case; a bare JSON Schema `array` (a `{place,cancel,modify}_batch` body) is
    the same shape as the `anyOf` case again -- one `list[...]`-typed
    parameter carrying the whole request, nothing to wrap it in -- rather than a fourth,
    genuinely different mechanism. Positional-vs-keyword derivation is
    `_flat_request_kwargs`.

    A flat request whose properties are *all* required renders `request_value_expr` as
    a single `Request(...)` call expression, no preceding statement needed. One with any
    `NotRequired` property instead returns `decl_lines` -- a `request: Request =
    Request(...)` declaration for the required fields, one `if {param} is not None:
    request['{name}'] = {param}` per optional field after it, mirroring the shape
    `HttpRequest.dict_declaration` already uses for `params`/`headers` -- and
    `request_value_expr` becomes the bare local name `'request'`. Passing every
    property unconditionally (the flat-`Request(...)`-call shape) would be wrong for an
    optional field: a caller who omits it gets `None` explicitly written into the wire
    dict instead of the key being absent, defeating the point of `NotRequired`.

    The `anyOf` branch never independently re-derives the union's type -- it reuses
    `request_type`, `rpc_endpoint`'s own already-registered resolution of `'$request'`
    (`types.identifiers.get('$request')`, from the one `type_generator(schemas,
    inline=True)` call that ran `Normalizer`/`Unnest.records()` first). Calling
    `type_generator.render.parser(request_schema, id=None)` directly here -- the original,
    buggy version of this method -- bypasses that normalization: each titled-object
    variant reaches `Parser.any_of`'s own `self.inline(v)` call as a raw, un-normalized
    `Schema` rather than the `Reference` `Unnest.records()` would have replaced it with,
    and `Parser.inline` refuses to inline a record (`raise ValueError('Found nested
    record type')`) -- confirmed by reproducing it against a synthetic buy/sell-shaped
    fixture. A hand-rolled backend never calls `.render.parser(...)` directly for
    exactly this reason; it always goes through the registered `type_generator(...)` call.

    Args:
      request_schema: `endpoint.spec.request`, parsed -- its own top-level `title` is
        read here (to name a union parameter) even though the copy fed to
        `type_generator` has that title stripped (the fixed `Request` naming); pass
        the original, not the stripped copy. `None` for a parameterless operation.
      type_generator: The same `TypeGenerator` instance used to render `Request`/the
        response type, so a flat object's per-property type here matches the one baked
        into `Request`'s own field annotations exactly (same `Parser`/`CodeGenerator`).
        Not consulted at all for a titled `anyOf` request -- see `request_type`.
      request_type: `types.identifiers.get('$request')`, already resolved by
        `rpc_endpoint` through the registered `type_generator(schemas, inline=True)`
        call -- the titled-`anyOf` union parameter's own type, reused verbatim rather
        than re-derived. `None` only when `request_schema` is also `None`.
      identifiers: `types.identifiers`, the same registered-call result `request_type`
        comes from -- consulted per flat property for the identical reason `anyOf`
        already is: a property that is itself a titled record (`$request/{name}`) or an
        array of one (`$request/{name}/item`, `Unnest`'s own array-element convention,
        the same one `_paged_rows_type`'s docstring documents for a response path) was
        already correctly resolved and named by that one call, and re-deriving it by
        calling `renderer.parser(prop, id=None)` directly hits the *identical* un-
        normalized-`Schema` bug the `anyOf` branch already had fixed for it: `Parser.
        array`'s own `self.inline(schema.items, ...)` refuses any record type outright
        (`ValueError('Found nested record type')`), titled or not -- a
        `get_nft_metadata_batch` (`tokens: list[NftMetadataBatchToken]`, a real,
        single-use nested shape rule 0 says to inline rather than `$ref`) is the real
        case that surfaced this. Omitted (`{}`) falls through to the direct parse for
        every scalar/array-of-scalar property, unchanged from before this parameter
        existed.
    """
    if request_schema is None:
      return [], [], [], 'None', []
    if request_schema.anyOf:
      name = self.identifier(request_schema.title or 'request')
      param = Function.Param(name=name, type=request_type, required=True)
      doc = Docstring.Param(name=name, required=True, docstring=request_schema.description)
      return [param], [], [], name, [doc]
    if request_schema.type == 'array':
      # A bare JSON Schema `array` request (a `{place,cancel,modify}_batch` body, each a
      # top-level array body with no enclosing object) -- the one
      # shape this method originally had no branch for at all: `properties` is `None` on
      # an array schema, so `properties or {}` silently evaluated to `{}`, `field_params`
      # stayed empty, and the method fell through to the final `if not field_params`
      # branch below, generating a *zero-parameter* method whose body was the literal
      # `Request()` call -- an empty TypedDict constructor standing in for what should
      # have been the caller's real list, always sending `[]`/nothing regardless of what
      # was passed. Confirmed live: all 3 of one project's real batch endpoints generated
      # this way, and all 3 real replay tests failed with a 422 from the mock server.
      #
      # There is nothing to wrap here -- the array *is* the whole request, not a property
      # of one -- so this mirrors the `anyOf` branch just above rather than the flat-
      # object branch below: one positional parameter, typed `request_type` (already
      # resolved, by the same `type_generator(schemas, inline=True)` call `anyOf` reuses,
      # to exactly `list[<ItemType>]` -- `rpc_endpoint`'s own registered call renders a
      # bare top-level array the identical way it renders a titled `anyOf`: `Renderer.
      # __call__`'s `elif inline and code.iden != 'None'` branch binds the array's own
      # rendered `list[...]` expression to the fixed `Request` alias name, with any
      # titled record `Unnest.records()` found nested inside `items` -- including inside
      # an `anyOf` of variants, a real shape -- rendered as its own class the
      # exact same way a flat property's nested record already is), passed straight
      # through as the request body with no `Request(...)` constructor call at all.
      name = self.identifier(request_schema.title or 'items')
      param = Function.Param(name=name, type=request_type, required=True)
      doc = Docstring.Param(name=name, required=True, docstring=request_schema.description)
      return [param], [], [], name, [doc]

    properties = request_schema.properties or {}
    required = set(request_schema.required or [])
    field_params: list[Function.Param] = []
    field_docs: list[Docstring.Param] = []
    required_parts: list[tuple[str, str]] = []
    optional_fields: list[tuple[str, str]] = []
    for name, prop in properties.items():
      # `Request`'s own field type for this property, resolved consulting `identifiers`
      # at every nested position `Unnest.records()` could have extracted one -- design
      # §5b's own `$ref`-into-`schemas.json` case has no `Unnest` entry at all (never
      # unnested; already a `Reference`), so it's `_nested_prop_type`'s own first check.
      type_name = self._nested_prop_type(
        prop, prefix=f'$request/{name}', type_generator=type_generator, identifiers=identifiers,
      )
      description = None if isinstance(prop, Reference) else prop.description
      # `_avoid_reserved_collision`: a flat request property literally named `validate`
      # (a real, motivating case) would otherwise duplicate the reserved `validate`
      # kwarg every generated `rpc`/`stream` method already carries -- a real `SyntaxError`.
      identifier = self._avoid_reserved_collision(self.identifier(name))
      is_required = name in required
      default = None
      if is_required and isinstance(prop, Schema) and prop.enum is not None and len(prop.enum) == 1:
        # The "required single-value enum gets a literal default" rule (originally for
        # `module`/`action` dispatch constants). A required, single-value `enum` request
        # property is wire dispatch plumbing, not a real per-call choice -- an API may
        # multiplex every operation through one path's fixed `module`/`action` query
        # parameters, and
        # a caller has no reason to ever pass a different value. Giving it a literal Python
        # default lets a caller omit it while the property stays genuinely required on the
        # wire: it's still unconditionally included in `required_parts` below (never
        # `NotRequired`), so the recorded example call and the mock's `expected_query` still
        # see it -- only the generated *Python* signature drops the busywork of typing it
        # out on every call.
        default = repr(prop.enum[0])
      field_params.append(
        Function.Param(name=identifier, type=type_name, required=is_required, default=default)
      )
      field_docs.append(Docstring.Param(name=identifier, required=is_required, docstring=description))
      if is_required:
        required_parts.append((name, identifier))
      else:
        optional_fields.append((name, identifier))
    if not field_params:
      return [], [], [], 'Request()', []
    positional, keyword = self._flat_request_kwargs(field_params)
    required_expr, needs_annotation = self._typed_dict_constructor('Request', required_parts)
    if not optional_fields and not needs_annotation:
      return positional, keyword, [], required_expr, field_docs
    decl_lines = [f'request: Request = {required_expr}']
    for name, identifier in optional_fields:
      decl_lines.append(f'if {identifier} is not None:')
      decl_lines.append(f"  request['{name}'] = {identifier}")
    return positional, keyword, decl_lines, 'request', field_docs

  def rpc_endpoint(
    self,
    endpoint: Endpoint,
    references: Mapping[str, ExternalReference],
    *,
    class_name: str,
    method_name: str,
    endpoint_dir: Path | None = None,
  ) -> str:
    """Generate Python code for a single rpc endpoint module, over whichever transport(s)
    it declares.

    The method body is one `self.request(...)` call -- a `Request`
    TypedDict (or union alias, for a titled `anyOf` request) built from
    `endpoint.spec.request`, a response type built from `endpoint.spec.response`, and --
    when the resolved core declares a `meta` JSON Schema -- a plain dict
    literal built from `endpoint.meta`'s own declared values (`meta={'scheme': 'l1', ...}`),
    passed as one `meta=` argument. This dict literal is never rendered against a
    generated type: the resolved core's own hand-written module defines its own plain
    `class Meta(TypedDict): ...` matching the declared schema (the same pattern this repo
    already uses for a spec-declared timestamp `format` -- `TimestampMillis` and friends
    are hand-written to match, never code-generated; S27), and pyright checks the emitted
    dict literal against that hand-written `meta: Meta`-typed parameter structurally, with
    zero import anywhere in the generated module: a dict literal is checked against a
    `TypedDict` by shape, not by whether the literal's enclosing module ever names the
    class. A core with no declared `meta` schema gets no `meta=` argument at all --
    `endpoint.meta` must then be `{}` (checked at generation time; `truewire check`
    catches it earlier, at authoring time). No per-parameter wire-placement logic runs
    here: that's exactly the ~990-endpoint gap `request` replacing
    `parameters`/`requestBody` closes -- `core.request()` decides query vs
    body vs header on its own, hand-written, per-API terms, from the bare `Request`
    type this method hands it. When every `request` property is required, that's exactly
    one statement (`return await self.request(Request(...), ...)`); a `NotRequired`
    property needs a `request: Request = Request(...)` declaration plus one `if {param}
    is not None: request[...] = {param}` line per optional field ahead of it, so an
    omitted optional argument leaves the key genuinely absent from the wire request
    rather than writing `None` into it (`_rpc_request_params`).

    Only the new `request`/`response`-shaped case is implemented today. A legacy
    `openapi`-shaped endpoint (not yet migrated) still raises `NotImplementedError`
    rather than silently mis-generate. A genuine multi-transport endpoint
    (`len(endpoint.spec.transports) > 1`) is generated for real: a
    `transport: Literal[...]` keyword, one literal per
    declared transport in `spec.transports` order, defaulting to `spec.transports[0]`
    (the first-listed, "primary" transport) -- threaded straight through as one more
    `self.request(...)` call argument, `transport=transport`. A single-transport endpoint
    gets no such parameter at all: there is nothing for a caller to choose. A WS-only RPC
    command (`spec.method is None`) is supported: `method=...` is simply omitted from the
    generated call.

    Args:
      endpoint: The endpoint whose module is about to be generated.
      references: Shared schemas already emitted elsewhere in the package.
      class_name: Name chosen for the generated class.
      method_name: Name chosen for the generated method.
      endpoint_dir: This endpoint's own `spec/endpoints/` directory -- resolves the base
        core class through `_resolve_core`, together with `project`/`codegen_config`
        (set by the CLI the same way `core_package` already is). Kept a per-call
        parameter, unlike those two, because it genuinely differs endpoint to endpoint --
        `_resolve_core` itself needs no constructor of its own for it.
    """
    spec = endpoint.spec
    if not isinstance(spec, RpcEndpointSpec):
      raise TypeError(f'rpc_endpoint called on a non-rpc endpoint (kind={spec.kind!r})')
    if spec.request is None and spec.response is None:
      raise NotImplementedError(
        'rpc_endpoint only generates the request/response shape; this '
        'endpoint is still openapi-shaped and needs migrating first'
      )
    if endpoint_dir is None or self.project is None or self.codegen_config is None:
      raise ValueError(
        'rpc_endpoint needs endpoint_dir, plus project/codegen_config (set by the '
        'CLI after loading the backend), to resolve the base core class'
      )

    core_config = self._resolve_core(endpoint_dir, self.project.spec_dir, self.codegen_config)
    core_module, _, core_class = core_config.base.partition(':')
    meta_schema = self._resolve_meta_schema(endpoint_dir, self.project.spec_dir, self.codegen_config)
    endpoint_plan = self.endpoint_plan(endpoint, endpoint_dir)

    type_generator = self.type_generator(references)
    # A response (or request) schema's own title can collide with `class_name` --
    # market/orderbook's response is itself titled `Orderbook`, matching the endpoint
    # class the directory-derived name also picks. `class_name`/`type_names` (the
    # collision-avoidance `Generator` already runs before choosing a leaf's class name)
    # haven't migrated to this request/response shape yet, so the type's own name has to
    # yield here instead -- `forbidden` makes `disambiguate` grow it past its bare title
    # (`Orderbook` -> `OrderbookResponse`) exactly the way a genuine title collision
    # between two schemas already does, rather than emitting two same-named classes.
    type_generator.forbidden = {class_name}
    request_schema = Schema.model_validate(spec.request) if spec.request is not None else None
    # A response that is, in its entirety, one bare `{"$ref": "..."}` -- not a `$ref`
    # nested inside an object/array (already handled correctly: the type_generator walks
    # into it and resolves the reference against `external_references` on its own) but
    # the *whole* schema being nothing but a reference -- can't go through
    # `Schema.model_validate` at all: `Schema` (openapi_schema_pydantic) declares no `ref`
    # field and its own `model_config` is `extra='allow'`, so `$ref` is silently accepted
    # as an ignored extra key and the result validates as an empty, untyped schema (no
    # `type`, no `properties`) rather than raising -- confirmed directly
    # (`Schema.model_validate({'$ref': 'X'}).type is None`). A
    # `simulation.execution`/`simulation.asset_changes` pair is the real, motivating case:
    # `ExecutionResult`/`AssetChangesResult` are each referenced twice (once here, once as
    # array items on the sibling `_bundle` endpoint), so neither can be inlined under rule
    # 0's "shared schema used once" allowance -- a bare top-level `$ref` response is a
    # legitimate spec shape, not a spec-authoring mistake to route around. Resolved the
    # identical way a `$ref`-typed *request* property already is (`_rpc_request_params`'s
    # own `external_references.get(prop.ref)` branch, just above): directly against
    # `references`, with no local schema definition or `schemas['$response']` entry at all.
    # `returned_response` is the schema of what the method returns: `spec.response`
    # itself, or the node `envelope.payload` selects inside it (ADR 0010) -- the wrapper
    # is never rendered. A selected node commonly carries a `description` beside its
    # `$ref` (rule 7 asks for one on every property), so a `$ref` with company is a
    # reference too, resolved the same way.
    returned_response = self.returned_response_schema(endpoint)
    response_ref = (
      returned_response['$ref']
      if isinstance(returned_response, dict) and isinstance(returned_response.get('$ref'), str)
      else None
    )
    response_schema: Schema | None = None
    response_type: str | None = None
    response_import: dict[str, set[str]] = {}
    if response_ref is not None:
      external = references.get(response_ref)
      if external is None:
        raise ResolutionError(response_ref)
      response_type = external['name']
      response_import = {external['package']: {external['name']}}
    elif returned_response is not None:
      response_schema = Schema.model_validate(returned_response)
    schemas: dict[str, Schema] = {}
    if request_schema is not None:
      # Forced to the fixed, per-module `Request` name regardless of the schema's own
      # `title` (the interface contract) -- stripping the title here makes
      # `naming.disambiguate`'s own path-based fallback (`type_name('$request')`, once
      # no title wins its n=0 pass) resolve to exactly `Request`, rather than fighting
      # `disambiguate`'s `fixed` parameter, which is designed for a name owned by an
      # *external* reference, not for renaming a schema that's itself a member of the
      # very `all_schemas` mapping being disambiguated (confirmed: `fixed` entries are
      # still recomputed by the first `unique_mapping` pass and silently overwritten).
      schemas['$request'] = request_schema.model_copy(update={'title': None})
    if response_schema is not None:
      schemas['$response'] = response_schema
    types = type_generator(schemas, inline=True)

    request_type = types.identifiers.get('$request')
    if response_ref is None:
      response_type = types.identifiers.get('$response')

    positional, keyword, request_decl_lines, request_value_expr, request_docs = self._rpc_request_params(
      request_schema, type_generator=type_generator, request_type=request_type,
      identifiers=types.identifiers,
    )

    validate_param = Function.Param(name='validate', type='bool', required=False)
    transport_param: Function.Param | None = None
    transport_imports: dict[str, set[str]] = {}
    if len(spec.transports) > 1:
      # A genuine multi-transport rpc (`transports: ['http', 'ws']`) gets one real,
      # caller-facing `transport` keyword -- reusing the already-existing "required
      # single-value enum gets a literal default" rule (originally for `module`/`action`
      # dispatch constants), applied
      # here to a genuine per-call *choice* instead of fixed wire plumbing: a length-1
      # `transports` (the overwhelmingly common case) produces no parameter at all --
      # nothing for a caller to choose -- while a length-2 one produces a real
      # `Literal[...]` over every declared transport, defaulting to the first-listed
      # ("primary") one.
      # `required=True` here (despite this being an optional-to-pass keyword) is
      # deliberate, mirroring the exact same dispatch-constant mechanism just
      # above (`_rpc_request_params`'s own `default` branch): `Param.code` only skips
      # appending `| None` to the rendered type when `required` is true, and a caller
      # omitting `transport` should still get a real `Literal['http', 'ws']`, never
      # `Literal['http', 'ws'] | None` -- there is no `None` value this parameter ever
      # legitimately holds.
      transport_literal = ', '.join(repr(t) for t in spec.transports)
      transport_param = Function.Param(
        name='transport', type=f'Literal[{transport_literal}]', required=True,
        default=repr(spec.transports[0]),
      )
      transport_imports = {'typing_extensions': {'Literal'}}
    # `endpoint.deprecated` is a top-level `Endpoint` field (a sibling of `spec`, like
    # `pagination`/`meta`), unrelated to the request/response shape itself -- the legacy
    # openapi-shaped `rpc_endpoint` predecessor read `op.deprecated` (OpenAPI's own field)
    # instead, which no longer exists once an endpoint migrates off `openapi` entirely.
    # Undetected until the first new-shape project with deprecated endpoints migrated:
    # silently dropping the decorator would regress
    # an already-documented deprecation warning to a plain, unflagged method.
    deprecated_imports: dict[str, set[str]] = {}
    decorators: list[str] = []
    if endpoint.deprecated:
      decorators.append("@deprecated('Deprecated method')")
      deprecated_imports = {'typing_extensions': {'deprecated'}}
    header = Function(
      name=method_name, asyn=True, method=True,
      args=positional,
      kwargs=[*keyword, validate_param, *([transport_param] if transport_param is not None else [])],
      return_type=response_type, decorators=decorators,
    )
    if response_type is not None:
      if (shadow := self_shadowing_alias(method_name, response_type)) is not None:
        header.return_type, header.return_type_alias = shadow

    docstring = Docstring(
      description=spec.description,
      docs_url=endpoint.docs,
      params=[
        *request_docs,
        Docstring.Param(
          name='validate', required=False,
          docstring=(
            "Override this call's response validation; falls back to the client-level "
            'default when omitted. `False` returns the parsed body as it came, typed `Any`.'
          ),
        ),
        *(
          [
            Docstring.Param(
              name='transport', required=False,
              docstring=f'Transport to send this call over. Defaults to {spec.transports[0]!r}.',
            ),
          ]
          if transport_param is not None
          else []
        ),
      ],
    )

    if not isinstance(endpoint.meta, dict):
      # `Endpoint._require_meta_for_new_shape` only checks `meta is not None`, so a
      # request/response-shaped endpoint spec'd `"meta": false` (or any other non-dict
      # value) passes spec loading -- silently coercing that to `{}` here would generate
      # a method with zero meta-related kwargs, indistinguishable from a genuinely public
      # endpoint and defeating the "meta is never omittable" guarantee. Fail loud
      # at generation time instead.
      raise ValueError(
        f'{spec.path}: endpoint.meta must be a dict to generate rpc_endpoint '
        f'("meta is never omittable"); got {endpoint.meta!r}'
      )
    meta = endpoint.meta
    meta_declared = meta_schema is not None
    if not meta_declared and meta:
      # The resolved core declares no `meta` schema at all -- a core with no `meta`
      # schema means every endpoint resolving to it must declare `meta: {}`. A
      # non-empty `meta` here has nowhere to be validated (`truewire.spec.authoring`'s
      # own `spec test` check catches this earlier, at authoring time -- this is the
      # generation-time backstop for the same invariant, the identical relationship the
      # non-dict-meta check just above already has to spec loading).
      raise ValueError(
        f'{spec.path}: endpoint.meta declares {sorted(meta)}, but its resolved core '
        f'({core_config.base!r}) declares no `meta` schema in truewire.toml [cores.<name>] '
        '-- either declare a schema for this core, or empty this endpoint\'s meta to {}'
      )
    call_args = [request_value_expr]
    if spec.method is not None:
      call_args.append(f'method={spec.method!r}')
    call_args.append(f'path={spec.path!r}')
    if meta_declared:
      # `meta` is never splatted as bare kwargs, and never a generated `Meta(...)` call
      # either -- a plain dict literal, checked structurally by pyright against the
      # resolved core's own hand-written `meta: Meta`-typed parameter (a real, hand-
      # written `class Meta(TypedDict): ...` living in that core's own module, matching
      # the declared schema -- the same pattern this repo already uses for a spec-
      # declared timestamp `format`, S27). No import needed anywhere in this module.
      meta_fields = ', '.join(f'{key!r}: {value!r}' for key, value in meta.items())
      call_args.append(f'meta={{{meta_fields}}}')
    call_args.append('validate=validate')
    if transport_param is not None:
      call_args.append('transport=transport')
    # `cast(type, ...)` around a bare `Literal[...]`/`Any` alias, which pyright refuses
    # where a `type[T] | UnionType | None` is expected -- the plan decides it from the
    # type tree (`RequestPlan.needs_cast`); a `$ref`-resolved external type is always a
    # real class and never needs it.
    # A parameter named `type` shadows the builtin inside the method body, so the cast
    # names it through `builtins` there.
    needs_cast = False
    cast_type = 'builtins.type' if any(param.name == 'type' for param in [*positional, *keyword]) else 'type'
    if request_type is not None:
      if endpoint_plan.request.needs_cast:
        call_args.append(f'request_type=cast({cast_type}, {request_type})')
        needs_cast = True
      else:
        call_args.append(f'request_type={request_type}')
    if response_type is not None:
      if endpoint_plan.response.needs_cast:
        call_args.append(f'response_type=cast({cast_type}, {response_type})')
        needs_cast = True
      else:
        call_args.append(f'response_type={response_type}')
    call_lines = group_lines(call_args, tab='  ', width=88)
    return_line = 'return await self.request(\n' + '\n'.join(call_lines) + '\n)'
    body = '\n'.join([*request_decl_lines, return_line])

    header.overloads = validate_overloads(header, raw_return_type='Any')
    params_shadow_builtin = header.qualify_self_shadowed_params()
    method_lines = [header.code()]
    doc_code = docstring.code()
    if doc_code:
      method_lines.append(indent(doc_code, '  '))
    method_lines.append(indent(body, '  '))
    method_code = '\n'.join(method_lines)

    # `endpoint.pagination` is a sibling of `spec` (docs/spec/authoring.md rule 8). Every
    # declared walk renders `PaginatedResponse`-shaped (ADR 0013), so the row type has to
    # resolve: `paged_response_rows_type` reads it off `$response` -- the value the method
    # returns, `envelope.payload` already selected (ADR 0010). Failing to resolve it is a
    # spec gap (a wrapped payload with no `rows` declared) and raises rather than silently
    # generating a lesser method. `zero_value_is_wire_absent=False`: an ordinary optional
    # REST/JSON-RPC parameter, never a proto3 message field.
    pagination = endpoint.pagination
    paged_source: str | None = None
    if pagination is not None:
      rows_type: str | None = None
      # Populated only when `paged_response_rows_type` resolves `rows_type` via the
      # bare-`$ref`-response path (`response_ref`) -- that row type is never seen by
      # `type_generator`, so nothing else registers its import (see that method's own
      # `imports` parameter docstring).
      rows_type_imports: dict[str, set[str]] = {}
      rows_path = self.pagination_rows_path(pagination)
      rows_type = self.paged_response_rows_type(
        types, rows_path, response_ref=response_ref, references=references,
        imports=rows_type_imports,
      )
      if rows_type is None:
        where = f'rows {rows_path!r}' if rows_path else 'no rows path, so the payload itself'
        raise ValueError(
          f'{endpoint.function or spec.path}: cannot resolve the row type of its `pagination` ({where}); '
          f'every paged method is `PaginatedResponse`-shaped (ADR 0013) and has to know '
          f'which field the rows are -- declare `rows` (`done.rows` for page/offset/token) '
          f'naming the array field, or check that the response schema declares it'
        )
      state_type = (
        self.paged_state_type(header, pagination) if pagination.strategy == 'token' else None
      )
      # Whether the single-request method's return type is nullable (an `anyOf`-wrapped
      # response whose real branch carries the rows) is the plan's
      # `ResponsePlan.optional`, read off the type tree; the walker guards every read.
      planned = endpoint_plan.pagination
      cursor_format = (
        seek_cursor_format(planned.cursor_type, planned.state_type) if planned is not None else None
      )
      paged_source = self.paged_response_method(
        endpoint, method_name=method_name, header=header,
        rows_type=rows_type, state_type=state_type, zero_value_is_wire_absent=False,
        response_optional=endpoint_plan.response.optional, docstring=docstring,
        cursor_format=cursor_format,
      )
      paged_imports = merge_imports([
        self.paged_imports(endpoint, header=header, cursor_format=cursor_format), rows_type_imports,
      ])

    # `paged_source` (when present) is emitted *before* `method_code`, not after -- a real
    # Python landmine, not a style choice: when `method_name` happens to shadow a builtin
    # (a `products.list`, `RpcEndpoint` subclass `List`), evaluating that method's
    # own parameter annotations binds the name `list` into the *class* namespace the moment
    # the `def` statement completes, and every class-body statement after it that writes a
    # bare `list[...]` annotation -- exactly what `list_paged`'s own `product_ids: list[str]`
    # does -- resolves against that now-shadowed class attribute instead of the builtin,
    # raising `TypeError: 'function' object is not subscriptable` at class-definition time
    # (reproduced directly: a class defining `def list(self, x: list[int])` then, in the
    # same body, another method also annotating a param `list[int]`, crashes identically).
    # `paged_source`'s own name always carries a `_paged` suffix, so it can never itself be
    # a builtin name -- ordering it first can only avoid this shadow, never introduce one.
    class_doc = spec.description or f'`{method_name}`.'
    refusal, refusal_imports = self.refusal()
    class_lines = [
      *refusal,
      f'class {class_name}({core_class}):',
      indent(f'"""{class_doc}"""', '  '),
    ]
    if paged_source is not None:
      class_lines.append('')
      class_lines.append(indent(paged_source, '  '))
    class_lines.append('')
    class_lines.append(indent(method_code, '  '))
    class_code = '\n'.join(class_lines)

    merged_imports = merge_imports([
      dict(types.imports), {core_module: {core_class}}, response_import,
      paged_imports if paged_source is not None else {}, deprecated_imports, transport_imports,
      {'typing_extensions': {'cast'}} if needs_cast else {},
      {'builtins': set()} if needs_cast and cast_type != 'type' else {},
      VALIDATE_OVERLOAD_IMPORTS if header.overloads else {},
      {'builtins': set()} if params_shadow_builtin else {},
      refusal_imports,
    ])
    imports_code = ImportsRenderer(
      imports=merged_imports, pkg_name=core_module.split('.')[0],
    ).code()

    type_defs = [types.definitions[id] for id in types.generation_order if id in types.definitions]

    parts: list[str] = []
    if imports_code:
      parts.append(imports_code)
    if type_defs:
      parts.append('\n\n\n'.join(type_defs))
    if header.return_type_alias:
      parts.append(header.return_type_alias)
    parts.append(class_code)
    return '\n\n\n'.join(parts)

  def _stream_parameters_params(
    self, parameters_schema: Schema | None, *, type_generator: TypeGenerator, parameters_type: str | None,
    identifiers: Mapping[str, str] = {},
  ) -> tuple[
    list['Function.Param'], list['Function.Param'], list[str], str, list['Docstring.Param'],
  ]:
    """Derive a `subscribe()` call's own generated parameters from `endpoint.spec.parameters`.

    A `stream` sibling of `_rpc_request_params`, kept as its own copy (rather than one
    shared top-level method) for the fixed local name difference (`Parameters` here,
    `Request` there) and the `parameters`-vs-`request` field alias --
    but the actual per-property type resolution *is* shared, via `_nested_prop_type`.
    Same two shapes, same reasoning: a flat object's `properties` become named kwargs,
    and the returned `parameters_value_expr` builds a `Parameters(...)` instance from them;
    a titled `anyOf` becomes one union-typed parameter instead, and `parameters_value_expr`
    is just that parameter's own name.

    A flat parameters schema whose properties are *all* required renders
    `parameters_value_expr` as a single `Parameters(...)` call expression, no preceding
    statement needed. One with any `NotRequired` property instead returns `decl_lines` -- a
    `parameters: Parameters = Parameters(...)` declaration for the required fields, one `if
    {param} is not None: parameters['{name}'] = {param}` line per optional field after it --
    and `parameters_value_expr` becomes the bare local name `'parameters'`.

    The `anyOf` branch never independently re-derives the union's type -- it reuses
    `parameters_type`, `stream_endpoint`'s own already-registered resolution of
    `'$parameters'` (`types.identifiers.get('$parameters')`, from the one
    `type_generator(schemas, inline=True)` call that ran `Normalizer`/`Unnest.records()`
    first). Calling `type_generator.render.parser(parameters_schema, id=None)` directly here
    would bypass that normalization the exact way an earlier, buggy version of
    `_rpc_request_params` once did (`ValueError: Found nested record type` on any real
    object-discriminated-union case) -- see that method's own docstring for the full
    mechanism this one deliberately avoids repeating.

    Args:
      parameters_schema: `endpoint.spec.parameters` (or `.request`, the stream-spec alias
        for the same field), parsed -- its own top-level `title` is read here (to name a
        union parameter) even though the copy fed to `type_generator` has that title
        stripped (the fixed `Parameters` naming); pass the original, not the stripped
        copy. `None` for a parameterless subscription.
      type_generator: The same `TypeGenerator` instance used to render `Parameters`/the
        payload type, so a flat object's per-property type here matches the one baked into
        `Parameters`'s own field annotations exactly (same `Parser`/`CodeGenerator`).
      parameters_type: `types.identifiers.get('$parameters')`, already resolved by
        `stream_endpoint` through the registered `type_generator(schemas, inline=True)`
        call -- the titled-`anyOf` union parameter's own type, reused verbatim rather than
        re-derived. `None` only when `parameters_schema` is also `None`.
    """
    if parameters_schema is None:
      return [], [], [], 'None', []
    if parameters_schema.anyOf:
      name = self.identifier(parameters_schema.title or 'parameters')
      param = Function.Param(name=name, type=parameters_type, required=True)
      doc = Docstring.Param(name=name, required=True, docstring=parameters_schema.description)
      return [param], [], [], name, [doc]

    properties = parameters_schema.properties or {}
    required = set(parameters_schema.required or [])
    field_params: list[Function.Param] = []
    field_docs: list[Docstring.Param] = []
    required_parts: list[tuple[str, str]] = []
    optional_fields: list[tuple[str, str]] = []
    for name, prop in properties.items():
      # See `_nested_prop_type` -- same mechanism `_rpc_request_params` uses, `Parameters`
      # rather than `Request`.
      type_name = self._nested_prop_type(
        prop, prefix=f'$parameters/{name}', type_generator=type_generator, identifiers=identifiers,
      )
      description = None if isinstance(prop, Reference) else prop.description
      # See `_rpc_request_params`'s identical `_avoid_reserved_collision` call, just
      # above this method's own sibling in the class -- same shape, same reasoning.
      identifier = self._avoid_reserved_collision(self.identifier(name))
      is_required = name in required
      field_params.append(Function.Param(name=identifier, type=type_name, required=is_required))
      field_docs.append(Docstring.Param(name=identifier, required=is_required, docstring=description))
      if is_required:
        required_parts.append((name, identifier))
      else:
        optional_fields.append((name, identifier))
    if not field_params:
      return [], [], [], 'Parameters()', []
    positional, keyword = self._flat_request_kwargs(field_params)
    required_expr, needs_annotation = self._typed_dict_constructor('Parameters', required_parts)
    if not optional_fields and not needs_annotation:
      return positional, keyword, [], required_expr, field_docs
    decl_lines = [f'parameters: Parameters = {required_expr}']
    for name, identifier in optional_fields:
      decl_lines.append(f'if {identifier} is not None:')
      decl_lines.append(f"  parameters['{name}'] = {identifier}")
    return positional, keyword, decl_lines, 'parameters', field_docs

  def _direct_channel_scalar(self, prop: 'Reference | Schema') -> bool:
    """Whether one `parameters` property is a plain enough shape to interpolate
    directly into a channel template with no `Unnest`-registered nested type to look
    up -- a `$ref` (resolved through `type_generator.external_references`, never
    `identifiers`), or a schema with no nested `properties`/`anyOf`/`prefixItems` and
    no `object`/`array` type of its own. Every real channel placeholder seen so far
    is this shape (a bare string, string enum, or integer enum) -- refusing
    anything else is a defensive guard, not a corpus fact this rule depends on: a
    future placeholder needing real nested-type extraction falls back to the ordinary
    `Parameters`/`dump_request` path (`_channel_direct_params` returns `False`)
    instead of risking `_nested_prop_type` resolving a schema `Unnest.records()` never
    walked (see that method's own docstring for why nested-record extraction depends
    on a prior registered `type_generator(schemas, inline=True)` call over the *whole*
    schema -- one `_channel_direct_params`'s caller deliberately skips for this path).

    Args:
      prop: One `parameters` property's own schema (or a `$ref` into a shared one).
    """
    return direct_channel_scalar(prop)

  def _channel_direct_params(self, parameters_schema: Schema | None, channel: str) -> bool:
    """Whether this stream endpoint's declared `parameters` are *exactly* `channel`'s
    own placeholders -- a full bijection, every field required, nothing left over on
    either side -- the shape `stream_endpoint` renders by substituting the generated
    method's own real local variables straight into `channel` at the call site
    (`channel_expr`), with no `Parameters` object and no runtime `dump_request` detour
    at all. Every value the
    generated method receives already has nowhere else to go but into `channel`'s own
    template, so wrapping it in a `Parameters` TypedDict only to unwrap it again one
    call deeper was pure ceremony -- an `index_price_kline(pair, interval)` is
    the motivating, worked case (49 of one project's 53 stream endpoints are this shape).

    `False` for every other shape, so the caller falls back to the ordinary
    `Parameters`/`dump_request` path unchanged:

    - `parameters_schema is None` (nothing to substitute) or a titled `anyOf` (no flat
      `properties` to compare against `channel`'s placeholders at all).
    - A genuine superset -- a `batched`-style extra field, real wire data that
      isn't a channel placeholder, which still needs `Parameters`/`dump_request` to
      reach the wire at all (a hand-written `subscribe` pops it off the serialized dict
      directly).
    - Any field left `NotRequired` -- a placeholder can't conditionally be missing
      from its own template, so this is spec debt to double check, not a shape to
      silently render around.
    - Any property `_direct_channel_scalar` refuses (see that method's own docstring).

    Args:
      parameters_schema: `endpoint.spec.parameters` (or `.request`), parsed. `None`
        for a parameterless subscription.
      channel: `endpoint.spec.channel`, the literal template string.
    """
    return channel_direct_params(parameters_schema, channel)

  def _direct_channel_prop_imports(self, prop: 'Reference | Schema', type_generator: TypeGenerator) -> Imports:
    """The bare import set one direct-channel `parameters` property's own rendered
    type needs -- the counterpart to `_nested_prop_type`'s `.iden`-only return value,
    which silently drops `Code.imports` on the floor (fine for every *other* caller:
    `_rpc_request_params`/`_stream_parameters_params` also register the same property
    through a whole-schema `type_generator(schemas, inline=True)` call to build
    `Request`/`Parameters` itself, and that call collects the identical import as a
    side effect of building the TypedDict body). A direct-channel endpoint never
    registers `$parameters` at all (`_direct_channel_scalar`'s own docstring), so
    nothing else collects this -- found live: a direct-channel `Literal[...]`-typed
    placeholder (a `klines` channel's own `type` field) generated
    with no `Literal` import at all, a real `NameError`-at-import-time bug caught by
    `truewire generate`'s own pyright pass, not by construction.

    Only called for the flat-scalar-or-`$ref` shape `_direct_channel_scalar` already
    restricts every direct-channel property to -- never on anything array/tuple/anyOf-
    nested (those recurse inside `_nested_prop_type` and would need the identical
    treatment at every recursive step, which this narrower call site doesn't need).

    Args:
      prop: The property's own schema (or a `$ref` into a shared one).
      type_generator: The same `TypeGenerator` instance used to resolve `type_name`.
    """
    if isinstance(prop, Reference):
      external = type_generator.external_references.get(prop.ref)
      if external is None:
        raise ResolutionError(prop.ref)
      return {external['package']: {external['name']}}
    renderer = type_generator.render
    return renderer.code(renderer.parser(prop, id=None)).imports

  def _stream_direct_params(
    self, parameters_schema: Schema, *, type_generator: TypeGenerator, identifiers: Mapping[str, str],
  ) -> tuple[
    list['Function.Param'], list['Function.Param'], list['Docstring.Param'], dict[str, str], Imports,
  ]:
    """Build one direct-channel stream endpoint's own real, individually-typed method
    parameters straight from its declared `parameters` schema -- the direct-channel
    counterpart to `_stream_parameters_params`'s `Parameters`-TypedDict construction.
    Every field is required (`_channel_direct_params` already confirmed the bijection
    before this is ever called), so there's no optional-field bookkeeping here at all,
    unlike that method's own `decl_lines`/`optional_fields` handling.

    Args:
      parameters_schema: `endpoint.spec.parameters` (or `.request`), parsed --
        already confirmed by `_channel_direct_params` to be a flat, fully-required
        bijection with `channel`'s own placeholders.
      type_generator: The same `TypeGenerator` instance used elsewhere in this
        endpoint's generation, so a field's rendered type matches exactly.
      identifiers: The endpoint's own already-registered type identifiers (`types.
        identifiers`, from generating `$payload` alone on this path -- `$parameters`
        is deliberately never registered here, see `_direct_channel_scalar`), passed
        straight through to `_nested_prop_type`.

    Returns:
      The method's own positional/keyword `Function.Param`s (`_flat_request_kwargs`-
      split), one `Docstring.Param` per field, the wire name -> generated identifier
      mapping `channel_expr` substitutes into the channel template, and the merged
      imports every field's own rendered type needs (`_direct_channel_prop_imports`;
      `$parameters` was never registered through `type_generator`, so nothing else
      collects these).
    """
    properties = parameters_schema.properties or {}
    field_params: list[Function.Param] = []
    field_docs: list[Docstring.Param] = []
    name_to_identifier: dict[str, str] = {}
    field_imports: list[Imports] = []
    for name, prop in properties.items():
      type_name = self._nested_prop_type(
        prop, prefix=f'$parameters/{name}', type_generator=type_generator, identifiers=identifiers,
      )
      description = None if isinstance(prop, Reference) else prop.description
      identifier = self._avoid_reserved_collision(self.identifier(name))
      field_params.append(Function.Param(name=identifier, type=type_name, required=True))
      field_docs.append(Docstring.Param(name=identifier, required=True, docstring=description))
      name_to_identifier[name] = identifier
      field_imports.append(self._direct_channel_prop_imports(prop, type_generator))
    positional, keyword = self._flat_request_kwargs(field_params) if field_params else ([], [])
    return positional, keyword, field_docs, name_to_identifier, merge_imports(field_imports)

  def channel_expr(self, channel: str, identifiers: Mapping[str, str]) -> str:
    """Render the Python expression that builds one direct-channel stream endpoint's
    fully resolved channel string straight from the generated method's own
    already-typed local variables -- no `Parameters` object, no runtime
    `dump_request` detour, called only once `_channel_direct_params` has confirmed
    the bijection.

    Default: an f-string literal substituting each of `channel`'s own `{name}`
    placeholders with `{identifier}`, the local variable already holding that field's
    value (e.g. `f'{pair}@indexPriceKline_{interval}'`). This is correct wherever a
    placeholder's wire representation is just its value's own `str()`/`format()` --
    confirmed for every direct-channel placeholder seen so far (a bare string, a
    string enum, or an integer enum; see `_direct_channel_scalar`).

    A client whose real wire convention needs a placeholder-specific transform can
    override this to route through its own small, documented core helper instead,
    keeping the transform centralized in one client-owned function rather than
    scattered as ad hoc per-call-site `.lower()`s with the convention undocumented at
    each site -- codegen would only decide *that* a placeholder's value flows through
    it, never *how* it's transformed. No project needs this today: one API's
    `symbol`/`pair` wire convention wants lowercase, but the caller is responsible for
    passing a correctly-cased value (every such parameter's spec `description` states
    the requirement) -- no implicit per-field transform is applied.

    Args:
      channel: The raw `channel` template string (`endpoint.spec.channel`).
      identifiers: Placeholder name -> generated Python identifier holding that
        parameter's own value, one entry per `channel` placeholder (a bijection,
        already confirmed by `_channel_direct_params` before this runs).
    """
    if not identifiers:
      return repr(channel)
    parts: list[str] = []
    last = 0
    for m in CHANNEL_PLACEHOLDER.finditer(channel):
      parts.append(_fstring_literal(channel[last:m.start()]))
      parts.append('{' + identifiers[m.group(1)] + '}')
      last = m.end()
    parts.append(_fstring_literal(channel[last:]))
    return "f'" + ''.join(parts) + "'"

  def _connect_channel_param(
    self, endpoint: Endpoint, parameters_schema: Schema | None, channel: str,
  ) -> str | None:
    """The wire property name, if this stream is a connect-only push (rule 11's `push:
    {"trigger": "connect"}`) whose `channel` template is entirely one placeholder, and
    `parameters` is a flat object with exactly that one, required property. `None` for
    every other shape -- including the ordinary subscribe/unsubscribe stream, any
    `after_rpc`-triggered push, a connect-only push with more than one parameter, or one
    whose `channel` doesn't consist of exactly that placeholder.

    This is the shape a listenKey-style private user-data stream takes: connecting *is*
    the subscription, so there is no real "channel" to route on and no field `channel`
    could ever disambiguate among siblings -- the whole point of `channel` in the
    ordinary case. listenKey-based `private_streams.user_data` endpoints are the only
    ones matching this shape so far (every `after_rpc`-triggered push declares
    `parameters: null` and so never matches here regardless of trigger).

    When this returns a name, `stream_endpoint` skips the general `channel`-template-plus-
    `Parameters`-object call shape entirely and instead passes the one already-generated
    positional argument straight through to `self.subscribe(...)` -- `channel` was
    functionally decorative there in the first place (a hand-written `core.subscribe()`
    for this shape has no per-call field *named* anything to disambiguate; it only ever
    reads the one value it's given).

    Args:
      endpoint: The endpoint being generated -- consulted for `push`.
      parameters_schema: `endpoint.spec.parameters` (or `.request`), parsed.
      channel: `endpoint.spec.channel`, the literal template string.
    """
    return connect_channel_param(endpoint.push, parameters_schema, channel)

  def stream_endpoint(
    self,
    endpoint: Endpoint,
    references: Mapping[str, ExternalReference],
    *,
    class_name: str,
    method_name: str,
    endpoint_dir: Path | None = None,
  ) -> str:
    """Generate Python code for a single WebSocket stream module.

    The method body is one `self.subscribe(...)` call -- a `Parameters`
    TypedDict (or union alias, for a titled `anyOf` parameters schema) built from
    `endpoint.spec.parameters`, a payload type built from `endpoint.spec.payload` (keeping
    its own schema title, exactly like `rpc_endpoint`'s response -- only the *parameters*
    side is forced to a fixed name), the raw `channel` template string passed straight
    through, unsplit, and -- when the resolved core declares a `meta` JSON Schema (design
    §2/§6) -- a plain dict literal built from `endpoint.meta`'s own declared values
    (mirrors `rpc_endpoint`'s identical treatment exactly, see that method's own
    docstring for the full reasoning: no generated `Meta` type, checked structurally by
    pyright against the resolved core's own hand-written `meta: Meta`-typed parameter,
    zero import anywhere). A core with no declared `meta` schema gets no `meta=` argument
    at all -- `endpoint.meta` must then be `{}`.

    The `channel` template is split into path-vs-rest parameters here in exactly one
    shape now, narrowed down from a rule that used to hold with no exception at all
    (read on for why). For `rpc_endpoint`'s `path`, no per-parameter wire-placement
    logic runs here -- `core.request()` alone decides how a `{placeholder}` in `path` gets
    filled from the flat `Request` it's handed. `channel` originally reused the same
    placeholder-substitution rule as `path` unconditionally: `channel`
    rendered as a plain `repr()` of the template string no matter what, and *every*
    property of `Parameters` -- whether or not its name happened to match a
    `{placeholder}` in `channel` -- was resolved by whatever `core.subscribe()` implemented
    for that API, on the "location markers are eliminated, not redeclared" principle.

    That still holds -- unchanged -- whenever `parameters` carries a field `channel`
    doesn't (a `batched`-style extra field is the real, motivating case: genuine wire
    data with nowhere to go but a real subscribe-frame payload, which only `core` can
    build). It stopped holding for the *other* case: an endpoint whose declared
    `parameters` are *exactly* `channel`'s own placeholders and nothing else
    (`_channel_direct_params`) has no field left that isn't already a location marker, so
    routing every one of them through a generated `Parameters` object -- built, serialized
    by `dump_request`, then torn back apart by `core.subscribe()`'s own `.format(**...)`
    -- was ceremony around data the generated method already held as real, correctly-typed
    local variables. There, `stream_endpoint` builds the channel string directly at the
    call site instead (`channel_expr`): no `Parameters` TypedDict is generated for that
    endpoint at all, and the one resolved channel expression is the whole first argument to
    `self.subscribe(...)`. An `index_price_kline(pair, interval)` is the motivating,
    worked case; see `channel_expr` and `_channel_direct_params`'s own docstrings for the
    exact boundary and the per-API-transform escape hatch (`pair`/`symbol` lowering).

    A second, narrower shape predates the one above and is still handled separately:
    `_connect_channel_param` detects a connect-only push (rule 11) whose `channel`
    template *is* its own one, required parameter -- nothing to route on and nothing else
    the value could be, since connecting with it already *is* the subscription. There too,
    `channel`/`Parameters` are both dropped from the call entirely: the one generated
    argument is passed straight through to `self.subscribe(...)`, and no `Parameters`
    TypedDict is generated at all. listenKey-based `private_streams.user_data`
    endpoints are this shape -- checked first, so it takes precedence over the more
    general direct-channel shape above for the one endpoint family both would otherwise
    match.

    Unlike `rpc_endpoint`, the generated call carries no `await` and the generated method
    is not `async` -- `self.subscribe(...)` hands back a stream/manager object constructed
    synchronously (the caller then iterates or awaits *that*), not a single reply to await
    directly. This mirrors the legacy per-project `stream_endpoint` backends
    (`header = Function(..., asyn=False, ...)`, `return StreamManager(lambda: ...)`)
    -- `core.subscribe()`'s own return shape is a `core`
    decision, not something this method needs to await into.

    Only the new `parameters`/`payload`-shaped, WS-only case is implemented today. A legacy
    `openapi`-shaped endpoint (not yet migrated) raises `NotImplementedError` rather than
    silently mis-generate -- future work, not exercised by this pass's own tests.

    Args:
      endpoint: The endpoint whose module is about to be generated.
      references: Shared schemas already emitted elsewhere in the package.
      class_name: Name chosen for the generated class.
      method_name: Name chosen for the generated method.
      endpoint_dir: This endpoint's own `spec/endpoints/` directory -- resolves the base
        core class through `_resolve_core`, together with `project`/`codegen_config`
        (set by the CLI the same way `core_package` already is), mirroring `rpc_endpoint`'s
        own parameter of the same name exactly.
    """
    spec = endpoint.spec
    if not isinstance(spec, StreamEndpointSpec):
      raise TypeError(f'stream_endpoint called on a non-stream endpoint (kind={spec.kind!r})')
    # `parameters` and `request` are the same field for a stream spec (`StreamEndpointSpec`'s
    # own docstring: "same as `request` for stream") -- prefer `parameters`, the name design
    # §8 actually uses, but accept `request` too rather than silently ignoring an endpoint
    # authored with the other alias.
    parameters_schema_raw = spec.parameters if spec.parameters is not None else spec.request
    if parameters_schema_raw is None and spec.payload is None:
      raise NotImplementedError(
        'stream_endpoint only generates the parameters/payload shape; this '
        'endpoint is still openapi-shaped and needs migrating first'
      )
    if endpoint_dir is None or self.project is None or self.codegen_config is None:
      raise ValueError(
        'stream_endpoint needs endpoint_dir, plus project/codegen_config (set by the '
        'CLI after loading the backend), to resolve the base core class'
      )

    core_config = self._resolve_core(endpoint_dir, self.project.spec_dir, self.codegen_config)
    core_module, _, core_class = core_config.base.partition(':')
    meta_schema = self._resolve_meta_schema(endpoint_dir, self.project.spec_dir, self.codegen_config)
    endpoint_plan = self.endpoint_plan(endpoint, endpoint_dir)

    type_generator = self.type_generator(references)
    # A payload schema's own title can collide with `class_name` -- see `rpc_endpoint`'s
    # identical comment for the worked example (`Orderbook` vs `market/orderbook`).
    type_generator.forbidden = {class_name}
    parameters_schema = (
      Schema.model_validate(parameters_schema_raw) if parameters_schema_raw is not None else None
    )
    # A bare top-level `$ref` payload -- the same shape `rpc_endpoint`'s own `response_ref`
    # already resolves for `response` (see that branch's own comment for the full
    # reasoning: `Schema.model_validate({'$ref': 'X'})` silently drops the ref as an
    # ignored extra key and validates as an empty, untyped schema rather than raising).
    # `raw_candles`-style endpoints are the motivating case here -- each declares `payload: {"$ref": "..."}` and, before this branch
    # existed, silently rendered as `Any` instead of the real referenced type.
    payload_ref = (
      spec.payload['$ref']
      if isinstance(spec.payload, dict) and set(spec.payload) == {'$ref'}
      else None
    )
    payload_schema: Schema | None = None
    payload_ref_type: str | None = None
    payload_ref_import: dict[str, set[str]] = {}
    if payload_ref is not None:
      external = references.get(payload_ref)
      if external is None:
        raise ResolutionError(payload_ref)
      payload_ref_type = external['name']
      payload_ref_import = {external['package']: {external['name']}}
    elif spec.payload is not None:
      payload_schema = Schema.model_validate(spec.payload)
    # `reply` (ADR 0014) resolves exactly like `payload` just above; absent, the generated
    # call and return annotation are byte-for-byte what they were before the field existed.
    reply_ref = (
      spec.reply['$ref']
      if isinstance(spec.reply, dict) and set(spec.reply) == {'$ref'}
      else None
    )
    reply_schema: Schema | None = None
    reply_ref_type: str | None = None
    reply_ref_import: dict[str, set[str]] = {}
    if reply_ref is not None:
      external = references.get(reply_ref)
      if external is None:
        raise ResolutionError(reply_ref)
      reply_ref_type = external['name']
      reply_ref_import = {external['package']: {external['name']}}
    elif spec.reply is not None:
      reply_schema = Schema.model_validate(spec.reply)
    connect_param_name = self._connect_channel_param(endpoint, parameters_schema, spec.channel)
    # A direct-channel endpoint (`_channel_direct_params`) also skips the `Parameters`
    # TypedDict entirely, same as the connect-channel shape above it -- its call builds
    # the channel string straight from the generated method's own real local variables
    # (`channel_expr`), never wraps them in a `Parameters` instance at all (see
    # `call_args` below), so registering one here would be dead code the same way it
    # would for `connect_param_name`.
    direct_channel = connect_param_name is None and self._channel_direct_params(
      parameters_schema, spec.channel,
    )
    schemas: dict[str, Schema] = {}
    if parameters_schema is not None and connect_param_name is None and not direct_channel:
      # Forced to the fixed, per-module `Parameters` name regardless of the schema's own
      # `title` -- same mechanism, same reasoning, as `rpc_endpoint`'s identical treatment
      # of `$request`.
      schemas['$parameters'] = parameters_schema.model_copy(update={'title': None})
    if payload_schema is not None:
      schemas['$payload'] = payload_schema
    if reply_schema is not None:
      schemas['$reply'] = reply_schema
    types = type_generator(schemas, inline=True)

    parameters_type = types.identifiers.get('$parameters')
    payload_type = payload_ref_type if payload_ref is not None else types.identifiers.get('$payload')
    reply_type = reply_ref_type if reply_ref is not None else types.identifiers.get('$reply')

    channel_identifiers: dict[str, str] = {}
    direct_channel_imports: Imports = {}
    if direct_channel:
      assert parameters_schema is not None  # `_channel_direct_params` already requires it
      positional, keyword, parameters_docs, channel_identifiers, direct_channel_imports = (
        self._stream_direct_params(
          parameters_schema, type_generator=type_generator, identifiers=types.identifiers,
        )
      )
      parameters_decl_lines: list[str] = []
    else:
      positional, keyword, parameters_decl_lines, parameters_value_expr, parameters_docs = (
        self._stream_parameters_params(
          parameters_schema, type_generator=type_generator, parameters_type=parameters_type,
          identifiers=types.identifiers,
        )
      )

    validate_param = Function.Param(name='validate', type='bool', required=False)
    # `self.subscribe(...)` hands back a stream/manager object, not a single payload
    # reply to await (see the docstring above) -- so the method's own return
    # annotation has to be the manager `core.subscribe()` actually returns
    # (`truewire_core.util.StreamManager`), never the bare payload type alone. An earlier
    # version of this method annotated `-> {payload_type}` directly, which pyright
    # correctly rejects on `return self.subscribe(...)` once a real `core.subscribe()`
    # implementation returns a `StreamManager` (confirmed against `market/ticker_stream`
    # in the end-to-end smoke test -- no unit test here constructs a real core, so this
    # mismatch was invisible to isolated tests).
    # The manager's second argument is the subscribe reply's own type (ADR 0014), and stays
    # `Any` for a stream that declares no `reply`.
    subscribe_return_type = f'StreamManager[{payload_type or "Any"}, {reply_type or "Any"}, Any]'
    header = Function(
      name=method_name, asyn=False, method=True,
      args=positional, kwargs=[*keyword, validate_param],
      return_type=subscribe_return_type,
    )
    if (shadow := self_shadowing_alias(method_name, subscribe_return_type)) is not None:
      header.return_type, header.return_type_alias = shadow

    docstring = Docstring(
      description=spec.description,
      docs_url=endpoint.docs,
      params=[
        *parameters_docs,
        Docstring.Param(
          name='validate', required=False,
          docstring=(
            "Override this call's pushed-payload validation; falls back to the "
            'client-level default when omitted.'
          ),
        ),
      ],
    )

    if not isinstance(endpoint.meta, dict):
      # Mirrors `rpc_endpoint`'s identical guard: `Endpoint._require_meta_for_new_shape`
      # only checks `meta is not None`, so a parameters/payload-shaped endpoint spec'd
      # `"meta": false` (or any other non-dict value) passes spec loading -- silently
      # coercing that to `{}` here would generate a method with zero meta-related kwargs,
      # indistinguishable from a genuinely public endpoint. Fail loud at generation time.
      raise ValueError(
        f'{spec.channel}: endpoint.meta must be a dict to generate stream_endpoint (design '
        f'§2 "meta is never omittable"); got {endpoint.meta!r}'
      )
    meta = endpoint.meta
    meta_declared = meta_schema is not None
    if not meta_declared and meta:
      # Mirrors `rpc_endpoint`'s identical guard -- see that method's own comment.
      raise ValueError(
        f'{spec.channel}: endpoint.meta declares {sorted(meta)}, but its resolved core '
        f'({core_config.base!r}) declares no `meta` schema in truewire.toml [cores.<name>] '
        '-- either declare a schema for this core, or empty this endpoint\'s meta to {}'
      )
    if connect_param_name is not None:
      # Connect-only push whose whole `channel` is one placeholder matching its one
      # required parameter (`_connect_channel_param`) -- `channel` and a wrapping
      # `Parameters` object are both redundant here (there's nothing to route on, and
      # nothing else the value could be), so the call passes that one already-generated
      # argument straight through instead of the general channel-template-plus-
      # `Parameters` shape below.
      connect_identifier = self._avoid_reserved_collision(self.identifier(connect_param_name))
      call_args = [connect_identifier]
    elif direct_channel:
      # Direct-channel shape (`_channel_direct_params`): the channel string is fully
      # resolved right here, from the method's own real local variables -- see
      # `channel_expr`'s own docstring for the default rendering and the one confirmed
      # per-project override (`pair`/`symbol` lowering).
      call_args = [self.channel_expr(spec.channel, channel_identifiers)]
    else:
      call_args = [repr(spec.channel), parameters_value_expr]
    if meta_declared:
      # `meta` is never splatted as bare kwargs, and never a generated `Meta(...)` call
      # either -- see `rpc_endpoint`'s identical plain-dict-literal construction.
      meta_fields = ', '.join(f'{key!r}: {value!r}' for key, value in meta.items())
      call_args.append(f'meta={{{meta_fields}}}')
    call_args.append('validate=validate')
    # `cast(type, ...)` around a bare `Literal[...]`/`Any` alias, decided by the plan
    # from the type tree (`RequestPlan.needs_cast`/`ResponsePlan.needs_cast`, see
    # `rpc_endpoint`'s identical guard). Skipped entirely for the connect-only
    # single-placeholder shape (`connect_param_name is not None`) and the direct-channel
    # shape (`direct_channel`) -- neither ever emits a `Parameters` wrapper or a
    # `request_type=` argument at all (see `connect_identifier`/`channel_expr` above), so
    # there's nothing here to cast; `parameters_type` is already `None` on both paths
    # (`$parameters` was never registered), but the explicit check keeps this guard
    # readable on its own. A `$ref`-resolved payload is always a real class and never
    # needs one.
    needs_cast = False
    cast_type = 'builtins.type' if any(param.name == 'type' for param in [*positional, *keyword]) else 'type'
    if connect_param_name is None and not direct_channel and parameters_type is not None:
      if endpoint_plan.request.needs_cast:
        call_args.append(f'request_type=cast({cast_type}, {parameters_type})')
        needs_cast = True
      else:
        call_args.append(f'request_type={parameters_type}')
    if payload_type is not None:
      # `payload_ref is None and ...` -- never needed for a `$ref`-resolved external
      # type, which is always a real class (see `rpc_endpoint`'s identical guard).
      if endpoint_plan.response.needs_cast:
        call_args.append(f'response_type=cast({cast_type}, {payload_type})')
        needs_cast = True
      else:
        call_args.append(f'response_type={payload_type}')
    if reply_type is not None:
      # The resolved core validates the subscribe acknowledgement through this type before
      # handing it back as `Stream.reply` (ADR 0014).
      if reply_ref is None and needs_type_cast(types.definitions.get('$reply')):
        call_args.append(f'reply_type=cast({cast_type}, {reply_type})')
        needs_cast = True
      else:
        call_args.append(f'reply_type={reply_type}')
    call_lines = group_lines(call_args, tab='  ', width=88)
    return_line = 'return self.subscribe(\n' + '\n'.join(call_lines) + '\n)'
    body = '\n'.join([*parameters_decl_lines, return_line])

    method_lines = [header.code()]
    doc_code = docstring.code()
    if doc_code:
      method_lines.append(indent(doc_code, '  '))
    method_lines.append(indent(body, '  '))
    method_code = '\n'.join(method_lines)

    class_doc = spec.description or f'`{method_name}`.'
    refusal, refusal_imports = self.refusal()
    class_code = '\n'.join([
      *refusal,
      f'class {class_name}({core_class}):',
      indent(f'"""{class_doc}"""', '  '),
      '',
      indent(method_code, '  '),
    ])

    merged_imports = merge_imports([
      dict(types.imports),
      dict(direct_channel_imports),
      dict(payload_ref_import),
      dict(reply_ref_import),
      {core_module: {core_class}, 'truewire_core.util': {'StreamManager'}, 'typing_extensions': {'Any'}},
      {'typing_extensions': {'cast'}} if needs_cast else {},
      {'builtins': set()} if needs_cast and cast_type != 'type' else {},
      refusal_imports,
    ])
    imports_code = ImportsRenderer(
      imports=merged_imports, pkg_name=core_module.split('.')[0],
    ).code()

    type_defs = [types.definitions[id] for id in types.generation_order if id in types.definitions]

    parts: list[str] = []
    if imports_code:
      parts.append(imports_code)
    if type_defs:
      parts.append('\n\n\n'.join(type_defs))
    if header.return_type_alias:
      parts.append(header.return_type_alias)
    parts.append(class_code)
    return '\n\n\n'.join(parts)

  def _grpc_proto_module(self, fqcn: str):
    """Import the generated proto submodule holding `fqcn`'s own last dotted segment.

    The client's proto package mirrors a declared FQCN's dotted package path
    one-to-one under `<package>.protos.<dotted-package>` (e.g.
    `<package>.protos.cosmos.bank.v1beta1`). `<package>` is the project's own declared
    package; its source directory is added to `sys.path` first so the import succeeds
    even for a project not otherwise installed (a synthetic test fixture, say).

    Args:
      fqcn: A fully-qualified proto type name (`service`, `request`, or `response` off
        `GrpcEndpointSpec`) -- only its package portion (everything before the final
        dot) is used here.
    """
    assert self.project is not None
    pkg_src = self.project.python_src
    if str(pkg_src) not in sys.path:
      sys.path.insert(0, str(pkg_src))
    package_name = self.project.package_name
    proto_package = '.'.join(fqcn.split('.')[:-1])
    return import_module(f'{package_name}.protos.{proto_package}')

  def _grpc_field_type(self, hint: object) -> tuple[str, bool, dict[str, set[str]]]:
    """Render one proto message field's live, resolved type hint to a bare type expression.

    Args:
      hint: One field's real type, as `typing_extensions.get_type_hints` resolved it --
        never the dataclass's raw, unresolved annotation string, which can hold a
        betterproto2-internal module alias no generated import could ever reach
        (confirmed against a real `cosmos.gov.v1.QueryProposalsRequest.pagination`, whose
        raw annotation reads `'__base__query__v1beta1__.PageRequest | None'`).

    Returns:
      The bare type expression -- no `| None`, added separately by `Function.Param`/
      `_grpc_zero_value`'s own callers when the field is optional -- whether `hint`
      itself is already a `T | None` union (a message-typed field, natively nullable in
      betterproto2), and the imports the bare expression needs.
    """
    if get_origin(hint) is UnionType:
      args = get_args(hint)
      non_none = [a for a in args if a is not type(None)]
      if len(args) != 2 or len(non_none) != 1:
        raise NotImplementedError(
          f'grpc_endpoint only supports a plain `T | None` union field type, got {hint!r}'
        )
      inner_expr, _, inner_imports = self._grpc_field_type(non_none[0])
      return inner_expr, True, inner_imports
    if get_origin(hint) is list:
      # A repeated proto3 scalar/message field -- betterproto2 renders it as a plain
      # `list[T]`, never `T | None` (an empty repeated field is `[]`, not `None`), so
      # this always reports `nullable=False`: a caller with nothing to send passes
      # `events=[]` explicitly, the same way a bare scalar with no `optional_scalars`
      # entry has to pass its own real value.
      inner_expr, _, inner_imports = self._grpc_field_type(get_args(hint)[0])
      return f'list[{inner_expr}]', False, inner_imports
    if isinstance(hint, type) and hint.__module__ == 'builtins':
      return hint.__name__, False, {}
    if isinstance(hint, type):
      return hint.__name__, False, {hint.__module__: {hint.__name__}}
    raise NotImplementedError(f'grpc_endpoint has no rendering rule for field type {hint!r}')

  def _grpc_zero_value(self, hint: object) -> str:
    """Render proto3's own wire zero-value expression for one scalar field's live type.

    Every proto3 scalar has a defined zero value a field silently takes when a caller
    never sets it -- this is the expression an `optional_scalars` field's constructor
    argument falls back to when the caller passes `None`, so an omitted filter reads on
    the wire exactly like one explicitly set to zero -- `None` itself is never valid there, since a scalar proto3 field cannot represent
    it. An enum's zero value is always its `0`-valued member (proto3 requires this), so
    `{EnumType}(0)` is correct without needing to know that member's own name.
    """
    if hint is str:
      return "''"
    if hint is bytes:
      return "b''"
    if hint is bool:
      return 'False'
    if hint is int:
      return '0'
    if hint is float:
      return '0.0'
    if isinstance(hint, type) and issubclass(hint, enum.Enum):
      return f'{hint.__name__}(0)'
    raise NotImplementedError(
      f'grpc_endpoint has no optional_scalars zero-value rule for field type {hint!r}'
    )

  def grpc_nested_fields(self, endpoint: Endpoint) -> Mapping[str, Mapping[str, str]] | None:
    """`nested_fields` for a gRPC endpoint's own declared `pagination` (see
    `flatten_nested_pagination`), or `None` when its cursor/size aren't nested inside one
    request-message parameter.

    A last-resort per-project override -- `grpc_endpoint` only ever consults this when
    `_grpc_introspect_nested_fields` itself returns `None` (no declared pagination, no
    nested outer parameter, or the outer field's own live type isn't a real dataclass this
    can introspect). That covers every real case so far: `common/lib` has no way to derive
    which field a gRPC service nests its pagination inside from nothing, but once a
    pagination declaration's own dotted cursor path names that field, the field's own real
    compiled type already states its shape, and nothing about that is a fact only a
    backend could know. This hook exists for whatever `_grpc_introspect_nested_fields`'s
    own documented scope doesn't reach -- not expected to be needed by a project using the
    ordinary one-level-of-nesting shape at all.
    """
    return None

  def _grpc_introspect_nested_fields(
    self, endpoint: Endpoint, request_class: type,
  ) -> Mapping[str, Mapping[str, str]] | None:
    """Resolve `grpc_nested_fields`'s own return shape automatically, from the endpoint's
    own already-live `request_class` -- so a per-project backend never has to hand-type a
    table of a nested pagination message's own field names and types. `grpc_endpoint`
    (this method's own call site) tries this first, falling back to
    `grpc_nested_fields`'s own per-project override only
    when this can't resolve.

    Mirrors the identical `dataclasses.fields`/`get_type_hints`/`_grpc_field_type`
    introspection `grpc_endpoint` already runs on `request_class` itself for its own
    top-level fields, one level deeper: once the pagination declaration's own dotted
    cursor/size path (`_nested_pagination_ref`) names which of `request_class`'s own
    fields is the nested outer parameter, this resolves *that* field's own real message
    type the same way, then introspects its fields in turn. No fact about any specific
    message (Cosmos SDK's `PageRequest` or anything else) is baked in here -- whatever the
    real compiled type actually says, for whatever API.

    Returns `None` (falls back to `grpc_nested_fields`, or no `nested_fields` at all)
    whenever this can't resolve: no declared pagination, no nested outer parameter, the
    outer field isn't found on `request_class`, or its own type isn't a real dataclass --
    exactly the boundary `flatten_nested_pagination`'s own docstring already commits this
    whole mechanism to ("scoped to one level of nesting: nothing in this codebase's
    pagination declarations needs more"). Never raises on an unresolvable shape; a
    genuinely broken declaration (a dotted path naming a field that isn't there at all)
    still surfaces as `flatten_nested_pagination`'s own `ValueError`, once neither this nor
    the backend's override could supply it.

    Args:
      endpoint: The endpoint whose pagination (if any) may declare a nested outer parameter.
      request_class: The endpoint's own already-resolved, live request message class.
    """
    pagination = endpoint.pagination
    if pagination is None:
      return None
    driver_field = self._NESTED_DRIVER_FIELD.get(pagination.strategy)
    if driver_field is None:
      return None
    driver = getattr(pagination, driver_field)
    cursor_ref = _nested_pagination_ref(driver.parameter)
    if cursor_ref is None:
      return None
    outer_name, _inner = cursor_ref
    outer_identifier = self.identifier(outer_name)
    hints = get_type_hints(request_class)
    outer_hint = hints.get(outer_identifier)
    if outer_hint is None:
      return None
    outer_type: object = outer_hint
    if get_origin(outer_hint) is UnionType:
      args = get_args(outer_hint)
      non_none = [a for a in args if a is not type(None)]
      if len(args) == 2 and len(non_none) == 1:
        outer_type = non_none[0]
    if not (isinstance(outer_type, type) and dataclasses.is_dataclass(outer_type)):
      return None
    nested_hints = get_type_hints(outer_type)
    fields: dict[str, str] = {}
    for f in dataclasses.fields(outer_type):
      hint = nested_hints[f.name]
      type_expr, nullable, _imports = self._grpc_field_type(hint)
      fields[f.name] = f'{type_expr} | None' if nullable else type_expr
    return {outer_identifier: fields}

  def _grpc_pagination_rows_type(
    self, response_class: type, *, rows: str,
  ) -> tuple[str, dict[str, set[str]]] | None:
    """Resolve a gRPC pagination's declared `done.rows` field to its rendered row type --
    the gRPC-dataclass analog of `paged_response_rows_type`'s JSON-Schema-based
    resolution, reading the response message's own live `get_type_hints` the same way
    `grpc_endpoint`'s own request-field resolution already does.

    Returns `None` when `rows` doesn't resolve to a real `list[...]` field on
    `response_class` -- never raises, matching `paged_response_rows_type`'s own "can't
    render this shape" contract, so `grpc_endpoint` can fall back to the plain
    `paged_method` iterator instead.

    Args:
      response_class: The endpoint's own live, resolved response dataclass.
      rows: `pagination.done.rows`'s own dotted field name -- gRPC responses are always a
        single flat dataclass, never an enclosing envelope, so (unlike
        `paged_response_rows_type`) this never needs to walk more than one attribute deep.
    """
    hints = get_type_hints(response_class)
    rows_hint = hints.get(rows)
    if get_origin(rows_hint) is not list:
      return None
    rows_type, _nullable, rows_imports = self._grpc_field_type(get_args(rows_hint)[0])
    return rows_type, rows_imports

  def grpc_endpoint(
    self,
    endpoint: Endpoint,
    references: Mapping[str, ExternalReference],
    *,
    class_name: str,
    method_name: str,
  ) -> str:
    """Generate Python code for a single gRPC unary call module -- the direct,
    mechanically-derived stub call, with no dispatch verb.

    Unlike `rpc_endpoint`/`stream_endpoint`, a grpc endpoint has no `openapi: Operation`
    to derive types from: `service`/`rpc`/`request`/`response`
    (plain FQCN strings on `GrpcEndpointSpec`) are resolved by actually importing and
    introspecting the client's own already-generated proto stub package
    (`_grpc_proto_module`), so the generated signature can never drift from what the
    stub really accepts. `GrpcEndpoint`/`wrap_exceptions` (`truewire_core.grpc`)
    are fully generic -- confirmed nothing Cosmos-specific remains in them -- so unlike
    `rpc_endpoint` this needs no `_resolve_core`/`truewire.toml` lookup at all: the base
    class is always `truewire_core.grpc.GrpcEndpoint`.

    `StubClass` is `service`'s last dotted segment plus `"Stub"`; `rpc_method` is
    `snake_case(rpc)`; `RequestType`/`ResponseType` are `request`'s/`response`'s own last
    dotted segment. The request message's own fields, in the live dataclass's declared
    order, become the generated method's own parameters, typed from
    `typing_extensions.get_type_hints` (`_grpc_field_type`) rather than the dataclass's
    raw annotation strings, for the reason `_grpc_field_type`'s own docstring gives.
    Positional-vs-keyword-only derivation reuses `_flat_request_kwargs` (shared across
    every call pattern) -- gRPC-specific code is only the field/type resolution itself.

    Two shapes of "optional" exist and are handled differently, matching the hand-written
    logic this replaces:

    - A message-typed field (`pagination: PageRequest | None`) is already nullable in
      its own live type -- no `optional_scalars` declaration needed -- and passes
      straight through unconditionally (`pagination=pagination`).
    - A scalar field (`str`/`bytes`/`int`/`float`/`bool`/enum) is never nullable in its
      own live type -- proto3 gives it a defined zero value instead -- so
      `optional_scalars` is how a spec declares that this particular scalar's
      absence is meaningful. Its generated parameter still defaults to `None`, but the
      constructor argument falls back to the type's own zero-value expression
      (`_grpc_zero_value`) when the caller passes `None` -- matching the
      `voter=voter if voter is not None else ''` shape exactly, so an omitted filter is
      wire-indistinguishable from one explicitly set to zero, never `None` itself (which
      a scalar proto3 field cannot represent).

    `endpoint.meta` is deliberately never inspected here, unlike `rpc_endpoint`/
    `stream_endpoint`'s own hard `isinstance(..., dict)` gate. Two reasons, not one:
    `meta` isn't yet required at spec-load time for a `GrpcEndpointSpec` endpoint at all
    (`_require_meta_for_new_shape` explicitly carves gRPC out), and even once populated
    there is no `self.request(...)`/`self.subscribe(...)` core-mediated surface for a grpc
    call to splat it into -- the call goes straight to the stub. Every real
    `GrpcEndpointSpec` so far is `{"public": true}` with no call-level auth concern at all
    (signing happens entirely client-side, before a call is ever made). A future project
    needing per-call metadata (a bearer token, say) would reuse the same kwargs-splat
    `rpc_endpoint`/`stream_endpoint` already have, not a new mechanism, but no real
    endpoint exercises it today.

    Args:
      endpoint: The endpoint whose module is about to be generated.
      references: Unused here -- gRPC types are never rendered by `TypeGenerator`/JSON
        Schema at all, only live proto classes. Kept for signature parity with
        `rpc_endpoint`/`stream_endpoint`, which `endpoint()`'s dispatch calls uniformly.
      class_name: Name chosen for the generated class.
      method_name: Name chosen for the generated method.
    """
    spec = endpoint.spec
    if not isinstance(spec, GrpcEndpointSpec):
      raise TypeError(f'grpc_endpoint called on a non-grpc endpoint (kind={spec.kind!r})')
    if self.project is None:
      raise NotImplementedError(
        'grpc_endpoint needs project (set by the CLI after loading the backend) to '
        "locate and introspect the client's own generated proto stubs"
      )
    if spec.streaming != 'unary':
      raise NotImplementedError(
        f'{spec.service}.{spec.rpc}: only unary gRPC calls generate today '
        f'(streaming={spec.streaming!r})'
      )

    stub_module = self._grpc_proto_module(spec.service)
    stub_class_name = spec.service.rsplit('.', 1)[-1] + 'Stub'
    stub_class = getattr(stub_module, stub_class_name, None)
    if stub_class is None:
      raise ValueError(f'{spec.service}: no {stub_class_name!r} in {stub_module.__name__}')
    rpc_method = snake_case(spec.rpc)
    if not hasattr(stub_class, rpc_method):
      raise ValueError(f'{spec.service}.{spec.rpc}: stub has no method {rpc_method!r}')

    request_module = self._grpc_proto_module(spec.request)
    request_class_name = spec.request.rsplit('.', 1)[-1]
    request_class = getattr(request_module, request_class_name, None)
    if request_class is None:
      raise ValueError(f'{spec.request}: no such class in {request_module.__name__}')

    response_module = self._grpc_proto_module(spec.response)
    response_class_name = spec.response.rsplit('.', 1)[-1]
    if not hasattr(response_module, response_class_name):
      raise ValueError(f'{spec.response}: no such class in {response_module.__name__}')

    imports: dict[str, set[str]] = merge_imports([
      {'truewire_core.grpc': {'GrpcEndpoint', 'wrap_exceptions'}},
      {stub_module.__name__: {stub_class_name}},
      {request_module.__name__: {request_class_name}},
      {response_module.__name__: {response_class_name}},
    ])

    hints = get_type_hints(request_class)
    field_params: list[Function.Param] = []
    call_parts: list[str] = []
    field_names: set[str] = set()
    for f in dataclasses.fields(request_class):
      field_names.add(f.name)
      hint = hints[f.name]
      type_expr, nullable, field_imports = self._grpc_field_type(hint)
      imports = merge_imports([imports, field_imports])
      if nullable:
        field_params.append(Function.Param(name=f.name, type=type_expr, required=False))
        call_parts.append(f'{f.name}={f.name}')
      elif f.name in spec.optional_scalars:
        zero = self._grpc_zero_value(hint)
        field_params.append(Function.Param(name=f.name, type=type_expr, required=False))
        call_parts.append(f'{f.name}=({f.name} if {f.name} is not None else {zero})')
      else:
        field_params.append(Function.Param(name=f.name, type=type_expr, required=True))
        call_parts.append(f'{f.name}={f.name}')

    unknown_optional = set(spec.optional_scalars) - field_names
    if unknown_optional:
      raise ValueError(
        f'{spec.request}: optional_scalars names field(s) not on the real message: '
        f'{sorted(unknown_optional)}'
      )

    positional, keyword = self._flat_request_kwargs(field_params) if field_params else ([], [])

    request_expr = f'{request_class_name}({", ".join(call_parts)})'
    return_line = f'return await {stub_class_name}(self.channel).{rpc_method}({request_expr})'

    header = Function(
      name=method_name, asyn=True, method=True,
      args=positional, kwargs=keyword,
      return_type=response_class_name,
      decorators=['@wrap_exceptions'],
    )
    docstring = Docstring(description=spec.description, docs_url=endpoint.docs)

    # A declared `pagination` gets the identical treatment `rpc_endpoint` already gives
    # one -- `paged_response_method` (a `PaginatedResponse`-shaped iterator, S24) whenever
    # `done.rows` resolves to a real `list[...]` field on the live response dataclass,
    # falling back to the plain-generator `paged_method` (via `response_accessor='attr'`,
    # documented on that method for exactly this gRPC case) for any other declared shape.
    # `nested_fields` is resolved automatically from `request_class`'s own live type
    # (`_grpc_introspect_nested_fields`) wherever the pagination's own dotted cursor/size
    # path names a nested outer parameter; `grpc_nested_fields`'s per-project override is
    # only a fallback for whatever that can't reach -- everything else here is the same
    # strategy-driven machinery every HTTP/WS endpoint's pagination already goes through.
    pagination = endpoint.pagination
    paged_source: str | None = None
    paged_imports: Imports = {}
    if pagination is not None:
      nested_fields = (
        self._grpc_introspect_nested_fields(endpoint, request_class)
        or self.grpc_nested_fields(endpoint)
      )
      rows_path = self.pagination_rows_path(pagination)
      rows_resolved = (
        self._grpc_pagination_rows_type(
          getattr(response_module, response_class_name), rows=rows_path,
        )
        if rows_path else None
      )
      if rows_resolved is None:
        raise ValueError(
          f'{endpoint.function}: cannot resolve the row type of its `pagination` '
          f'(rows {rows_path!r}) on {response_class_name}; every paged method is '
          f'`PaginatedResponse`-shaped (ADR 0013) and has to know which field the rows are'
        )
      rows_type, rows_type_imports = rows_resolved
      state_type = (
        self.paged_state_type(header, pagination, nested_fields)
        if pagination.strategy == 'token' else None
      )
      paged_source = self.paged_response_method(
        endpoint, method_name=method_name, header=header,
        rows_type=rows_type, state_type=state_type,
        nested_fields=nested_fields, response_accessor='attr', docstring=docstring,
      )
      paged_imports = merge_imports([self.paged_imports(endpoint, header=header), rows_type_imports])

    method_lines = [header.code()]
    doc_code = docstring.code()
    if doc_code:
      method_lines.append(indent(doc_code, '  '))
    method_lines.append(indent(return_line, '  '))
    method_code = '\n'.join(method_lines)

    # `paged_source` before `method_code` -- see `rpc_endpoint`'s identical ordering and
    # its own comment for the real class-body name-shadowing bug this avoids.
    refusal, refusal_imports = self.refusal()
    class_lines = [
      *refusal,
      f'class {class_name}(GrpcEndpoint):',
      indent(f'"""{spec.description}"""', '  '),
    ]
    if paged_source is not None:
      class_lines.append('')
      class_lines.append(indent(paged_source, '  '))
    class_lines.append('')
    class_lines.append(indent(method_code, '  '))
    class_code = '\n'.join(class_lines)

    imports_code = ImportsRenderer(
      imports=merge_imports([imports, paged_imports, refusal_imports]), pkg_name=self.project.package_name,
    ).code()

    parts: list[str] = []
    if imports_code:
      parts.append(imports_code)
    parts.append(class_code)
    return '\n\n\n'.join(parts)

  def _new_param_annotation(self, type_ref: str, imports: dict[str, set[str]]) -> str:
    """Render one declared `[python.cores.<name>].params` type -- `module.path:Name`
    (imported by name into this module) or a bare builtin such as `str` -- to the
    annotation the exposed parameter carries, recording the import in `imports`.

    Args:
      type_ref: The declared type, exactly as `truewire.toml` spells it.
      imports: This module's accumulated import table, mutated in place.
    """
    module, sep, name = type_ref.rpartition(':')
    if not sep:
      if not type_ref.isidentifier():
        raise ValueError(
          f'{type_ref!r}: a `params` type is `module.path:Name` or a bare builtin name'
        )
      return type_ref
    if not module or not name.isidentifier():
      raise ValueError(f'{type_ref!r}: a `params` type is `module.path:Name`')
    imports.setdefault(module, set()).add(name)
    return name

  def _router_cached_property(
    self, child: RouterChild, *, own_core: PythonCoreConfig | None, imports: dict[str, set[str]],
  ) -> str:
    """Render one subdirectory child as a `@cached_property` constructing it, or -- when
    the child's own resolved core declares `params` this composing class cannot supply
    from its own fields -- a real method exposing exactly those parameters.

    Everything here is read from `truewire.toml` (ADR 0011); the child's base is never
    imported. A child whose core declares neither `forward` nor `params` is built as
    `Child(client=self.<field>)`, `<field>` being `client` unless `own_core.children`
    names another field for this child. A child whose core declares either is built
    through `Child.new(self.<field>, ...)`: every `forward` name passes the composing
    class's own same-named field, and every `params` name is exposed as a keyword-only
    parameter -- unless `own_core` is that same core, in which case this class already
    carries the field (its base is the child's base) and forwards `self.<name>` instead.
    That last rule is what lets a parameterized subtree (`token(network=...)` at the root)
    compose its own descendants without re-exposing `network` at every level.

    Args:
      child: A `kind: 'router'` child (a subdirectory grouping).
      own_core: This composing class's own resolved `PythonCoreConfig`, if any (unset for
        an aggregate parent, which reaches `self.client` through its leaf bases).
      imports: This module's accumulated import table -- mutated in place with whatever
        an exposed parameter's declared type needs.
    """
    attr = self.identifier(child['attr_name'])
    child_class = child['class_name']
    forwarded_field = 'client'
    if own_core is not None and own_core.children is not None:
      forwarded_field = own_core.children.get(child['attr_name'], 'client')
    docstring = indent(router_docstring(attr, child.get('doc')), '  ')

    child_core: PythonCoreConfig | None = None
    child_spec_dir = child.get('spec_dir')
    if child_spec_dir is not None and self.project is not None and self.codegen_config is not None:
      child_core = self._resolve_core(child_spec_dir, self.project.spec_dir, self.codegen_config)
    if child_core is None or not child_core.composes_via_new:
      return '\n'.join([
        '@cached_property',
        f'def {attr}(self) -> {child_class}:',
        docstring,
        indent(f'return {child_class}(client=self.{forwarded_field})', '  '),
      ])

    call_parts = [f'self.{forwarded_field}']
    for name in child_core.forward or ():
      call_parts.append(f'{name}=self.{name}')
    same_core = own_core is not None and own_core.base == child_core.base
    extra_params: list[Function.Param] = []
    for name, param in child_core.new_params.items():
      if same_core:
        call_parts.append(f'{name}=self.{name}')
        continue
      extra_params.append(Function.Param(
        name=name, type=self._new_param_annotation(param.type, imports), required=param.required,
      ))
      call_parts.append(f'{name}={name}')
    call_args = ', '.join(call_parts)

    if not extra_params:
      return '\n'.join([
        '@cached_property',
        f'def {attr}(self) -> {child_class}:',
        docstring,
        indent(f'return {child_class}.new({call_args})', '  '),
      ])

    header = Function(
      name=attr, asyn=False, method=True, kwargs=extra_params, return_type=child_class,
    )
    return '\n'.join([
      header.code(),
      docstring,
      indent(f'return {child_class}.new({call_args})', '  '),
    ])

  def _check_no_sibling_collision(
    self, section: str, children: Iterable[tuple[str, RouterChild]],
  ):
    """
    Raise if two sibling children -- whether both leaves, both subdirectories, or one of
    each -- resolve to the same generated identifier (`self.identifier` collapses two
    differently-spelled directory names to one Python name). Shared by `router` at any
    depth including the client root (the separate `_root_class` method this used to
    also be shared with is retired), since composing siblings the identical
    directory-driven way always needs the identical guard: a leaf collision would let
    alphabetical base order silently decide which one's method wins, and a subdirectory
    collision would silently redefine or shadow a class member, in either case rather
    than raising an import-time `NameError`/`TypeError` the way a genuine duplicate
    normally would.

    Args:
      section: The router section name, or directory, named in the raised message.
      children: Every direct child being composed, as `(name, child)` pairs.

    Raises:
      ValueError: Two children resolve to the same generated identifier.
    """
    seen: dict[str, str] = {}
    for name, child in children:
      identifier = self.identifier(child['attr_name'])
      if identifier in seen:
        raise ValueError(
          f'{section}: sibling children {seen[identifier]!r} and {name!r} both resolve '
          f'to the generated identifier {identifier!r} -- rename one in its spec (design '
          '§3: a sibling identifier collision must be a loud error, never a silent, '
          'base-order-dependent MRO surprise)'
        )
      seen[identifier] = name

  def _check_aggregate_core_consistency(
    self, section: str, leaves: list[tuple[str, RouterChild]],
  ):
    """
    Raise if two or more sibling leaves this router multiply-inherits (the
    aggregate/mixed shape) resolve to different cores (`_resolve_core`).

    Each leaf already subclasses its own resolved core in its own module -- that is
    exactly why an aggregate/mixed router needs no separate base of its own when it has
    leaf children (`router`'s own comment: "each leaf already subclasses a resolved core
    class in its own module, so `self.client` reaches the composed router transitively").
    That reasoning silently assumes every multiply-inherited leaf resolves to the *same*
    core. It doesn't have to: a `token_search` leaf served by one API host once sat as
    a sibling of four leaves served by another under one `SearchAndDiscovery(...)`
    aggregate. Every
    mechanical check passed -- `spec test`, `pytest`, `standards` -- because none of them
    construct the real composed class and inspect its MRO. C3 linearization pulled
    `SolanaEndpoint` (needed by the other four siblings) ahead of `TokenSearch`'s own
    `DeepIndexEndpoint` in the merged MRO, so `self._base_url()` on a real
    `SearchAndDiscovery` instance silently resolved through `SolanaEndpoint` -- a real
    wrong-host call working code compiled and every other check passed. This is the same
    "refuse ambiguity, don't paper over it" shape rule 16's own leaf-and-router refusal
    already takes (`docs/spec/authoring.md` rule 16) applied to a second, independent
    place codegen can silently do the wrong thing from directory shape alone: refuse the
    generation outright, rather than emit a class whose real behavior depends on Python's
    own MRO tie-breaking. The fix is always structural, never a code-level workaround: a
    leaf whose core genuinely differs from its multiply-inherited siblings needs a
    `router.json`-carrying ancestor of its own (a real subdirectory, so its parent
    composes it as a `cached_property` child instead of a base) -- moving that
    `token_search/` up to be a direct child of the product directory, not nested
    alongside its now-homogeneous siblings, is the worked fix.

    Only checked when this position's own `spec/endpoints/` directory is resolvable
    (`_router_endpoint_dir`) and fewer than two leaves are present is a no-op -- a
    `Generator` built outside the CLI loop (a unit test with hand-built `RouterChild`
    dicts and no real directory tree, or a router composing zero/one leaf) has nothing
    to compare and is silently exempt, matching every other `_resolve_core`-based check
    in this class.

    Args:
      section: This router's own section name, exactly as `router()` received it.
      leaves: This router's own leaf children (`kind == 'endpoint'`), name-sorted.

    Raises:
      ValueError: Two or more leaves resolve to different cores.
    """
    if len(leaves) < 2 or self.project is None or self.codegen_config is None:
      return
    endpoint_dir = self._router_endpoint_dir(section)
    if endpoint_dir is None:
      return
    spec_root = self.project.spec_dir
    resolved = {
      name: self._resolve_core(endpoint_dir / name, spec_root, self.codegen_config).base
      for name, _child in leaves
    }
    distinct = sorted(set(resolved.values()))
    if len(distinct) > 1:
      detail = ', '.join(f'{name}={base!r}' for name, base in sorted(resolved.items()))
      raise ValueError(
        f'{section}: this directory multiply-inherits {len(leaves)} leaves that resolve '
        f'to {len(distinct)} different cores ({detail}) -- C3 linearization does not '
        'guarantee the leaf whose core a caller expects actually wins a shared method '
        'like `_base_url()` on the composed class. Give the differently-'
        "cored leaf(ves) a router.json-carrying subdirectory of their own so this "
        'router composes them as a `cached_property` child instead of a multiply-'
        "inherited base -- never resolvable by reordering leaves, since C3's own "
        'algorithm, not declaration order, decides the merged MRO.'
      )

  def _router_endpoint_dir(self, section: str) -> Path | None:
    """Resolve `section`'s own `spec/endpoints/` directory -- the identical explicit-
    path-wins, `router_context`-otherwise resolution `router_doc`/`_router_base_class`
    both already use, factored out here so `router()` can also use it to recognize the
    client root (the root is just another position, resolved the identical way, never a
    separately-dispatched one).

    Args:
      section: This router's own section name, exactly as `router()` received it.
    """
    if self.project is None:
      return None
    if '/' in section:
      return self.project.spec_dir / 'endpoints' / section
    if self.router_context is not None:
      base, node = self.router_context
      return self.project.spec_dir / 'endpoints' / Path(base, *node)
    return None

  def _router_base_class(self, section: str) -> PythonCoreConfig:
    """Resolve the `PythonCoreConfig` a composite router -- one with no leaf children of
    its own -- subclasses to reach `self.client`, via the same `_resolve_core` mechanism
    `rpc_endpoint` already uses for a leaf. A leaf-carrying router (aggregate or mixed)
    never calls this: its multiply-inherited leaf bases already subclass a resolved core
    in their own module, so `self.client` reaches it transitively.

    Called identically whether `section` is the client's own root or an ordinary
    subtree -- the root's own base is resolved through this exact method,
    not a separate one.

    Args:
      section: This router's own section name, exactly as `router()` received it.

    Raises:
      ValueError: `project`/`codegen_config` aren't set (a `Generator` built outside
        the CLI loop), or `section` carries no explicit `/`-joined path and
        `router_context` (set by the CLI immediately before every `router()` call) is
        unset either -- there is no directory to resolve a core from.
    """
    if self.project is None or self.codegen_config is None:
      raise ValueError(
        f'{section}: router() needs project/codegen_config (set by the CLI after '
        "loading the backend) to resolve a composite router's base class"
      )
    endpoint_dir = self._router_endpoint_dir(section)
    if endpoint_dir is None:
      raise ValueError(
        f'{section}: router_context is unset and section carries no explicit `/`-joined '
        'path -- nothing to resolve a composite base class from'
      )
    return self._resolve_core(endpoint_dir, self.project.spec_dir, self.codegen_config)

  def router(self, section: str, children: Mapping[str, RouterChild]) -> str:
    """
    Generate Python package/router code for one function-tree grouping, called
    identically for every position in the tree -- an ordinary subtree, or the client's
    own root (`node == ()`) -- with no separate dispatch.

    A directory holding only leaves multiply-inherits their generated classes directly
    (aggregate) -- each leaf already subclasses a resolved core class in its own module,
    so `self.client` reaches the composed router transitively and no further base is
    needed. A directory holding only subdirectories (composite) has nothing to inherit
    `self.client` from, so it subclasses a resolved core itself (`_router_base_class`,
    the same `_resolve_core` mechanism `rpc_endpoint` uses for a leaf) and composes each
    subdirectory as a `@cached_property` (or, when its core's `.new()` needs a
    caller-supplied value, a real method) constructing the
    child. A directory holding a mix of *sibling* leaf children and subdirectory children
    (e.g. `market/order` a bare leaf alongside `market/history/` a further grouping) is
    the identical composite shape -- both compose on the same class, the leaves as bases,
    the subdirectories as `cached_property`s/methods -- since neither is this section's
    *own* endpoint, only one of its children.

    A directory that is itself *also* a leaf -- carries its own `endpoint.json` in
    addition to an endpoint-bearing descendant subdirectory (`docs/spec/authoring.md` rule
    16, `docs/production_standards.md` S30) -- is refused outright, not composed: a
    synthetic self-child could only ever render as `__call__` (S29-forbidden), since
    `Generator.method_name` returns `__call__` unless the leaf's parent is an *aggregate*
    node, and a self-referential node's own parent can never qualify (`aggregate_nodes`
    requires every child to be a bare leaf with no descendants of its own -- this section
    itself, having a further subdirectory, never is). Refusing the shape is simpler than
    solving that naming problem, and a spec in this shape already resolves it for free
    whenever its leaf's `function` is hand-authored one segment deeper than its physical
    directory, which is exactly the subdirectory it needs to physically move into.

    At the client root specifically (this section's own `spec/endpoints/` directory is
    the client's `endpoints_root` itself, and a `[python]` section is configured), the
    class takes its name from `truewire.toml`'s `config.python.name` (unmechanizable --
    real API names like `KuCoin`/`dYdX` defeat PascalCase-of-slug) instead of
    the derived `group_class_name` (the `class` a group's own `router.json` declares, else
    the PascalCase of `section`), and a bare leaf endpoint directly under the
    root is refused for a related but distinct reason: `router()`'s own composite branch
    never calls `_router_base_class` at all when `leaves` is non-empty, so a root with a
    bare leaf would silently multiply-inherit that leaf's own resolved core (an ordinary
    request/subscribe core) as the root's *only* effective base, losing whatever
    `[python.cores]` entry the root's own `router.json` actually declares (its `ClientBase`
    -- `.new()`/lifecycle) entirely rather than smuggling it in as a second one. This is
    the one root-specific rule: applied consistently, the general composite shape
    genuinely cannot express "subclass the root's
    own declared base *and* multiply-inherit a leaf's *different* one" without silently
    dropping one of the two -- so this stays refused, not because inheriting a second core
    is forbidden in general (an ordinary composite node never needed a resolved core of
    its own at all, so it never had two to begin with), but because the root specifically
    *does* need its own declared base and a bare leaf would silently cost it.

    Base order (leaves) and `cached_property`/method order (subdirectories) are both
    alphabetical by dict key, never insertion order -- the determinism requirement: regenerating twice is byte-identical, and a sibling collision becomes a
    loud error here rather than a silent, base-order-dependent MRO surprise.

    Args:
      section: This router's own section name, as `router_doc` reads it -- an explicit
        `/`-joined `spec/endpoints/` path, or (the ordinary case) a bare last-segment
        name resolved through `self.router_context`, set by the CLI immediately before
        this call.
      children: Every direct child this router composes, keyed by its own attribute name
        (`RouterChild.attr_name`, duplicated as the mapping key by the CLI).

    Raises:
      ValueError: This section is itself also a leaf (a `children` entry whose `attr_name`
        equals `section` -- the CLI's own convention for "this router node is itself also
        an endpoint"). Also raised when two children -- whether both leaves,
        both subdirectories, or one of each -- resolve to the same generated identifier
        (`self.identifier` collapses two differently-spelled directory names to one Python
        name): two leaves would then have alphabetical base order silently decide which
        one's method wins; a subdirectory colliding with anything would silently redefine
        or shadow a class member instead of raising an import-time `NameError`/`TypeError`
        the way a genuine duplicate normally would. Also raised for a bare leaf endpoint
        directly under the client root (see above).
    """
    leaves = sorted(
      (name, child) for name, child in children.items() if child['kind'] == 'endpoint'
    )
    subdirectories = sorted(
      (name, child) for name, child in children.items() if child['kind'] == 'router'
    )

    self_leaf = next(
      (child for _name, child in leaves if child['attr_name'] == section), None,
    )
    if self_leaf is not None:
      raise ValueError(
        f'{section}: this directory declares its own endpoint.json and also has an '
        'endpoint-bearing descendant subdirectory -- a directory is a leaf endpoint or a '
        'router grouping, never both (docs/spec/authoring.md rule 16). Move '
        f'{section}/endpoint.json (and its examples/) into a new subdirectory named after '
        "its own function's last segment, matching a real sibling directory name."
      )

    self._check_no_sibling_collision(section, [*leaves, *subdirectories])
    self._check_aggregate_core_consistency(section, leaves)

    endpoint_dir = self._router_endpoint_dir(section)
    config = self.codegen_config
    root_name: str | None = None
    if (
      endpoint_dir is not None and self.project is not None
      and endpoint_dir == self.project.spec_dir / 'endpoints'
      and config is not None and config.python is not None
    ):
      root_name = config.python.name or ''.join(
        part.capitalize() for part in (self.project.name if self.project else 'Client').split('_')
      )

    if root_name is not None and leaves:
      raise ValueError(
        f'{section}: a bare leaf endpoint directly under the client root cannot be '
        'composed onto the generated root class -- that leaf already subclasses a '
        "resolved core in its own module, and router()'s aggregate/mixed branch never "
        'also resolves a base when leaves are present, so the root would silently lose '
        "the base its own router.json actually declares. Restructure "
        'this endpoint under a real grouping directory.'
      )

    imports: dict[str, set[str]] = {}
    for _name, child in [*leaves, *subdirectories]:
      imports.setdefault(child['import_path'], set()).add(child['class_name'])

    own_core: PythonCoreConfig | None = None
    if leaves:
      base_names = [child['class_name'] for _name, child in leaves]
      pkg_name = None
      if subdirectories:
        # A node with sibling leaf children *and* subdirectory children (the
        # composite shape, never this section's own leaf -- that shape is refused above)
        # still composes the subdirectory children too -- and the `.new()` own-field
        # matching for those children (`_router_cached_property`'s `parent_fields`) needs
        # this node's actual resolved core, transitively inherited through its own leaf
        # bases, not a hardcoded `{'client'}` default. Resolved here (not used as an extra
        # base -- `base_names` above is already final) purely so a node under a
        # parameterized subtree (a `network`-style forwarded parameter) doesn't
        # silently re-expose an already-inherited forwarded parameter as a caller
        # parameter that can disagree with `self`'s own value. Never attempted for a pure
        # aggregate (no subdirectories, so `_router_cached_property` is never even called)
        # -- avoids a resolution attempt, and any error it could raise, for the common
        # case that doesn't need it.
        own_core = self._router_base_class(section)
    else:
      own_core = self._router_base_class(section)
      core_module, _, core_class = own_core.base.partition(':')
      base_names = [core_class]
      imports.setdefault(core_module, set()).add(core_class)
      pkg_name = core_module.split('.')[0]

    if subdirectories:
      imports.setdefault('functools', set()).add('cached_property')

    doc = self.router_doc(section)
    class_name = root_name if root_name is not None else group_class_name(doc, section)
    class_lines = [
      f'class {class_name}({", ".join(base_names)}):',
      indent(router_docstring(section, doc), '  '),
    ]
    if root_name is not None and self.project is not None:
      policy_lines = http_policy_lines(self.project)
      if policy_lines:
        imports.setdefault('typing_extensions', set()).add('ClassVar')
        class_lines += ['', *(indent(line, '  ') for line in policy_lines)]
    if subdirectories:
      cached_properties = '\n\n'.join(
        self._router_cached_property(child, own_core=own_core, imports=imports)
        for _name, child in subdirectories
      )
      class_lines.append('')
      class_lines.append(indent(cached_properties, '  '))
    class_code = '\n'.join(class_lines)

    imports_code = ImportsRenderer(
      imports={pkg: imports[pkg] for pkg in sorted(imports)}, pkg_name=pkg_name,
    ).code()

    parts: list[str] = []
    if imports_code:
      parts.append(imports_code)
    parts.append(class_code)
    return '\n\n\n'.join(parts)
