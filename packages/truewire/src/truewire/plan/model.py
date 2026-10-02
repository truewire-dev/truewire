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
  """The class a backend names the child after: PascalCase of `name`, or for a router the
  `class` its `router.json` declares. A backend may still rename an endpoint class that
  collides with a type in the same module."""


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
  """The request side of an endpoint: a flat object, one union, one array, nothing, or (for
  a gRPC endpoint) the proto message `grpc.request` names."""
  shape: Literal['none', 'fields', 'union', 'array', 'message']
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
  reply: str | None = None
  """The subscribe acknowledgement's type (`spec.reply`, ADR 0014): a `types` entry, or a
  shared scope's. `null` when the stream declares none, and the reply stays untyped."""


class PaginationPlan(PlanModel):
  """Everything a backend decides before it renders a page walker."""
  strategy: Literal['page', 'token', 'seek', 'offset']
  driver: str
  """Wire name of the request parameter the walk advances (page index, cursor, offset,
  or a `seek` walk's anchored bound)."""
  driver_required: bool
  """Whether a `token` cursor is required on the single call, so the first page is
  seeded from the caller's own value rather than a zero value."""
  size: str | None = None
  """Wire name of the page-size parameter, when declared and present on the request."""
  size_default: int | None = None
  """The API's documented default page size, from the size property's own `default`."""
  size_maximum: int | None = None
  """The largest page size the API serves, from the size property's own `maximum`: a caller
  asking for more gets a page full at this many rows, so a walk measures a short page (or a
  `seek` walk's full page) against it."""
  start: int | None = None
  """`index.start` for a `page` walk."""
  done: dict[str, Any]
  """The declared terminator, as written; `{}` for a `seek` walk, which has none (ADR 0013)."""
  rows: str | None = None
  """`done.rows` (or a `seek` walk's own `rows`): the response path holding the page's
  rows, when declared."""
  cursor_from: str | None = None
  """`cursor.from` of a `token` walk: the response path the next cursor is read from."""
  seek: dict[str, Any] | None = None
  """A `seek` walk's declaration beyond `rows`/`size` (ADR 0013): `cursor` (`field`,
  `unique`), `bound` (`start`/`end`), `anchor`, `cap`, `span`, `exclusive` (`parameters`,
  `first`, `far`: the parameters sent on the first request only, `null` when none), plus
  the derived `moving` and `far` bound names and `descending`."""
  row_type: Type | None = None
  """One row's type, resolved through `rows` (or the payload itself when it is the
  collection); `null` when the tree cannot name it."""
  state_type: Type | None = None
  """The walk state's type: the driver parameter's own type without its `null`."""
  cursor_type: Type | None = None
  """A `seek` walk's cursor field on one row, its own type without `null`: what a backend
  parses a raw row value through before comparing it with the bound, since the two may
  declare different timestamp formats (a row's `epoch-seconds` under an `epoch-millis`
  bound). `null` for any other strategy, or when the tree cannot name the field."""
  seedable: bool
  """Whether a `PaginatedResponse`-shaped walker can seed its first state: any strategy
  but `token`, a cursor with a zero value, or a required cursor. A `seek` walk always
  can: its first state is the caller's own bound, or none."""
  walker: Literal['paginated', 'generator', 'none']
  """`paginated`: rows and a seedable state, so the walker exposes pages and a state to
  resume from. `generator`: a plain async iterator of responses. `none`: the declaration
  cannot be walked (an `offset` walk with nothing to step by)."""


class ProtoTypePlan(PlanModel):
  """A proto type a gRPC endpoint names, resolved against the project's `spec/proto/` tree."""
  kind: Literal['scalar', 'message', 'enum']
  name: str
  """A scalar's proto name (`bytes`, `uint64`), or a message's or enum's fully-qualified
  name without the leading dot (`cosmos.base.query.v1beta1.PageRequest`)."""
  file: str | None = None
  """The `.proto` declaring it, relative to `spec/proto/`; `null` for a scalar. A well-known
  type (`google.protobuf.Timestamp`) names the file every toolchain ships."""
  package: str | None = None
  """The proto package declaring it, so a backend spells a nested type from its path inside
  the package (`SearchResponse.Hit`); `null` for a scalar or a name the tree lacks."""


