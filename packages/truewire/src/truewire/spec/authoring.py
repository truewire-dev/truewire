"""
Audit endpoint specs against the spec-authoring contract.

`docs/spec/authoring.md` is the source of truth. Every mechanizable clause of
its rules becomes one check here, and each check reports one violation per offending
schema, parameter, or property so that counts state the real size of the debt rather
than the number of files it is spread across.

Every violation also carries a `severity` (see `severity` and `WARNING_RULES`): most rules
are `error` and fail gate 1, but a rule whose own name-based heuristic can flag a spec that
is already correct reports `warning` instead, so gate 1 fails on `0` unacknowledged errors
rather than forcing an author to fabricate data to reach `0` violations.

What is deliberately not checked is recorded in `UNCHECKED`.
"""
import json
import re
from pathlib import Path
from typing_extensions import Any, Iterator, Literal, TypedDict

from jsonschema import Draft202012Validator

from truewire.generation.schema import Schema
from truewire.generation.types import unrenderable_cycles
from truewire.project import Project, resolve, spec_dir as project_spec_dir
from .endpoint import (
  Endpoint, GrpcEndpointSpec, Pagination, PaginationParameter, RpcEndpointSpec,
  StreamEndpointSpec, last_row_field, path_segments,
)
from .repo import load_endpoint, load_shared_schemas
from .request import PLACEHOLDER
from .router import load_router

Rule = Literal[
  'error-responses', 'title', 'title-empty-object', 'enum', 'const', 'positional-rows',
  'unions', 'description', 'pagination', 'identifier-templating', 'ws-verb',
  'timestamp-format', 'reserved-param', 'meta-collision', 'meta-schema',
  'router-core-missing', 'schemas-shadowing', 'mixed-leaf-router', 'envelope',
  'schema-cycle',
]
"""Mechanizable checks of the spec-authoring contract, in contract order."""

Severity = Literal['error', 'warning']
"""Whether a violation fails gate 1 (`error`) or only flags it for a human (`warning`)."""

WARNING_RULES: frozenset[Rule] = frozenset(
  {
    'enum', 'ws-verb', 'timestamp-format', 'reserved-param', 'title-empty-object',
  }
)
"""Rules that report as `warning` rather than `error`.

`enum` fires on field *names* that usually denote a closed set — `type`, `state`, `side`,
`status` — not on a proven one, so it is a heuristic and it overshoots by design: when the
API names such a field but never publishes its values, a bare `string`/`integer` is the
correct spec, and a guessed `enum` becomes a `Literal` that rejects values the API later
sends. Nothing in a spec can tell "closed set the author missed" from "the API never
closed this set" apart, so the check cannot fail the gate on its own say-so — it can only
ask. See `docs/spec/authoring.md` rule 2.

`ws-verb` is a different kind of warning: unlike `enum`, it is never in doubt -- every
`kind: 'stream'` endpoint has subscribe/unsubscribe semantics, so an absent `verb` is always
a real gap, not a heuristic overshoot. It warns for a rollout reason instead: the check
landed alongside the `verb` field itself, and existing projects still need a real
per-endpoint migration pass before the corpus is clean. Promoting it to `error` is that
migration's last step, not a permanent exception -- see ADR 0004.

`timestamp-format` is an `enum`-shaped heuristic, not a proven one: it fires on a field
*name* that usually holds a wire timestamp (`startTime`, `createdAt`, `blockTimestamp`, a
bare `datetime`) but declares no `epoch-*`/`date-time` format. A name can be wrong the same
two ways `enum`'s can — a field that merely contains a timestamp-shaped word without being
one (nothing observed yet, but `check_enums`' own history says to expect it) — so it only
asks. See `docs/spec/authoring.md` rule 3; the audit that motivated this found whole
projects with no declared format at all, invisible to `truewire check` before this rule
existed.

`reserved-param` is the third kind: never a heuristic overshoot (a parameter really is
named `validate`, or it is not) but also never fixable from the spec side, the same way
`ws-verb` is never fixable by rewriting a schema. The wire parameter has to keep the API's
own name — an `AddOrder` really may call its dry-run flag `validate` — so the fix
lives in the codegen backend that resolves the generated Python parameter name, not in the
spec. It warns rather than blocking gate 1 because no backend has a declarative
way to record that rename yet; promoting it to `error` waits on that mechanism, not on
authors fixing their specs. See `docs/production_standards.md` S8.

`title-empty-object` is never in doubt either -- an empty-object schema with no `title`
always gets a placeholder generated name -- but it warns for the same rollout reason as
`ws-verb`: it is a newly-widened half of an existing rule (`title`), and every
currently-clean title-less empty-object schema across existing projects flips to a violation the
moment it lands. See `docs/production_standards.md` S6.

`mixed-leaf-router` was a rollout warning of the same shape as `ws-verb`/`title-empty-object`
and is no longer one: promoted to `error` once the only offending project restructured its
8 mixed directories (`docs/spec/authoring.md` rule 16, standard S30) and 0 occurrences
remained. A directory carrying its own `endpoint.json` alongside an
endpoint-bearing descendant subdirectory has no name left to render its own leaf's method
under except the S29-forbidden `__call__`, and is never a heuristic overshoot the way
`enum`/`timestamp-format` can be -- so once the corpus was clean there was no reason left to
keep it as only a warning, unlike `reserved-param` (which stays one for a different reason:
no backend has a declarative fix for it yet).
"""

def severity(violation: 'Violation') -> Severity:
  """
  Whether one reported violation fails gate 1 or only flags it.

  A pure function of `violation['rule']`: today only `enum` warns, but the split lives
  here rather than at each `Violation(...)` call site so a rule's severity is stated once.

  Args:
    violation: One reported breach of the spec-authoring contract.
  """
  return 'warning' if violation['rule'] in WARNING_RULES else 'error'

RULE_HEADINGS: dict[Rule, str] = {
  'error-responses': '0. Endpoint shape — keep 2xx responses only',
  'title': '1. Title every object schema',
  'title-empty-object': (
    '1. Title every object schema — including an empty one (production_standards.md S6)'
  ),
  'enum': '2. Closed sets use `enum`',
  'const': '2. Closed sets use `enum` — prefer a single-value `enum` over `const`',
  'positional-rows': '3. Positional rows use `prefixItems`, bounded by `minItems`/`maxItems`',
  'unions': '4. Unions are `anyOf`, and only `anyOf`',
  'envelope': '6. Schemas describe the wire body — `envelope.payload` names a property of the response schema',
  'description': '7. Describe everything that becomes a docstring',
  'pagination': '8. Pagination is declared, not inferred',
  'identifier-templating': (
    'ADR 0006 — `in: "path"` parameters must match `{name}` in the operation identifier'
  ),
  'ws-verb': (
    'ADR 0004 — a `kind: "stream"` endpoint declares how its frames state subscribe/'
    'unsubscribe intent'
  ),
  'timestamp-format': '3. Timestamp parameters carry their real wire format',
  'reserved-param': (
    'production_standards.md S8 — a wire parameter must not collide with a name the '
    'generated method reserves for itself'
  ),
  'meta-collision': (
    'a resolved core\'s declared `meta` schema must not share a property '
    'name with the endpoint\'s own `request`/`parameters` schema'
  ),
  'meta-schema': (
    '`endpoint.meta` must validate against its resolved core\'s declared '
    '`meta` JSON Schema'
  ),
  'mixed-leaf-router': (
    '16. A directory is a leaf endpoint or a router grouping, never both'
  ),
  'schemas-shadowing': (
    'no two `schemas.json` scopes on the same path to root may declare the '
    'same id'
  ),
  'schema-cycle': (
    '17. A schema may reference itself, through a record'
  ),
}
"""Contract heading each check is derived from, for reporting."""

UNCHECKED: dict[str, str] = {
  '0. Endpoint shape': (
    'whether an operation is one self-contained upstream call, whether a `$ref` into '
    'schemas.json is shared by more than one endpoint, and whether a `function` segment '
    'is idiomatically short are editorial judgements with no spec-local signal'
  ),
  '1. Title every object schema': (
    'a title is checked for presence, not for being PascalCase, semantic and idiomatic'
  ),
  '2. Closed sets use `enum`': (
    'a closed set stated only in prose cannot be read out of a description; the name '
    'vocabulary in `CLOSED_SET_NAMES` is a lower bound on the real gap'
  ),
  '5. Schemas describe the unwrapped payload': (
    'entirely unchecked. Whether a schema is the envelope or the payload is only '
    'decidable against the upstream API, and the rule that actually blocks codegen — be '
    'consistent within a project — is a property of a project, not of an endpoint, so no '
    'per-endpoint audit can see it'
  ),
  '7. Pagination is declared, not inferred': (
    'whether an endpoint paginates at all. Nothing in a spec says so, and the parameter '
    'names that hint at it are exactly the sniffing the declaration exists to replace — '
    '`limit` alone sits on dozens of endpoints that paginate in no useful sense. Only a '
    'declaration that is present is checked, so a missing one is silence, not a pass. '
    'Two further clauses are enforced by the model rather than reported here, because an '
    'endpoint carrying them never loads: a strategy holding another strategy\'s fields, '
    'and a response path that is not a plain dotted key. A declared path is also left '
    'unjudged when the payload it walks is a `$ref` or an `anyOf`, since the operation '
    'alone resolves neither, and pagination on a gRPC endpoint is unchecked because '
    'there is no operation to name parameters against. A `window` declaration is checked '
    'for naming real bound parameters and for those bounds being typed for arithmetic — '
    'numerically, or as a `date-time`-formatted string, since a `window` bound alone may '
    'render to a `datetime` — and nothing more: whether those bounds are inclusive, which '
    'is what `step` is set from, and which way the API sorts, which is what `order` is '
    'set from, are facts about the API that were settled by calling it. No operation '
    'states either, so a wrong `step` or `order` reads as a clean spec and shows up as a '
    'duplicated or skipped row at runtime. The arithmetic check itself only reads a `type` '
    '(or `format`) the operation states outright: a parameter schema that is a `$ref`, an '
    '`anyOf`, or types nothing at all is left unjudged, for the reason `resolves` leaves a '
    '`$ref` payload unjudged. A `size` '
    'parameter is not checked either. It is the one pagination parameter the walk never '
    'computes — the caller\'s own value is passed straight through — and it only reaches '
    'arithmetic under some terminators, so requiring a number of it everywhere would flag '
    'declarations that never touch it. Nor is a `window` flagged for declaring no `size`, '
    'or a `size` carrying no schema `default`, though either costs it its truncation '
    'guard. Both say the API published nothing to declare — no page size documented at '
    'all, or a `limit` documented without a default — and a check firing on them would '
    'push an author to invent the number, which rule 2 forbids for the same reason: a '
    'guessed default stops a walk the API answered in full. The generated walk already '
    'states the gap, by '
    'carrying no guard and promising none, and the endpoint `notes` record why'
  ),
}
"""Clauses this module does not check, and why."""

