"""The plan: every decision a backend renders, computed once from a project.

A `PackagePlan` is what `truewire plan --json` prints and what a backend reads. It holds
no rendered code and no language name: types are the tree in `truewire.plan.types`,
identifiers are the wire's own names, and every decision the Python backend used to
re-derive from rendered strings (which cursor type seeds a walker, whether a response is
nullable, whether a request type is a bare alias) is a plain field here. A second backend
consumes the same object, or the same JSON, and renders it in its own language.

Every model is frozen and serialises with camelCase keys (`model_dump(by_alias=True)`,
see `PackagePlan.to_json`); absent keys mean `null`.
"""
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from typing_extensions import Any, Literal

from .types import Type

TypeSet = dict[str, Type]
"""Type name -> its tree. Names are what a backend defines the types under; a `Ref` inside
any tree names either a key of the same set or of a shared scope's set."""


class PlanModel(BaseModel):
  """Base for every plan node: frozen, strict, camelCase on the wire."""
  model_config = ConfigDict(
    frozen=True, extra='forbid', alias_generator=to_camel, populate_by_name=True,
  )


class NewParamPlan(PlanModel):
  """One caller-supplied `new()` keyword a composite core declares (`[python.cores.<name>].params`)."""
  type: str
  """The declared type reference, as written in `truewire.toml`."""
  required: bool = True


class CorePlan(PlanModel):
  """One symbolic core name: what `truewire.toml` declares about it, in every language."""
  meta: dict[str, Any] | None = None
  """`[cores.<name>].meta`, the JSON Schema every endpoint's `meta` on this core satisfies."""
  forward: list[str] | None = None
  """`new()` keywords the composing class passes from its own same-named fields (ADR 0011)."""
  params: dict[str, NewParamPlan] | None = None
  """`new()` keywords a caller supplies (ADR 0011)."""
  children: dict[str, str] | None = None
  """Child attribute name -> field on the composing class holding that child's transport."""


class RouterDocPlan(PlanModel):
  """A router grouping's declared `router.json` prose."""
  description: str
  upstream: str


class RouterChildPlan(PlanModel):
  """One child a router composes."""
  name: str
  """The attribute the child is reached through (`repos` in `client.repos`)."""
  kind: Literal['endpoint', 'router']
  class_: str = Field(alias='class')
  """The class a backend names the child after: PascalCase of `name`. A backend may
  still rename an endpoint class that collides with a type in the same module."""


class RouterPlan(PlanModel):
  """One grouping of the function tree: a directory under `spec/endpoints/`."""
  path: list[str]
  """Function-tree position, root first; `[]` for the package root."""
  core: str | None = None
  """Symbolic core name the nearest `router.json` declares; `null` when none does, which
  `truewire check` reports."""
  doc: RouterDocPlan | None = None
  children: list[RouterChildPlan]


class WirePlan(PlanModel):
  """Where the endpoint lives on the wire."""
  path: str | None = None
  """`spec.path`: an HTTP path template, or the RPC identifier over WS."""
  method: str | None = None
  """`spec.method`, for HTTP."""
  channel: str | None = None
  """`spec.channel`, for a stream."""
  placeholders: list[str] = []
  """`{name}` placeholders in `path`/`channel`, in order of appearance."""


class RequestFieldPlan(PlanModel):
  """One property of a flat request: what the wire calls it, and what it takes."""
  wire: str
  """The property name as the API spells it."""
  required: bool
  type: Type
  description: str | None = None
  default: Any | None = None
  """The schema's own `default`, when it documents one."""
  fixed: Any | None = None
  """The single value a required single-value `enum` takes: wire dispatch plumbing a
  backend defaults for the caller."""


class RequestPlan(PlanModel):
  """The request side of an endpoint: a flat object, one union, one array, or nothing."""
  shape: Literal['none', 'fields', 'union', 'array']
  type: str | None = None
  """The `types` entry holding the whole request (`Request`/`Parameters`), when one is rendered."""
  fields: list[RequestFieldPlan] = []
  """The flat properties, for `shape == 'fields'`."""
  needs_cast: bool = False
  """Whether `type` is a bare alias (`Literal[...]`, `Any`) rather than a class, which a
  typed backend cannot pass where a `type[T]` is expected without a cast."""


class ResponsePlan(PlanModel):
  """What the method hands back."""
  wire: str | None = None
  """The `wire_types` entry describing the whole wire body, when `selector` is not
  empty (ADR 0010). Equal to `payload` otherwise."""
  payload: str | None = None
  """The type the method returns: a `types` entry, or a shared scope's."""
  selector: str = ''
  """`envelope.payload`: the path inside the wire body the returned value sits at."""
  optional: bool = False
  """Whether `payload` is nullable (`X | null`), so a walker guards every read on it."""
  needs_cast: bool = False
  """See `RequestPlan.needs_cast`."""


