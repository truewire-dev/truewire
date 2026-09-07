from typing_extensions import Callable, Mapping
from dataclasses import dataclass, field

from truewire.generation.schema import (
  Operation, Reference, Schema, SchemaResolver, ensure_nonref, ensure_ref,
)
from truewire.generation.types import RenderedTypes
from truewire.generation.openapi import BODY_KEY
from truewire.generation.python.types.parser import TIMESTAMP_FORMATS, DECIMAL_STRING_FORMATS
from truewire.generation.python.util import safe_identifier, body_param_name
from truewire.generation.util import indent

@dataclass(kw_only=True)
class Param:
  name: str
  required: bool
  type: str | None = None

BODY_WIRE_NAME = 'body_wire'
"""Fixed local name for a body copy that needs conversion before reaching the wire.

Deliberately NOT derived from the caller-facing body parameter's own name, unlike the
original `{name}_wire` scheme this replaces: that name is the request's rendered type,
snake-cased -- reasonable as a public parameter name (`transfer_request`,
`cross_margin_borrow_request`), but for an `anyOf`-shaped body it's the *whole union type
string* concatenated (`LimitOrderRequest | MarketOrderRequest` ->
`limit_order_request_market_order_request`). `body_conversion_lines` repeats whichever
name it's given on every conversion line, so a long or concatenated one reads far worse
there than it does appearing once in a signature. Purely a local inside the generated
method body, never part of the public API, so shortening it is free -- guarded by
`body_wire_name`'s own shadow check the same way `param_value` guards `TIMESTAMP_HELPERS`."""

TIMESTAMP_HELPERS: Mapping[str, str] = {
  'TimestampSeconds': 'timestamp_seconds',
  'TimestampMillis': 'timestamp_millis',
  'TimestampMicros': 'timestamp_micros',
  'TimestampNanos': 'timestamp_nanos',
  'TimestampIso': 'timestamp_iso',
  'DateIso': 'date_iso',
}
"""Rendered timestamp type -> the module-level converter instance name the project's own
`core_package` is expected to export alongside it. Matching
`truewire.generation.python.types.parser.Parser.TIMESTAMP_FORMATS`'s render ids -- kept as
two tables, not one, because a schema only carries the *format*, and only the request
builder needs the *helper*, so nothing here re-derives the other's key."""


def _variant_schemas(schema: Schema, resolver: SchemaResolver | None = None) -> list[Schema]:
  """Every inline `Schema` variant of an `anyOf`-shaped schema. A bare `Reference` variant
  is resolved through `resolver` when one is given -- a shared component schema (`spec/
  schemas.json`'s `HfAddOrderLimit`, say) is by definition reused, so this only guesses at
  its shape when a real resolver confirms it; with no `resolver`, a `Reference` variant is
  skipped exactly as before this parameter existed (the same reasoning `parse` already
  applies to a `$ref`'d top-level body)."""
  out: list[Schema] = []
  for v in schema.anyOf:
    if isinstance(v, Schema):
      out.append(v)
    elif resolver is not None:
      resolved = resolver(v)
      if resolved is not None:
        out.append(resolved)
  return out


def _body_variants(schema: Schema, resolver: SchemaResolver | None = None) -> list[Schema]:
  """The set of shapes one body value can actually take, as a list of plain object
  schemas to walk for formatted properties: `[schema]` for an ordinary object,
  `_variant_schemas(schema, resolver)` for an `anyOf`-shaped discriminated union (each
  `Reference` variant resolved when `resolver` is given), or `[]` for anything else (a
  bare scalar body, or an unresolvable `anyOf` of only `Reference`s)."""
  if schema.properties:
    return [schema]
  if schema.anyOf:
    return _variant_schemas(schema, resolver)
  return []