CLOSED_SET_NAMES = frozenset({
  'accounttype', 'action', 'category', 'currencytype', 'direction', 'granularity',
  'interval', 'kind', 'marginmode', 'ordermode', 'orderside', 'orderstatus', 'ordertype',
  'ordstatus', 'period', 'positionside', 'resolution', 'side', 'state', 'status', 'tif',
  'timeinforce', 'transfertype', 'triggerby', 'triggertype', 'type',
})
"""Field names that usually denote a closed set of values across real APIs."""

SCALAR_TYPES = frozenset({'string', 'integer', 'number'})

KEYLESS_TYPES = frozenset({'string', 'integer', 'number', 'boolean', 'null'})
"""JSON Schema types that decidably carry no property, for a dotted-path walk."""
"""Types on which a closed set would be declared."""

TIMESTAMP_FORMATS = frozenset({
  'epoch-seconds', 'epoch-millis', 'epoch-micros', 'epoch-nanos', 'date-time', 'date',
})
"""Formats rule 3 accepts as stating a field's real wire timestamp shape."""

TIMESTAMP_NAME_TOKENS = frozenset({'time', 'timestamp', 'date', 'datetime', 'ts', 'since', 'at'})
"""Last-word tokens (after splitting a field name at snake_case/camelCase boundaries) that
usually name a wire timestamp.

Checked against the *last* word only, never any word: `timeInForce` splits to `time`/`in`/
`force`, and matching any word would flag that field itself — a documented `Literal` closed
set (rule 2), not a timestamp — on every API that has one. A last-word match still misses
a genuine suffix variant (`startTimeMs`); see `WARNING_RULES` for why an undershoot here is
the accepted tradeoff, same as rule 2's overshoot."""

WORD_BOUNDARY = re.compile(r'[_\-]+|(?<=[a-z0-9])(?=[A-Z])')
"""Splits a field name at an underscore/hyphen run or a lower-to-upper camelCase transition."""

RESERVED_PARAM_NAMES = frozenset({'validate'})
"""Wire parameter names that collide with a keyword every generated endpoint method
reserves for itself. `validate: bool | None = None` is the one keyword every backend in
this repo is required to emit (`docs/production_standards.md` S8) — a wire parameter
sharing that name is silently shadowed rather than sent. A real `AddOrder`/`EditOrder`
pair once reused it for an upstream dry-run flag, and the flag was never threaded to the
transport's own `validate` at all: `validate=True` stopped meaning "validate the response"
for exactly the highest-stakes write endpoints in that project."""

NUMERIC_TYPES = frozenset({'integer', 'number'})
"""Types the arithmetic a paginated walk does on a request parameter is defined on."""

TYPING_KEYS = (
  '$ref', 'type', 'properties', 'items', 'prefixItems', 'enum', 'const',
  'anyOf', 'oneOf', 'allOf', 'additionalProperties',
)
"""Keys through which a schema can constrain the value it describes."""

WS_RESPONSE_KEYS = frozenset({'reply', 'message'})
"""Pseudo-status keys the websocket spec shape uses instead of HTTP statuses."""

class Violation(TypedDict):
  """One breach of the spec-authoring contract."""
  rule: Rule
  location: str
  """
  Dotted path of the offending node inside the `spec.openapi` operation, or inside the
  endpoint's `pagination` block, which is a sibling of `spec` rather than part of it.
  """
  message: str

def restore_refs(value: Any) -> Any:
  """
  Undo the lossy `$ref` alias of `truewire.generation.schema.Reference`.

  That model serializes its `$ref` field under the alias `ref`, so a plain
  `model_dump` no longer round-trips. No schema in any client spec carries a literal
  `ref` key, which makes the rename unambiguous.

  Args:
    value: Decoded JSON value from a model dump.
  """
  if isinstance(value, dict):
    return {
      ('$ref' if key == 'ref' and isinstance(item, str) else key): restore_refs(item)
      for key, item in value.items()
    }
  if isinstance(value, list):
    return [restore_refs(item) for item in value]
  return value

def operation_json(endpoint: Endpoint) -> dict[str, Any] | None:
  """
  Plain-JSON view of an endpoint's operation -- from `spec.openapi` (legacy) or synthesized
  from the new request/response-equivalent shape -- or `None` for gRPC.

  For a `StreamEndpointSpec`, the subscribe-side schema is `request` if set, else
  `parameters` (the two name the same thing -- docs/spec/authoring.md rule 0), and the
  response-side schema is `payload` (the pushed-message schema), not `response`. A
  push-only stream endpoint (rule 11: no subscribe frame at all, only `payload` set) still
  synthesizes -- it is new-shape the moment any of `request`/`parameters`/`payload` is set,
  not only when a request-side schema is present.

  Args:
    endpoint: Endpoint record loaded from an `endpoint.json`.
  """
  spec = endpoint.spec
  if isinstance(spec, GrpcEndpointSpec):
    return None
  if isinstance(spec, RpcEndpointSpec):
    request_schema = spec.request
    response_schema = spec.response
    new_shape = request_schema is not None or response_schema is not None
    description = spec.description
  elif isinstance(spec, StreamEndpointSpec):
    request_schema = spec.request if spec.request is not None else spec.parameters
    response_schema = spec.payload
    new_shape = spec.request is not None or spec.parameters is not None or spec.payload is not None
    description = spec.description
  else:
    new_shape = False
    description = None
  if new_shape:
    return {
      'description': description,
      'request': request_schema,
      'responses': {'200': {
        'description': 'Success',
        'content': {'application/json': {'schema': response_schema}},
      }} if response_schema is not None else {},
    }
  operation = endpoint.openapi
  if operation is None:
    return None
  dumped = operation.model_dump(mode='json', by_alias=True, exclude_none=True)
  return restore_refs(dumped)

def nodes(
  value: Any, path: str = '', name: str | None = None, *, is_properties: bool = False,
) -> Iterator[tuple[str, str | None, dict[str, Any]]]:
  """
  Every schema-shaped mapping in a JSON tree, with its path and the field name it describes.

  The walk is deliberately untyped so that schema-shaped nodes are found wherever they
  sit, including the off-spec `responses.<status>.schema` that some upstream documents
  leak into an endpoint.

  A `properties` map is **not** yielded. It is a mapping of field name to schema, not a
  schema, and every check here reads its node as a schema — so yielding it made an API
  that happens to name a field `type`, `properties` or `title` fail rules it complies
  with. One real API's NFT records carry a Metaplex field literally named
  `properties`, and the map holding it was read as a schema whose own `anyOf`, `allOf`,
  `prefixItems` and `nullable` keywords were then reported as undescribed response
  properties. Fourteen violations against a correct spec, unfixable from the spec side.

  Whether a child is a properties map is decided by the parent as it descends, not by the
  shape of the path: a field named `properties` sits at a path ending in `.properties`
  too, which is exactly what the previous path-suffix test could not tell apart.

  Args:
    value: Decoded JSON value.
    path: Dotted path of `value` inside the operation.
    name: Property or parameter name `value` describes, when it has one.
    is_properties: Whether `value` is a `properties` map rather than a schema.
  """
  if isinstance(value, dict):
    if not is_properties:
      yield path or '<operation>', name, value
    parameter = value.get('name')
    for key, child in value.items():
      if is_properties:
        child_name = key
      elif key == 'schema' and isinstance(parameter, str):
        child_name = parameter
      else:
        child_name = name
      yield from nodes(
        child, f'{path}.{key}' if path else key, child_name,
        is_properties=key == 'properties' and not is_properties,
      )
  elif isinstance(value, list):
    for index, child in enumerate(value):
      yield from nodes(child, f'{path}[{index}]', name)

def is_ref(value: Any) -> bool:
  """
  Return whether a node is a `$ref` rather than an inline definition.

  Args:
    value: Decoded JSON value.
  """
  return isinstance(value, dict) and isinstance(value.get('$ref'), str)

def declares_type(schema: Any) -> bool:
  """
  Return whether a schema constrains its value at all.

  An empty schema generates as `Any`, and `{}` survives a round trip through the
  parser as a `Schema` carrying only defaults, so emptiness is decided by the typing
  keywords rather than by the size of the mapping.

  Args:
    schema: Decoded JSON value sitting in a schema position.
  """
  if not isinstance(schema, dict):
    return schema is not None
  return any(schema.get(key) for key in TYPING_KEYS)

