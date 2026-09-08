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
from truewire.generation.python.types.parser import TYPES_PACKAGE
from truewire.generation.python.code import Docstring, Function, HttpRequest, self_shadowing_alias
from truewire.generation.python.code.functions import group_lines
from truewire.generation.python.code.imports import Imports as ImportsRenderer
from truewire.generation.python.util import safe_identifier
from truewire.generation.types import ExternalReference, Imports, RenderedTypes, merge_imports
from truewire.generation.schema import Operation, Reference, ResolutionError, Schema, ensure_nonref
from truewire.generation.util import indent, snake_case

from truewire.codegen.layout import class_name as router_class_name
from truewire.plan.build import (
  PlanBuilder, channel_direct_params, connect_channel_param, direct_channel_scalar,
  driver_parameter,
)
from truewire.plan.model import EndpointPlan, PackagePlan
from truewire.plan.types import Type as PlanType
from truewire.project import Project
from truewire.spec import (
  Endpoint, GrpcEndpointSpec, Pagination, RouterDoc, RpcEndpointSpec, RpcEnvelopeSpec,
  StreamEndpointSpec, TokenPagination, WindowOverlap, WindowPagination, last_row_field,
  load_router, load_shared_schemas, path_segments, select_schema,
)
from truewire.spec.codegen_toml import CodegenConfig, PythonCoreConfig
from truewire.spec.request import PLACEHOLDER