def _merge_formatted_props(variants: list[Schema]) -> tuple[dict[str, str], list[str]]:
  """Union every variant's own top-level timestamp/decimal-string formatted properties.

  A property name is only ever declared once per variant, but different variants of a
  discriminated union can each declare it -- checking `body_wire.get(prop) is not None`
  at runtime already tells the generated code whether *this* call's variant actually has
  it, so collecting the union needs no per-variant branching in the emitted code. A
  property name declared with two different formats across variants is dropped from both
  results rather than guessing which declaration is right.
  """
  formats: dict[str, str] = {}
  conflicts: set[str] = set()
  for variant in variants:
    if not variant.properties:
      continue
    for prop_name, prop_schema in variant.properties.items():
      if not isinstance(prop_schema, Schema) or prop_schema.format is None:
        continue
      fmt = prop_schema.format
      if fmt not in TIMESTAMP_FORMATS and fmt not in DECIMAL_STRING_FORMATS:
        continue
      if prop_name in formats and formats[prop_name] != fmt:
        conflicts.add(prop_name)
      else:
        formats[prop_name] = fmt
  for prop_name in conflicts:
    formats.pop(prop_name, None)
  timestamp_props = {
    prop_name: TIMESTAMP_FORMATS[fmt]
    for prop_name, fmt in formats.items() if fmt in TIMESTAMP_FORMATS
  }
  decimal_props = [prop_name for prop_name, fmt in formats.items() if fmt in DECIMAL_STRING_FORMATS]
  return timestamp_props, decimal_props


def _array_property_specs(
  variants: list[Schema], resolver: SchemaResolver | None = None,
) -> dict[str, tuple[dict[str, str], list[str]]]:
  """The nested-array analogue of `_merge_formatted_props`: for each variant's own
  top-level properties, find one whose schema is `type: 'array'` with a plain-object/
  `anyOf`/(`resolver`-resolved) `$ref` `items` schema carrying its own formatted
  properties -- `classic.mix.order.batch_place`'s `orderList` is the motivating case, a
  batch-order request wrapping its per-order array inside an enclosing object alongside
  `symbol`/`productType`, rather than the body itself being an array (`_body_is_array`'s
  case). Keyed by property name; a name appearing in more than one variant pools every
  variant's item shapes into one `_merge_formatted_props` call for that property's items,
  same conflict handling."""
  by_prop: dict[str, list[Schema]] = {}
  for variant in variants:
    if not variant.properties:
      continue
    for prop_name, prop_schema in variant.properties.items():
      if not isinstance(prop_schema, Schema) or prop_schema.type != 'array':
        continue
      items = prop_schema.items
      if isinstance(items, Reference) and resolver is not None:
        items = resolver(items)
      if not isinstance(items, Schema):
        continue
      item_variants = _body_variants(items, resolver)
      if item_variants:
        by_prop.setdefault(prop_name, []).extend(item_variants)
  return {prop_name: _merge_formatted_props(vs) for prop_name, vs in by_prop.items()}


def _conversion_lines(
  wire_ref: str, timestamp_props: Mapping[str, str], decimal_props: list[str], *, tab: str,
) -> str:
  """One `if {wire_ref}.get(prop) is not None: {wire_ref}[prop] = ...` block per property,
  joined by newlines -- the shared per-property rewrite shape, reused for a whole-body
  dict, one item of an array body's per-row loop, or one nested array property's items.
  """
  lines = []
  for prop, render_id in timestamp_props.items():
    helper = TIMESTAMP_HELPERS[render_id]
    lines.append(
      f"if {wire_ref}.get('{prop}') is not None:\n"
      + tab + f"{wire_ref}['{prop}'] = {helper}.dump({wire_ref}['{prop}'])"
    )
  for prop in decimal_props:
    lines.append(
      f"if {wire_ref}.get('{prop}') is not None:\n"
      + tab + f"{wire_ref}['{prop}'] = str({wire_ref}['{prop}'])"
    )
  return '\n'.join(lines)