def is_array(schema: dict[str, Any]) -> bool:
  """
  Return whether a schema describes an array.

  Args:
    schema: Decoded schema node.
  """
  declared = schema.get('type')
  if declared == 'array' or bool(schema.get('prefixItems')):
    return True
  return isinstance(declared, list) and 'array' in declared

def normalize(name: str) -> str:
  """
  Fold a field name to lowercase alphanumerics, so `time_in_force` meets `timeInForce`.

  Args:
    name: Property or parameter name as written in the spec.
  """
  return re.sub(r'[^a-z0-9]', '', name.lower())

def check_error_responses(operation: dict[str, Any]) -> list[Violation]:
  """
  Rule 0: keep 2xx responses only; errors belong to the client core.

  Args:
    operation: Plain-JSON `spec.openapi` operation.
  """
  out: list[Violation] = []
  for status in operation.get('responses') or {}:
    if status in WS_RESPONSE_KEYS or status.startswith('2'):
      continue
    out.append(Violation(
      rule='error-responses',
      location=f'responses.{status}',
      message=(
        f'response `{status}` is not a 2xx; errors belong to the client core, not to '
        f'every endpoint schema'
      ),
    ))
  return out

def check_titles(operation: dict[str, Any]) -> list[Violation]:
  """
  Rule 1: every schema with a non-empty `properties` map carries a `title`.

  `title` is the generated type name. Without it the generator derives one from the
  schema's position and a per-project rename table has to undo that.

  Also flags an empty-object schema (`type: 'object'`, no `properties` or an empty one,
  and no `additionalProperties` -- which would make it a map rather than a record) with no
  `title`: codegen invents a placeholder type name for it exactly the same way, a `ping`
  endpoint's `Response200` being the real case that motivated this half of the
  check. Reported under a separate `title-empty-object` rule, `warning`-severity, rather
  than folded into `title` itself: widening `title` outright would immediately flip every
  currently-clean title-less empty-object schema across all sixteen clients to `error`, and
  `docs/production_standards.md` S6 asks for a staged `warning` rollout first, the same
  shape `ws-verb` used.

  Args:
    operation: Plain-JSON `spec.openapi` operation.
  """
  out: list[Violation] = []
  for path, _name, node in nodes(operation):
    if node.get('title'):
      continue
    properties = node.get('properties')
    if isinstance(properties, dict) and properties:
      out.append(Violation(
        rule='title',
        location=path,
        message=(
          f'object schema with {len(properties)} properties has no `title`; the generated '
          f'type will be named after its position in the document'
        ),
      ))
    elif node.get('type') == 'object' and not node.get('additionalProperties'):
      out.append(Violation(
        rule='title-empty-object',
        location=path,
        message=(
          'empty-object schema has no `title`; codegen will invent a placeholder type '
          'name for it (e.g. `Response200`) -- docs/production_standards.md S6'
        ),
      ))
  return out

def check_enums(operation: dict[str, Any]) -> list[Violation]:
  """
  Rule 2: a value with a documented finite set of values declares `enum`.

  Prose cannot be read, so this checks the mechanizable half: a scalar named after a
  closed set that declares neither `enum` nor `const`. The name alone cannot tell a
  genuinely closed set apart from an API that never publishes one, so this reports as
  a `warning` (see `severity`) that asks the author to check rather than a fault that
  says they got it wrong — `docs/spec/authoring.md` rule 2 explains why the
  heuristic overshoots.

  Args:
    operation: Plain-JSON `spec.openapi` operation.
  """
  out: list[Violation] = []
  for path, name, node in nodes(operation):
    if name is None or normalize(name) not in CLOSED_SET_NAMES:
      continue
    declared = node.get('type')
    if not isinstance(declared, str) or declared not in SCALAR_TYPES:
      continue
    if node.get('enum') or 'const' in node:
      continue
    out.append(Violation(
      rule='enum',
      location=path,
      message=(
        f'`{name}` may be a closed set. Declare `enum` if the API documents its '
        f'values; leave it bare if it does not — a guessed `enum` becomes a `Literal` '
        f'that rejects values the API later sends'
      ),
    ))
  return out

def check_consts(operation: dict[str, Any]) -> list[Violation]:
  """
  Rule 2: prefer a single-value `enum` over `const`.

  The generator emits a literal for `const` now, so nothing is lost by writing one. The
  preference is for uniformity: every closed set in a spec reads as `enum`.

  Args:
    operation: Plain-JSON `spec.openapi` operation.
  """
  out: list[Violation] = []
  for path, _name, node in nodes(operation):
    if 'const' not in node or node.get('enum'):
      continue
    out.append(Violation(
      rule='const',
      location=path,
      message=(
        f'`const: {node["const"]!r}` states a closed set of one; write '
        f'`enum: [{node["const"]!r}]` so every closed set in the spec reads the same way'
      ),
    ))
  return out

def _words(name: str) -> list[str]:
  """
  Split a field name into lowercase words at snake_case/camelCase boundaries.

  Args:
    name: Property or parameter name as written in the spec.
  """
  return [word.lower() for word in WORD_BOUNDARY.split(name) if word]

def check_timestamp_format(operation: dict[str, Any]) -> list[Violation]:
  """
  Rule 3: a wire timestamp declares its real format, not a bare `string`/`integer`.

  Same shape as `check_enums`: the name heuristic is a lower bound, not a proof, so a hit
  reports as a `warning` that asks the author to check rather than an error that says they
  got it wrong. Unlike `check_enums`, this reads every scalar node in the operation, request
  and response alike — rule 3 applies identically to both, and a request-side timestamp
  with no declared format is sent to the wire wrong, not merely rendered untyped.

  Args:
    operation: Plain-JSON `spec.openapi` operation.
  """
  out: list[Violation] = []
  for path, name, node in nodes(operation):
    if name is None or is_ref(node):
      continue
    words = _words(name)
    if not words or words[-1] not in TIMESTAMP_NAME_TOKENS:
      continue
    declared = node.get('type')
    if not isinstance(declared, str) or declared not in SCALAR_TYPES:
      continue
    if node.get('enum') or 'const' in node:
      continue
    if node.get('format') in TIMESTAMP_FORMATS:
      continue
    out.append(Violation(
      rule='timestamp-format',
      location=path,
      message=(
        f'`{name}` looks like a wire timestamp with no declared format; declare '
        f'`format: "epoch-millis"` (or `epoch-seconds`/`epoch-micros`/`epoch-nanos`, or '
        f'`date-time` on a string, or `date` for a plain calendar date) so it renders as a '
        f'real `datetime` instead of a bare `{declared}` -- docs/spec/authoring.md rule 3'
      ),
    ))
  return out

def check_reserved_names(operation: dict[str, Any]) -> list[Violation]:
  """
  A request parameter must not be named `validate`.

  `validate` is the keyword every generated endpoint method reserves for the per-call
  response-validation override (`docs/production_standards.md` S8). A wire parameter
  sharing that name — query, path, or a `requestBody` property — collides with it in the
  generated signature. Warns rather than errors: the fix is a codegen-side rename, and the
  spec is not wrong to use the API's own wire name for it. See `RESERVED_PARAM_NAMES`.

  Args:
    operation: Plain-JSON `spec.openapi` operation.
  """
  out: list[Violation] = []
  for name in sorted(request_parameters(operation)):
    if normalize(name) not in RESERVED_PARAM_NAMES:
      continue
    out.append(Violation(
      rule='reserved-param',
      location=f'parameters.{name}',
      message=(
        f'`{name}` collides with `validate`, the parameter every generated endpoint '
        f'method reserves for the response-validation override; the codegen backend must '
        f'resolve the generated Python parameter to a distinct, non-colliding name -- '
        f'docs/production_standards.md S8'
      ),
    ))
  return out

def check_positional_rows(operation: dict[str, Any]) -> list[Violation]:
  """
  Rule 3: fixed-length heterogeneous rows use `prefixItems` plus `minItems`/`maxItems`.

  Two mechanizable halves. An array that types nothing degrades to `list[Any]` and its
  real type gets hand-written; and `prefixItems` without a `maxItems` equal to the row
  length still validates a row that lost a column.

  Args:
    operation: Plain-JSON `spec.openapi` operation.
  """
  out: list[Violation] = []
  for path, _name, node in nodes(operation):
    if is_ref(node) or not is_array(node):
      continue
    prefix = node.get('prefixItems')
    if prefix:
      missing = [
        key for key in ('minItems', 'maxItems') if not isinstance(node.get(key), int)
      ]
      if missing:
        out.append(Violation(
          rule='positional-rows',
          location=path,
          message=(
            f'`prefixItems` row of {len(prefix)} is unbounded: {", ".join(missing)} '
            f'missing, so a row that lost a column still validates'
          ),
        ))
      elif node['maxItems'] != len(prefix):
        out.append(Violation(
          rule='positional-rows',
          location=path,
          message=(
            f'`maxItems` is {node["maxItems"]} but the row declares {len(prefix)} '
            f'positions; the row is not closed at its own length'
          ),
        ))
      continue
    if not declares_type(node.get('items')):
      out.append(Violation(
        rule='positional-rows',
        location=path,
        message=(
          'array types nothing; declare `prefixItems` with `minItems`/`maxItems` for a '
          'positional row, or `items` for a homogeneous list, never an empty schema'
        ),
      ))
  return out