PAGED_SUFFIX = '_paged'
"""Suffix separating a generated page iterator from the single-request method it drives."""
PAGED_CALL_NAME = 'paged'
"""Name a page iterator takes when the method it drives is its class's `__call__`."""
PAGED_IMPORTS: Mapping[str, set[str]] = {'typing_extensions': {'AsyncIterator'}}
"""Imports a generated module needs once it carries a page iterator."""
PAGED_TRUNCATION_IMPORTS: Mapping[str, set[str]] = {'truewire_core.exceptions': {'LogicError'}}
"""Imports a generated module needs once a window walk carries its truncation guard, or a
`seek` walk carries `pagination.overlap`'s order-stability/full-page raises."""
PAGED_TRUNCATION_PARAM = 'allow_truncation'
"""Keyword that lets a caller keep a window walk running past rows the API withheld."""
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
TIMESTAMP_TICKS: Mapping[str, str] = {
  'TimestampSeconds': 'seconds',
  'TimestampMillis': 'milliseconds',
  'TimestampMicros': 'microseconds',
}
"""Timestamp render id -> the `timedelta` keyword naming one wire tick of that unit.

`paged_overlap_seek`'s fallback cursor advance (`cursor += 1`) assumes an arithmetic
cursor, which does not hold for a `seek`+`overlap` cursor declared with a timestamp
`format` (rule 3/S7) -- there the generated cursor is a `datetime` and `+= 1` is not
arithmetic. Time-cursored endpoints (`funding_history`, `user_fills_by_time`) are the
real case this generalizes from. `TimestampNanos`/`TimestampIso`/`DateIso` have no fixed-duration `timedelta`
equivalent a generic single-tick advance could express, so they're not covered."""


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
  typing is one overload per outcome: `validate: Literal[False]` returns
  `raw_return_type` (`Any`, or `PaginatedResponse[Any, ...]`/`AsyncIterator[Any]` for a
  walker), and `validate: Literal[True] | None = None` -- the default, which leaves the
  decision to the client -- returns the declared type. The implementation keeps
  `validate: bool | None = None`, and the runtime is untouched.

  A third stub, `validate: bool | None = None` returning the declared type, catches a
  caller forwarding a `bool` variable -- the generated walkers do exactly that
  (`validate=validate`). Pyright expands a `bool` argument over `Literal[True]`/
  `Literal[False]` only within a budget it spends left to right, so a signature with a
  few `Literal[...] | None` parameters ahead of `validate` runs out before it gets there,
  and mypy never expands `bool` at all. Only a literal `False` at the call site is
  knowably raw; a flag decided elsewhere is the caller's own decision, like `None`.

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
    variant('Literal[True] | None', default='None', return_type=header.return_type),
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

  plan: PackagePlan | None = None
  """The whole package's plan (`truewire.plan.build.build_plan`), attached by the CLI once
  per run. `rpc_endpoint`/`stream_endpoint` read their endpoint's decisions from it --
  request fields, the returned type's nullability, whether a type is a bare alias, and
  every pagination fact -- instead of re-deriving them from rendered strings. `None` for a
  `Generator` built outside the CLI (a unit test, say): `endpoint_plan` then computes the
  one endpoint's plan on demand from `project`."""

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

  _cursor_render_id: str | None = None
  """Set for the duration of one `paged_overlap_seek` call, to the seek cursor's rendered
  timestamp type (`'TimestampMillis'`, ...) when it has one -- see `row_field_expression`'s
  own timestamp-normalizing branch below, and `TIMESTAMP_TICKS`."""

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
      if parameters_raw is not None or spec.payload is not None:
        schemas = {}
        if parameters_raw is not None:
          schemas['$parameters'] = Schema.model_validate(parameters_raw).model_copy(update={'title': None})
        if spec.payload is not None:
          schemas['$payload'] = Schema.model_validate(spec.payload)
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
    return driver_parameter(pagination)

  def pagination_driver_required(
    self, header: 'Function', pagination: Pagination,
  ) -> bool:
    """Whether a `token`/`seek` walk's cursor parameter is required on the
    single-request method (no server-side default), the way `paged_method` and
    `paged_response_seek`/`paged_response_method`'s own `token` branch each decide it
    independently -- factored out here so a dispatch site deciding whether an endpoint
    is `PaginatedResponse`-*seedable* (`rpc_endpoint`/`grpc_endpoint`'s own `seedable`
    gate) can ask the identical question before ever calling into either.

    `False` for every other strategy: `page`/`offset` seed from `pagination.index.start`
    or `0`, never ambiguous with "done" the way an absent `token`/`seek` cursor is, so
    neither needs this question asked at all. A `token` endpoint whose `end_timestamp`
    is required, and a `seek` endpoint whose `start_timestamp` is, are the real,
    motivating cases.

    Args:
      header: Rendered header of the single-request method the walk drives, *before* any
        nested-pagination flattening -- matching every real caller today, none of which
        declares a required nested cursor (`docs/pagination.md`/`flatten_nested_pagination`'s
        own scope note).
      pagination: Declaration carried by the endpoint.
    """
    if pagination.strategy not in ('token', 'seek'):
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
    not always a string: a `get_trade_history` `lastId` may be a genuine `integer`
    cursor, confirmed against a
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
    (wrapper-typed) parameter, which is the wrong type entirely for the inner field.
    Cosmos-SDK-style gRPC `token`/`absent_cursor` endpoints are the motivating case: `key`
    is a `bytes`
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
    """Return the expression that reduces a window bound to the number the API takes,
    and whether that expression is still a `datetime`.

    A window walk subtracts its bounds from each other, so a project whose timestamp
    parameters also accept a `datetime` overrides this to normalise them first —
    `datetime - int` is not arithmetic. The unit is passed because an API that bounds in
    seconds and one that bounds in milliseconds need different conversions.

    The second element of the return is not decoration: it is the one place a backend
    states what the loop variable ends up being, and `paged_step_expression` reads it
    from here rather than re-deriving it from `param.type`. A bound normalised to an
    integer here and a step still rendered as `timedelta(...)` is a `TypeError` at
    runtime — a real project hit exactly this — and the two cannot say different things because
    there is only one place either of them is said. **An override that normalises a
    `datetime` bound to a number must return `False`, not the declared type.**

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

  def paged_step_expression(
    self, step: int, *, unit: str, is_datetime: bool,
  ) -> str:
    """Render the amount a window walk adds to clear the edge it just covered.

    A window bounded by integers steps by a bare count of the API's own ticks. A window
    bounded by `datetime` steps by a `timedelta`, because `datetime + 1` is not arithmetic —
    which is why the declaration carries a unit at all.

    `is_datetime` is the second element `paged_window_bound` returned for this bound, not
    `param.type` — a backend that normalises the bound to an integer there has already
    made this decision, and asking the declared type again here is exactly how the two
    fell out of sync before.

    Args:
      step: Ticks between one window's edge and the next window's.
      unit: Unit the declaration states the bounds in.
      is_datetime: Whether the bound `paged_window_bound` produced is still a `datetime`.
    """
    if not is_datetime:
      return str(step)
    return f'timedelta({self._TIMEDELTA_UNITS[unit]}={step})'

  def paged_window(
    self, pagination: WindowPagination, *,
    method_name: str, parameters: list['Function.Param'], taken: Container[str],
  ) -> tuple[list[str], list[str], dict[str, str]]:
    """Emit the setup and advance of a time-window walk, and the bounds it rebinds.

    The walk is arithmetic on the request bounds and nothing else: it keeps the width the
    caller's own window states and moves that window along, one step past the edge it just
    covered. The step is declared rather than assumed to be one, because an API whose far
    bound is inclusive re-reads the boundary row unless the next window clears it, and an
    API whose far bound is exclusive would skip a row if it did.

    Advancing never moves past the bound the caller originally gave: the caller's own far
    bound (`end` ascending, `start` descending) is captured once, before the loop ever
    reassigns anything, and every advance checks the freshly-moved window against it --
    stopping (no further request made) rather than walking on the way the loop used to,
    unconditionally, until the first empty page. A plain `>`/`<` would under-stop for a
    API whose far bound is exclusive (`step: 0`): the very next window's near edge then
    lands exactly *on* the original bound, a value the walk already knows is out of range
    (it was never returned, by that same exclusivity), not one step past it -- so the
    check is `>=`/`<=`, which is exactly as strict for an inclusive bound (`step: 1`, where
    the next edge is always one past, never equal) and correctly stops one step earlier for
    an exclusive one.

    The bounds are rebound to loop locals rather than reassigned in place, since a
    parameter typed `datetime | int` cannot hold the integer the arithmetic produces.

    Args:
      pagination: Declaration carried by the endpoint.
      method_name: Name of the single-request method the iterator drives.
      parameters: Header parameters of that method.
      taken: Names the generated signature already binds.

    Returns:
      Statements to run before the loop, statements that advance it, and the expression
      each bound parameter is passed as.

    Raises:
      ValueError: When a declared bound is not a parameter of the generated method.
    """
    names = {
      'start': self.identifier(pagination.bound.start),
      'end': self.identifier(pagination.bound.end),
    }
    bounds: dict[str, Function.Param] = {}
    for role, name in names.items():
      param = next((item for item in parameters if item.name == name), None)
      if param is None:
        raise ValueError(
          f'the window bound `{name}` is not a parameter of `{method_name}`, so the walk '
          f'has nothing to advance'
        )
      bounds[role] = param
    lower = self.paged_local('lower', taken)
    upper = self.paged_local('upper', taken)
    width = self.paged_local('width', taken)
    limit = self.paged_local('limit', taken)
    setup: list[str] = []
    optional = [name for role, name in names.items() if not self.paged_always_set(bounds[role])]
    if optional:
      message = (
        f'`{self.paged_name(method_name)}` walks a time window: pass both '
        f'`{names["start"]}` and `{names["end"]}`'
      )
      setup.append(f'if {" or ".join(f"{name} is None" for name in optional)}:')
      setup.append(f'  raise ValueError({message!r})')
    unit = pagination.step.unit
    lower_expr, lower_is_datetime = self.paged_window_bound(names['start'], unit=unit, param=bounds['start'])
    upper_expr, _ = self.paged_window_bound(names['end'], unit=unit, param=bounds['end'])
    setup.append(f'{lower} = {lower_expr}')
    setup.append(f'{upper} = {upper_expr}')
    setup.append(f'{width} = {upper} - {lower}')
    descending = pagination.order == 'descending'
    # Captured once, from the caller's own literal (post-normalisation) bounds, before the
    # loop ever reassigns `lower`/`upper` -- the one value every later advance is checked
    # against, never a since-advanced one.
    setup.append(f'{limit} = {lower if descending else upper}')
    step = pagination.step.size
    step_code = (
      self.paged_step_expression(step, unit=unit, is_datetime=lower_is_datetime) if step else None
    )
    if descending:
      advance = [
        f'{upper} = {lower}{f" - {step_code}" if step_code else ""}',
        f'{lower} = {upper} - {width}',
        f'if {upper} <= {limit}:',
        '  break',
      ]
    else:
      advance = [
        f'{lower} = {upper}{f" + {step_code}" if step_code else ""}',
        f'{upper} = {lower} + {width}',
        f'if {lower} >= {limit}:',
        '  break',
      ]
    return setup, advance, {names['start']: lower, names['end']: upper}

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
    """Return the row cap the API applies when the caller sends no page size.

    Declared, not inferred, like everything else here: an API documenting a default page
    size states it as `default` on the size parameter's own schema. Prose does not reach
    the generator — one API writes "defaults to 200" in five descriptions and the walk that
    read only the parameter could not see any of them.

    A migrated endpoint carries this on `endpoint.request`'s own flat
    `properties[pagination.size.parameter]` rather than `endpoint.openapi.parameters` --
    mirrors every other dual-shape gate in this module (`_one_shape`'s own pattern).
    Without this, `paged_step` raised `ValueError` for every migrated
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

  def paged_cap(self, endpoint: Endpoint, size: 'Function.Param | None') -> str | None:
    """Return the row count a full page is full relative to, when the spec settles one.

    The cap is what the API would have returned had it more to give: the caller's own
    size where the method always sends one, and the API's declared default where the
    caller may omit it. `None` means the spec settles neither — the size is optional and
    no default is declared — and then no guard is generated at all. A guard reading
    `size is not None` would be silent on precisely the call that loses rows, the one that
    passes bounds and no size, so it would buy a promise the docstring could not keep. The
    fix for such an endpoint is to declare the default the API documents.

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

  def paged_imports(self, endpoint: Endpoint, *, header: Function) -> Mapping[str, set[str]]:
    """Return the imports the page iterator this endpoint generates needs.

    A window walk with a page size to measure against raises on truncation, so it needs
    the exception. A `page`/`offset` walk terminated by `total` always needs it too, now
    that a missing or disagreeing total raises (`docs/pagination.md` §4) rather than
    silently stopping. A window walk that steps by a `timedelta` needs the import for that
    too — the bound the step advances is the one that tells us, since both bounds share a
    representation. It asks `paged_window_bound` the same question `paged_step_expression`
    does, rather than re-deriving it from the parameter's declared type, so a backend that
    normalises a bound rendered as one of `TimestampSeconds`/`TimestampMillis`/
    `TimestampMicros`/`TimestampIso` to an integer does not end up with a dead `timedelta`
    import here while its step is a bare number. No other iterator reaches outside
    `typing_extensions`.

    Args:
      endpoint: The endpoint whose module is being generated.
      header: Rendered header of the single-request method the iterator drives.
    """
    pagination = endpoint.pagination
    if pagination is None:
      return {}
    if pagination.strategy == 'seek' and pagination.overlap is not None:
      imports: Mapping[str, set[str]] = {**PAGED_IMPORTS, **PAGED_TRUNCATION_IMPORTS}
      base = self._seek_cursor_base_type(endpoint, header=header)
      if base is None:
        return imports
      unit = TIMESTAMP_TICKS.get(base)
      if unit is None:
        return imports
      helper = HttpRequest.TIMESTAMP_HELPERS[base]
      # `datetime` (the bare class, not just `timedelta`) is needed alongside the helper:
      # `row_field_expression`'s own `isinstance(x, datetime)` discriminator (Fix 2,
      # `docs/pagination.md`) is emitted wherever this branch's own `unit is not None`
      # gate fires -- the identical condition, so it belongs in the same already-gated
      # `merge_imports` call rather than a separate one.
      return merge_imports([
        imports,
        {'datetime': {'timedelta', 'datetime'}},
        {TYPES_PACKAGE: {helper}},
        {'typing_extensions': {'cast'}},
      ])
    if pagination.strategy in ('page', 'offset') and pagination.done.kind == 'total':
      return {**PAGED_IMPORTS, **PAGED_TRUNCATION_IMPORTS}
    if pagination.strategy == 'window' and pagination.overlap is not None:
      # Always needs `LogicError` (both raises are unconditional -- no
      # `allow_truncation`-style opt-out the way a plain window's own guard has). The
      # `timedelta`/core-helper/`cast` question is the identical one the `seek`+`overlap`
      # branch above answers, just asked of the window's own `bound.start` instead of a
      # `seek` cursor.
      imports: Mapping[str, set[str]] = {**PAGED_IMPORTS, **PAGED_TRUNCATION_IMPORTS}
      start_name = self.identifier(pagination.bound.start)
      bound = next(
        (p for p in (*header.args, *header.kwargs) if p.name == start_name), None,
      )
      if bound is None:
        return imports
      _, is_datetime = self.paged_window_bound('', unit=pagination.step.unit, param=bound)
      if not is_datetime:
        return imports
      # `datetime` (the bare class), not just `timedelta`: `row_field_expression`'s own
      # `isinstance(x, datetime)` discriminator (Fix 2) is emitted wherever `is_datetime`
      # is true here, the identical condition -- so it belongs in the same place, same as
      # `timedelta` above it.
      imports = merge_imports([imports, {'datetime': {'timedelta', 'datetime'}}])
      base = (bound.type or 'str').removesuffix(' | None')
      helper = HttpRequest.TIMESTAMP_HELPERS.get(base)
      if helper is None:
        return imports
      return merge_imports([
        imports, {TYPES_PACKAGE: {helper}}, {'typing_extensions': {'cast'}},
      ])
    if pagination.strategy != 'window':
      return PAGED_IMPORTS
    imports = PAGED_IMPORTS
    size = self.paged_size(pagination, [*header.args, *header.kwargs])
    if self.paged_cap(endpoint, size) is not None:
      imports = {**imports, **PAGED_TRUNCATION_IMPORTS}
    if pagination.step.size:
      bound = next(
        (
          p for p in (*header.args, *header.kwargs)
          if p.name == self.identifier(pagination.bound.start)
        ),
        None,
      )
      if bound is not None:
        _, is_datetime = self.paged_window_bound('', unit=pagination.step.unit, param=bound)
        if is_datetime:
          imports = merge_imports([imports, {'datetime': {'timedelta'}}])
    return imports

  def paged_truncation(
    self, *, method_name: str, cap: str, rows: str, lower: str, upper: str,
  ) -> list[str]:
    """Emit the guard stopping a window walk from advancing past rows it never saw.

    An API caps a window holding more rows than its page size and says nothing about
    having done so; the walk then moves the window past the rows that were left out, and
    the caller reads a series with a hole in it. A full page is the evidence available:
    a response carrying as many rows as were asked for may have carried more. That is
    weaker than proof — a window whose rows happen to number exactly the page size trips
    it too — which is why it is refusable rather than fatal, and why it raises instead of
    warning. Losing rows silently is the outcome worth spending a false positive on.

    Args:
      method_name: Name of the single-request method the iterator drives.
      cap: Expression holding the row count a full page is full relative to.
      rows: Expression holding the page's rows.
      lower: Loop-local holding the window's lower bound.
      upper: Loop-local holding the window's upper bound.
    """
    message = (
      f'`{self.paged_name(method_name)}` requested the window {{{lower}}} to {{{upper}}} and the '
      f'API returned a full page of {{len({rows})}} rows, so it may hold more; advancing '
      f'would move past the rows that were left out. Narrow the window, or pass '
      f'`{PAGED_TRUNCATION_PARAM}=True` to accept the loss.'
    )
    return [
      f'if not {PAGED_TRUNCATION_PARAM} and len({rows}) >= {cap}:',
      f'  raise LogicError(f{message!r})',
    ]

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

  def row_field_expression(self, row: str, field: str) -> str:
    """Return an expression reading a dotted-key/bracket-index field off one row, tolerant
    of a missing key or an out-of-range index at any level -- the same tolerance
    `read_path` gives a top-level response path.

    A dict-key segment emits `.get(key)`; a bracket-index segment emits a bounds-checked
    `[index]`, since indexing a list out of range raises `IndexError` where a dict's
    `.get` merely returns `None` -- most `window`-strategy rows are positional tuples
    (a candle's timestamp at a fixed array index), which is exactly what needs this
    (`docs/pagination.md` §3), where `seek`'s own `LastRowPath` was always a named field.

    Normalizes the read value to a `datetime` when `_cursor_render_id` marks the
    enclosing `paged_overlap_seek`/`paged_window_overlap` call as driving a
    timestamp-typed cursor/field. That caller compares this expression's value against
    an already-`datetime` value (a cursor/bound seeded from the caller's own
    timestamp-typed argument, or reassigned only from values that already passed through
    this method). The value this expression reads, though, is a *response* field -- typed
    the same way, but only actually converted to a `datetime` when validation is on (S8);
    with validation off it is still the raw wire value a `TypedDict` annotation cannot
    itself enforce -- an `int` (epoch-formatted) or a `str` (candle rows whose wire
    timestamp is a JSON string, not a number). Comparing either directly
    against the already-`datetime` cursor then raises `TypeError` the moment a caller
    disables validation, which is exactly the trade-off `validate=False` is supposed to be
    safe to make. The discriminator is `not isinstance({expr}, datetime)` rather than
    `isinstance({expr}, int)` for exactly this reason: the latter falls through unparsed
    for a wire string, and `not isinstance(..., datetime)` still correctly skips the parse
    on the one case that's already converted (validation on) while catching both raw wire
    shapes (validation off) -- confirmed a real, previously-blocking bug on one project's
    7 candle endpoints (`docs/pagination.md`).

    Args:
      row: Expression holding one row of a `seek`/`window` walk's own row collection.
      field: A `LastRowPath`/`WindowOverlap.field`, its `[-1]` prefix already stripped
        (`last_row_field`) -- everything relative to one row.
    """
    expr = row
    for kind, key in path_segments(field):
      if kind == 'index':
        bound = f'len({expr}) > {key}' if key >= 0 else f'len({expr}) >= {-key}'
        expr = f'({expr}[{key}] if {expr} is not None and {bound} else None)'
      else:
        expr = f'({expr}.get({key!r}) if {expr} is not None else None)'
    if self._cursor_render_id is None:
      return expr
    helper = HttpRequest.TIMESTAMP_HELPERS[self._cursor_render_id]
    return f'({helper}.parse(cast(int, {expr})) if not isinstance({expr}, datetime) else {expr})'

  def read_last_row(
    self, path: str, *, rows: str, name: str, taken: Container[str], cast: str | None = 'str',
  ) -> list[str]:
    """Emit statements binding `name` to a field of the *last* row of `rows` that carries
    it, or `None` when no row does.

    Mirrors a hand-written `historical_trades_paged`: collect the field off every row,
    skip the rows that
    omit it, and take the last one collected -- a page whose very last row happens to omit
    the field still has a real cursor to advance from.

    Args:
      path: `last:<dotted-path>` a `SeekCursor.from_` declares.
      rows: Expression holding the walk's row collection.
      name: Name the cursor value is bound to.
      taken: Names the generated signature already binds.
      cast: Python type constructor the collected value is cast through -- `str` by
        default, matching the destination cursor parameter's own declared type
        (`int`/`float`) when it is one of those, so the walk's own local variable never
        disagrees with the type its own signature declares it as (a row field typed
        differently on the wire, e.g. a string `id` feeding an `int`-typed
        `lastId`, is exactly the case this exists for). `None` skips the cast entirely --
        the destination type's own converted rendering (`TimestampIso`, say) is what the
        row field already comes back as once read off an already-response-validated row,
        so wrapping it in `str(...)`/`int(...)` would corrupt a real `datetime` rather
        than normalize an untyped one; a `get_candles` (cursor `toISO`, read from
        `startedAt` on an already-typed `Candle` row) is the confirmed motivating case --
        `str(a_real_datetime)` doesn't round-trip back into `TimestampIso | None`.
    """
    field = last_row_field(path)
    item = self.paged_local('item', taken)
    value = self.paged_local(f'{name}_value', taken)
    values = self.paged_local(f'{name}_values', taken)
    expr = self.row_field_expression(item, field)
    last = f'{values}[-1]' if cast is None else f'{cast}({values}[-1])'
    return [
      # bound via `:=` and yielded as the bare name, not re-evaluated inline, so pyright
      # narrows the comprehension's own element type past `is not None` -- re-evaluating
      # `expr` itself in both the yield and the filter (the naive version) type-checks the
      # list as `X | None` regardless, since pyright cannot prove two syntactically
      # separate re-evaluations of the same expression agree -- which broke `int(...)`/
      # `float(...)` below (unlike `str`, neither constructor accepts `None`).
      f'{values} = [{value} for {item} in {rows} if ({value} := {expr}) is not None]',
      f'{name} = {last} if {values} else None',
    ]

  def paged_termination(
    self, pagination: Pagination, *,
    response: str, counter: str, driver: str, size: 'Function.Param | None',
    rows: str | None, rows_read: list[str], taken: Container[str],
    response_accessor: Literal['dict', 'attr'] = 'dict', driver_cast: str | None = 'str',
    size_default: int | None = None, method_name: str = '', total_seen: str | None = None,
  ) -> list[str]:
    """Emit the statements that end one turn of a paginated walk.

    `rows` is passed in rather than derived here because the advance needs the same
    expression: an `offset` walk steps by the rows it received, and a terminator and an
    advance disagreeing about where the rows are is a loop that walks wrong.

    Args:
      pagination: Declaration carried by the endpoint.
      response: Name holding the page just yielded.
      counter: Name holding the number of pages yielded so far.
      driver: Name of the loop variable the next request is made with.
      size: Header parameter carrying the page size, when one is declared.
      rows: Expression holding the page's rows, when the terminator measures them.
      rows_read: Statements binding `rows`, empty when the payload is itself the rows.
      taken: Names the generated signature already binds.
      response_accessor: How the response payload's fields are read -- see `read_path`.
      driver_cast: `seek` only -- see `read_last_row`'s own `cast` argument.
      size_default: The API's documented default for `size` (`paged_size_default`),
        when declared -- an item-counted `total` needs a real numeric size to convert
        the page count into an item count, and a caller who omits an optional `size`
        sends the API's own default, not `None`. Without this, the walk had no way
        to resolve that call and (before this parameter existed) treated the omission
        itself as "done", silently truncating to page 1 -- exactly the call this
        exists to serve correctly instead.
      method_name: `page`/`offset`+`total` only -- name of the single-request method the
        iterator drives, quoted in the `LogicError` message a missing/disagreeing total
        raises (`docs/pagination.md` §4).
      total_seen: `page`/`offset`+`total` only -- loop-local, declared `None` once in
        `setup` (sibling to the counter/index locals, before the loop -- see
        `paged_method`'s own call site), holding the `total` a previous turn of this same
        walk saw. Compared against this turn's own `total` before trusting either: a
        missing or disagreeing value means continuing the walk's index arithmetic would
        splice rows fetched against two different `total`s, which is not a coherent
        result -- see `docs/pagination.md` §4's own reasoning.

    Raises:
      ValueError: When the declaration cannot decide the walk — a short page or an
        item-counted total with no page size on the method to measure against.
    """
    lines: list[str] = []
    if pagination.strategy == 'token':
      cursor = self.read_path(
        pagination.cursor.from_, subject=response, name=driver, accessor=response_accessor,
      )
      if pagination.done.kind == 'absent_cursor':
        return [*cursor, f'if not {driver}:', '  break']
      return [*rows_read, f'if not {rows}:', '  break', *cursor]
    if pagination.strategy == 'seek':
      assert rows is not None, (
        'a seek walk ends on `short_page`, `empty` or `unchanged`, which always read rows'
      )
      if pagination.done.kind == 'unchanged':
        # Neither `short_page` nor `empty` is safe when the cursor field isn't unique
        # enough per row for the API to ever serve a short or an empty page once the
        # walk is truly done (`UnchangedDone`'s own docstring) -- so this reads the new
        # cursor and compares it against the one the just-completed request used,
        # instead of measuring the page at all. `previous` is saved before `cursor`'s own
        # statements overwrite `driver` with the newly-read value; an empty page reads no
        # last row and leaves `driver` `None`, which can never equal a real previous
        # cursor, so that case falls out of the same comparison rather than needing its
        # own branch.
        previous = self.paged_local(f'{driver}_previous', taken)
        cursor = self.read_last_row(
          pagination.cursor.from_, rows=rows, name=driver, taken=taken, cast=driver_cast,
        )
        return [
          *rows_read,
          # Unlike `short_page`/`empty` (below), this branch has no `if not {rows}:
          # break` ahead of `read_last_row`'s own `for item in {rows}` comprehension --
          # `rows` still reads through `read_path`'s always-guarded `.get(...)`, so
          # `rows` is `list[X] | None`, not narrowed to `list[X]` by anything before
          # this point. `for item in None` is a real runtime `TypeError`, not just an
          # unnarrowed pyright type -- confirmed by generating this exact endpoint
          # before this line existed. Normalizing here (never mutates what's already
          # been yielded to the caller -- `response`/`rows` are termination-only
          # locals by this point) treats a response that omits the rows field the same
          # as a genuinely empty page, which `unchanged`'s own "empty pages included"
          # semantics already call for.
          f'{rows} = {rows} or []',
          f'{previous} = {driver}',
          *cursor,
          f'if {driver} is None or {driver} == {previous}:',
          '  break',
        ]
      cursor = self.read_last_row(
        pagination.cursor.from_, rows=rows, name=driver, taken=taken, cast=driver_cast,
      )
      exhausted = self.paged_rows_exhausted(rows=rows, size=size, done_kind=pagination.done.kind)
      return [*rows_read, exhausted, '  break', *cursor, f'if {driver} is None:', '  break']
    done = pagination.done
    if done.kind == 'total':
      assert total_seen is not None, (
        'a page/offset+total walk always declares its own total_seen local -- see '
        '`paged_method`\'s own call site'
      )
      total = self.paged_local('total', taken)
      lines.extend(self.read_path(
        done.path, subject=response, name=total, accessor=response_accessor,
      ))
      # An API can serialize a total as a numeral string rather than a bare number
      # (`total: NotRequired[str]`) -- coerced here,
      # once, rather than at each comparison site below, and guarded for `None` since a
      # short/absent page already means "no total to compare against."
      lines.append(f'{total} = int({total}) if {total} is not None else None')
      # `docs/pagination.md` §4: a page with no `total` at all, or one whose `total`
      # disagrees with an earlier page of this same walk, is a hard error rather than a
      # silent stop -- concatenating rows fetched against two different `total` values
      # isn't a coherent result, and page/offset's own index arithmetic can turn that
      # splice into a duplicated or skipped row around wherever the underlying data
      # shifted. Checked (and `total_seen` updated) *after* this turn's rows were already
      # yielded -- real data, valid on its own -- the same order the window truncation
      # guard already uses.
      message = (
        f'`{self.paged_name(method_name)}` needs a `total` on every page. The API '
        f'omitted it here, or reported a value ({{{total}}}) that disagrees with an '
        f'earlier page of this same walk ({{{total_seen}}}); retry the whole walk from '
        f'the start.'
      )
      lines.append(
        f'if {total} is None or ({total_seen} is not None and {total} != {total_seen}):'
      )
      lines.append(f'  raise LogicError(f{message!r})')
      lines.append(f'{total_seen} = {total}')
      if done.counts == 'pages':
        lines.append(f'if {counter} >= {total}:')
      else:
        if size is None:
          raise ValueError('a total counting items needs a page size on the method to convert it')
        if self.paged_always_set(size):
          lines.append(f'if {counter} * {size.name} >= {total}:')
        elif size_default is not None:
          resolved = f'({size.name} if {size.name} is not None else {size_default})'
          lines.append(f'if {counter} * {resolved} >= {total}:')
        else:
          # `size` is optional and the API documents no default -- an omitted
          # `size` can't be multiplied against a page count to test against the
          # total at all, and treating the omission itself as "done" (the bug this
          # replaces) silently truncated every such call to page 1. Only decide by
          # the arithmetic when the caller actually supplied a real size; a caller
          # who omits it keeps paging until the API's own terminator (a short or
          # empty page) would end it -- not modeled here, so this alone cannot
          # detect that end, but it never *falsely* claims one either.
          lines.append(f'if {size.name} is not None and {counter} * {size.name} >= {total}:')
      return [*lines, '  break']
    lines.extend(rows_read)
    lines.append(self.paged_rows_exhausted(rows=rows, size=size, done_kind=done.kind))
    return [*lines, '  break']

  def paged_rows_exhausted(
    self, *, rows: str | None, size: 'Function.Param | None', done_kind: str,
  ) -> str:
    """Return the condition line ending a `short_page`/`empty` walk, without the `break`.

    Shared between the generic `page`/`offset`/`window` terminator above and `seek`'s own
    below -- both end a walk on a short or empty page the same way, and only `seek` also
    has a cursor to read once the check says the walk continues.

    Args:
      rows: Expression holding the page's rows.
      size: Header parameter carrying the page size, when one is declared.
      done_kind: `pagination.done.kind`, `'empty'` or `'short_page'`.

    Raises:
      ValueError: A `short_page` terminator with no page size to measure against.
    """
    if done_kind == 'empty':
      return f'if not {rows}:'
    if size is None:
      raise ValueError('a short page is only short relative to a page size on the method')
    if self.paged_always_set(size):
      return f'if not {rows} or len({rows}) < {size.name}:'
    return f'if not {rows} or ({size.name} is not None and len({rows}) < {size.name}):'

  def paged_step(
    self, size: 'Function.Param | None', *, rows: str | None, default: int | None = None,
  ) -> str:
    """Return the expression an `offset` walk advances its offset by.

    Row count first, because it is what an offset walk means and it is right whether or not
    the caller asked for a page size. A size parameter the method leaves optional cannot be
    added to an integer when it is omitted, which is why this is not simply the size --
    unless the API documents what it defaults to, the same `default` a `window` walk's
    cap already falls back to (`paged_cap`); the two mirror each other because both are
    "what the API actually used when the caller left it unset".

    Args:
      size: Header parameter carrying the page size, when one is declared.
      rows: Expression holding the page's rows, when the terminator measures them.
      default: The API's documented default for `size`, when declared on its schema
        (`paged_size_default`). `None` when the API documents none.

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
    own use unconditionally indexed/iterated by `paged_overlap_seek`/`paged_window_overlap`,
    Fix 1) fails pyright for real: `reportOptionalSubscript`/`reportOptionalIterable`/
    `len()`-on-`Sized` errors, confirmed against a real generation failure (an
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

  def paged_docstring(
    self, pagination: Pagination, *,
    method_name: str, size: str | None, step: str | None, truncation: bool = False,
    docstring: 'Docstring | None' = None, outer: 'Function | None' = None,
    extra_params: 'list[Docstring.Param] | None' = None,
  ) -> str:
    """Describe the walk a generated iterator performs, and how it ends.

    The terminator is named rather than implied: a caller who has to know whether a walk
    trusts a total, a short page or an absent token is reading the API's docs, which is
    the reading the declaration exists to have done once.

    Args:
      pagination: Declaration carried by the endpoint.
      method_name: Name of the single-request method the iterator drives.
      size: Name of the page-size parameter, when one is declared.
      step: Expression an `offset` walk advances by, as `paged_step` chose it.
      truncation: Whether a truncation guard was generated. A window walk without one
        says so, rather than promising a raise the method does not carry.
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
    elif pagination.strategy == 'seek' and pagination.overlap is not None:
      field = last_row_field(pagination.cursor.from_)
      cap = pagination.overlap.cap
      walk = f"Passes the largest `{field}` seen so far back as `{pagination.cursor.parameter}`"
      ends = 'stops on the first empty page'
      note = (
        f'Rows already yielded for that `{field}` value are dropped by position from the '
        f'next page, so a value shared by more than one row is never duplicated or '
        f'skipped. Raises `LogicError` if the API\'s row order is not stable across '
        f'requests, or if a full page of `{cap}` rows shares one `{field}` value, since the '
        f'rest of it would then be unreachable.'
      )
    elif pagination.strategy == 'seek':
      field = last_row_field(pagination.cursor.from_)
      walk = (
        f"Passes the previous page's last row's `{field}` back as "
        f'`{pagination.cursor.parameter}`'
      )
      if pagination.done.kind == 'empty':
        ends = 'stops on the first empty page'
      elif pagination.done.kind == 'unchanged':
        ends = (
          f"stops once a page's own last row's `{field}` no longer differs from the "
          f'previous request, empty pages included'
        )
      else:
        ends = f'stops on the first page shorter than `{size}`'
    elif pagination.strategy == 'window' and pagination.overlap is not None:
      lower = self.identifier(pagination.bound.start)
      upper = self.identifier(pagination.bound.end)
      descending = pagination.order == 'descending'
      direction = 'backwards' if descending else 'forwards'
      far = lower if descending else upper
      field = last_row_field(pagination.overlap.field)
      walk = (
        f'Moves the `{lower}`–`{upper}` window {direction} in chunks, narrowing and '
        f'retrying a chunk that comes back full instead of stopping'
      )
      ends = f'stops once advancing would move past the caller\'s own `{far}`, or sooner on an empty window'
      note = (
        f'Raises `LogicError` if the API\'s row order is not stable across requests, '
        f'or if a full chunk shares one `{field}` value, since the rest of it would then '
        f'be unreachable. Never fetches outside the caller\'s own `{lower}`–`{upper}` '
        f'range.'
      )
    elif pagination.strategy == 'window':
      lower = self.identifier(pagination.bound.start)
      upper = self.identifier(pagination.bound.end)
      descending = pagination.order == 'descending'
      direction = 'backwards' if descending else 'forwards'
      far = lower if descending else upper
      walk = f'Moves the `{lower}`–`{upper}` window {direction} by its own width'
      ends = f'stops once advancing would move past the caller\'s own `{far}`, or sooner on an empty window'
      if not truncation:
        note = (
          f'Every request spans the width the caller\'s own `{lower}` and `{upper}` state, '
          f'so choose a window the API answers in one response: it caps a wider one, and '
          f'the walk moves past the rows that were left out.'
        )
      else:
        note = (
          f'Every request spans the width the caller\'s own `{lower}` and `{upper}` state, '
          f'so choose a window the API answers in one response — at most `{size}` rows. A '
          f'full page is evidence the window was capped, and raises `LogicError` rather '
          f'than walking past the rows that were left out; pass '
          f'`{PAGED_TRUNCATION_PARAM}=True` to accept the loss and keep going.'
        )
    else:
      if pagination.strategy == 'page':
        walk = f'Requests `{pagination.index.parameter}` from {pagination.index.start} upwards'
      else:
        walk = f'Advances `{pagination.offset.parameter}` by `{step}`'
      done = pagination.done
      if done.kind == 'total':
        unit = 'pages' if done.counts == 'pages' else 'items'
        ends = f'stops once it has covered the `{done.path}` {unit} the response reports'
        note = (
          f'Raises `LogicError` if a page omits `{done.path}`, or reports a value that '
          f'disagrees with an earlier page of the same walk -- retry the whole walk '
          f'from the start rather than trust a spliced result.'
        )
      elif done.kind == 'short_page':
        ends = f'stops on the first page shorter than `{size}`'
      else:
        ends = 'stops on the first empty page'
    return self.paged_summary(
      method_name, walk=walk, ends=ends, note=note,
      docstring=docstring, outer=outer, extra_params=extra_params,
    )

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
    it; `extra_params` (`max_pages`, `allow_truncation`) are appended for what the paged
    variant adds that the sibling never had. Falls back to the original bare text when
    either is omitted — a caller that has not threaded the sibling's `Docstring` through
    yet (a not-yet-updated per-project backend) keeps exactly today's rendering.

    Args:
      method_name: Name of the single-request method the iterator drives.
      walk: Prose naming the parameter the loop advances. Required unless `body` is given.
      ends: Prose naming what ends the loop. Required unless `body` is given.
      note: Prose stating what the caller has to get right, when the walk asks anything
        of them — a window walk covers the width they chose, and no other.
      body: A complete summary paragraph, verbatim, replacing the `walk`/`ends`-built one
        — the `PaginatedResponse`-shaped wrappers' own "awaitable or async-iterable" fact,
        which isn't a walk/terminator statement at all.
      docstring: The single-request sibling's own `Docstring`, when available.
      outer: The page wrapper's own real, rendered signature, when available.
      extra_params: `Args:` entries the paged variant adds beyond the sibling's own
        parameters (`max_pages`, `allow_truncation`).
    """
    if body is None:
      assert walk is not None and ends is not None
      body = f'{walk} and {ends}, or after `max_pages` pages when one is given.'
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
      *(['', *textwrap.wrap(note, width=PAGED_DOC_WIDTH)] if note else []),
      '"""',
    ])

  _NESTED_DRIVER_FIELD: Mapping[str, str] = {
    'page': 'index', 'offset': 'offset', 'token': 'cursor', 'seek': 'cursor',
  }
  """Pagination-model attribute holding the request-parameter-carrying sub-model
  (`PageIndex`/`PaginationParameter`/`Cursor`), per strategy -- every strategy but `page`
  names it the same as the strategy itself; `page`'s is `index`. `window` has no entry: it
  advances two independent top-level bounds, not one driver parameter, so there is nothing
  here for a nested-message shape to name in the first place."""

  def flatten_nested_pagination(
    self, pagination: Pagination, header: Function,
    nested_fields: Mapping[str, Mapping[str, str]] | None,
  ) -> tuple[
    Pagination, Function,
    tuple[str, str, str, str | None, Mapping[str, str]] | None,
  ]:
    """Rewrite a `page`/`offset`/`token`/`seek` pagination whose cursor/size are dotted
    paths into one nested request-message parameter into an equivalent flat declaration +
    synthetic header, so the rest of `paged_method`'s flat-parameter-name logic runs
    completely unchanged.

    A gRPC unary request that wraps Cosmos-SDK-style pagination in one message field
    (`pagination: PageRequest | None`, itself carrying `key`/`limit`/`reverse`) is the
    motivating case: `pagination.cursor.parameter` is declared `"pagination.key"`, which
    names no flat parameter of the single-request method at all -- `"pagination"` does,
    but `"pagination.key"` doesn't, and never can, since a message field is not a
    parameter. A REST API whose page-driven listing endpoints bundle `pageNo`/`pageSize`
    inside one POST body object rather than exposing them as flat query-role parameters
    hits the identical shape one level down
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
      to reconstruct at call time -- `paged_method` reads this after building the call
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
        # the same convention `token` walks already assume for a flat cursor. An API
        # that genuinely requires a starting value on every call (an `end_timestamp` with
        # no server-side default) has no nested-message shape to be declaring here at all.
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

  def _seek_cursor_base_type(self, endpoint: Endpoint, *, header: Function) -> str | None:
    """Return a `seek`+`overlap` cursor parameter's rendered type, `| None` stripped, or
    `None` when this isn't a `seek`+`overlap` endpoint or its cursor parameter can't be
    found on `header`.

    Args:
      endpoint: The endpoint whose module is being generated.
      header: Rendered header of the single-request method the walk drives.
    """
    pagination = endpoint.pagination
    if pagination is None or pagination.strategy != 'seek' or pagination.overlap is None:
      return None
    driver = self.identifier(pagination.cursor.parameter)
    parameters = [*header.args, *header.kwargs]
    driver_param = next((param for param in parameters if param.name == driver), None)
    if driver_param is None or driver_param.type is None:
      return None
    return driver_param.type.removesuffix(' | None')

  def paged_overlap_seek(
    self, endpoint: Endpoint, *,
    method_name: str, header: Function, response_type: str,
    docstring: 'Docstring | None' = None,
  ) -> str:
    """Generate the page iterator for a `seek` walk declaring `pagination.overlap`.

    Structurally mirrors the hand-written `paginate()` helper this generalizes: rows
    already yielded for the cursor value
    in play are dropped from the next page *by position*, verified as an exact-order prefix
    rather than assumed (a mismatch means the API's own stable-order guarantee broke
    mid-walk, and the walk raises instead of silently dropping or duplicating rows). The
    cursor advances to the *largest* field value collected off the page, not simply its last
    row's, since nothing guarantees the trailing row of a page is the largest one. A page
    that fills to `pagination.overlap.cap` while every row still shares the cursor value in
    play is the one case the walk cannot resolve -- the rest of that value may be
    unreachable -- and it raises there too, rather than guessing.

    The fallback cursor advance (`cursor += 1`) assumes an arithmetic cursor -- not true for
    a cursor declared with a timestamp `format` (rule 3/S7), where the generated cursor is a
    `datetime` and `+= 1` is not arithmetic. `TIMESTAMP_TICKS` covers that case (`cursor +=
    timedelta({unit}=1)`); see `row_field_expression`'s matching normalization, needed so a
    response-read value (only actually a `datetime` when validation is on, S8) compares
    correctly against the cursor regardless. This was originally a project-local backend
    override, hoisted here once that project migrated onto the universal `Generator`.

    This does not share `paged_method`'s single loop template below: that template always
    yields the raw response and only decides *whether to keep walking* afterwards, but an
    overlap walk has to compute the deduplicated slice *before* it can even decide whether
    there is anything fresh to yield. It is its own small template instead, built the same
    way `paged_window`/`paged_truncation` supply the pieces `paged_method` assembles for
    `window` -- only here the divergence from that assembly runs too deep to stay a set of
    pluggable pieces.

    `pagination.done.rows` is unwrapped via `paged_rows` (the same helper `paged_method`'s
    own plain generator and `paged_response_seek` already call) right after the
    single-request call returns, so every prefix-check/dedup/cap/value-extraction step
    below reads the row collection itself, not a wrapper the API put around it
    (`{category, symbol, list}`, `{dataList, hasMore}`) -- a pure no-op when
    `done.rows` is unset, since `paged_rows` then returns the response unchanged.

    Args:
      endpoint: The endpoint whose module is being generated.
      method_name: Name of the single-request method the iterator drives.
      header: Rendered header of that method, after any project-specific renaming.
      response_type: Return type of that method, which the iterator yields -- the caller
        (`paged_method`) already resolves this to the flattened `list[T]` row type when
        `done.rows` is declared, not the enveloped type.
      docstring: See `paged_method`.

    Raises:
      ValueError: When the declared cursor parameter is not one the method takes.
    """
    pagination = endpoint.pagination
    assert pagination is not None and pagination.strategy == 'seek' and pagination.overlap is not None
    parameters = [*header.args, *header.kwargs]
    driver = self.identifier(pagination.cursor.parameter)
    driver_param = next((param for param in parameters if param.name == driver), None)
    if driver_param is None:
      raise ValueError(
        f'the seek cursor `{driver}` is not a parameter of `{method_name}`, so the walk has '
        f'nothing to advance'
      )
    field = last_row_field(pagination.cursor.from_)
    positional = list(header.args)
    keyword = [param for param in header.kwargs if param.name != 'validate']
    validate = next((param for param in header.kwargs if param.name == 'validate'), None)
    taken = {
      'self', 'max_pages',
      *(param.name for param in positional),
      *(param.name for param in keyword),
      *({validate.name} if validate is not None else set()),
    }
    cursor = self.paged_local('cursor', taken)
    overlap = self.paged_local('overlap', taken)
    response = self.paged_local('response', taken)
    counter = self.paged_local('pages', taken)
    fresh = self.paged_local('fresh', taken)
    values = self.paged_local('values', taken)
    value = self.paged_local('value', taken)
    last = self.paged_local('last', taken)
    item = self.paged_local('item', taken)
    # Unwrap `pagination.done.rows` (`{category, symbol, list}`, `{dataList, hasMore}`)
    # into its own local -- `paged_rows` is a pure no-op (returns `response` unchanged,
    # no lines emitted) when `done.rows` is unset, which is the bare-array shape.
    taken = {*taken, cursor, overlap, response, counter, fresh, values, value, last, item}
    rows_read: list[str] = []
    rows = self.paged_rows(
      pagination.done.rows, response=response, taken=taken, lines=rows_read,
    )

    base = (driver_param.type or 'str').removesuffix(' | None')
    unit = TIMESTAMP_TICKS.get(base)
    call_parts = [cursor if param.name == driver else param.name for param in positional]
    call_parts += [
      f'{param.name}={cursor if param.name == driver else param.name}' for param in keyword
    ]
    if validate is not None:
      call_parts.append(f'{validate.name}={validate.name}')
    call = ', '.join(call_parts)

    paged = Function(
      name=self.paged_name(method_name), asyn=True, method=True,
      args=list(positional),
      kwargs=[*keyword, Function.Param(name='max_pages', type='int | None', default='None')],
      return_type=f'AsyncIterator[{response_type}]',
    )
    if validate is not None:
      paged.kwargs.append(validate)

    extra_params = [
      Docstring.Param(
        name='max_pages', required=False,
        docstring='Stop after this many pages, even if the walk is not done.',
      ),
    ]
    docstring_code = self.paged_docstring(
      pagination, method_name=method_name, size=None, step=None,
      docstring=docstring, outer=paged, extra_params=extra_params,
    )
    self._cursor_render_id = base if unit is not None else None
    try:
      item_expr = self.row_field_expression(item, field)
    finally:
      self._cursor_render_id = None
    cap = pagination.overlap.cap
    prefix_mismatch = (
      f'`{self.paged_name(method_name)}` requested from {{{cursor}}} and the API returned '
      f'a different prefix than the previous page ended with; row order was expected to be '
      f'stable across requests, so the walk stopped instead of dropping or duplicating rows.'
    )
    full_page = (
      f'`{self.paged_name(method_name)}` requested from {{{cursor}}} and the API returned '
      f'a full page of {{len({rows})}} rows, all sharing `{field}` {{{cursor}}}; the '
      f'rest of that value is unreachable and advancing would drop it.'
    )
    advance = f'{cursor} += 1' if unit is None else f'{cursor} += timedelta({unit}=1)'
    paged.overloads = validate_overloads(
      paged, raw_return_type='AsyncIterator[Any]', generator=True,
    )
    lines = [
      paged.code(),
      *(f'  {line}' if line else '' for line in docstring_code.splitlines()),
      f'  {cursor}: {base} = {driver}',
      f'  {overlap}: {response_type} = []',
      f'  {counter} = 0',
      '  while True:',
      f'    {response} = await self.{method_name}({call})',
      *(f'    {line}' for line in rows_read),
      f'    {counter} += 1',
      f'    if not {rows}:',
      '      break',
      f'    if {rows}[:len({overlap})] != {overlap}:',
      f'      raise LogicError(f{prefix_mismatch!r})',
      f'    {fresh} = {rows}[len({overlap}):]',
      f'    if {fresh}:',
      f'      yield {fresh}',
      f'    if max_pages is not None and {counter} >= max_pages:',
      '      break',
      f'    {values} = ['
      f'{value} for {item} in {rows} if ({value} := {item_expr}) is not None]',
      f'    {last} = max({values}) if {values} else None',
      f'    if {last} is not None and {last} > {cursor}:',
      f'      {cursor} = {last}',
      f'      {overlap} = [{item} for {item} in {rows} if {item_expr} == {last}]',
      f'    elif len({rows}) >= {cap}:',
      f'      raise LogicError(f{full_page!r})',
      '    else:',
      f'      {advance}',
      f'      {overlap} = []',
    ]
    return '\n'.join(lines)

  def paged_window_overlap_cap(
    self, endpoint: Endpoint, overlap: WindowOverlap, size: 'Function.Param | None',
  ) -> str:
    """Return the expression a full `window`+`overlap` chunk is measured against.

    `overlap.cap` wins when declared; otherwise this falls back to exactly the same
    resolution a plain `window` walk's own truncation guard already uses (`paged_cap`) --
    a caller-always-set `size`, or one with a declared default.

    Args:
      endpoint: The endpoint whose module is being generated.
      overlap: `pagination.overlap`.
      size: Header parameter carrying the page size, when one is declared.

    Raises:
      ValueError: Neither `overlap.cap` nor `size` resolves one -- `check_pagination`'s
        own mutual-exclusion validator normally catches this before generation is ever
        attempted, so reaching this is a sign the spec bypassed that check somehow.
    """
    if overlap.cap is not None:
      return str(overlap.cap)
    resolved = self.paged_cap(endpoint, size)
    if resolved is None:
      raise ValueError(
        f'{endpoint.function}: window+overlap declares no `size` default and no '
        f'`overlap.cap` -- nothing resolves the row cap a full chunk is measured against'
      )
    return resolved

  def paged_window_overlap(
    self, endpoint: Endpoint, *,
    method_name: str, header: Function, response_type: str,
    docstring: 'Docstring | None' = None,
  ) -> str:
    """Generate the page iterator for a `window` walk declaring `pagination.overlap`.

    `docs/pagination.md` §5's redesigned window walk: a real declared step (`Δt`,
    `pagination.overlap.chunk`) turns a wide caller range into genuine multi-request
    coverage instead of one all-or-nothing call, and a chunk that comes back full narrows
    and retries instead of raising unconditionally -- the same mechanism
    `paged_overlap_seek` already uses for `seek`, applied to a window's own per-chunk
    paging instead of a per-request cursor.

    Not directly reusable from either sibling: a window walk needs both a persistent
    "next chunk" transition (`paged_window`'s own bound-advance, but stepping by the
    declared `Δt` instead of the caller's whole range) *and* a narrow-and-retry decision
    per request (`paged_overlap_seek`'s own, but choosing the extreme value *toward* the
    walk's direction of travel rather than always the largest), neither of which either
    sibling needs alone -- so this is its own small template, reusing the pieces that do
    carry over verbatim: `paged_window_bound`/`paged_step_expression`'s own bound
    normalization and step rendering, `row_field_expression`'s §3-grammar row reader, and
    the max-value/prefix-mismatch idioms `paged_overlap_seek` established.

    `pagination.done.rows` is unwrapped via `paged_rows` (the same helper `paged_method`'s
    own plain generator and `paged_response_seek` already call) right after the
    single-request call returns, so the prefix-check/dedup/cap/value-extraction steps below
    read the row collection itself, not a wrapper the API put around it
    (`{category, symbol, list}`, `{dataList, hasMore}`, `{candles: [...]}`) -- a pure
    no-op when `done.rows` is unset (a bare-array shape),
    since `paged_rows` then returns the response unchanged.

    `Δt` (`pagination.overlap.chunk`) is optional: undeclared, it defaults to the
    caller's own `t1 - t0` (one chunk covering the whole requested range), degrading to a
    single chunk with narrow-and-retry instead of the unconditional raise a plain
    (non-`overlap`) `window` walk's `paged_truncation` still generates. Declared, it
    becomes a real, caller-overridable keyword on the generated method.

    Args:
      endpoint: The endpoint whose module is being generated.
      method_name: Name of the single-request method the iterator drives.
      header: Rendered header of that method, after any project-specific renaming.
      response_type: Return type of that method, which the iterator yields -- the caller
        (`paged_method`) already resolves this to the flattened `list[T]` row type when
        `done.rows` is declared, not the enveloped type.
      docstring: See `paged_method`.

    Raises:
      ValueError: When a declared bound is not a parameter of the generated method, or
        `paged_window_overlap_cap` cannot resolve a row cap.
    """
    pagination = endpoint.pagination
    assert (
      pagination is not None and pagination.strategy == 'window'
      and pagination.overlap is not None
    )
    overlap = pagination.overlap
    parameters = [*header.args, *header.kwargs]
    names = {
      'start': self.identifier(pagination.bound.start),
      'end': self.identifier(pagination.bound.end),
    }
    bounds: dict[str, Function.Param] = {}
    for role, name in names.items():
      param = next((item for item in parameters if item.name == name), None)
      if param is None:
        raise ValueError(
          f'the window bound `{name}` is not a parameter of `{method_name}`, so the walk '
          f'has nothing to advance'
        )
      bounds[role] = param
    size = self.paged_size(pagination, parameters)
    cap = self.paged_window_overlap_cap(endpoint, overlap, size)
    descending = pagination.order == 'descending'

    positional = list(header.args)
    keyword = [param for param in header.kwargs if param.name != 'validate']
    validate = next((param for param in header.kwargs if param.name == 'validate'), None)
    taken = {
      'self', 'max_pages',
      *(param.name for param in positional),
      *(param.name for param in keyword),
      *({validate.name} if validate is not None else set()),
    }

    chunk_param: Function.Param | None = None
    if overlap.chunk is not None:
      chunk_name = self.identifier(overlap.chunk.parameter)
      chunk_param = Function.Param(name=chunk_name, type='int', default=str(overlap.chunk.default))
      keyword = [*keyword, chunk_param]
      taken = {*taken, chunk_name}

    lower = self.paged_local('lower', taken)
    upper = self.paged_local('upper', taken)
    pos = self.paged_local('pos', taken)
    edge = self.paged_local('edge', taken)
    width = self.paged_local('width', taken)
    limit = self.paged_local('limit', taken)
    overlap_local = self.paged_local('overlap', taken)
    response = self.paged_local('response', taken)
    counter = self.paged_local('pages', taken)
    fresh = self.paged_local('fresh', taken)
    values = self.paged_local('values', taken)
    value = self.paged_local('value', taken)
    item = self.paged_local('item', taken)
    largest = self.paged_local('largest', taken)
    # Unwrap `pagination.done.rows` (see this method's own docstring above) -- a pure
    # no-op when `done.rows` is unset, since `paged_rows` then returns `response` unchanged.
    taken = {
      *taken, lower, upper, pos, edge, width, limit, overlap_local, response, counter,
      fresh, values, value, item, largest,
    }
    rows_read: list[str] = []
    rows = self.paged_rows(
      pagination.done.rows, response=response, taken=taken, lines=rows_read,
    )

    unit = pagination.step.unit
    lower_expr, lower_is_datetime = self.paged_window_bound(
      names['start'], unit=unit, param=bounds['start'],
    )
    upper_expr, _ = self.paged_window_bound(names['end'], unit=unit, param=bounds['end'])
    optional = [name for role, name in names.items() if not self.paged_always_set(bounds[role])]

    step = pagination.step.size
    step_code = (
      self.paged_step_expression(step, unit=unit, is_datetime=lower_is_datetime) if step else None
    )
    if chunk_param is not None:
      width_expr = (
        f'timedelta({self._TIMEDELTA_UNITS[unit]}={chunk_param.name})' if lower_is_datetime
        else chunk_param.name
      )
    else:
      width_expr = f'{upper} - {lower}'

    setup: list[str] = []
    if optional:
      message = (
        f'`{self.paged_name(method_name)}` walks a time window: pass both '
        f'`{names["start"]}` and `{names["end"]}`'
      )
      setup.append(f'if {" or ".join(f"{name} is None" for name in optional)}:')
      setup.append(f'  raise ValueError({message!r})')
    setup.append(f'{lower} = {lower_expr}')
    setup.append(f'{upper} = {upper_expr}')
    setup.append(f'{width} = {width_expr}')
    if descending:
      setup.append(f'{pos} = {upper}')
      setup.append(f'{limit} = {lower}')
      setup.append(f'{edge} = max({pos} - {width}, {limit})')
    else:
      setup.append(f'{pos} = {lower}')
      setup.append(f'{limit} = {upper}')
      setup.append(f'{edge} = min({pos} + {width}, {limit})')

    field = last_row_field(overlap.field)
    self._cursor_render_id = (
      (bounds['start'].type or 'str').removesuffix(' | None') if lower_is_datetime else None
    )
    try:
      item_expr = self.row_field_expression(item, field)
    finally:
      self._cursor_render_id = None

    req_start, req_end = (edge, pos) if descending else (pos, edge)
    call_bounds = {names['start']: req_start, names['end']: req_end}
    call_parts = [call_bounds.get(param.name, param.name) for param in positional]
    call_parts += [
      f'{param.name}={call_bounds.get(param.name, param.name)}'
      for param in keyword if chunk_param is None or param.name != chunk_param.name
    ]
    if validate is not None:
      call_parts.append(f'{validate.name}={validate.name}')
    call = ', '.join(call_parts)

    paged = Function(
      name=self.paged_name(method_name), asyn=True, method=True,
      args=list(positional),
      kwargs=[*keyword, Function.Param(name='max_pages', type='int | None', default='None')],
      return_type=f'AsyncIterator[{response_type}]',
    )
    if validate is not None:
      paged.kwargs.append(validate)

    extra_params = [
      Docstring.Param(
        name='max_pages', required=False,
        docstring='Stop after this many pages, even if the walk is not done.',
      ),
    ]
    if chunk_param is not None:
      extra_params.append(Docstring.Param(
        name=chunk_param.name, required=False,
        docstring=(
          f'Width of one chunk, in `{unit}` ticks. Defaults to `{overlap.chunk.default}`, '
          f'the API\'s own documented density.'
        ),
      ))
    docstring_code = self.paged_docstring(
      pagination, method_name=method_name, size=size.name if size is not None else None,
      step=None, docstring=docstring, outer=paged, extra_params=extra_params,
    )

    prefix_mismatch = (
      f'`{self.paged_name(method_name)}` requested {{{req_start}}}..{{{req_end}}} and the '
      f'API returned a different prefix than the previous chunk ended with; row order '
      f'was expected to be stable across requests, so the walk stopped instead of '
      f'dropping or duplicating rows.'
    )
    full_chunk = (
      f'`{self.paged_name(method_name)}` requested {{{req_start}}}..{{{req_end}}} and the '
      f'API returned a full chunk of {{len({rows})}} rows, all sharing one '
      f'`{field}` value; the rest of that value is unreachable and advancing would drop it.'
    )
    if descending:
      transition = [
        f'if {edge} <= {limit}:',
        '  break',
        f'{pos} = {edge}{f" - {step_code}" if step_code else ""}',
        f'{edge} = max({pos} - {width}, {limit})',
      ]
    else:
      transition = [
        f'if {edge} >= {limit}:',
        '  break',
        f'{pos} = {edge}{f" + {step_code}" if step_code else ""}',
        f'{edge} = min({pos} + {width}, {limit})',
      ]
    narrower = f'{largest} < {pos}' if descending else f'{largest} > {pos}'
    extremum = 'min' if descending else 'max'

    paged.overloads = validate_overloads(
      paged, raw_return_type='AsyncIterator[Any]', generator=True,
    )
    lines = [
      paged.code(),
      *(f'  {line}' if line else '' for line in docstring_code.splitlines()),
      *(f'  {line}' for line in setup),
      f'  {overlap_local}: {response_type} = []',
      f'  {counter} = 0',
      '  while True:',
      f'    {response} = await self.{method_name}({call})',
      *(f'    {line}' for line in rows_read),
      f'    {counter} += 1',
      f'    if {rows}[:len({overlap_local})] != {overlap_local}:',
      f'      raise LogicError(f{prefix_mismatch!r})',
      f'    {fresh} = {rows}[len({overlap_local}):]',
      f'    if {fresh}:',
      f'      yield {fresh}',
      f'    if max_pages is not None and {counter} >= max_pages:',
      '      break',
      f'    if len({rows}) < {cap}:',
      *(f'      {line}' for line in transition),
      f'      {overlap_local} = []',
      '    else:',
      f'      {values} = ['
      f'{value} for {item} in {rows} if ({value} := {item_expr}) is not None]',
      f'      {largest} = {extremum}({values}) if {values} else None',
      f'      if {largest} is not None and {narrower}:',
      f'        {pos} = {largest}',
      f'        {overlap_local} = [{item} for {item} in {rows} if {item_expr} == {largest}]',
      '      else:',
      f'        raise LogicError(f{full_chunk!r})',
    ]
    return '\n'.join(lines)

  def paged_method(
    self, endpoint: Endpoint, *,
    method_name: str, header: Function, response_type: str,
    nested_fields: Mapping[str, Mapping[str, str]] | None = None,
    response_accessor: Literal['dict', 'attr'] = 'dict',
    overlap_rows_type: str | None = None,
    docstring: 'Docstring | None' = None,
  ) -> str | None:
    """Generate the page iterator an endpoint's `pagination` declaration describes.

    The declaration is a spec fact and the loop it implies is a convention, so the loop is
    written once here rather than per project — a project can paginate eleven endpoints
    and generate no iterator at all. A backend passes the header it has already built, since
    parameter names, types and renames are its business; this reads only the declaration,
    and an endpoint that declares nothing gets nothing.

    `nested_fields` lets a backend whose pagination cursor/size are dotted paths into one
    nested request-message parameter reuse this same convention instead of inventing its
    own -- see `flatten_nested_pagination`, which this calls first.

    Args:
      endpoint: The endpoint whose module is being generated.
      method_name: Name of the single-request method the iterator drives.
      header: Rendered header of that method, after any project-specific renaming.
      response_type: Return type of that method, which the iterator yields.
      response_accessor: How the response payload's fields are read when the walk reads
        a cursor/total/rows path off it -- `'dict'` (`.get(key)`) for every JSON-shaped
        HTTP/WS response, the default every existing client relies on; `'attr'`
        (`getattr(x, key, None)`) for a real typed object, which is what a gRPC response
        actually is (a betterproto2 dataclass, never a dict).
      overlap_rows_type: Rendered element type of `pagination.done.rows`'s own field, for
        a `seek`/`window` walk that also declares `overlap` -- resolved by the caller the
        same way `paged_response_rows_type` resolves it for a `PaginatedResponse`-shaped
        wrapper (this method has no schema/`RenderedTypes` access of its own to do it).
        `paged_overlap_seek`/`paged_window_overlap` yield a flattened `list[T]` of rows,
        not the envelope `response_type` echoes -- when this is given, the dispatch below
        renders `AsyncIterator[list[{overlap_rows_type}]]` instead of blindly forwarding
        `response_type`. `None` (every caller before this parameter existed, and every
        endpoint whose payload already *is* its own row collection, `done.rows` unset) is
        a pure no-op: the dispatch falls back to echoing `response_type` exactly as before.

      docstring: The single-request sibling's own `Docstring` (built once in
        `rpc_endpoint`), reused for the `_paged` variant's own description/`Args:`/
        `References:` instead of the bare structural text — see `paged_summary`.

    Returns:
      Source for the `<method_name>_paged` method, or None when nothing is declared, or
      when the declared shape can't be rendered at all (see the `paged_step` call below).
    """
    pagination = endpoint.pagination
    if pagination is None:
      return None
    # A `seek`/`window`+`overlap` walk yields a flattened `list[T]` of rows, never the
    # enveloped `response_type` itself, once `pagination.done.rows` is declared -- see
    # `overlap_rows_type`'s own docstring above. Falls back to a blind echo of
    # `response_type` when the caller resolved nothing (unset, or the endpoint's payload
    # already *is* the row collection), which is every currently-shipped case.
    overlap_response_type = (
      f'list[{overlap_rows_type}]' if overlap_rows_type is not None else response_type
    )
    if pagination.strategy == 'seek' and pagination.overlap is not None:
      return self.paged_overlap_seek(
        endpoint, method_name=method_name, header=header,
        response_type=overlap_response_type, docstring=docstring,
      )
    if pagination.strategy == 'window' and pagination.overlap is not None:
      return self.paged_window_overlap(
        endpoint, method_name=method_name, header=header,
        response_type=overlap_response_type, docstring=docstring,
      )
    pagination, header, nested = self.flatten_nested_pagination(
      pagination, header, nested_fields,
    )
    parameters = [*header.args, *header.kwargs]
    size = self.paged_size(pagination, parameters)

    driver = ''
    driver_param: Function.Param | None = None
    if pagination.strategy != 'window':
      driver = self.identifier(self.pagination_driver(pagination))
      driver_param = next((param for param in parameters if param.name == driver), None)

    # A `token`/`seek` cursor is usually optional -- the first call omits it and the walk
    # seeds it from `None`. Some APIs require it on every call, including the first
    # (an `end_timestamp` with no server-side default),
    # and there the caller's own starting value has to survive onto the iterator's
    # signature instead of being discarded in favor of a hardcoded `None` the underlying
    # method would reject.
    driver_required = (
      (pagination.strategy == 'token' or pagination.strategy == 'seek')
      and driver_param is not None
      and driver_param.required
    )

    positional = [
      param for param in header.args if param.name != driver or driver_required
    ]
    keyword = [
      param
      for param in header.kwargs
      if (param.name != driver or driver_required) and param.name != 'validate'
    ]
    validate = next((param for param in header.kwargs if param.name == 'validate'), None)
    cap = self.paged_cap(endpoint, size) if pagination.strategy == 'window' else None
    guards_truncation = cap is not None
    taken = {
      'self', 'max_pages',
      *({PAGED_TRUNCATION_PARAM} if guards_truncation else set()),
      *(param.name for param in positional),
      *(param.name for param in keyword),
      *({validate.name} if validate is not None else set()),
    }
    response = self.paged_local('response', taken)
    counter = self.paged_local('pages', taken)

    done = pagination.done
    rows: str | None = None
    rows_read: list[str] = []
    if done.kind == 'short_page' or done.kind == 'empty' or done.kind == 'unchanged':
      rows = self.paged_rows(
        done.rows, response=response, taken=taken, lines=rows_read,
        response_accessor=response_accessor,
      )
    # `docs/pagination.md` §4: a `page`/`offset` walk terminated by `total` now raises
    # rather than silently stopping when a page omits it, or reports one that disagrees
    # with an earlier page of the same walk -- `total_seen` is the loop-local carrying
    # what an earlier turn saw, declared once here (before the loop starts), not inside
    # `paged_termination`'s own returned lines, which re-execute every turn.
    total_seen = (
      self.paged_local('total_seen', taken)
      if pagination.strategy in ('page', 'offset') and done.kind == 'total'
      else None
    )

    setup: list[str] = []
    advance: list[str] = []
    overrides: dict[str, str] = {}
    truncation: list[str] = []
    step = None
    driver_runtime = driver
    """The name actually read/written turn to turn -- `driver` itself, except a required
    `token` cursor's own freshly-typed local (see the `token` branch below)."""
    driver_cast: str | None = 'str'
    """`seek` only -- the constructor `read_last_row` casts the collected row-field value
    through, so the walk's own local variable never disagrees with the destination cursor
    parameter's declared type. Stays the wire-safe `str` default unless that parameter is
    itself declared `int`/`float` below, and becomes `None` (no cast at all) for anything
    else -- a rendered format type (`TimestampIso`, say) is what the row field already
    comes back as once read off an already-response-validated row, so casting through
    `str(...)`/`int(...)` would corrupt a real value rather than normalize an untyped
    one (a `get_candles`, cursor `toISO` read from `startedAt`, is the confirmed
    motivating case -- see `read_last_row`'s own `cast` docstring)."""
    if pagination.strategy == 'window':
      setup, advance, overrides = self.paged_window(
        pagination, method_name=method_name, parameters=parameters, taken=taken,
      )
      if cap is not None:
        assert rows is not None, 'a window walk ends on `empty`, which always reads rows'
        truncation = self.paged_truncation(
          method_name=method_name, cap=cap, rows=rows,
          lower=overrides[self.identifier(pagination.bound.start)],
          upper=overrides[self.identifier(pagination.bound.end)],
        )
    elif pagination.strategy == 'page':
      setup.append(f'{driver} = {pagination.index.start}')
      advance.append(f'{driver} += 1')
      if total_seen is not None:
        setup.append(f'{total_seen} = None')
    elif pagination.strategy == 'token' or pagination.strategy == 'seek':
      annotation = (driver_param.type or 'str') if driver_param is not None else 'str'
      base = annotation.removesuffix(' | None')
      if pagination.strategy == 'seek':
        driver_cast = base if base in ('str', 'int', 'float') else None
      if driver_required:
        # `driver` stays a required parameter above (see `driver_required`), typed to
        # match it exactly (bare `int`, say) -- reassigning it a nullable `continuation`
        # on every later turn would conflict with that narrower declared type
        # (`reportRedeclaration`), so the loop tracks its own, freshly-typed local
        # instead, seeded from the caller's starting value and threaded back into the
        # call via `overrides` exactly like a `window` walk's renamed bounds.
        driver_runtime = self.paged_local(f'{driver}_cursor', taken)
        setup.append(f'{driver_runtime}: {base} | None = {driver}')
        overrides[driver] = driver_runtime
      else:
        setup.append(f'{driver}: {base} | None = None')
    else:
      # `paged_step` raises `ValueError` for a real, structural pagination-shape
      # limitation -- an `offset` walk terminated by an item-counted `total` with no
      # rows to count and no size (declared or defaulted) to convert the count into a
      # step (`docs/spec/authoring.md` rule 8). Not a bug in this endpoint's own
      # otherwise-valid `pagination` declaration; every legacy hand-rolled backend
      # already had to swallow this identically (a `trades_history` endpoint is the
      # real, motivating case). Caught narrowly, around only this one call -- not the whole method
      # body below it -- so a real bug anywhere else in `paged_method` still surfaces as
      # a real exception instead of being silently swallowed into "no `_paged` variant".
      try:
        step = self.paged_step(size, rows=rows, default=self.paged_size_default(endpoint))
      except ValueError:
        return None
      setup.append(f'{driver} = 0')
      advance.append(f'{driver} += {step}')
      if total_seen is not None:
        setup.append(f'{total_seen} = None')
    termination = self.paged_termination(
      pagination,
      response=response, counter=counter, driver=driver_runtime, size=size,
      rows=rows, rows_read=rows_read, taken=taken, response_accessor=response_accessor,
      driver_cast=driver_cast, size_default=self.paged_size_default(endpoint),
      method_name=method_name, total_seen=total_seen,
    )

    paged = Function(
      name=self.paged_name(method_name), asyn=True, method=True,
      args=list(positional),
      kwargs=[*keyword, Function.Param(name='max_pages', type='int | None', default='None')],
      return_type=f'AsyncIterator[{response_type}]',
    )
    if guards_truncation:
      paged.kwargs.append(
        Function.Param(name=PAGED_TRUNCATION_PARAM, type='bool', default='False')
      )
    if validate is not None:
      paged.kwargs.append(validate)
    call = [overrides.get(param.name, param.name) for param in positional]
    call.extend(f'{param.name}={overrides.get(param.name, param.name)}' for param in keyword)
    if driver and not driver_required:
      call.append(f'{driver}={driver}')
    if validate is not None:
      call.append(f'{validate.name}={validate.name}')
    if nested is not None:
      # Merge the flattened driver/size call entries `flatten_nested_pagination` split
      # apart back into one constructed message argument -- whichever expression each
      # ended up as (a bare loop-local normally, `overrides.get(...)` for a required
      # `token` cursor's own renamed local), read straight off `call` rather than
      # re-derived, so the two can't disagree.
      outer_name, outer_type, inner_cursor, inner_size, fields = nested

      def merged_arg(inner: str) -> str | None:
        """Return `inner=<expr>` for the constructed message, coercing a loop-local
        that can be `None` to the field's own zero value when its real constructor
        does not accept `None` -- see `_SCALAR_ZERO_VALUES`."""
        flat = self.identifier(inner)
        entry = next((c for c in call if c.startswith(f'{flat}=')), None)
        if entry is None:
          return None
        expr = entry.split('=', 1)[1]
        field_type = fields[inner]
        if field_type.endswith(' | None'):
          return f'{inner}={expr}'
        bare = field_type.removesuffix(' | None')
        if bare not in _SCALAR_ZERO_VALUES:
          raise ValueError(
            f'nested pagination field {inner!r} of {outer_name!r} has type {field_type!r}, '
            f'which is not one of {sorted(_SCALAR_ZERO_VALUES)} -- no zero value to '
            f'substitute for the loop-local when it is None on the first call'
          )
        zero = _SCALAR_ZERO_VALUES[bare]
        return f'{inner}=({expr} if {expr} is not None else {zero})'

      inner_names = [inner_cursor, *([inner_size] if inner_size is not None else [])]
      construct_args = [arg for inner in inner_names if (arg := merged_arg(inner)) is not None]
      call = [
        c for c in call
        if not c.startswith(f'{self.identifier(inner_cursor)}=')
        and (inner_size is None or not c.startswith(f'{self.identifier(inner_size)}='))
      ]
      call.append(f'{outer_name}={outer_type}({", ".join(construct_args)})')

    extra_params = [
      Docstring.Param(
        name='max_pages', required=False,
        docstring='Stop after this many pages, even if the walk is not done.',
      ),
    ]
    if guards_truncation:
      extra_params.append(Docstring.Param(
        name=PAGED_TRUNCATION_PARAM, required=False,
        docstring=(
          'Accept a truncated page instead of raising `LogicError` when one is detected.'
        ),
      ))
    docstring_code = self.paged_docstring(
      pagination,
      method_name=method_name, size=size.name if size is not None else None, step=step,
      truncation=guards_truncation,
      docstring=docstring, outer=paged, extra_params=extra_params,
    )
    paged.overloads = validate_overloads(
      paged, raw_return_type='AsyncIterator[Any]', generator=True,
    )
    lines = [
      paged.code(),
      *(f'  {line}' if line else '' for line in docstring_code.splitlines()),
      *(f'  {line}' for line in setup),
      f'  {counter} = 0',
      '  while True:',
      f'    {response} = await self.{method_name}({", ".join(call)})',
      f'    yield {response}',
      f'    {counter} += 1',
      f'    if max_pages is not None and {counter} >= max_pages:',
      '      break',
      *(f'    {line}' for line in termination),
      *(f'    {line}' for line in truncation),
      *(f'    {line}' for line in advance),
    ]
    return '\n'.join(lines)

  def paged_response_rows_type(
    self, types: RenderedTypes, rows_path: str, *,
    response_ref: str | None = None, references: Mapping[str, ExternalReference] = {},
    imports: dict[str, set[str]] | None = None,
  ) -> str | None:
    """Resolve the rendered element type of a declared `pagination.done.rows` field, for
    a `PaginatedResponse`-shaped `_paged` wrapper's own `T` (ported from the equivalent
    per-project helper every hand-rolled backend needing this used to carry its own copy
    of).

    `Unnest` gives every titled-record schema nested inside `$response` a stable
    identifier keyed by its own path plus a trailing `item` step for an array's element
    schema (`$response/{dotted-path-with-'/'}/item`) -- tried first, and returned as-is
    when the row schema is a titled record (or `$ref`, resolved into `$response`'s own
    definition the identical way any other nested reference already is).

    Not every row schema is a titled record, though: a bare `additionalProperties: true`
    map (rule 1: "a map, not a record, needs no title") or a plain scalar array
    (`owners: list[str]`) gets no identifier from
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
    same way an ordinary flat request property already does. Found live on one project's
    `streams.*` endpoints: each response is a bare
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
    filter out). A `get_closed_orders` pair is the real, motivating case: both declare
    `pagination.done.rows:
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
      # the payload is itself the collection"). An `hf_ledgers` pair (`seek` strategy,
      # no declared `done.rows`) is the real, motivating case -- `Unnest` keys a bare top-level array's own item type
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
          if field_type.startswith('list[') and field_type.endswith(']'):
            return field_type[len('list[') : -1]
      return None

    direct = resolve_from('$response')
    if direct is not None:
      return direct

    # `$response` itself resolved nothing -- retry against every `$response/anyOf/{i}`
    # branch `Unnest` extracted its own record for (see this method's own docstring, the
    # `get_closed_orders` case). A plain scalar/`null` branch is never extracted
    # at all (`Unnest.unnest` requires an object schema with `properties`), so this never
    # tries, or wrongly matches against, a branch with no row field to find -- `sorted`
    # by branch index keeps resolution order deterministic and matching declaration order
    # for the (currently hypothetical) case of more than one real record branch.
    branch_root = re.compile(r'^\$response/anyOf/(\d+)$')
    branch_roots = sorted(
      (key for key in types.definitions if branch_root.match(key)),
      key=lambda key: int(branch_root.match(key).group(1)),  # type: ignore[union-attr]
    )
    for root in branch_roots:
      branched = resolve_from(root)
      if branched is not None:
        return branched

    # The bare-top-level-array shape (`not rows_path`) has no `segments` to walk against
    # `self.shared_schemas` below -- `resolve_from`'s own empty-`rows_path` branch is the
    # only resolution that shape gets, same as before this method grew a second root to
    # try. `response_ref` (the bare-`$ref`-response shape, this method's own
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

  def paged_response_method(
    self, endpoint: Endpoint, *,
    method_name: str, header: Function, rows_type: str, state_type: str | None = None,
    zero_value_is_wire_absent: bool = True,
    nested_fields: Mapping[str, Mapping[str, str]] | None = None,
    response_accessor: Literal['dict', 'attr'] = 'dict',
    response_optional: bool = False,
    docstring: 'Docstring | None' = None,
  ) -> str | None:
    """Generate a `truewire_core.util.paging.PaginatedResponse`-shaped page wrapper --
    awaitable (flattens every page into one list) *and* async-iterable (one page at a
    time) -- for a project whose own pre-existing pagination convention is this
    shape rather than the ordinary async-generator one `paged_method` produces.

    A project whose hand-written pagination already returns `PaginatedResponse`
    everywhere has callers that treat every `_paged` method as awaitable-or-iterable --
    generating the plain-async-generator shape instead would be a real, visible break to
    that existing public API. Scoped to `token` strategy with `absent_cursor`
    termination, `page` strategy with a `total` terminator, and plain `seek` strategy (no
    declared `overlap`) -- `offset`/`window`, and `seek` with `overlap`, still generate
    nothing here: `PaginatedResponse`'s own contract (`next: state -> (rows, next_state |
    None)`) maps directly onto a token walk's own state (the cursor) and terminator (an
    absent next cursor), onto a page walk's own state (the page index) and terminator (the
    declared total reached) -- see `paged_response_page_total`, this method's own dispatch
    target for `page` strategy -- and onto a seek walk's own state (the cursor, read off
    the previous page's last row rather than a declared response field) and terminator (a
    short or empty page) -- see `paged_response_seek`, the dispatch target for `seek`.

    Args:
      endpoint: The endpoint whose module is being generated.
      method_name: Name of the single-request method the wrapper drives.
      header: Rendered header of that method, after any project-specific renaming.
      rows_type: Rendered type of one row (`PaginatedResponse`'s own `T`) -- the element
        type of `pagination.done.rows`'s own field, not the collection type.
      state_type: Rendered (bare, no ` | None`) type of the cursor (`PaginatedResponse`'s
        own `S`) -- must be one of `_SCALAR_ZERO_VALUES`, since the wrapper's own seed
        value is that type's zero value, never `None` (a `None` state means "done" to
        `PaginatedResponse.__aiter__`, so seeding it would mean "no pages at all") --
        *unless* the cursor parameter itself is required on the single-request method
        (an `end_timestamp` with no server-side default), in which case there is no
        ambiguity to avoid seeding around
        in the first place, and the seed is the caller's own real argument instead (see
        `driver_required` below, mirroring `paged_method`'s identical case). Required for
        `token` strategy; ignored for `page` strategy, whose state is always the page
        index (`int`), seeded from `pagination.index.start` -- never ambiguous with "done"
        the way an absent cursor is, so it needs none of this zero-value machinery.
      zero_value_is_wire_absent: Whether the cursor's zero value (the seed
        `PaginatedResponse` starts the walk with) is itself a valid, wire-correct way to
        say "no cursor". `True` for a proto3/betterproto2 message field, where a
        zero-value field is indistinguishable on the wire from an absent one -- passing
        the seed straight through as `<driver>=<driver>` on the first call is correct
        there, and is exactly what the hand-written `page_request()` helper this
        replaces already did. `False` for an ordinary optional REST/JSON-RPC parameter,
        where the single-request method only sends the parameter when given a real,
        non-`None` value -- the seed is coerced back to `None` before being passed
        through, so the first call omits the parameter instead of sending the API a
        literal (and wrong) zero-value cursor. Defaults `True` -- the behavior every
        caller before this parameter existed already got -- for backward compatibility
        with real proto3 usage; a REST/JSON-RPC caller should pass
        `False` explicitly rather than lean on this default. Ignored for `page` strategy.
      nested_fields: See `paged_method`. Ignored for `page` strategy.
      response_accessor: See `paged_method`.
      response_optional: Whether the single-request method's own declared return type is
        itself nullable (`Response = HfClosedOrdersPage | None`, an `anyOf`-wrapped
        response whose non-null branch is what actually carries `rows_path`/`cursor_from`
        -- a `get_closed_orders` pair is the first real, motivating case). `False` (the
        default,
        and every already-migrated caller's prior, hardcoded behavior) when the response
        is known non-`None` by construction, the same case `read_path`'s own
        `subject_optional=False` already exists for -- passing `True` here is what makes
        this method thread that same `subject_optional` value through instead of
        hardcoding the assumption away.
      docstring: See `paged_method`.

    Returns:
      Source for the `<method_name>_paged` method, or None when nothing is declared.

    Raises:
      ValueError: `pagination` is `token` strategy without `absent_cursor` termination and
        a declared `done.rows`, or has no `state_type` to seed with; is `page` strategy
        without a `total` terminator and a declared `done.rows` (see
        `paged_response_page_total`); is `seek` strategy with `overlap` declared, is
        terminated by `unchanged`, or whose cursor parameter has no zero-value-seedable
        type (see `paged_response_seek`); or is any other strategy.
    """
    pagination = endpoint.pagination
    if pagination is None:
      return None
    if pagination.strategy == 'page':
      if pagination.done.kind == 'total':
        return self.paged_response_page_total(
          endpoint, method_name=method_name, header=header, rows_type=rows_type,
          response_accessor=response_accessor, response_optional=response_optional,
          docstring=docstring,
        )
      return self.paged_response_page_exhausted(
        endpoint, method_name=method_name, header=header, rows_type=rows_type,
        response_accessor=response_accessor, response_optional=response_optional,
        docstring=docstring,
      )
    if pagination.strategy == 'seek':
      # No `response_optional` to thread here -- `paged_response_seek`'s own row read
      # (`paged_rows`, for its declared-`done.rows` case) already defaults `read_path`'s
      # `subject_optional` to `True` unconditionally, so it's already safe regardless.
      return self.paged_response_seek(
        endpoint, method_name=method_name, header=header, rows_type=rows_type,
        response_accessor=response_accessor, docstring=docstring,
      )
    if pagination.strategy != 'token':
      raise ValueError(
        f'{endpoint.function}: paged_response_method only supports token-strategy, '
        f'page-strategy, or plain seek-strategy pagination'
      )
    if state_type is None:
      raise ValueError(
        f'{endpoint.function}: paged_response_method needs state_type for token-strategy '
        f'pagination'
      )
    if pagination.done.kind != 'absent_cursor':
      raise ValueError(
        f'{endpoint.function}: paged_response_method only supports absent-cursor termination'
      )
    if pagination.done.rows is None:
      raise ValueError(
        f'{endpoint.function}: paged_response_method needs pagination.done.rows declared '
        f'-- unlike the async-generator shape, which yields the whole response, this one '
        f'has to know exactly which response field the rows are'
      )
    # Captured before `flatten_nested_pagination` reassigns `pagination` to its own
    # (widely-typed) return value, which would otherwise lose the narrowing the guards
    # above just established -- these two are response-side paths flattening never
    # rewrites anyway (only the request-side `.parameter` fields change).
    rows_path: str = pagination.done.rows
    cursor_from: str = pagination.cursor.from_

    pagination, header, nested = self.flatten_nested_pagination(
      pagination, header, nested_fields,
    )
    parameters = [*header.args, *header.kwargs]
    driver = self.identifier(self.pagination_driver(pagination))
    driver_param = next((param for param in parameters if param.name == driver), None)
    # Mirrors `paged_method`'s own `driver_required` check: a cursor with no server-side
    # default has to survive onto the outer
    # signature and seed `PaginatedResponse.init` from the caller's own real argument --
    # there is no zero value to seed from, and none is needed, since a real value is
    # always given.
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

    positional = [param for param in header.args if param.name != driver or driver_required]
    keyword = [
      param for param in header.kwargs
      if (param.name != driver or driver_required) and param.name != 'validate'
    ]
    validate = next((param for param in header.kwargs if param.name == 'validate'), None)

    # `'response'`/`'rows'`/`'state'` are deliberately *not* pre-seeded here -- they are
    # exactly the bare names `paged_local` below is meant to hand out, and pre-adding them
    # would make it think its own candidates were already taken, appending pointless
    # trailing underscores (`response_`, `rows_`) to every one of them.
    taken = {
      'self', 'next', driver,
      *(param.name for param in positional), *(param.name for param in keyword),
      *({validate.name} if validate is not None else set()),
    }
    response = self.paged_local('response', taken)

    # `driver` is the loop-local cursor, seeded from `seed` (the state type's own zero
    # value) on the walk's first call. A proto3/betterproto2 field can't tell a zero-value
    # cursor from an absent one on the wire, so passing it straight through is correct
    # there (`zero_value_is_wire_absent=True`); an ordinary optional REST/JSON-RPC
    # parameter can, and only omits itself when given a real `None` -- so the zero-value
    # seed is coerced back to `None` before being passed through, and every real
    # (non-zero-value) cursor still passes unchanged. A required cursor needs none of this
    # coercion (no falsy-zero-value ambiguity -- a real value is always passed), and is
    # already carried through by the generic positional/keyword loops below (kept on the
    # outer signature when `driver_required`), so appending it again here would pass it
    # twice.
    driver_arg = driver if zero_value_is_wire_absent else f'({driver} or None)'
    call = [param.name for param in positional]
    call.extend(f'{param.name}={param.name}' for param in keyword)
    if not driver_required:
      call.append(f'{driver}={driver_arg}')
    if validate is not None:
      call.append(f'{validate.name}={validate.name}')
    if nested is not None:
      outer_name, outer_type, inner_cursor, inner_size, fields = nested
      flat_cursor = self.identifier(inner_cursor)
      flat_size = self.identifier(inner_size) if inner_size is not None else None
      construct_args = [f'{inner_cursor}={flat_cursor}']
      if inner_size is not None and flat_size is not None:
        # The flattened size param lands in `call` as a bare `flat_size` entry when
        # `flatten()` put it in `header.args` (positional -- e.g. the outer nested
        # pagination field was a request's *sole* parameter, so it took the "first
        # unique type stays positional" slot per the same-type-forces-kwargs house
        # rule, inverted), or as `f'{flat_size}={flat_size}'` when it
        # landed in `header.kwargs` (keyword). Both forms have to be recognized here --
        # missing the positional one leaves it both merged into the constructed message
        # below *and* passed through unchanged, colliding with the message keyword arg.
        size_entry = next(
          (c for c in call if c == flat_size or c.startswith(f'{flat_size}=')), None,
        )
        if size_entry is not None:
          field_type = fields[inner_size]
          size_expr = size_entry.split('=', 1)[1] if '=' in size_entry else size_entry
          if not field_type.endswith(' | None'):
            bare = field_type.removesuffix(' | None')
            if bare not in _SCALAR_ZERO_VALUES:
              raise ValueError(
                f'nested pagination field {inner_size!r} of {outer_name!r} has type '
                f'{field_type!r}, which is not one of {sorted(_SCALAR_ZERO_VALUES)}'
              )
            size_expr = f'({size_expr} if {size_expr} is not None else {_SCALAR_ZERO_VALUES[bare]})'
          construct_args.append(f'{inner_size}={size_expr}')
      call = [
        c for c in call
        if c != flat_cursor and not c.startswith(f'{flat_cursor}=')
        and (flat_size is None or (c != flat_size and not c.startswith(f'{flat_size}=')))
      ]
      call.append(f'{outer_name}={outer_type}({", ".join(construct_args)})')

    # `response` is `await self.{method_name}(...)`'s own return value -- never `None` by
    # construction regardless of accessor when the single-request method's own declared
    # return type isn't itself nullable (the single-request method either returns a real
    # value or raises, the same way every generated method does), so `subject_optional`
    # only ever needs to track `response_optional` (an `anyOf`-wrapped response whose
    # non-null branch carries `rows_path`/`cursor_from` -- a `get_closed_orders` is
    # the real, motivating case) rather than hardcoding the non-nullable case away. See
    # `read_path`'s own `subject_optional` docstring, and this method's `response_optional`
    # one.
    rows_local = self.paged_local('rows', taken)
    rows_read = self.read_path(
      rows_path, subject=response, name=rows_local, accessor=response_accessor,
      subject_optional=response_optional,
    )
    state_local = self.paged_local('state', taken)
    state_read = self.read_path(
      cursor_from, subject=response, name=state_local, accessor=response_accessor,
      subject_optional=response_optional,
    )

    inner = Function(
      name='next', asyn=True, method=False,
      args=[Function.Param(name=driver, type=state_type)],
      return_type=f'tuple[list[{rows_type}], {state_type} | None]',
    )
    # `{rows_local} or []`, not the bare name: `read_path`'s emitted `.get(key)` always
    # types a `TypedDict`'s field as `X | None` regardless of `subject_optional` or
    # whether the field itself is declared `Required` -- typeshed's own `TypedDict.get`
    # stub never narrows on Required-ness, only `__getitem__`/`[key]` does -- so
    # `rows_local` disagrees with this method's own declared non-optional `list[...]`
    # return type even though the field is genuinely required on the wire. `rows_path`
    # (`pagination.done.rows`) is documented as always present when `done.kind ==
    # 'absent_cursor'`, so coercing a structurally-impossible `None` to `[]` here changes
    # nothing at runtime -- confirmed via the original per-project workaround this
    # generalizes, which patched this exact line with a regex for the identical reason
    # on every one of its 15 `token`/`absent_cursor` endpoints.
    inner_body = '\n'.join([
      f'{response} = await self.{method_name}({", ".join(call)})',
      *rows_read,
      *state_read,
      f'return {rows_local} or [], {state_local} or None',
    ])
    inner_code = inner.code() + '\n' + indent(inner_body)

    outer = Function(
      name=self.paged_name(method_name), asyn=False, method=True,
      args=list(positional), kwargs=list(keyword),
      return_type=f'PaginatedResponse[{rows_type}, {state_type}]',
    )
    if validate is not None:
      outer.kwargs.append(validate)
    outer.overloads = validate_overloads(
      outer, raw_return_type=f'PaginatedResponse[Any, {state_type}]',
    )
    outer_doc = self.paged_summary(
      method_name,
      body='Awaitable (flattens every page) or async-iterable (one page at a time).',
      docstring=docstring, outer=outer,
    )
    outer_body = '\n'.join([outer_doc, inner_code, f'return PaginatedResponse({seed}, {inner.name})'])
    return outer.code() + '\n' + indent(outer_body)

  def paged_response_page_total(
    self, endpoint: Endpoint, *,
    method_name: str, header: Function, rows_type: str,
    response_accessor: Literal['dict', 'attr'] = 'dict',
    response_optional: bool = False,
    docstring: 'Docstring | None' = None,
  ) -> str | None:
    """Generate a `PaginatedResponse`-shaped page wrapper for `page`-strategy pagination
    terminated by a published total -- `paged_response_method`'s own dispatch target for
    `pagination.strategy == 'page'`, not meant to be called directly.

    Unlike a `token`/`seek` cursor, a page index's own starting value
    (`pagination.index.start`) is never ambiguous with "the walk is done" the way an
    absent cursor is, so this needs none of `paged_response_method`'s zero-value-seeding
    machinery: the state is simply the page index itself, seeded from `index.start` and
    incremented by one each turn until the declared total is reached -- the same
    arithmetic `paged_termination`'s own `total` branch already does for `paged_method`,
    restructured into the `next: state -> (rows, next_state | None)` expression
    `PaginatedResponse` requires instead of an imperative loop-with-`break`.

    Args:
      endpoint: The endpoint whose module is being generated.
      method_name: Name of the single-request method the wrapper drives.
      header: Rendered header of that method, after any project-specific renaming.
      rows_type: Rendered type of one row (`PaginatedResponse`'s own `T`) -- the element
        type of `pagination.done.rows`'s own field, not the collection type.
      response_accessor: See `paged_method`.
      response_optional: See `paged_response_method`.
      docstring: See `paged_method`.

    Returns:
      Source for the `<method_name>_paged` method, or None when nothing is declared.

    Raises:
      ValueError: `pagination.done` isn't a `total` terminator or declares no `rows`, or
        an items-counted total has no page size on the method to convert it.
    """
    pagination = endpoint.pagination
    assert pagination is not None and pagination.strategy == 'page'
    done = pagination.done
    if done.kind != 'total':
      raise ValueError(
        f'{endpoint.function}: paged_response_method only supports page-strategy '
        f'pagination terminated by a total'
      )
    if done.rows is None:
      raise ValueError(
        f'{endpoint.function}: paged_response_method needs pagination.done.rows declared '
        f'-- unlike the async-generator shape, which yields the whole response, this one '
        f'has to know exactly which response field the rows are'
      )
    rows_path: str = done.rows
    total_path: str = done.path
    start = pagination.index.start

    parameters = [*header.args, *header.kwargs]
    driver = self.identifier(pagination.index.parameter)
    size = self.paged_size(pagination, parameters)
    if done.counts == 'items' and size is None:
      raise ValueError(
        f'{endpoint.function}: a total counting items needs a page size on the method to '
        f'convert it'
      )

    positional = [param for param in header.args if param.name != driver]
    keyword = [
      param for param in header.kwargs if param.name != driver and param.name != 'validate'
    ]
    validate = next((param for param in header.kwargs if param.name == 'validate'), None)

    taken = {
      'self', 'next', driver,
      *(param.name for param in positional), *(param.name for param in keyword),
      *({validate.name} if validate is not None else set()),
    }
    response = self.paged_local('response', taken)

    call = [param.name for param in positional]
    call.extend(f'{param.name}={param.name}' for param in keyword)
    call.append(f'{driver}={driver}')
    if validate is not None:
      call.append(f'{validate.name}={validate.name}')

    rows_local = self.paged_local('rows', taken)
    rows_read = self.read_path(
      rows_path, subject=response, name=rows_local, accessor=response_accessor,
      subject_optional=response_optional,
    )
    total_local = self.paged_local('total', taken)
    total_read = self.read_path(
      total_path, subject=response, name=total_local, accessor=response_accessor,
      subject_optional=response_optional,
    )
    # `docs/pagination.md` §4: same stricter `total` as `paged_termination`'s own `total`
    # branch, restructured for `next`'s `nonlocal`-captured state instead of a loop-local
    # -- `next` is a fresh Python call every turn, so what an earlier turn saw has to live
    # in the *enclosing* (`outer`) function's scope to survive between calls, the same
    # way `PaginatedResponse`'s own state argument does.
    #
    # The disagreement has to be raised on the *next* call, not this one: `next`'s own
    # contract is to return this turn's rows and a next state in the same call, with no
    # way to both return real rows *and* raise from that same return -- unlike the plain
    # async-generator shape's `yield`, which commits to handing a page back before its own
    # termination check ever runs. Rather than drop the page that exposed the
    # disagreement (real data, valid on its own, the same reasoning the `yield`-first
    # order exists for), this "poisons" the walk instead: the page is returned normally,
    # with a next state forcing exactly one more call regardless of whether `done_expr`
    # would otherwise have ended the walk cleanly here -- ending it cleanly is exactly the
    # silent case this guards against -- and that forced call raises immediately, before
    # fetching anything else.
    total_seen_local = self.paged_local('total_seen', taken)
    total_poisoned_local = self.paged_local('total_poisoned', taken)
    total_poison_message_local = self.paged_local('total_poison_message', taken)

    if done.counts == 'pages':
      done_expr = f'({driver} - {start} + 1) >= {total_local}'
    else:
      assert size is not None, 'checked above'
      if self.paged_always_set(size):
        done_expr = f'({driver} - {start} + 1) * {size.name} >= {total_local}'
      else:
        default = self.paged_size_default(endpoint)
        if default is not None:
          resolved = f'({size.name} if {size.name} is not None else {default})'
          done_expr = f'({driver} - {start} + 1) * {resolved} >= {total_local}'
        else:
          # `size` is optional and the API documents no default -- an omitted
          # `size` can't be multiplied against a page count to test against the
          # total, and treating the omission itself as "done" (the bug this
          # replaces) silently truncated every such call to page 1. Decide by the
          # arithmetic when the caller actually supplied a real size; otherwise
          # fall back to the one signal that's always safe regardless of `size` --
          # an empty page really is the end.
          done_expr = (
            f'({size.name} is not None and ({driver} - {start} + 1) * {size.name} '
            f'>= {total_local}) or not {rows_local}'
          )

    total_message = (
      f'`{self.paged_name(method_name)}` needs a `total` on every page. The API omitted '
      f'it here, or reported a value ({{{total_local}}}) that disagrees with an earlier '
      f'page of this same walk ({{{total_seen_local}}}); retry the whole walk from the '
      f'start.'
    )
    inner = Function(
      name='next', asyn=True, method=False,
      args=[Function.Param(name=driver, type='int')],
      return_type=f'tuple[list[{rows_type}], int | None]',
    )
    inner_body = '\n'.join([
      f'nonlocal {total_seen_local}, {total_poisoned_local}, {total_poison_message_local}',
      f'if {total_poisoned_local}:',
      f'  raise LogicError({total_poison_message_local})',
      f'{response} = await self.{method_name}({", ".join(call)})',
      *rows_read,
      f'{rows_local} = {rows_local} if {rows_local} is not None else []',
      *total_read,
      f'{total_local} = int({total_local}) if {total_local} is not None else None',
      f'if {total_local} is None or ('
      f'{total_seen_local} is not None and {total_local} != {total_seen_local}):',
      f'  {total_poisoned_local} = True',
      f'  {total_poison_message_local} = f{total_message!r}',
      f'  return {rows_local}, {driver} + 1',
      f'{total_seen_local} = {total_local}',
      f'if {done_expr}:',
      f'  return {rows_local}, None',
      f'return {rows_local}, {driver} + 1',
    ])
    inner_code = inner.code() + '\n' + indent(inner_body)

    outer = Function(
      name=self.paged_name(method_name), asyn=False, method=True,
      args=list(positional), kwargs=list(keyword),
      return_type=f'PaginatedResponse[{rows_type}, int]',
    )
    if validate is not None:
      outer.kwargs.append(validate)
    outer.overloads = validate_overloads(
      outer, raw_return_type='PaginatedResponse[Any, int]',
    )
    outer_doc = self.paged_summary(
      method_name,
      body='Awaitable (flattens every page) or async-iterable (one page at a time).',
      docstring=docstring, outer=outer,
    )
    outer_body = '\n'.join([
      outer_doc,
      f'{total_seen_local} = None',
      f'{total_poisoned_local} = False',
      f'{total_poison_message_local} = \'\'',
      inner_code,
      f'return PaginatedResponse({start}, {inner.name})',
    ])
    return outer.code() + '\n' + indent(outer_body)

  def paged_response_page_exhausted(
    self, endpoint: Endpoint, *,
    method_name: str, header: Function, rows_type: str,
    response_accessor: Literal['dict', 'attr'] = 'dict',
    response_optional: bool = False,
    docstring: 'Docstring | None' = None,
  ) -> str | None:
    """Generate a `PaginatedResponse`-shaped page wrapper for `page`-strategy pagination
    terminated by a short or empty page -- `paged_response_method`'s own dispatch target
    for `pagination.strategy == 'page'` when `done.kind` is not `total`, not meant to be
    called directly.

    The most common REST shape there is (`page`/`per_page`, no count anywhere, the walk
    ends when a page comes back short): GitHub's list endpoints are the motivating case.
    The state is the page index, seeded from `index.start` and incremented by one each
    turn; `paged_rows_exhausted` supplies the same termination test `paged_method`'s
    plain async-generator shape already uses, restructured into the
    `next: state -> (rows, next_state | None)` expression `PaginatedResponse` requires.
    Unlike `paged_response_page_total`, `done.rows` need not be declared: the response
    itself is the row collection when it is not (`paged_rows`).

    Args:
      endpoint: The endpoint whose module is being generated.
      method_name: Name of the single-request method the wrapper drives.
      header: Rendered header of that method, after any project-specific renaming.
      rows_type: Rendered type of one row (`PaginatedResponse`'s own `T`) -- the element
        type of `pagination.done.rows`'s own field, or of the response itself when
        `done.rows` is undeclared.
      response_accessor: See `paged_method`.
      response_optional: See `paged_response_method`.
      docstring: See `paged_method`.

    Returns:
      Source for the `<method_name>_paged` method, or None when nothing is declared.

    Raises:
      ValueError: `pagination.done` is not a `short_page` or `empty` terminator, or a
        `short_page` terminator has no page size on the method to measure against.
    """
    pagination = endpoint.pagination
    assert pagination is not None and pagination.strategy == 'page'
    done = pagination.done
    if done.kind not in ('short_page', 'empty'):
      raise ValueError(
        f'{endpoint.function}: paged_response_page_exhausted only supports page-strategy '
        f'pagination terminated by a short or empty page'
      )
    start = pagination.index.start
    parameters = [*header.args, *header.kwargs]
    driver = self.identifier(pagination.index.parameter)
    size = self.paged_size(pagination, parameters)
    if done.kind == 'short_page' and size is None:
      raise ValueError(
        f'{endpoint.function}: a short page is only short relative to a page size on the '
        f'method'
      )

    positional = [param for param in header.args if param.name != driver]
    keyword = [
      param for param in header.kwargs if param.name != driver and param.name != 'validate'
    ]
    validate = next((param for param in header.kwargs if param.name == 'validate'), None)

    taken = {
      'self', 'next', driver,
      *(param.name for param in positional), *(param.name for param in keyword),
      *({validate.name} if validate is not None else set()),
    }
    response = self.paged_local('response', taken)

    call = [param.name for param in positional]
    call.extend(f'{param.name}={param.name}' for param in keyword)
    call.append(f'{driver}={driver}')
    if validate is not None:
      call.append(f'{validate.name}={validate.name}')

    rows_read: list[str] = []
    rows_local = self.paged_rows(
      done.rows, response=response, taken=taken, lines=rows_read,
      response_accessor=response_accessor,
    )
    if done.rows is None:
      # The payload is the collection; give the walk its own local so the `None`
      # normalisation below never rebinds `response`.
      rows_local = self.paged_local('rows', {*taken, response})
      rows_read = [f'{rows_local} = {response}']
    exhausted = self.paged_rows_exhausted(rows=rows_local, size=size, done_kind=done.kind)
    # A `short_page` walk whose caller omitted the size still knows when a page is short
    # once the API documents its default page size (`default` on the size parameter,
    # authoring rule 8): measure against that instead of waiting for an empty page.
    default = self.paged_size_default(endpoint) if done.kind == 'short_page' else None
    if size is not None and default is not None and not self.paged_always_set(size):
      exhausted = (
        f'if not {rows_local} or len({rows_local}) < '
        f'({size.name} if {size.name} is not None else {default}):'
      )

    inner = Function(
      name='next', asyn=True, method=False,
      args=[Function.Param(name=driver, type='int')],
      return_type=f'tuple[list[{rows_type}], int | None]',
    )
    inner_body = '\n'.join([
      f'{response} = await self.{method_name}({", ".join(call)})',
      *rows_read,
      f'{rows_local} = list({rows_local}) if {rows_local} is not None else []',
      exhausted,
      f'  return {rows_local}, None',
      f'return {rows_local}, {driver} + 1',
    ])
    inner_code = inner.code() + '\n' + indent(inner_body)

    outer = Function(
      name=self.paged_name(method_name), asyn=False, method=True,
      args=list(positional), kwargs=list(keyword),
      return_type=f'PaginatedResponse[{rows_type}, int]',
    )
    if validate is not None:
      outer.kwargs.append(validate)
    outer.overloads = validate_overloads(
      outer, raw_return_type='PaginatedResponse[Any, int]',
    )
    outer_doc = self.paged_summary(
      method_name,
      body='Awaitable (flattens every page) or async-iterable (one page at a time).',
      docstring=docstring, outer=outer,
    )
    outer_body = '\n'.join([
      outer_doc,
      inner_code,
      f'return PaginatedResponse({start}, {inner.name})',
    ])
    return outer.code() + '\n' + indent(outer_body)

  def paged_response_seek(
    self, endpoint: Endpoint, *,
    method_name: str, header: Function, rows_type: str,
    response_accessor: Literal['dict', 'attr'] = 'dict',
    docstring: 'Docstring | None' = None,
  ) -> str:
    """Generate a `PaginatedResponse`-shaped page wrapper for plain `seek`-strategy
    pagination -- `paged_response_method`'s own dispatch target for
    `pagination.strategy == 'seek'` with no declared `overlap`, not meant to be called
    directly.

    Closely mirrors `token`'s own branch in `paged_response_method`:
    the cursor comes from `read_last_row` -- the previous page's own last row -- instead of
    a declared top-level response field, but both are just a local bound off the response
    threaded into the same `next: state -> (rows, next_state | None)` shape. Unlike
    `token`, `pagination.done.rows` need not be declared here: a seek walk's own
    termination (`paged_rows_exhausted`) already tolerates the payload itself being the
    row collection (`paged_rows`), and `PaginatedResponse`'s row type is exactly that
    collection's own element type either way -- an `hf_ledgers` endpoint is this shape
    (a bare array response, no wrapper).

    Args:
      endpoint: The endpoint whose module is being generated.
      method_name: Name of the single-request method the wrapper drives.
      header: Rendered header of that method, after any project-specific renaming.
      rows_type: Rendered type of one row (`PaginatedResponse`'s own `T`) -- the element
        type of `pagination.done.rows`'s own field, or of the response itself when
        `done.rows` is undeclared.
      response_accessor: See `paged_method`.
      docstring: See `paged_method`.

    Returns:
      Source for the `<method_name>_paged` method.

    Raises:
      ValueError: `pagination` declares `overlap` (see `paged_overlap_seek` instead), is
        terminated by `unchanged` (not yet supported here -- see `Generator.paged_method`
        instead), its cursor parameter isn't on the method, its type has no zero value to
        seed `PaginatedResponse` with (unless the cursor is required -- see
        `driver_required` below, which needs no zero value at all), or a `short_page`
        terminator has no page size on the method to measure against.
    """
    pagination = endpoint.pagination
    assert pagination is not None and pagination.strategy == 'seek'
    if pagination.overlap is not None:
      raise ValueError(
        f'{endpoint.function}: paged_response_method does not support seek pagination '
        f'with overlap declared -- see paged_overlap_seek'
      )
    if pagination.done.kind == 'unchanged':
      # `paged_rows_exhausted` below has no `unchanged` case -- it only ever measures a
      # page as short or empty -- so a `PaginatedResponse`-shaped wrapper for this
      # terminator isn't built yet (S24's own scope note: plain `paged_method`, the
      # ordinary async-generator shape, is the only one that supports it so far). Raising
      # here rather than silently generating a wrapper that never terminates the way its
      # own declaration promises.
      raise ValueError(
        f'{endpoint.function}: paged_response_method does not yet support seek pagination '
        f'terminated by `unchanged` -- see Generator.paged_method for the plain '
        f'async-generator shape instead'
      )
    parameters = [*header.args, *header.kwargs]
    size = self.paged_size(pagination, parameters)
    driver = self.identifier(self.pagination_driver(pagination))
    driver_param = next((param for param in parameters if param.name == driver), None)
    if driver_param is None:
      raise ValueError(
        f'{endpoint.function}: the seek cursor {driver!r} is not a parameter of '
        f'{method_name!r}, so the walk has nothing to advance'
      )
    base = (driver_param.type or 'str').removesuffix(' | None')
    # `driver_required` mirrors `paged_method`'s own identical check: a cursor with no
    # server-side default (a `start_timestamp` with none) has to survive onto the
    # outer signature and seed `PaginatedResponse.init` from the caller's own real
    # argument -- there is no zero value to seed from, and none is needed, since a real
    # value is always given.
    driver_required = driver_param.required
    if driver_required:
      seed = driver
    else:
      if base not in _SCALAR_ZERO_VALUES:
        raise ValueError(
          f'{endpoint.function}: cursor type {base!r} is not one of '
          f'{sorted(_SCALAR_ZERO_VALUES)} -- no zero value to seed PaginatedResponse with'
        )
      seed = _SCALAR_ZERO_VALUES[base]

    positional = [param for param in header.args if param.name != driver or driver_required]
    keyword = [
      param for param in header.kwargs
      if (param.name != driver or driver_required) and param.name != 'validate'
    ]
    validate = next((param for param in header.kwargs if param.name == 'validate'), None)

    # `'response'`/`'rows'`/`'state'` deliberately left out of `taken` here, same reasoning
    # as `paged_response_method`'s own token branch above.
    taken = {
      'self', 'next', driver,
      *(param.name for param in positional), *(param.name for param in keyword),
      *({validate.name} if validate is not None else set()),
    }
    response = self.paged_local('response', taken)

    # `driver` is `next`'s own bare-typed (never `None`) parameter, seeded from `seed` --
    # an ordinary optional REST/JSON-RPC cursor parameter only sends a real value, so the
    # zero-value seed is coerced back to `None` on the first call, exactly like
    # `paged_response_method`'s own `zero_value_is_wire_absent=False` token case. A
    # required cursor needs no such coercion (no falsy-zero-value ambiguity to resolve --
    # a real value is always passed), and is already carried through by the generic
    # positional/keyword loops above (kept on the outer signature when `driver_required`),
    # so appending it again here would pass it twice.
    call = [param.name for param in positional]
    call.extend(f'{param.name}={param.name}' for param in keyword)
    if not driver_required:
      call.append(f'{driver}=({driver} or None)')
    if validate is not None:
      call.append(f'{validate.name}={validate.name}')

    rows_read: list[str] = []
    rows = self.paged_rows(
      pagination.done.rows, response=response, taken=taken, lines=rows_read,
      response_accessor=response_accessor,
    )
    exhausted = self.paged_rows_exhausted(rows=rows, size=size, done_kind=pagination.done.kind)
    # A fresh local, not `driver` itself: `driver` is declared as `next`'s own bare
    # (non-`None`) parameter type, and `read_last_row` can bind `None` to what it reads --
    # reusing `driver` here would disagree with its own declared type under pyright, the
    # same class of bug the cursor-cast fix above this method exists to prevent.
    state_local = self.paged_local('state', taken)
    # `cast` is restricted to `str`/`int`/`float`, mirroring `paged_method`'s own
    # `driver_cast` (`read_last_row`'s own docstring): a rendered format type
    # (`TimestampMillis`, say -- only reachable here once a required, non-scalar cursor
    # qualifies, Fix 3 above) is already what the row field comes back as once read off an
    # already-response-validated row, so casting through it would call a type alias as a
    # constructor (`TypeError: Annotated cannot be instantiated`) rather than normalize an
    # untyped value. Passing `base` through unfiltered here was never reachable before Fix
    # 3, since `base` was always one of `_SCALAR_ZERO_VALUES` until a required cursor
    # could bypass that check.
    row_cast = base if base in ('str', 'int', 'float') else None
    cursor = self.read_last_row(
      pagination.cursor.from_, rows=rows, name=state_local, taken=taken, cast=row_cast,
    )

    inner = Function(
      name='next', asyn=True, method=False,
      args=[Function.Param(name=driver, type=base)],
      return_type=f'tuple[list[{rows_type}], {base} | None]',
    )
    inner_body = '\n'.join([
      f'{response} = await self.{method_name}({", ".join(call)})',
      *rows_read,
      exhausted,
      # `{rows} or []`, not a bare `{rows}` -- inside this branch `rows`'s own declared
      # type is still `list[{rows_type}] | None` (a dict-accessor `paged_rows` read, or
      # any read `response_optional` covers), so returning the variable itself fails
      # pyright under the declared `tuple[list[{rows_type}], ...]` return type. A literal
      # `[]` alone would be wrong here, not just a type workaround: `short_page` (a
      # `historical_trades`, this method's own motivating S24 case) reaches this branch
      # with `rows` genuinely non-empty -- fewer rows than the requested size, not zero
      # -- and that page's real rows still belong in the walk's last yield. `or []`
      # narrows the type correctly *and* only substitutes `[]` when `rows` truly is
      # empty/`None` (confirmed against a real regression: an earlier version of this
      # fix hardcoded `[]` unconditionally and silently dropped `historical_trades`'s
      # own final short page).
      f'  return {rows} or [], None',
      *cursor,
      f'return {rows}, {state_local} or None',
    ])
    inner_code = inner.code() + '\n' + indent(inner_body)

    outer = Function(
      name=self.paged_name(method_name), asyn=False, method=True,
      args=list(positional), kwargs=list(keyword),
      return_type=f'PaginatedResponse[{rows_type}, {base}]',
    )
    if validate is not None:
      outer.kwargs.append(validate)
    outer.overloads = validate_overloads(
      outer, raw_return_type=f'PaginatedResponse[Any, {base}]',
    )
    outer_doc = self.paged_summary(
      method_name,
      body='Awaitable (flattens every page) or async-iterable (one page at a time).',
      docstring=docstring, outer=outer,
    )
    outer_body = '\n'.join([outer_doc, inner_code, f'return PaginatedResponse({seed}, {inner.name})'])
    return outer.code() + '\n' + indent(outer_body)

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
    return target(endpoint, references, **kwargs)

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
    needs_cast = False
    if request_type is not None:
      if endpoint_plan.request.needs_cast:
        call_args.append(f'request_type=cast(type, {request_type})')
        needs_cast = True
      else:
        call_args.append(f'request_type={request_type}')
    if response_type is not None:
      if endpoint_plan.response.needs_cast:
        call_args.append(f'response_type=cast(type, {response_type})')
        needs_cast = True
      else:
        call_args.append(f'response_type={response_type}')
    call_lines = group_lines(call_args, tab='  ', width=88)
    return_line = 'return await self.request(\n' + '\n'.join(call_lines) + '\n)'
    body = '\n'.join([*request_decl_lines, return_line])

    header.overloads = validate_overloads(header, raw_return_type='Any')
    method_lines = [header.code()]
    doc_code = docstring.code()
    if doc_code:
      method_lines.append(indent(doc_code, '  '))
    method_lines.append(indent(body, '  '))
    method_code = '\n'.join(method_lines)

    # The request/response shape is silent on pagination -- `endpoint.pagination` is a
    # sibling of `spec`
    # (docs/spec/authoring.md rule 8), untouched by the request/response collapse, so
    # rendering it reuses the exact same shared `paged_method`/`paged_response_method`
    # primitives every already-migrated (legacy `openapi`-shaped) backend already calls,
    # just supplied with this method's own new-shape `header`/`response_type` instead of
    # the old ones. `rows_type` (S24's `PaginatedResponse`-shaped wrapper) is resolved the
    # identical way a nested request property already is, just off `$response` instead
    # of `$request` (confirmed empirically: `Unnest` keys a `done.rows` array element
    # `$response/{dotted path with '/' instead of '.'}/item`, no `response200`-shaped
    # wrapper segment the way the legacy openapi branch's own equivalent helper needed --
    # there is no per-status envelope left to name once `response` is one bare schema).
    # A project whose declared-pagination endpoints are all `token`/`absent_cursor` with
    # a declared `done.rows` renders every one `PaginatedResponse`-shaped rather than a
    # plain async generator -- the exact shape its pre-migration hand-written core
    # already published (S24's own citation), not a new behavior.
    # `zero_value_is_wire_absent=False`: an ordinary optional REST/JSON-RPC parameter,
    # never a proto3 message field (`paged_response_method`'s own default assumes the
    # latter).
    pagination = endpoint.pagination
    paged_source: str | None = None
    if pagination is not None:
      rows_type: str | None = None
      # Populated only when `paged_response_rows_type` resolves `rows_type` via the
      # bare-`$ref`-response path (`response_ref`) -- that row type is never seen by
      # `type_generator`, so nothing else registers its import (see that method's own
      # `imports` parameter docstring).
      rows_type_imports: dict[str, set[str]] = {}
      if (
        pagination.strategy == 'token'
        and pagination.done.kind == 'absent_cursor'
        and pagination.done.rows is not None
      ):
        rows_type = self.paged_response_rows_type(
          types, pagination.done.rows, response_ref=response_ref, references=references,
          imports=rows_type_imports,
        )
      elif (
        pagination.strategy == 'seek'
        and pagination.overlap is None
        and pagination.done.kind != 'unchanged'
      ):
        # S24's third `PaginatedResponse`-shaped shape: plain `seek` strategy, no
        # declared `overlap` (`paged_response_method`'s own dispatch already handles this
        # internally via `paged_response_seek` -- the gap was this call site never
        # reaching it at all for `seek`). An `hf_ledgers` pair -- S24's own named
        # first real endpoints for this shape --
        # declare no `done.rows` at all (the response *is* the row collection), which
        # `paged_response_rows_type` resolves via its own empty-`rows_path` branch.
        # `done.kind != 'unchanged'` excludes the one seek terminator
        # `paged_response_seek` deliberately doesn't support yet (it raises rather than
        # mis-generate) -- confirmed as a real generation-blocking gap on a
        # `get_historical_funding`: this branch matched (`unchanged` still
        # has `overlap is None`), so `rows_type` got set and `paged_response_method` was
        # attempted anyway, hitting `paged_response_seek`'s own guard as a fatal
        # `ValueError` instead of ever reaching the plain-generator fallback below --
        # exactly the same class of gap this branch's own comment already documents for
        # `page`/`token` above (a dispatch site not reaching a strategy's real support),
        # just for a terminator instead of a whole strategy.
        rows_type = self.paged_response_rows_type(
          types, pagination.done.rows or '', response_ref=response_ref, references=references,
          imports=rows_type_imports,
        )
      elif pagination.strategy == 'page' and (
        (pagination.done.kind == 'total' and pagination.done.rows is not None)
        or pagination.done.kind == 'empty'
        or (
          pagination.done.kind == 'short_page'
          and self.paged_size(pagination, [*header.args, *header.kwargs]) is not None
        )
      ):
        # S24's second `PaginatedResponse`-shaped shape: `page` strategy terminated by a
        # declared `total`, with `done.rows` also declared. `paged_response_method`'s own
        # dispatch (`paged_response_page_total`, S24's own citation) has supported this
        # for some time -- this call site simply never reached it for `page`
        # strategy at all, the same class of gap `seek` had before the branch above:
        # `rows_type` stayed `None` for every `page`+`total`+`rows` endpoint, so it fell
        # straight through to the plain-generator `paged_method` fallback below,
        # regardless of whether `paged_response_page_total` could have rendered it.
        # A `history.rate_history` (`{rows: [...], total: "N"}`, rule 8's own
        # `done.rows` worked example) is the real, motivating case -- a
        # user-reported validation error traced to the response schema's `total` field
        # led to inspecting the generated `_paged` method and finding it was a plain
        # `AsyncIterator`, not `PaginatedResponse`-shaped, despite qualifying.
        # `short_page`/`empty` (GitHub's `page`/`per_page` shape, the fourth
        # `PaginatedResponse`-shaped shape): `done.rows` may be unset, the response then
        # being the row collection itself, which `paged_response_rows_type` resolves via
        # its empty-`rows_path` branch the same way plain `seek` above does. A
        # `short_page` with no size parameter on the method stays on the plain generator
        # path, which refuses it with the same `ValueError` it always has.
        rows_type = self.paged_response_rows_type(
          types, pagination.done.rows or '', response_ref=response_ref, references=references,
          imports=rows_type_imports,
        )
      # The cursor's own type and whether a `PaginatedResponse` can be seeded with it are
      # plan decisions (`PaginationPlan.state_type`/`seedable`): the driver parameter's
      # type without its `null` (a cursor is not always a string -- an `int`
      # `continuation_token`, a `TimestampIso` `toISO`), rendered here through the same
      # type renderer its parameter annotation came from; seedable when the strategy
      # never needs a zero value (`page`/`offset`/`window`), the cursor type has one
      # (`''`/`0`/`False`), or the cursor is required and the caller's own argument
      # seeds the walk. `token` and plain (no-`overlap`) `seek` both need the seed:
      # `paged_response_seek`'s `PaginatedResponse(seed, next)` still wants a real
      # initial value even though later cursors are read off a row.
      pagination_plan = endpoint_plan.pagination
      assert pagination_plan is not None and pagination_plan.state_type is not None
      state_type = (
        self.plan_type_code(pagination_plan.state_type) if rows_type is not None else None
      )
      seedable = pagination_plan.seedable
      # Fix 1 (`docs/pagination.md`): a `window`/`seek`+`overlap` walk (`paged_method`'s
      # own dispatch to `paged_window_overlap`/`paged_overlap_seek`) yields a flattened
      # `list[T]` of rows once `done.rows` is declared, never the enveloped type --
      # resolved independently of the `rows_type`/`seedable` gate above, since `window` is
      # never `PaginatedResponse`-eligible (S24) and `seedable` is unconditionally `True`
      # for it (`pagination.strategy not in ('token', 'seek')`) -- folding this into the
      # same `rows_type` local would wrongly route a `window`+`overlap` endpoint into
      # `paged_response_method` the moment `rows_type` resolved to anything at all.
      overlap_rows_type: str | None = None
      overlap_rows_type_imports: dict[str, set[str]] = {}
      if (
        pagination.strategy in ('window', 'seek')
        and pagination.overlap is not None
        and pagination.done.rows is not None
      ):
        overlap_rows_type = self.paged_response_rows_type(
          types, pagination.done.rows, response_ref=response_ref, references=references,
          imports=overlap_rows_type_imports,
        )
      # Whether the single-request method's return type is nullable (`Response =
      # HfClosedOrdersPage | None`, an `anyOf`-wrapped response whose real branch carries
      # the rows) is the plan's `ResponsePlan.optional`, read off the type tree; the
      # walker then guards every read on the response.
      response_optional = endpoint_plan.response.optional
      if rows_type is not None and seedable:
        paged_source = self.paged_response_method(
          endpoint, method_name=method_name, header=header,
          rows_type=rows_type, state_type=state_type, zero_value_is_wire_absent=False,
          response_optional=response_optional, docstring=docstring,
        )
      elif response_type is not None:
        # `paged_method` itself returns `None` (rather than raising) for a real,
        # structural pagination-shape limitation it can't render -- a
        # `trades_history` (an `offset` walk terminated by an
        # item-counted `total` with no rows to count and no size to convert the count
        # into a step) is the real, motivating case. See `paged_method`'s own
        # `paged_step` call for the narrowly-scoped `try/except ValueError` this relies
        # on -- catching there, not here, means a genuine bug anywhere else in
        # `paged_method`'s body still surfaces as a real exception instead of being
        # silently swallowed into "no `_paged` variant".
        paged_source = self.paged_method(
          endpoint, method_name=method_name, header=header, response_type=response_type,
          overlap_rows_type=overlap_rows_type, docstring=docstring,
        )
      # `self.paged_imports(...)` -- not a hardcoded guess -- is what actually knows what
      # a rendered page iterator needs: `AsyncIterator`/`PaginatedResponse` always, plus
      # `LogicError` for a window-truncation or seek-overlap raise, `timedelta` for a
      # window step or a timestamp-typed seek cursor's advance, and that cursor's own
      # converter import (`TIMESTAMP_TICKS`). A hardcoded `{'typing_extensions':
      # {'AsyncIterator'}}`/`{'truewire_core': {'PaginatedResponse'}}` here (this method's own
      # bug until now) happened to be enough while every migrated endpoint was
      # `token`/`absent_cursor` with a declared `done.rows` (the `rows_type is not None`
      # branch, which needs nothing extra) -- `seek`+`overlap`, timestamp-cursored
      # endpoints are the first real case needing the rest.
      if rows_type is not None and seedable:
        paged_imports = merge_imports([{'truewire_core': {'PaginatedResponse'}}, rows_type_imports])
        # `paged_response_page_total` (`pagination.strategy == 'page'`) always needs
        # `LogicError` now too -- `docs/pagination.md` §4's stricter `total`, raised from
        # the very same `page`+`total` dispatch this branch renders.
        if pagination.strategy == 'page' and pagination.done.kind == 'total':
          paged_imports = merge_imports([paged_imports, PAGED_TRUNCATION_IMPORTS])
      else:
        paged_imports = self.paged_imports(endpoint, header=header)
        if overlap_rows_type_imports:
          paged_imports = merge_imports([paged_imports, overlap_rows_type_imports])
    else:
      paged_imports = {}

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
    class_lines = [
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
      VALIDATE_OVERLOAD_IMPORTS if header.overloads else {},
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
    types = type_generator(schemas, inline=True)

    parameters_type = types.identifiers.get('$parameters')
    payload_type = payload_ref_type if payload_ref is not None else types.identifiers.get('$payload')

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
    subscribe_return_type = f'StreamManager[{payload_type or "Any"}, Any, Any]'
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
    if connect_param_name is None and not direct_channel and parameters_type is not None:
      if endpoint_plan.request.needs_cast:
        call_args.append(f'request_type=cast(type, {parameters_type})')
        needs_cast = True
      else:
        call_args.append(f'request_type={parameters_type}')
    if payload_type is not None:
      # `payload_ref is None and ...` -- never needed for a `$ref`-resolved external
      # type, which is always a real class (see `rpc_endpoint`'s identical guard).
      if endpoint_plan.response.needs_cast:
        call_args.append(f'response_type=cast(type, {payload_type})')
        needs_cast = True
      else:
        call_args.append(f'response_type={payload_type}')
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
    class_code = '\n'.join([
      f'class {class_name}({core_class}):',
      indent(f'"""{class_doc}"""', '  '),
      '',
      indent(method_code, '  '),
    ])

    merged_imports = merge_imports([
      dict(types.imports),
      dict(direct_channel_imports),
      dict(payload_ref_import),
      {core_module: {core_class}, 'truewire_core.util': {'StreamManager'}, 'typing_extensions': {'Any'}},
      {'typing_extensions': {'cast'}} if needs_cast else {},
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
      rows_resolved = (
        self._grpc_pagination_rows_type(
          getattr(response_module, response_class_name), rows=pagination.done.rows,
        )
        if pagination.done.rows is not None and pagination.done.kind != 'unchanged'
        else None
      )
      # `done.kind != 'unchanged'`: the same exclusion `rpc_endpoint`'s identical dispatch
      # needs (see its own comment for the confirmed generation-blocking gap this guards
      # against) -- `paged_response_method`/`paged_response_seek` doesn't support this
      # terminator yet and raises rather than mis-generate, so attempting it here would
      # crash a gRPC `unchanged`-terminated endpoint the same way an RPC one did, the
      # moment one is ever declared. No real gRPC endpoint uses `unchanged` today, so this
      # is preemptive, not yet triggered live -- but it's the identical shape of gap.
      if rows_resolved is not None:
        rows_type, rows_type_imports = rows_resolved
        state_type = self.paged_state_type(header, pagination, nested_fields)
        # `token` and plain `seek` both need a real zero value to seed
        # `PaginatedResponse`'s very first call with -- see `rpc_endpoint`'s identical
        # check and its own comment for the confirmed generation-failure case. Fix 3: a
        # required cursor needs no zero value at all (see `rpc_endpoint`'s identical
        # addition) -- no real gRPC endpoint declares one today, but the check costs
        # nothing to carry here too.
        seedable = (
          pagination.strategy not in ('token', 'seek')
          or state_type in _SCALAR_ZERO_VALUES
          or self.pagination_driver_required(header, pagination)
        )
        if seedable:
          paged_source = self.paged_response_method(
            endpoint, method_name=method_name, header=header,
            rows_type=rows_type, state_type=state_type,
            nested_fields=nested_fields, response_accessor='attr', docstring=docstring,
          )
          if paged_source is not None:
            paged_imports = merge_imports([{'truewire_core': {'PaginatedResponse'}}, rows_type_imports])
            # See the identical comment at `rpc_endpoint`'s own dispatch: `page`+`total`
            # always needs `LogicError` now too (`docs/pagination.md` §4).
            if pagination.strategy == 'page' and pagination.done.kind == 'total':
              paged_imports = merge_imports([paged_imports, PAGED_TRUNCATION_IMPORTS])
      if paged_source is None:
        # Fix 1 (`docs/pagination.md`): reuse the same `rows_resolved` lookup above for a
        # `window`/`seek`+`overlap` endpoint's flattened row type -- no real gRPC endpoint
        # declares either today, so this is a no-op (`None`) in practice, same reasoning
        # as `rpc_endpoint`'s identical addition.
        overlap_rows_type = rows_resolved[0] if rows_resolved is not None else None
        paged_source = self.paged_method(
          endpoint, method_name=method_name, header=header,
          response_type=response_class_name, nested_fields=nested_fields,
          response_accessor='attr', overlap_rows_type=overlap_rows_type, docstring=docstring,
        )
        if paged_source is not None:
          paged_imports = self.paged_imports(endpoint, header=header)
          if rows_resolved is not None:
            paged_imports = merge_imports([paged_imports, rows_resolved[1]])

    method_lines = [header.code()]
    doc_code = docstring.code()
    if doc_code:
      method_lines.append(indent(doc_code, '  '))
    method_lines.append(indent(return_line, '  '))
    method_code = '\n'.join(method_lines)

    # `paged_source` before `method_code` -- see `rpc_endpoint`'s identical ordering and
    # its own comment for the real class-body name-shadowing bug this avoids.
    class_lines = [
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
      imports=merge_imports([imports, paged_imports]), pkg_name=self.project.package_name,
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
    the derived `router_class_name(section)`, and a bare leaf endpoint directly under the
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

    class_name = root_name if root_name is not None else router_class_name(section)
    class_lines = [
      f'class {class_name}({", ".join(base_names)}):',
      indent(router_docstring(section, self.router_doc(section)), '  '),
    ]
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