@dataclass(kw_only=True)
class HttpRequest:
  """HTTP request generator."""
  Param = Param
  TIMESTAMP_HELPERS = TIMESTAMP_HELPERS
  method: str
  path: str
  path_params: list[Param] = field(default_factory=list)
  query_params: list[Param] = field(default_factory=list)
  header_params: list[Param] = field(default_factory=list)
  body: Param | None = None
  body_is_array: bool = False
  """Whether the body is a `type: 'array'` schema (a batch-style request: one row per
  order) rather than a single object/discriminated-union -- decides which shape
  `body_conversion_lines` emits: a per-item loop instead of a single dict rewrite."""
  body_timestamp_props: Mapping[str, str] = field(default_factory=dict)
  """Request-body property name -> its Timestamp*/DateIso render id, for a top-level
  property of a plain object body, of every variant of an `anyOf`-shaped discriminated-
  union body, or of every variant of an array body's own `items` schema. `body.type` is
  always the body's own object/TypedDict render id (`MarkUpFeeSetRequest`, never
  `TimestampMillis`), so `timestamp_params`' per-parameter check can never match a body
  property the way it matches a query/path/header parameter -- this is the separate pass
  `parse()` runs over the body's own raw schema to find them. See `_merge_formatted_props`
  for how a multi-variant body is walked, and `parse`'s own docstring for what's left
  unconverted (a `$ref`'d body or `anyOf` variant).
  """
  body_decimal_props: list[str] = field(default_factory=list)
  """Request-body property name, for a top-level property (in a plain object body, any
  `anyOf` variant, or an array body's `items` variants) declared `format:
  'decimal-string'`. No per-format helper table is needed the way `TIMESTAMP_HELPERS` is
  for a timestamp -- unlike a wire timestamp, a `Decimal`'s wire form is always its own
  plain string, regardless of which schema declared it, so `body_conversion_lines` emits
  a bare `str(...)` call for every one of these rather than looking up a converter.
  """
  body_array_props: list[tuple[str, Mapping[str, str], list[str]]] = field(default_factory=list)
  """`(property name, its items' timestamp props, its items' decimal props)`, for a
  top-level property of a plain object body (or `anyOf` variant) that is itself an array
  of formatted items -- `classic.mix.order.batch_place`'s `orderList` is the motivating
  case: the per-order array sits inside an enclosing object alongside `symbol`/
  `productType`, rather than the body itself being an array (`body_is_array`'s case). See
  `_array_property_specs` for how a multi-variant body's array properties are pooled.
  """

  max_line_length: int = 80
  identifier: Callable[[str], str] = safe_identifier
  indent: str = '  '

  @property
  def required_query_params(self) -> list[Param]:
    return [param for param in self.query_params if param.required]

  @property
  def optional_query_params(self) -> list[Param]:
    return [param for param in self.query_params if not param.required]

  @property
  def required_header_params(self) -> list[Param]:
    return [param for param in self.header_params if param.required]

  @property
  def optional_header_params(self) -> list[Param]:
    return [param for param in self.header_params if not param.required]

  @property
  def timestamp_params(self) -> list[Param]:
    """Every parameter the caller passes as a `datetime`, in path/query/header order."""
    return [
      param
      for param in (*self.path_params, *self.query_params, *self.header_params)
      if param.type in TIMESTAMP_HELPERS
    ]

  @property
  def parameter_identifiers(self) -> frozenset[str]:
    """Every name this request binds inside the generated method, body included."""
    names = {
      self.identifier(param.name)
      for param in (*self.path_params, *self.query_params, *self.header_params)
    }
    if self.body:
      names.add(self.identifier(self.body.name))
    return frozenset(names)

  @property
  def helpers(self) -> frozenset[str]:
    """Names the emitted code calls but does not import, for the backend to supply. See
    `TIMESTAMP_HELPERS` -- one helper per distinct timestamp shape actually used by this
    request, not a single fixed name. `body_decimal_props` needs no entry here: its
    conversion is a bare `str(...)` call, not a helper the backend has to supply."""
    nested_timestamp_props = (ts for _, ts, _ in self.body_array_props)
    return frozenset(TIMESTAMP_HELPERS[p.type] for p in self.timestamp_params) | frozenset(
      TIMESTAMP_HELPERS[render_id] for render_id in self.body_timestamp_props.values()
    ) | frozenset(
      TIMESTAMP_HELPERS[render_id] for ts in nested_timestamp_props for render_id in ts.values()
    )

  @property
  def _body_needs_conversion(self) -> bool:
    return bool(self.body_timestamp_props or self.body_decimal_props or self.body_array_props)

  @property
  def body_wire_name(self) -> str | None:
    """The identifier a `json=...` argument should reference: the caller's own body
    argument unchanged when it needs no conversion, or the fixed `BODY_WIRE_NAME` local
    (see `body_conversion_lines`) when `body_timestamp_props`/`body_decimal_props`/
    `body_array_props` has to rewrite one or more of its top-level keys before the body
    reaches the wire.

    Raises:
      ValueError: A parameter of this request is literally named `BODY_WIRE_NAME`
        (`'body_wire'`) -- the same shadowing concern `param_value` already guards for
        `TIMESTAMP_HELPERS`, just against this module's own fixed name instead of an
        API's wire parameter.
    """
    if self.body is None:
      return None
    if not self._body_needs_conversion:
      return self.identifier(self.body.name)
    if BODY_WIRE_NAME in self.parameter_identifiers:
      raise ValueError(
        f'{self.method} {self.path}: a parameter is literally named {BODY_WIRE_NAME!r}, '
        'which collides with the fixed local name `body_conversion_lines` uses for the '
        'body copy it rewrites. Give that parameter a different generated name.'
      )
    return BODY_WIRE_NAME

  def _prop_conversion_lines(self, wire_ref: str) -> str:
    """`_conversion_lines` bound to this request's own top-level `body_timestamp_props`/
    `body_decimal_props` -- whether `wire_ref` is a whole-body dict or one item of an
    array body's per-row loop."""
    return _conversion_lines(wire_ref, self.body_timestamp_props, self.body_decimal_props, tab=self.indent)

  def _array_prop_conversion_lines(
    self, wire: str, prop: str, timestamp_props: Mapping[str, str], decimal_props: list[str],
  ) -> str:
    """`if {wire}.get(prop) is not None:` guarding a per-item copy-and-loop over one
    `body_array_props` entry -- the nested-array analogue of the top-level `body_is_array`
    case in `body_conversion_lines`, for a property like `orderList` that is itself an
    array of formatted items rather than the whole body being one."""
    inner = f"{wire}['{prop}'] = [dict(item) for item in {wire}['{prop}']]"
    item_lines = _conversion_lines('item', timestamp_props, decimal_props, tab=self.indent)
    inner += f"\nfor item in {wire}['{prop}']:\n" + indent(item_lines, self.indent)
    return f"if {wire}.get('{prop}') is not None:\n" + indent(inner, self.indent)

  def body_conversion_lines(self) -> str:
    """Declare `body_wire_name` as a plain-`dict`/`list` copy of the caller's body
    argument, with every `body_timestamp_props`/`body_decimal_props` key rewritten to
    wire shape -- the body-level analogue of `param_value`.

    A query/path/header parameter is bound to its own Python variable, so `param_value`
    can wrap it in one `.dump()` call inline. A request-body property is one key inside a
    caller-supplied `TypedDict` argument, not its own variable -- and a `NotRequired` key
    can't be bracket-indexed inside a single pyright-narrowable conditional expression
    (confirmed: `{'k': h.dump(body['k'])} if body.get('k') is not None else {}` still
    trips `reportTypedDictNotRequiredAccess`, because the two `.get`/`[]` accesses aren't
    the same narrowable expression). Copying to a plain `dict` first sidesteps that --
    once `body_wire_name` is a plain `dict`, indexing it is unrestricted, the same reason
    `dict_declaration` already builds `params`/`headers` as a fresh `dict` rather than
    mutating a TypedDict argument in place. `body_is_array` needs the same sidestep once
    per row, so it copies each item too, inside a loop over the fresh `list`.

    Emits a preceding *statement* declaring a new local, not a single `json=...`-droppable
    expression -- so a backend must call this (and prepend its own output) before it
    references `body_wire_name`, the same way it already calls `params_declaration`/
    `headers_declaration` before referencing `params`/`headers`. `request_call` does not
    call this: its own contract is a single expression with nothing to prepend a statement
    to, so a backend building a request body with `body_timestamp_props`/
    `body_decimal_props` needs its own call-assembly code, not `request_call`.
    """
    if self.body is None or not self._body_needs_conversion:
      return ''
    name = self.identifier(self.body.name)
    wire = self.body_wire_name
    assert wire is not None
    if self.body_is_array:
      out = f'{wire}: list = [dict(item) for item in {name}]'
      out += f'\nfor item in {wire}:\n' + indent(self._prop_conversion_lines('item'), self.indent)
      return out
    out = f'{wire}: dict = dict({name})'
    body = self._prop_conversion_lines(wire)
    if body:
      out += '\n' + body
    for prop, timestamp_props, decimal_props in self.body_array_props:
      out += '\n' + self._array_prop_conversion_lines(wire, prop, timestamp_props, decimal_props)
    return out

  def param_value(self, param: Param) -> str:
    """Render the expression that puts one parameter on the wire.

    A `datetime` is converted here rather than in the caller's own code, so the API's
    wire format -- an epoch unit, or an RFC3339/ISO date-time string -- stays an
    implementation detail of the generated client.

    Raises:
      ValueError: When a parameter of this request shadows a helper the emitted code
        calls. An API with a parameter literally named `timestamp_seconds` (or
        `timestamp_millis`/`timestamp_micros`/`timestamp_iso` -- see `TIMESTAMP_HELPERS`)
        would otherwise generate `timestamp_seconds.dump(timestamp_seconds)`, which reads
        the caller's own `datetime` and raises `AttributeError` at runtime instead of at
        generation time.
    """
    identifier = self.identifier(param.name)
    if param.type in TIMESTAMP_HELPERS:
      helper = TIMESTAMP_HELPERS[param.type]
      if (shadowed := sorted(self.helpers & self.parameter_identifiers)):
        raise ValueError(
          f'{self.method} {self.path}: parameter {shadowed[0]!r} shadows a helper an '
          f'emitted timestamp dump call needs. Rename it from the backend\'s `identifier` '
          'hook, so the generated method binds it under another name.'
        )
      return f'{helper}.dump({identifier})'
    if param.type == 'Decimal':
      # No per-format helper table needed the way `TIMESTAMP_HELPERS` is -- every
      # `decimal-string`-formatted parameter renders to the same builtin `Decimal`
      # regardless of which schema declared it (`docs/spec/authoring.md` rule 15), and
      # `str(Decimal(...))` always reproduces the exact wire value, since `Decimal`
      # preserves the string it was constructed from.
      return f'str({identifier})'
    return identifier

  def dict_declaration(self, variable: str, required: list[Param], optional: list[Param]) -> str:
    if not required and not optional:
      return ''

    if not required:
      out = variable + ' = {}'
    else:
      out = variable + ': dict = {\n'
      for param in required:
        out += self.indent + f"'{param.name}': {self.param_value(param)},\n"
      out += '}'

    for param in optional:
      iden = self.identifier(param.name)
      out += f"\nif {iden} is not None:\n"
      out += self.indent + f"{variable}['{param.name}'] = {self.param_value(param)}"

    return out

  def params_declaration(self) -> str:
    return self.dict_declaration('params', self.required_query_params, self.optional_query_params)

  def headers_declaration(self) -> str:
    return self.dict_declaration('headers', self.required_header_params, self.optional_header_params)

  def request_call(self, call_code: str = 'r = await self.request', /) -> str:
    """Render the full request-call expression.

    Doesn't apply `body_timestamp_props`/`body_decimal_props` -- see
    `body_conversion_lines`'s own docstring for why that needs a preceding statement this
    single-expression method has no way to emit. A body with no conversion needed (every
    caller not yet passing `body_schema` into `HttpRequest.parse`, and every body with no
    top-level formatted property) is unaffected either way.
    """
    method = f"'{self.method}'"
    if '{' in self.path:
      path = self.path
      for param in self.path_params:
        src = '{' + param.name + '}'
        dst = '{' + self.identifier(param.name) + '}'
        path = path.replace(src, dst)
      path = f"f'{path}'"
    else:
      path = f"'{self.path}'"
    params_code = 'params=params' if self.query_params else ''
    headers_code = 'headers=headers' if self.header_params else ''
    body_code = f'json={self.identifier(self.body.name)}' if self.body else ''

    parts = [method, path]
    if params_code:
      parts.append(params_code)
    if headers_code:
      parts.append(headers_code)
    if body_code:
      parts.append(body_code)
    single_line = call_code + f"({', '.join(parts)})"

    if len(single_line) <= self.max_line_length:
      return single_line

    else:
      line1 = f'{method}, {path}'
      line2 = []
      if params_code:
        line2.append(params_code)
      if headers_code:
        line2.append(headers_code)
      if body_code:
        line2.append(body_code)

      out = '(\n'
      out += self.indent + line1
      if line2:
        out += ',\n'
        out += self.indent + ',\n'.join(line2) + '\n'
      else:
        out += '\n'
      out += ')'
      return call_code + out


  @classmethod
  def parse(
    cls, op: Operation, types: RenderedTypes, *, method: str, path: str,
    body_schema: Schema | Reference | None = None,
    resolver: SchemaResolver | None = None,
  ):
    """Build a request from one operation.

    Args:
      op: The operation this request implements.
      types: Rendered types for every parameter and the body, keyed the way
        `truewire.generation.openapi.normalize_schemas` keys them.
      method: HTTP method.
      path: URL path template.
      body_schema: The request body's own raw schema (`normalize_schemas`'s second return
        value, keyed by `BODY_KEY`) -- optional, and unrelated to `types`, because
        `body_timestamp_props`/`body_decimal_props` need each top-level property's
        declared wire `format` (`docs/spec/authoring.md` rules 3 and 15), which the
        rendered type name alone doesn't carry. A caller with no `format`-formatted body
        property to convert, or one not yet passing `body_schema` through, gets the same
        behavior as before this parameter existed.

        Shapes recognized: a plain object (`body_schema.properties`); an `anyOf`-shaped
        discriminated union (`body_schema.anyOf`, each inline variant walked via
        `_merge_formatted_props`); an array body (`body_schema.type == 'array'`, walking
        `body_schema.items` the same way and setting `body_is_array`); and, for a plain
        object or `anyOf` body, any top-level property that is itself an array of
        formatted items (`_array_property_specs`, populating `body_array_props`) --
        `classic.mix.order.batch_place`'s `orderList` is this last case, an enclosing
        object wrapping the per-order array alongside `symbol`/`productType`, rather than
        the body itself being the array. A bare `Reference` body, `anyOf` variant, or
        array `items` schema (`$ref`'d into `spec/schemas.json` rather than inlined) is
        resolved through `resolver` when one is given; with no `resolver`, each is skipped
        exactly as before this parameter existed -- a shared component schema is by
        definition reused, so guessing its shape without a real resolver confirming it
        would be wrong more often than it'd help. The motivating case is a write endpoint
        whose request body is an `anyOf` of two bare `$ref`s into `schemas.json`: before
        `resolver` existed, a `decimal-string`-formatted `price`/`size` there was never
        converted back to a wire string, and `httpx`'s default JSON encoder raised
        `TypeError: Object of type Decimal is not JSON serializable` on the very first
        real call.
      resolver: Resolves a bare `Reference` schema against the project's shared component
        schemas (`spec/schemas.json`) -- typically a `truewire.generation.schema.
        LocalResolver` built from the same mapping `Generator.schemas` was handed.
        Optional; omitting it reproduces the pre-`resolver` behavior of skipping every
        `Reference` this method encounters.
    """
    request = cls(method=method, path=path)
    params = [ensure_nonref(p) for p in op.parameters or []]
    for p in sorted(params, key=lambda p: p.in_ == 'query'): # path first, query second
      s = ensure_ref(p.schema_)
      type = types.identifiers[s.ref]
      if p.in_ == 'query':
        request.query_params.append(HttpRequest.Param(
          name=p.name,
          required=p.required,
          type=type,
        ))
      elif p.in_ == 'header':
        request.header_params.append(HttpRequest.Param(
          name=p.name,
          required=p.required,
          type=type,
        ))
      elif p.in_ == 'path':
        request.path_params.append(HttpRequest.Param(
          name=p.name,
          required=p.required,
          type=type,
        ))
    if op.request_body:
      type = types.identifiers.get(BODY_KEY)
      request.body = HttpRequest.Param(
        name=body_param_name(type, body_schema),
        required=True,
        type=type,
      )
      if isinstance(body_schema, Reference) and resolver is not None:
        resolved = resolver(body_schema)
        if resolved is not None:
          body_schema = resolved
      if isinstance(body_schema, Schema):
        items = body_schema.items
        if isinstance(items, Reference) and resolver is not None:
          items = resolver(items)
        if body_schema.type == 'array' and isinstance(items, Schema):
          request.body_is_array = True
          variants = _body_variants(items, resolver)
          if variants:
            request.body_timestamp_props, request.body_decimal_props = _merge_formatted_props(variants)
        else:
          variants = _body_variants(body_schema, resolver)
          if variants:
            request.body_timestamp_props, request.body_decimal_props = _merge_formatted_props(variants)
            request.body_array_props = [
              (prop_name, ts, dec)
              for prop_name, (ts, dec) in _array_property_specs(variants, resolver).items() if ts or dec
            ]

    return request