def check_unions(operation: dict[str, Any]) -> list[Violation]:
  """
  Rule 4: unions are `anyOf`, and only `anyOf`.

  `Parser.one_of` and `Parser.all_of` raise `NotImplementedError`, and a nullable
  record written as `type: ['object', 'null']` raises `ValueError('Found nested record
  type')`.

  Args:
    operation: Plain-JSON `spec.openapi` operation.
  """
  out: list[Violation] = []
  for path, _name, node in nodes(operation):
    for keyword in ('oneOf', 'allOf'):
      if not node.get(keyword):
        continue
      out.append(Violation(
        rule='unions',
        location=path,
        message=(
          f'`{keyword}` raises NotImplementedError unless a backend happens to run '
          f'`Normalizer` first; write `anyOf`'
        ),
      ))
    declared = node.get('type')
    if isinstance(declared, list) and len(declared) > 1 and 'object' in declared:
      out.append(Violation(
        rule='unions',
        location=path,
        message=(
          f'`type: {declared}` routes a record through the multi-type path and raises '
          f"ValueError('Found nested record type'); write an `anyOf` branch per type"
        ),
      ))
  return out

def check_descriptions(operation: dict[str, Any]) -> list[Violation]:
  """
  Rule 6: describe the operation, parameters, request bodies, responses, and response
  object properties.

  Each of those lands in generated source as a docstring. A `$ref` carries its
  description at the referenced schema, so it is not required inline. A new-shape
  `request` schema has no `parameters`/`requestBody` to walk, so its flat
  `properties` are checked the same way rule 6 already requires for every other
  described field -- a titled `anyOf` `request` (a discriminated body) has no flat
  `properties` and is left unwalked here, the same way an `anyOf`-shaped legacy
  `requestBody` is.

  Args:
    operation: Plain-JSON operation, from `operation_json`.
  """
  out: list[Violation] = []
  if not operation.get('description'):
    out.append(Violation(
      rule='description',
      location='<operation>',
      message='operation has no `description`; it is the generated method docstring',
    ))
  request_schema = operation.get('request')
  if isinstance(request_schema, dict):
    properties = request_schema.get('properties')
    if isinstance(properties, dict):
      for field, schema in properties.items():
        if is_ref(schema) or (isinstance(schema, dict) and schema.get('description')):
          continue
        out.append(Violation(
          rule='description',
          location=f'request.properties.{field}',
          message=f'request property `{field}` has no `description`; it is its `Args:` bullet',
        ))
  for index, parameter in enumerate(operation.get('parameters') or []):
    if is_ref(parameter) or not isinstance(parameter, dict) or parameter.get('description'):
      continue
    label = parameter.get('name') or index
    out.append(Violation(
      rule='description',
      location=f'parameters[{index}]',
      message=f'parameter `{label}` has no `description`; it is its `Args:` bullet',
    ))
  body = operation.get('requestBody')
  if isinstance(body, dict) and not is_ref(body) and not body.get('description'):
    out.append(Violation(
      rule='description',
      location='requestBody',
      message='request body has no `description`',
    ))
  for status, response in (operation.get('responses') or {}).items():
    if not isinstance(response, dict) or is_ref(response):
      continue
    if not response.get('description'):
      out.append(Violation(
        rule='description',
        location=f'responses.{status}',
        message=f'response `{status}` has no `description`',
      ))
    for path, _name, node in nodes(response, f'responses.{status}'):
      properties = node.get('properties')
      if not isinstance(properties, dict):
        continue
      for field, schema in properties.items():
        if is_ref(schema) or (isinstance(schema, dict) and schema.get('description')):
          continue
        out.append(Violation(
          rule='description',
          location=f'{path}.properties.{field}',
          message=(
            f'response property `{field}` has no `description`; it is the generated '
            f'field docstring'
          ),
        ))
  return out

def json_schema(container: Any) -> dict[str, Any] | None:
  """
  The `application/json` schema of a request body or response, when it has one.

  Args:
    container: Decoded request body or response object.
  """
  if not isinstance(container, dict):
    return None
  content = container.get('content')
  if not isinstance(content, dict):
    return None
  media = content.get('application/json')
  if not isinstance(media, dict):
    return None
  schema = media.get('schema')
  return schema if isinstance(schema, dict) else None

def request_parameters(operation: dict[str, Any]) -> set[str]:
  """
  Every request parameter name an operation declares -- new `request` shape, or legacy
  `parameters`/`requestBody`.

  A pagination cursor rides in the body on a POST endpoint and in the query on a GET one,
  so both have to count under the legacy shape or the check would fire on half of a project's
  real declarations.

  Args:
    operation: Plain-JSON operation, from `operation_json`.
  """
  request_schema = operation.get('request')
  if isinstance(request_schema, dict):
    properties = request_schema.get('properties')
    return set(properties) if isinstance(properties, dict) else set()
  out: set[str] = set()
  for parameter in operation.get('parameters') or []:
    name = parameter.get('name') if isinstance(parameter, dict) else None
    if isinstance(name, str):
      out.add(name)
  body = json_schema(operation.get('requestBody'))
  properties = body.get('properties') if body else None
  if isinstance(properties, dict):
    out.update(properties)
  return out

def parameter_schema(operation: dict[str, Any], name: str) -> dict[str, Any] | None:
  """
  The schema an operation declares for one request parameter, new `request` shape or
  legacy `parameters`/`requestBody`.

  The sources are searched in the order `request_parameters` counts them, so a name that
  check accepts is a name this one can find a schema for.

  Args:
    operation: Plain-JSON operation, from `operation_json`.
    name: Parameter name, exactly as a pagination declaration writes it.
  """
  request_schema = operation.get('request')
  if isinstance(request_schema, dict):
    properties = request_schema.get('properties')
    if isinstance(properties, dict) and isinstance(properties.get(name), dict):
      return properties[name]
    return None
  for parameter in operation.get('parameters') or []:
    if not isinstance(parameter, dict) or parameter.get('name') != name:
      continue
    schema = parameter.get('schema')
    return schema if isinstance(schema, dict) else None
  body = json_schema(operation.get('requestBody'))
  properties = body.get('properties') if body else None
  if isinstance(properties, dict) and isinstance(properties.get(name), dict):
    return properties[name]
  return None

def is_arithmetic(schema: dict[str, Any], *, datetime_ok: bool = False) -> bool | None:
  """
  Whether a paginated walk can compute a value for a parameter typed by this schema, or
  `None` when the operation cannot say.

  A `$ref` resolves against `schemas.json` and an `anyOf` may type either branch, so
  neither is judged here for the same reason `resolves` refuses them, and a schema that
  states no `type` at all constrains nothing to judge. A multi-type parameter answers
  only when every type it admits but `null` agrees.

  `datetime_ok` widens this past numbers to a `string` typed `format: 'date-time'` — the
  Parser maps that straight to a stdlib `datetime`
  (`truewire.generation.python.types.parser.Parser.string`), and `datetime` supports the same
  `-`/`+` arithmetic a number does, through `timedelta` rather than a bare count. Only a
  `window` bound computes a `datetime` this way: a `page` index or an `offset` is always a
  fresh loop counter the walk itself seeds and increments, never a value read back out of
  the caller's own parameter, so it stays plain-numeric regardless of what the caller
  passed.

  Args:
    schema: Schema the operation declares for a request parameter.
    datetime_ok: Whether a `date-time`-formatted string also counts, i.e. whether this
      parameter is a `window` bound.
  """
  if is_ref(schema) or schema.get('anyOf'):
    return None
  if datetime_ok and schema.get('format') == 'date-time':
    return True
  declared = schema.get('type')
  if isinstance(declared, str):
    return declared in NUMERIC_TYPES
  if isinstance(declared, list):
    members = {item for item in declared if item != 'null'}
    if not members:
      return None
    if members <= NUMERIC_TYPES:
      return True
    if not members & NUMERIC_TYPES:
      return False
  return None

OVERLAP_ARITHMETIC_TIMESTAMP_FORMATS: dict[str, frozenset[str]] = {
  'window': TIMESTAMP_FORMATS,
  'seek': frozenset({'epoch-seconds', 'epoch-millis', 'epoch-micros'}),
}
"""Per-strategy formats an `overlap.field`'s own enclosing window bound / seek cursor can
carry that make a non-numeric row field arithmetic-safe anyway (rule 8's `overlap`,
`Generator.row_field_expression`). `window`'s own gating (`Generator.paged_window_bound`'s
default, keyed off `HttpRequest.TIMESTAMP_HELPERS`) recognizes all six timestamp formats;
`seek`'s (`TIMESTAMP_TICKS`) only the three with a fixed-tick `timedelta` to advance a
cursor by -- `epoch-nanos`/`date-time`/`date` have none, so a `seek` cursor in one of those
never actually converts a row field before comparing, and a non-numeric field there stays
genuinely unsafe."""

def overlap_field_arithmetic(
  field_schema: dict[str, Any], *, bound_schema: dict[str, Any] | None, strategy: str,
) -> bool | None:
  """
  Whether an `overlap.field` value is safe to compare with `>` and pass to `max()`.

  A bare number always is (`is_arithmetic`'s own existing verdict). A non-numeric field --
  most commonly a wire *string* (candle rows commonly carry their own timestamp this
  way) -- still is when the walk's own bound/cursor
  parameter is itself timestamp-formatted *and* the row field declares that identical
  format: `Generator.row_field_expression` converts every value it reads through the
  *bound*'s own converter (`truewire_core.times.ms.EpochConverter.parse` for an epoch format,
  accepting either a raw `int` or a numeral `str`) -- never the row field's own, so a row
  field genuinely encoded a different way (an ISO-8601 string row value under an
  epoch-millis bound, say) would be silently fed to the wrong converter. Requiring an exact
  format match, not just "some timestamp format on both sides", is what keeps that
  mismatch caught rather than approved (`docs/pagination.md`; the codegen half of this is
  Fix 2).

  Args:
    field_schema: `overlap.field`'s own resolved schema.
    bound_schema: The window bound (`pagination.bound.start`) or seek cursor
      (`pagination.cursor.parameter`) parameter's own declared schema -- `None` when it
      can't be resolved.
    strategy: `pagination.strategy` (`'window'` or `'seek'`).
  """
  verdict = is_arithmetic(field_schema)
  if verdict is not False:
    return verdict
  if bound_schema is None:
    return False
  bound_format = bound_schema.get('format')
  allowed = OVERLAP_ARITHMETIC_TIMESTAMP_FORMATS.get(strategy, frozenset())
  if bound_format not in allowed:
    return False
  return field_schema.get('format') == bound_format and field_schema.get('type') in ('string', 'integer')