class StreamPlan(PlanModel):
  """Facts a stream endpoint adds beyond `request` (its parameters) and `response`
  (its pushed payload)."""
  channel_params: list[str] = []
  """Parameter names that are placeholders of `wire.channel`."""
  connect_only: bool = False
  """A connect-triggered push whose channel is exactly its one required parameter: the
  connection is the subscription, and the value is passed straight to the core."""
  direct_channel: bool = False
  """The parameters are exactly the channel's placeholders: a backend fills the template
  from its own locals and passes no parameters object."""
  push: dict[str, Any] | None = None
  """`endpoint.push`, when the stream starts without a subscribe frame."""
  verb: dict[str, Any] | None = None
  """`envelope.verb`: how subscribe/unsubscribe frames state their intent."""
  reply_payload: str | None = None
  """`envelope.reply_payload`, when the ack's value sits at a different path."""


class PaginationPlan(PlanModel):
  """Everything a backend decides before it renders a page walker."""
  strategy: Literal['page', 'token', 'seek', 'offset', 'window']
  driver: str
  """Wire name of the request parameter the walk advances (page index, cursor, offset,
  or the moving window bound)."""
  driver_required: bool
  """Whether a `token`/`seek` cursor is required on the single call, so the first page
  is seeded from the caller's own value rather than a zero value."""
  size: str | None = None
  """Wire name of the page-size parameter, when declared and present on the request."""
  size_default: int | None = None
  """The API's documented default page size, from the size property's own `default`."""
  start: int | None = None
  """`index.start` for a `page` walk."""
  done: dict[str, Any]
  """The declared terminator, as written."""
  rows: str | None = None
  """`done.rows`: the response path holding the page's rows, when declared."""
  cursor_from: str | None = None
  """`cursor.from`: where the next cursor is read (a response path, or a last-row field)."""
  overlap: dict[str, Any] | None = None
  window: dict[str, Any] | None = None
  """`bound`/`order`/`step` for a `window` walk."""
  row_type: Type | None = None
  """One row's type, resolved through `done.rows` (or the payload itself when it is the
  collection); `null` when the tree cannot name it."""
  state_type: Type | None = None
  """The walk state's type: the driver parameter's own type without its `null`."""
  seedable: bool
  """Whether a `PaginatedResponse`-shaped walker can seed its first state: any strategy
  but `token`/`seek`, a cursor with a zero value, or a required cursor."""
  walker: Literal['paginated', 'generator', 'none']
  """`paginated`: rows and a seedable state, so the walker exposes pages and a state to
  resume from. `generator`: a plain async iterator of responses. `none`: the declaration
  cannot be walked (an `offset` walk with nothing to step by)."""


class DocsPlan(PlanModel):
  description: str | None = None
  url: str | None = None
  notes: list[str] = []


class EndpointPlan(PlanModel):
  """One leaf of the function tree."""
  path: list[str]
  """Function path, last segment the method name."""
  kind: Literal['rpc', 'stream']
  transports: list[Literal['http', 'ws']]
  wire: WirePlan
  core: str
  """Symbolic core name, from the nearest `router.json`."""
  auth: Literal['public', 'required'] | None = None
  """Reserved (architecture review, item 4): no spec field declares it yet."""
  meta: dict[str, Any] = {}
  deprecated: bool = False
  request: RequestPlan
  response: ResponsePlan
  stream: StreamPlan | None = None
  pagination: PaginationPlan | None = None
  types: TypeSet = {}
  """The endpoint module's own types: `Request`/`Parameters`, the returned value, and
  every titled record nested inside them, named as a backend defines them."""
  wire_types: TypeSet = {}
  """The wire body's types, only when an envelope selects a payload inside it: the frame
  a core unwraps, which no generated method returns."""
  docs: DocsPlan = DocsPlan()

  @property
  def function(self) -> str:
    """The dotted function path."""
    return '.'.join(self.path)


class PackagePlan(PlanModel):
  """The whole package, computed once from a `Project`."""
  name: str
  """`[project].name`."""
  root_class: str
  """The root client class: `[python].name`, or PascalCase of `name`."""
  cores: dict[str, CorePlan] = {}
  schemas: dict[str, TypeSet] = {}
  """Shared types per scope: `''` for `spec/schemas.json`, `a/b` for
  `spec/endpoints/a/b/schemas.json`."""
  routers: list[RouterPlan] = []
  endpoints: list[EndpointPlan] = []

  def endpoint(self, function: str) -> EndpointPlan | None:
    """The plan for one dotted function path, or `None`."""
    for endpoint in self.endpoints:
      if endpoint.function == function:
        return endpoint
    return None

  def to_json(self) -> dict[str, Any]:
    """The plan as plain JSON data, camelCase keys, `null` fields omitted."""
    return self.model_dump(mode='json', by_alias=True, exclude_none=True)