class ProtoFieldPlan(PlanModel):
  """One field of a proto message."""
  name: str
  """The field's proto name (`next_key`), which the proto JSON mapping also accepts."""
  json_name: str
  """Its JSON name (`nextKey`)."""
  number: int
  type: ProtoTypePlan
  """The element type for a `repeated` field, the value type for a map."""
  repeated: bool = False
  map_key: str | None = None
  """The key scalar of a `map<K, V>` field."""
  presence: bool = False
  """Whether the field tells absent from its zero value: a message, a proto3 `optional`,
  or a scalar the endpoint lists in `optional_scalars`."""
  optional: bool = False
  """Whether the field is declared `optional` (explicit presence in the stubs: a pointer in
  Go, an optional property in TypeScript)."""
  oneof: str | None = None


class GrpcPagingPlan(PlanModel):
  """The dotted paths a gRPC endpoint's `pagination` declares, each resolved hop by hop
  through the request or response message: a backend reads a hop's type to build nested
  messages and to spell a cursor's state (`bytes`, `uint64`)."""
  driver: list[ProtoFieldPlan]
  """The request path the walk advances (`pagination.key`, or `page`)."""
  size: list[ProtoFieldPlan] | None = None
  """The request path of the page size (`pagination.limit`)."""
  cursor: list[ProtoFieldPlan] | None = None
  """The response path the next cursor is read from, for a `token` walk (`pagination.next_key`)."""
  rows: list[ProtoFieldPlan] | None = None
  """The response path of the page's rows (a repeated field)."""
  total: list[ProtoFieldPlan] | None = None
  """The response path of a `total` terminator."""


class GrpcPlan(PlanModel):
  """What a gRPC endpoint adds (ADR 0017): the service method and its proto types.

  `wire.path` is the call's HTTP/2 path (`/<service>/<rpc>`). The request is the whole
  `request` message (`RequestPlan.shape` is `message`) and the method returns the whole
  `response` message; both are rendered by the language's protobuf stubs, built from the
  same `spec/proto/` tree (`truewire protos`), never by the plan's type tree.
  """
  service: str
  """Fully-qualified service (`cosmos.bank.v1beta1.Query`)."""
  service_file: str
  """The `.proto` declaring the service, relative to `spec/proto/`."""
  rpc: str
  """The method's proto name (`AllBalances`)."""
  streaming: Literal['unary', 'server', 'client', 'bidi'] = 'unary'
  request: ProtoTypePlan
  response: ProtoTypePlan
  request_fields: list[ProtoFieldPlan] = []
  optional_scalars: list[str] = []
  paging: GrpcPagingPlan | None = None
  """`pagination`'s paths, resolved; `null` without a pagination or when a path does not
  resolve (then `pagination.walker` is `none`)."""


class DocsPlan(PlanModel):
  description: str | None = None
  url: str | None = None
  notes: list[str] = []


class EndpointPlan(PlanModel):
  """One leaf of the function tree."""
  path: list[str]
  """Function path, last segment the method name."""
  kind: Literal['rpc', 'stream', 'grpc']
  transports: list[Literal['http', 'ws']]
  """Empty for a gRPC endpoint, whose transport is HTTP/2 by definition."""
  wire: WirePlan
  core: str
  """Symbolic core name, from the nearest `router.json`."""
  auth: Literal['public', 'required'] | None = None
  """Reserved (architecture review, item 4): no spec field declares it yet."""
  surface: Literal['handwritten'] | None = None
  """`handwritten` when the spec declares `surface: {kind: handwritten}`: a backend that
  honours it renders no method and the project supplies one (`[python.extras]`,
  `[typescript.extras]`). An `absent` endpoint has no plan at all."""
  meta: dict[str, Any] = {}
  deprecated: bool = False
  refused: bool = False
  """Named in `[policy].refuse` (W15): every generated method fails with the package's
  `RefusedByPolicy` before any request is made."""
  request: RequestPlan
  response: ResponsePlan
  stream: StreamPlan | None = None
  grpc: GrpcPlan | None = None
  """The service method and proto types, for `kind: grpc` (ADR 0017)."""
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