def resolvable_size_cap(operation: dict[str, Any], size: PaginationParameter) -> bool:
  """
  Whether a declared `size` parameter's own schema already resolves a row cap to measure
  a full `window`+`overlap` chunk against, without `overlap.cap`.

  Mirrors `Generator.paged_size_default`'s own resolution at codegen time, restricted to
  the one signal available from the operation alone: a documented `default` on the size
  parameter's schema. A caller-always-required size with no default (the other half of
  `Generator.paged_always_set`'s own check) needs the resolved header parameter's real
  `required` flag, which this authoring-time check has no way to derive from a raw
  operation dict alone -- so it is not attempted here, and declaring `overlap.cap`
  explicitly on such an endpoint is always accepted rather than flagged as redundant.

  Args:
    operation: Plain-JSON operation, from `operation_json`.
    size: `pagination.size`.
  """
  schema = parameter_schema(operation, size.parameter)
  default = schema.get('default') if schema is not None else None
  return isinstance(default, int)

def payload_schemas(operation: dict[str, Any]) -> list[dict[str, Any]]:
  """
  The success payload schema of every 2xx or websocket response.

  Args:
    operation: Plain-JSON `spec.openapi` operation.
  """
  out: list[dict[str, Any]] = []
  for status, response in (operation.get('responses') or {}).items():
    if status not in WS_RESPONSE_KEYS and not status.startswith('2'):
      continue
    schema = json_schema(response)
    if schema is not None:
      out.append(schema)
  return out

def _resolve_schema_path(
  schema: dict[str, Any], path: str,
) -> tuple[dict[str, Any] | None, bool | None]:
  """
  Walk a `docs/pagination.md` §3 dotted-key/bracket-index path through a JSON schema,
  shared by `resolves` (a payload-rooted path) and `row_field_schema` (a row-rooted one) --
  the only difference between the two is what they do with the (node, verdict) result.

  A dict-key segment steps through `properties`; a bracket-index segment steps through
  `prefixItems` (a tuple row's own positional schema, rule 4) when declared, falling back
  to a homogeneous array's `items` otherwise -- the same two shapes `row_item_schema`
  already resolves the outer *collection* through, one level up.

  Args:
    schema: Schema node the path is read relative to.
    path: A validated `docs/pagination.md` §3 path (its `last:`/`[-1]` prefix, if any,
      already stripped by the caller).

  Returns:
    `(node, True)` when every segment resolves to a real schema node; `(None, False)`
    when a segment is definitively absent from a schema that does declare the collection
    shape (a `properties`/`prefixItems`/`items` map that doesn't carry it); `(None, None)`
    when a `$ref`, an `anyOf`, or a schema that doesn't describe the collection shape at
    all leaves it undecidable -- the same "don't guess" stance every caller here takes.
  """
  node: Any = schema
  for kind, key in path_segments(path):
    if not isinstance(node, dict) or is_ref(node) or node.get('anyOf'):
      return None, None
    if kind == 'index':
      prefix_items = node.get('prefixItems')
      if isinstance(prefix_items, list):
        index = key if key >= 0 else len(prefix_items) + key
        if not (0 <= index < len(prefix_items)):
          return None, False
        node = prefix_items[index]
        continue
      items = node.get('items')
      if not isinstance(items, dict):
        return None, None
      node = items
      continue
    properties = node.get('properties')
    if not isinstance(properties, dict) or not properties:
      # A scalar-typed node decidably carries no key at all; anything else (a map, an
      # untyped node) might, and is left undecided.
      return None, (False if node.get('type') in KEYLESS_TYPES else None)
    if key not in properties:
      return None, False
    node = properties[key]
  return node, True

def resolves(schema: dict[str, Any], path: str) -> bool | None:
  """
  Whether a dotted-key/bracket-index path names a property of a payload, or `None` when
  undecidable.

  A `$ref` resolves against `schemas.json`, and an `anyOf` may or may not carry the field
  in the branch that arrives. The operation alone settles neither, so both answer `None`
  rather than guess — an audit that invents a verdict here would flag correct specs.

  Args:
    schema: Response payload schema.
    path: Dotted key, per `docs/pagination.md` §3 optionally carrying bracket indices.
  """
  if path == '':
    return True
  _, verdict = _resolve_schema_path(schema, path)
  return verdict

def row_item_schema(schema: dict[str, Any], rows: str | None) -> dict[str, Any] | None:
  """
  Resolve the item schema of the array a `seek`/`short_page`/`empty`/`unchanged` `rows`
  path names, or
  `None` when it cannot be resolved.

  This is one level past `resolves`: a `seek` cursor's `[-1]<...>` names a field of one
  *row*, not of the payload itself, so checking it needs the array's `items` schema rather
  than the payload's own properties. `None` covers everything undecidable along the way —
  a `$ref`/`anyOf` node at any segment, a segment missing from `properties`, or a resolved
  node that isn't a `type: array` with an object-shaped `items` — the same "don't guess"
  stance `resolves` takes.

  Args:
    schema: Response payload schema.
    rows: Dotted path to the row collection, or `None` when the payload is the collection.
  """
  node: Any = schema
  if rows:
    node, verdict = _resolve_schema_path(schema, rows)
    if not verdict:
      return None
  if not isinstance(node, dict) or is_ref(node) or node.get('anyOf') or node.get('type') != 'array':
    return None
  items = node.get('items')
  return items if isinstance(items, dict) else None

def row_field_schema(item: dict[str, Any], field: str) -> dict[str, Any] | None:
  """
  Resolve the schema of one dotted-key/bracket-index field of a row item, or `None` when
  it cannot be resolved -- a `$ref`/`anyOf` node at any segment, a segment missing from
  `properties`, or a bracket index out of a declared `prefixItems`' bounds.

  One level past `row_item_schema`: that resolves the *array*'s item schema, this resolves
  one *field* of it, the way `SeekOverlap.cap`'s and `WindowOverlap.cap`'s arithmetic
  checks need. A bracket-index segment (a candle's timestamp at a fixed tuple position,
  say) steps through `prefixItems` -- `docs/spec/authoring.md` rule 4's positional-row
  shape -- falling back to a homogeneous array's `items` when no `prefixItems` is declared.

  Args:
    item: Item schema `row_item_schema` resolved.
    field: A `LastRowPath`/`WindowOverlap.field`, its `[-1]` prefix already stripped by
      the caller (`last_row_field`).
  """
  node, verdict = _resolve_schema_path(item, field)
  return node if verdict else None

def pagination_parameters(pagination: Pagination) -> list[tuple[str, str]]:
  """
  Every request parameter a declaration names, as `(location, parameter name)`.

  Args:
    pagination: Declaration carried by the endpoint.
  """
  out: list[tuple[str, str]] = []
  if pagination.strategy == 'page':
    out.append(('pagination.index.parameter', pagination.index.parameter))
  elif pagination.strategy == 'token' or pagination.strategy == 'seek':
    out.append(('pagination.cursor.parameter', pagination.cursor.parameter))
  elif pagination.strategy == 'window':
    out.append(('pagination.bound.start', pagination.bound.start))
    out.append(('pagination.bound.end', pagination.bound.end))
  else:
    out.append(('pagination.offset.parameter', pagination.offset.parameter))
  if pagination.size is not None:
    out.append(('pagination.size.parameter', pagination.size.parameter))
  return out

def arithmetic_parameters(pagination: Pagination) -> list[tuple[str, str]]:
  """
  Every request parameter the walk computes a value for, as `(location, parameter name)`.

  These are the parameters `Generator.paged_method` does arithmetic on: a `window` takes
  the width of the caller's own two bounds as `end - start` and moves the window by it, a
  `page` counts up from `index.start`, and an `offset` counts up from zero by the rows it
  received. `token` is absent because a cursor is opaque — it is echoed back exactly as it
  arrived, and nothing is computed from it.

  Args:
    pagination: Declaration carried by the endpoint.
  """
  if pagination.strategy == 'page':
    return [('pagination.index.parameter', pagination.index.parameter)]
  if pagination.strategy == 'offset':
    return [('pagination.offset.parameter', pagination.offset.parameter)]
  if pagination.strategy == 'window':
    return [
      ('pagination.bound.start', pagination.bound.start),
      ('pagination.bound.end', pagination.bound.end),
    ]
  return []

def pagination_paths(pagination: Pagination) -> list[tuple[str, str]]:
  """
  Every response path a declaration names, as `(location, dotted key)`.

  Args:
    pagination: Declaration carried by the endpoint.
  """
  out: list[tuple[str, str]] = []
  if pagination.strategy == 'token':
    out.append(('pagination.cursor.from', pagination.cursor.from_))
  done = pagination.done
  if done.kind == 'total':
    out.append(('pagination.done.path', done.path))
    if done.rows is not None:
      out.append(('pagination.done.rows', done.rows))
  elif done.kind == 'short_page' or done.kind == 'empty' or done.kind == 'unchanged':
    if done.rows is not None:
      out.append(('pagination.done.rows', done.rows))
  return out

def returned_schemas(endpoint: Endpoint, operation: dict[str, Any]) -> list[dict[str, Any]]:
  """
  The schema of the value the generated method returns, per success response: the
  payload schema itself, or, for an rpc endpoint declaring `envelope.payload`, the node
  that path selects inside it (ADR 0010). A response the path does not resolve in is
  dropped -- `check_envelope` reports that on its own, and an undecidable walk (a `$ref`
  or `anyOf` on the path) is nothing to check a pagination path against.

  Args:
    endpoint: Endpoint record loaded from an `endpoint.json`.
    operation: Plain-JSON operation, from `operation_json`.
  """
  payloads = payload_schemas(operation)
  envelope = endpoint.envelope
  if not isinstance(endpoint.spec, RpcEndpointSpec) or envelope is None or envelope.payload == '':
    return payloads
  out: list[dict[str, Any]] = []
  for schema in payloads:
    node, verdict = _resolve_schema_path(schema, envelope.payload)
    if verdict and isinstance(node, dict):
      out.append(node)
  return out

def check_envelope(endpoint: Endpoint, operation: dict[str, Any]) -> list[Violation]:
  """
  Rule 6: `envelope.payload` names a property of the response schema.

  The response schema describes the whole wire frame and `envelope.payload` selects the
  value the generated method returns (ADR 0010), so a path the schema does not carry
  leaves the generator nothing to type the return value from. A `$ref` or `anyOf` on the
  path is undecidable from the operation alone and reports nothing, the same stance
  `check_pagination` takes for its own paths.

  Args:
    endpoint: Endpoint record loaded from an `endpoint.json`.
    operation: Plain-JSON operation, from `operation_json`.
  """
  envelope = endpoint.envelope
  if not isinstance(endpoint.spec, RpcEndpointSpec) or envelope is None or envelope.payload == '':
    return []
  verdicts = [resolves(schema, envelope.payload) for schema in payload_schemas(operation)]
  if True in verdicts or False not in verdicts:
    return []
  return [Violation(
    rule='envelope',
    location='envelope.payload',
    message=(
      f'`{envelope.payload}` names no property of the response schema, so the generated '
      f'method has nothing to type its return value from; the schema describes the whole '
      f'wire frame and `envelope.payload` selects the returned value inside it (ADR 0010). '
      f'A schema written for the unwrapped value is rewritten by `truewire migrate`'
    ),
  )]

def check_pagination(endpoint: Endpoint, operation: dict[str, Any]) -> list[Violation]:
  """
  Rule 8: a pagination declaration is internally consistent with the operation it sits on.

  This check cannot know that an undeclared endpoint paginates — that is the name-sniffing
  the declaration replaces — so it says nothing about a missing block. What it can settle
  is that a present one is not a dead letter: parameters the generator will set have to be
  parameters the operation has, a parameter it will do arithmetic on has to be typed as a
  number, a path it will read has to be a field the payload declares, and a short page
  cannot be recognised without a page size to compare against.

  Args:
    endpoint: Endpoint record loaded from an `endpoint.json`.
    operation: Plain-JSON `spec.openapi` operation.
  """
  pagination = endpoint.pagination
  if pagination is None:
    return []
  out: list[Violation] = []
  declared = request_parameters(operation)
  for location, parameter in pagination_parameters(pagination):
    if parameter in declared:
      continue
    out.append(Violation(
      rule='pagination',
      location=location,
      message=(
        f'`{parameter}` is not a parameter of this operation, so the generated loop would '
        f'set an argument the endpoint does not take; it declares '
        f'{", ".join(f"`{name}`" for name in sorted(declared)) or "no parameters"}'
      ),
    ))
  for location, parameter in arithmetic_parameters(pagination):
    schema = parameter_schema(operation, parameter)
    datetime_ok = pagination.strategy == 'window'
    if schema is None or is_arithmetic(schema, datetime_ok=datetime_ok) is not False:
      continue
    fix = 'Declare it `integer`, or `string` with `format: "date-time"`' if datetime_ok else 'Declare it `integer`'
    out.append(Violation(
      rule='pagination',
      location=location,
      message=(
        f'`{parameter}` is typed `{schema["type"]}`, but a `{pagination.strategy}` walk '
        f'computes the value it sends by arithmetic; a `window` subtracts the caller\'s '
        f'own two bounds and raises TypeError on anything else, and a `page` or `offset` '
        f'passes an integer the operation says this parameter does not take. {fix} — a '
        f'API documenting every query parameter as a string still takes a number here'
      ),
    ))
  # Relative to what the method returns -- the schema at `envelope.payload`, when one is
  # declared -- since the generated walk reads every path off the value the core handed
  # back, never off the wire frame (ADR 0010).
  payloads = returned_schemas(endpoint, operation)
  for location, path in pagination_paths(pagination):
    verdicts = [resolves(schema, path) for schema in payloads]
    if True in verdicts or False not in verdicts:
      continue
    out.append(Violation(
      rule='pagination',
      location=location,
      message=(
        f'`{path}` names no property of the response payload, so the generated loop would '
        f'read a field the schema says is never there'
      ),
    ))
  if pagination.strategy == 'seek':
    field = last_row_field(pagination.cursor.from_)
    rows = pagination.done.rows
    items = [row_item_schema(schema, rows) for schema in payloads]
    verdicts = [resolves(item, field) for item in items if item is not None]
    if verdicts and True not in verdicts and False in verdicts:
      collection = f'`{rows}`' if rows is not None else 'the response payload'
      out.append(Violation(
        rule='pagination',
        location='pagination.cursor.from',
        message=(
          f'`{field}` names no property of a row of {collection}, so the generated loop '
          f'would read a field the schema says a row never carries'
        ),
      ))
    if pagination.overlap is not None:
      bound_schema = parameter_schema(operation, pagination.cursor.parameter)
      field_schemas = [row_field_schema(item, field) for item in items if item is not None]
      arithmetic = [
        overlap_field_arithmetic(schema, bound_schema=bound_schema, strategy='seek')
        for schema in field_schemas if schema is not None
      ]
      if arithmetic and True not in arithmetic and False in arithmetic:
        out.append(Violation(
          rule='pagination',
          location='pagination.overlap',
          message=(
            f'`{field}` is not typed as a number, but an `overlap` walk compares '
            f'successive values of it with `>` and takes their `max`; declare it '
            f'`integer` or `number`, or declare a timestamp `format` on '
            f'`pagination.cursor.parameter` so it converts before comparing'
          ),
        ))
  if pagination.strategy == 'window' and pagination.overlap is not None:
    overlap = pagination.overlap
    field = last_row_field(overlap.field)
    rows = pagination.done.rows
    items = [row_item_schema(schema, rows) for schema in payloads]
    verdicts = [resolves(item, field) for item in items if item is not None]
    if verdicts and True not in verdicts and False in verdicts:
      collection = f'`{rows}`' if rows is not None else 'the response payload'
      out.append(Violation(
        rule='pagination',
        location='pagination.overlap.field',
        message=(
          f'`{field}` names no property of a row of {collection}, so the generated walk '
          f'would read a field the schema says a row never carries'
        ),
      ))
    bound_schema = parameter_schema(operation, pagination.bound.start)
    field_schemas = [row_field_schema(item, field) for item in items if item is not None]
    arithmetic = [
      overlap_field_arithmetic(schema, bound_schema=bound_schema, strategy='window')
      for schema in field_schemas if schema is not None
    ]
    if arithmetic and True not in arithmetic and False in arithmetic:
      out.append(Violation(
        rule='pagination',
        location='pagination.overlap.field',
        message=(
          f'`{field}` is not typed as a number, but an `overlap` walk compares '
          f'successive values of it with `>` and takes their `max`; declare it '
          f'`integer` or `number`, or declare a timestamp `format` on '
          f'`pagination.bound.start` so it converts before comparing'
        ),
      ))
    resolvable = pagination.size is not None and resolvable_size_cap(operation, pagination.size)
    if resolvable and overlap.cap is not None:
      out.append(Violation(
        rule='pagination',
        location='pagination.overlap.cap',
        message=(
          '`overlap.cap` is redundant: `pagination.size`\'s own declared default already '
          'resolves a row cap to measure a full chunk against'
        ),
      ))
    elif not resolvable and overlap.cap is None:
      out.append(Violation(
        rule='pagination',
        location='pagination.overlap.cap',
        message=(
          'neither `pagination.size`\'s own declared default nor `overlap.cap` resolves '
          'a row cap to measure a full chunk against; declare one or the other'
        ),
      ))
  if pagination.size is None:
    done = pagination.done
    if done.kind == 'short_page':
      out.append(Violation(
        rule='pagination',
        location='pagination.done',
        message=(
          'termination is `short_page` but no `size` parameter is declared; a page is short '
          'only relative to the size that was asked for'
        ),
      ))
    elif done.kind == 'total' and done.counts == 'items':
      out.append(Violation(
        rule='pagination',
        location='pagination.done',
        message=(
          'the total counts items but no `size` parameter is declared; how many pages an '
          'item count is worth is only decidable against the page size that was asked for'
        ),
      ))
  return out

IDENTIFIER_PLACEHOLDER = PLACEHOLDER
"""A `{name}` template slot inside an operation identifier (`path` or `channel`).

Matches the parameter name verbatim, whatever characters it uses -- codegen's own
substitution (`truewire.generation.python.code.http.HttpRequest.request_call`) does
`'{' + param.name + '}'` against the raw declared name, hyphens and all (an
`order-id` path segment, say), not a Python-identifier-restricted one."""

def check_identifier_templating(endpoint: Endpoint, operation: dict[str, Any]) -> list[Violation]:
  """
  ADR 0006: every `{name}` in the operation's identifier has a matching declared parameter.

  The identifier is `path` for `kind: 'rpc'`, `channel` for `kind: 'stream'` -- whichever one
  a path parameter substitutes into (`docs/spec/spec.md`). Legacy shape (`spec.openapi`) is
  checked in both directions: every `in: 'path'` parameter has a matching `{name}`
  placeholder, and every placeholder has a matching `in: 'path'` parameter. New shape
  has no `in` role at all -- a path parameter is simply any `request` property
  whose name matches a `{placeholder}`, so only the referenced-but-undeclared direction is
  checkable; a `request` property that happens not to be templated is an ordinary query/body
  field, not a violation. A mismatch means codegen would either emit a parameter that plugs
  into nothing, or an f-string referencing a name nothing declared.

  Args:
    endpoint: Endpoint record loaded from an `endpoint.json`.
    operation: Plain-JSON operation, from `operation_json`.
  """
  identifier = endpoint.path if endpoint.spec.kind == 'rpc' else endpoint.channel
  if identifier is None:
    return []
  placeholders = set(IDENTIFIER_PLACEHOLDER.findall(identifier))
  location = 'spec.path' if endpoint.spec.kind == 'rpc' else 'spec.channel'
  out: list[Violation] = []
  if 'request' in operation:
    request_schema = operation.get('request')
    properties = request_schema.get('properties') if isinstance(request_schema, dict) else None
    declared = set(properties) if isinstance(properties, dict) else set()
    for name in sorted(placeholders - declared):
      out.append(Violation(
        rule='identifier-templating',
        location=location,
        message=(
          f'`{{{name}}}` in {identifier!r} has no matching `request` property named '
          f'`{name}`, so the generated f-string would reference a name nothing declares'
        ),
      ))
    return out
  declared = {
    parameter.get('name')
    for parameter in operation.get('parameters') or []
    if isinstance(parameter, dict) and parameter.get('in') == 'path'
    and isinstance(parameter.get('name'), str)
  }
  for name in sorted(declared - placeholders):
    out.append(Violation(
      rule='identifier-templating',
      location=f'parameters.{name}',
      message=(
        f'`{name}` is declared `in: "path"` but `{identifier!r}` has no `{{{name}}}` '
        f'placeholder for it, so codegen has nowhere to substitute it'
      ),
    ))
  for name in sorted(placeholders - declared):
    out.append(Violation(
      rule='identifier-templating',
      location=location,
      message=(
        f'`{{{name}}}` in {identifier!r} has no matching `in: "path"` parameter named '
        f'`{name}`, so the generated f-string would reference a name nothing declares'
      ),
    ))
  return out

def check_ws_verb(endpoint: Endpoint) -> list[Violation]:
  """
  ADR 0004: a `kind: 'stream'` endpoint declares `envelope.verb`.

  Every stream endpoint has subscribe and unsubscribe frames, whether or not its wire dialect
  looks the same for both -- so unlike `check_pagination` (which cannot know that an
  undeclared endpoint paginates), an absent `verb` is always a real gap, never a "maybe this
  doesn't apply" case. Currently `warning`-severity (`WARNING_RULES`) while the corpus
  migrates client by client; see that docstring for why.

  Args:
    endpoint: Endpoint record loaded from an `endpoint.json`.
  """
  if endpoint.spec.kind != 'stream':
    return []
  envelope = endpoint.envelope
  if envelope is not None and getattr(envelope, 'verb', None) is not None:
    return []
  return [Violation(
    rule='ws-verb',
    location='envelope.verb',
    message=(
      'this stream endpoint declares no `envelope.verb`, so the mock server cannot tell a '
      'subscribe frame from an unsubscribe frame for it without guessing -- declare '
      '`{"path": ..., "subscribe": ..., "unsubscribe": ...}` naming the field (and its two '
      'literal values) that states intent on this API\'s wire frame'
    ),
  )]

def _meta_core_name(endpoint_dir: Path, spec_root: Path) -> str | None:
  """
  Nearest-ancestor `router.json`-declared `core` name for `endpoint_dir` --
  the identical ancestor walk `truewire.codegen.python.Generator._resolve_core_name`
  performs, duplicated here rather than imported so `spec/authoring.py` doesn't take on
  that module's own heavier `truewire.generation` dependency chain just to check `meta`.

  Returns `None`, not a raise, when no ancestor declares one -- `check_router_core`
  already reports that gap on its own; `check_meta` has nothing to validate against for
  such an endpoint and skips it silently rather than raising here too.

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
      return None
    current = current.parent

def check_meta(client_root: Path | Project) -> list[Violation]:
  """
  Two checks against a resolved core's declared `meta` JSON Schema,
  scoped to every endpoint whose resolved core actually declares one -- a core with no
  declared schema has nothing for either check to validate against, and
  `rpc_endpoint`/`stream_endpoint`'s own generation-time `ValueError` is the backstop for
  a non-empty `meta` on such a core (a core with no `meta` schema means
  every endpoint resolving to it must declare `meta: {}`):

  - `meta-schema`: the endpoint's own `meta` dict fails to validate against its resolved
    core's declared schema -- a missing required field, a wrong type, an unrecognized
    property under `additionalProperties: false`, etc. Caught here, at authoring time,
    rather than only surfacing as a runtime `KeyError`/`TypeError` inside a hand-written
    `core` that assumed a field `meta` never actually carries.
  - `meta-collision`: a property name the resolved core's `meta` schema declares also
    appears in the endpoint's own `request`/`parameters` schema -- a real collision risk
    the same shape S8 already flags for a wire parameter
    literally named `validate`.

  Both `error`-severity: unlike `enum`/`timestamp-format`'s own name-based heuristics,
  this is checkable fact -- a schema and a dict, or two schemas' own property names, not
  a guess about what a bare field name usually means. It is `error` from the moment any
  project actually declares a `[cores.<name>].meta` schema.

  Whole-project, like `check_mixed_leaf_router`/`check_router_core` beside it -- not
  per-endpoint via `audit`, since resolving a core needs `truewire.toml` loaded once
  and the nearest-ancestor `router.json` walk `_meta_core_name` performs, which `audit`'s
  own per-endpoint signature (`Endpoint` alone) has nowhere to carry. Only checked for a
  client that has migrated to the request/response shape at all (a `truewire.toml`'s
  presence, matching `check_router_core`'s identical gate) -- a legacy client's `meta`
  stays fully unchecked, exactly as `auth` always was.

  Args:
    client_root: Project (or project root).
  """
  project = resolve(client_root)
  codegen_config = project.config
  if codegen_config.cores is None:
    return []
  client_root = project.root
  spec_root = project.spec_dir
  endpoints_root = spec_root / 'endpoints'
  if not endpoints_root.is_dir():
    return []
  violations: list[Violation] = []
  for path in sorted(endpoints_root.rglob('endpoint.json')):
    core_name = _meta_core_name(path.parent, spec_root)
    if core_name is None:
      continue
    core = codegen_config.cores.get(core_name)
    if core is None or core.meta is None:
      continue
    endpoint = load_endpoint(path)
    location = str(path.relative_to(client_root))

    if isinstance(endpoint.meta, dict):
      validator = Draft202012Validator(core.meta)
      errors = sorted(validator.iter_errors(endpoint.meta), key=lambda error: list(error.path))
      for error in errors:
        violations.append(Violation(
          rule='meta-schema',
          location=f'{location} meta',
          message=(
            f'endpoint.meta fails validation against its resolved core ({core_name!r}) '
            f'declared `meta` schema: {error.message}'
          ),
        ))

    operation = operation_json(endpoint)
    if operation is not None:
      schema_properties = set(core.meta.get('properties') or {})
      colliding = schema_properties & request_parameters(operation)
      for name in sorted(colliding):
        violations.append(Violation(
          rule='meta-collision',
          location=f'{location} request.properties.{name}',
          message=(
            f'{name!r} is declared by both the resolved core ({core_name!r})\'s `meta` '
            "schema and this endpoint's own request/parameters schema -- a real "
            'collision risk (the same shape S8 flags for a wire parameter '
            'literally named `validate`)'
          ),
        ))
  return violations


def check_router_core(client_root: Path | Project) -> list[Violation]:
  """
  The project's root `spec/endpoints/router.json` must declare `core` -- there is no
  implicit fallback: every project's resolution walk needs a real ancestor to terminate at.

  Only checked for a project that declares a `[python]` section at all (a spec-only
  project has no `core`-resolvable classes yet, so nothing to flag).

  Args:
    client_root: Project (or project root).
  """
  project = resolve(client_root)
  if project.python is None:
    return []
  root_router = project.endpoints_dir / 'router.json'
  doc = load_router(root_router.parent)
  if doc is None or doc.core is None:
    return [Violation(
      rule='router-core-missing',
      location=str(root_router),
      message=(
        'root router.json must declare `core` -- there is no implicit fallback; '
        'a project may name theirs "default" as a matter of taste, but every project must '
        'declare one explicitly'
      ),
    )]
  return []


def schemas_on_same_path_to_root(a: Path, b: Path, *, root_schemas: Path) -> bool:
  """
  Whether two `schemas.json` file paths sit on the same ancestor-to-root walk
  -- one endpoint's own `_resolve_schemas`/`_resolve_core` ancestor walk could visibly reach
  both, so a shared id between them is a real collision, not two unrelated declarations.

  `root_schemas` (the project root's own `spec/schemas.json`) sits on *every* path to root
  (the final fallback -- the walk always reaches it last), so it is always
  considered on the same path as any other scope. Two nested scopes are on the same path
  when one's directory is an ancestor of the other's (including being the same directory).

  Factored out of `check_schemas_no_shadowing` (below) so `cli/codegen.py`'s own
  generation-time shadowing backstop can reuse the identical relationship test rather than
  a global, path-blind dict collision check -- this function's own caller refuses
  shadowing only between scopes on one path to root,
  and deliberately allow two unrelated sibling scopes (`spot/schemas.json`,
  `futures/schemas.json`) to share an id.

  Args:
    a: One `schemas.json` file path.
    b: The other `schemas.json` file path.
    root_schemas: The client root's own `spec/schemas.json` path.
  """
  if a == root_schemas or b == root_schemas:
    return True
  return a.parent == b.parent or a.parent in b.parent.parents or b.parent in a.parent.parents


def check_mixed_leaf_router(client_root: Path | Project) -> list[Violation]:
  """
  A `spec/endpoints/` directory must not carry its own `endpoint.json` alongside an
  endpoint-bearing descendant subdirectory, at any depth (`docs/spec/authoring.md` rule 16
  / `docs/production_standards.md` S30) -- a directory holding both has no name left to
  render its own leaf's method under except the S29-forbidden `__call__`.

  A client-root-level check, like `check_router_core`/`check_schemas_no_shadowing` beside
  it -- not per-endpoint, since what it inspects (directory shape) has no single endpoint
  of its own to be reported against. Unlike those two, this one *is* wired into
  `report_authoring` (`truewire/cli/check.py`), since `warning`-severity rollout checks
  (`ws-verb`, `title-empty-object`) are meant to actually surface in `truewire check`'s
  own output, not just exist in isolation.

  One violation per offending directory, not per endpoint beneath it -- a project with 8
  offending directories (`v1/account/endpoint.json` sitting alongside
  `v1/account/subaccount/`, etc.) should report as 8 findings, the number of directories to
  restructure, not the number of endpoints affected.

  Args:
    client_root: Project (or project root).
  """
  endpoints_root = project_spec_dir(client_root) / 'endpoints'
  client_root = client_root.root if isinstance(client_root, Project) else client_root
  if not endpoints_root.is_dir():
    return []
  leaf_dirs = sorted({path.parent for path in endpoints_root.rglob('endpoint.json')})
  violations: list[Violation] = []
  for directory in leaf_dirs:
    has_descendant_leaf = any(
      other != directory and directory in other.parents for other in leaf_dirs
    )
    if not has_descendant_leaf:
      continue
    location = str(directory.relative_to(client_root))
    violations.append(Violation(
      rule='mixed-leaf-router',
      location=location,
      message=(
        f'{location} declares its own endpoint.json and also has an endpoint-bearing '
        'descendant subdirectory -- a directory is a leaf endpoint or a router grouping, '
        'never both (docs/spec/authoring.md rule 16). Move its own endpoint.json (and '
        "examples/) into a new subdirectory named after its own function's last segment."
      ),
    ))
  return violations


def check_schemas_no_shadowing(client_root: Path | Project) -> list[Violation]:
  """
  No two `schemas.json` files may declare the same id when one scope is an ancestor of the
  other (refused as a collision, not resolved by nearer-wins precedence) --
  the project root's own `spec/schemas.json` counts as an ancestor of every nested scope,
  since it is the final fallback every `_resolve_schemas` walk always reaches. A same-named id declared by two *unrelated* scopes (`spot/schemas.json` and
  `futures/schemas.json`, say) is not a collision at all: no single endpoint's ancestor
  walk (`truewire.codegen.python.Generator._resolve_schemas`) ever sees both, so nothing
  downstream can confuse the two.

  A client-root-level check, like `check_router_core` beside it -- not per-endpoint, since
  what it inspects (`schemas.json` files) has no endpoint of its own to be reported
  against. Like that check, this is not yet wired into `truewire check`'s own CLI
  command (`report_authoring`, `truewire/cli/test.py`) -- `check_router_core` was added
  at spec-test-time layer and left unwired the same way, and closing that gap for both is
  tracked together rather than solved once here for only one of them.

  Deliberately does not import `truewire.codegen.layout`'s own `discover_schemas_files`/
  `load_schema_file` (the identical filesystem walk this reimplements in miniature) --
  `codegen.python` already imports from `truewire.spec`, so a `spec` module reaching back
  into `codegen` would be circular. Only raw JSON top-level keys are read here (real ids,
  never parsed into a full `Schema`), since a shadowing check only ever needs to compare
  id sets, not resolve or render anything.

  Args:
    client_root: Project (or project root).
  """
  spec = project_spec_dir(client_root)
  client_root = client_root.root if isinstance(client_root, Project) else client_root
  root_schemas = spec / 'schemas.json'
  endpoints_root = spec / 'endpoints'
  nested = sorted(endpoints_root.rglob('schemas.json')) if endpoints_root.is_dir() else []
  files = ([root_schemas] if root_schemas.is_file() else []) + nested
  if len(files) < 2:
    return []

  ids_by_file: dict[Path, set[str]] = {}
  for path in files:
    data = json.loads(path.read_text())
    if isinstance(data, dict):
      ids_by_file[path] = set(data)

  violations: list[Violation] = []
  for i, a in enumerate(files):
    for b in files[i + 1:]:
      if not schemas_on_same_path_to_root(a, b, root_schemas=root_schemas):
        continue
      shared = ids_by_file.get(a, set()) & ids_by_file.get(b, set())
      for id in sorted(shared):
        a_rel, b_rel = a.relative_to(client_root), b.relative_to(client_root)
        violations.append(Violation(
          rule='schemas-shadowing',
          location=f'{a_rel} / {b_rel}',
          message=(
            f'schema id {id!r} is declared in both {a_rel} and {b_rel} -- shadowing '
            'between scopes on the same path to root is refused rather than '
            'resolving it by nearer-wins precedence'
          ),
        ))
  return violations


CHECKS = (
  check_error_responses,
  check_titles,
  check_enums,
  check_consts,
  check_positional_rows,
  check_unions,
  check_descriptions,
  check_timestamp_format,
  check_reserved_names,
)
"""Every operation-local check, in contract order. Rule 7 reads the endpoint too."""

def audit(endpoint: Endpoint) -> list[Violation]:
  """
  Report every mechanizable spec-authoring violation in one endpoint.

  `meta`'s own checks (`meta-schema`/`meta-collision`) are not among these -- unlike
  everything else here, they need `truewire.toml` loaded once and a nearest-ancestor
  `router.json` walk to resolve a core, neither of which this function's own per-endpoint
  signature (`Endpoint` alone) has anywhere to carry. See `check_meta`, wired
  client-root-wide alongside `check_mixed_leaf_router` in `report_authoring`
  (`truewire/cli/check.py`), not here.

  Args:
    endpoint: Endpoint record loaded from an `endpoint.json`.

  References:
    - `docs/spec/authoring.md`
  """
  operation = operation_json(endpoint)
  if operation is None:
    return []
  out: list[Violation] = []
  for check in CHECKS:
    out.extend(check(operation))
  out.extend(check_envelope(endpoint, operation))
  out.extend(check_pagination(endpoint, operation))
  out.extend(check_identifier_templating(endpoint, operation))
  out.extend(check_ws_verb(endpoint))
  return out


def check_schema_cycles(client_root: Path | Project) -> list[Violation]:
  """
  A reference cycle among a project's shared schemas must pass through at least one
  record (`docs/spec/authoring.md` rule 17).

  A record has a name in the generated module, so a reference back to it renders as a
  forward reference and the cycle closes. A cycle where every schema renders inline --
  `Tree` = a string or an array of `Tree` -- has no name to close on: each schema is an
  expression pasted at its use sites, and pasting one pastes the next forever.

  A client-root-level check, like `check_router_core`/`check_mixed_leaf_router` beside
  it: a shared schema belongs to a `schemas.json` scope, not to any one endpoint, so
  there is no endpoint to report it against. Wired into `report_authoring` so it
  actually fails `truewire check` -- before it existed, a spec on such a cycle passed
  the gate and then died inside `truewire generate python` with a bare `RecursionError`.

  One violation per cycle, not per schema on it: a two-schema cycle is one thing to fix.

  Args:
    client_root: Project (or project root).
  """
  try:
    raw = load_shared_schemas(client_root)
  except ValueError:
    # A shadowed id; `check_schemas_no_shadowing` reports that, and there is no single
    # schema set to look for a cycle in until it is fixed.
    return []
  try:
    schemas = {id: Schema.model_validate(schema) for id, schema in raw.items()}
  except Exception:
    # An unparseable schema is reported where the schema is loaded, not here.
    return []
  out: list[Violation] = []
  for cycle in unrenderable_cycles(schemas):
    if len(cycle) == 1:
      subject = f'{cycle[0]} references itself, and it is not a record'
      fix = f'Give {cycle[0]} `properties`'
    else:
      subject = f'{" and ".join(cycle)} reference each other in a cycle, and none is a record'
      fix = 'Give one of them `properties`'
    out.append(Violation(
      rule='schema-cycle',
      location=' -> '.join((*cycle, cycle[0])),
      message=(
        f'{subject} -- every schema on the cycle renders inline, so there is no '
        'generated name for it to close on and no backend can express it. '
        f'{fix} (and `title`) so it renders as its own type, and let the rest of the '
        'cycle reference that; a record may reference itself freely '
        '(docs/spec/authoring.md rule 17).'
      ),
    ))
  return out
