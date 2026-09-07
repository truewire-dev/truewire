from typing_extensions import Sequence, Mapping
from dataclasses import dataclass, field

from truewire.generation.schema import Reference, Schema, DataType
from truewire.plan.types import OPAQUE_FORMATS
from .schema import Type, InlineType, Scalar

TIMESTAMP_FORMATS: Mapping[str, str] = {
  'epoch-seconds': 'TimestampSeconds',
  'epoch-millis': 'TimestampMillis',
  'epoch-micros': 'TimestampMicros',
  'epoch-nanos': 'TimestampNanos',
  'date-time': 'TimestampIso',
  'date': 'DateIso',
}
"""Wire timestamp format -> the alias `truewire_core.types` exports for it. Uniform across
every project: the runtime defines every pair, and the generator only ever asks for a
name from this table."""

TYPES_PACKAGE = 'truewire_core.types'
"""Runtime module the `TIMESTAMP_FORMATS` aliases (and the converter instances the request
builders call `.dump()` on) are imported from. Generated code never reaches into a
project's own core for these (ADR 0011)."""

OPAQUE_STRING_FORMATS = set(OPAQUE_FORMATS)
"""Standard OpenAPI string formats that document a value without narrowing its Python type.

A `uuid` is a `str` to every caller: nothing in the generated surface parses it, and a
`Literal` cannot enumerate it. The format is worth keeping in the spec because it tells a
reader what the API sends, and unsupported formats raise rather than render, so it has to
be named here to be allowed through as the `str` it already is. `hostname`/`uri` are the
same call: a webhook URL or a DNS hostname is a `str` to every caller the way a UUID is.
"""

BOOLEAN_STRING_FORMATS = {'boolean-string'}
"""String formats that narrow to the builtin `bool`, `docs/spec/authoring.md` rule 12.

Unlike `TIMESTAMP_FORMATS`, this needs no runtime alias: `pydantic`'s default (lax)
coercion already turns the wire strings `"true"`/`"false"` into a real `bool` with no custom
`BeforeValidator`, so this renders straight to the builtin the same way `uuid` above renders
straight to `str`.
"""

INTEGER_STRING_FORMATS = {'integer-string'}
"""String formats that narrow to the builtin `int`, `docs/spec/authoring.md` rule 13."""

DECIMAL_STRING_FORMATS = {'decimal-string'}
"""String formats that narrow to stdlib `decimal.Decimal`, `docs/spec/authoring.md` rule 15."""


def scalar(base: str, format: str | None = None) -> Scalar:
  """Build one `Scalar` node, carrying `format` only when the schema declared one."""
  node: Scalar = {'type': 'scalar', 'base': base}  # type: ignore[typeddict-item]
  if format is not None:
    node['format'] = format
  return node


@dataclass(kw_only=True)
class Parser:
  typing_package: str = 'typing_extensions'
  any: InlineType = field(default_factory=lambda: scalar('any'))

  def __call__(self, schema: Reference | Schema | None, *, id: str | None = None, inline: bool = False) -> Type:
    if schema is None:
      return self.unknown()
    if isinstance(schema, Reference):
      return self.ref(schema)
    if schema.const is not None:
      return self.const(schema, id=id)
    match schema.type:
      case list():
        return self.multitypes(schema.type, schema, id=id)
      case 'string':
        return self.string(schema, id=id)
      case 'number':
        return self.number(schema, id=id)
      case 'integer':
        return self.integer(schema, id=id)
      case 'boolean':
        return self.boolean(schema, id=id)
      case 'null':
        return self.null(schema, id=id)
      case 'array':
        return self.array(schema, id=id)
      case 'object':
        return self.object(schema, id=id)
      case _:
        if schema.allOf:
          return self.all_of(schema, id=id)
        if schema.oneOf:
          return self.one_of(schema, id=id)
        if schema.anyOf:
          return self.any_of(schema, id=id)
        if schema.enum:
          return self.enum(schema, id=id)
        if schema.properties or schema.additionalProperties:
          return self.object(schema, id=id)
        else:
          return self.other(schema, id=id)

  def inline(self, schema: Reference|Schema|None, *, id: str | None = None) -> InlineType:
    type = self(schema, id=id)
    if type['type'] == 'record':
      raise ValueError('Found nested record type')
    return type

  def unknown(self, *, id: str | None = None) -> InlineType:
    return self.any

  def multitypes(self, types: Sequence[DataType], schema: Schema, *, id: str | None = None) -> Type:
    """Render a multi-type (`type: [...]`) schema as a union, one variant per listed type.

    A shared `format` (e.g. an `epoch-millis` field declared `type: ['integer', 'string']`)
    renders every branch to the same target type, so an already-seen variant is skipped
    rather than repeated -- otherwise the union collapses to a no-op `X | X` instead of
    plain `X`.
    """
    variants: list[InlineType] = []
    for t in types:
      new_schema = schema.model_copy()
      new_schema.type = t
      variant = self.inline(new_schema, id=id)
      if variant not in variants:
        variants.append(variant)
    return {'type': 'union', 'variants': [{'type': v} for v in variants]}

  def ref(self, ref: Reference, *, id: str | None = None) -> Type:
    return {'type': 'ref', 'id': ref.ref}

  def string(self, schema: Schema, *, id: str | None = None) -> Type:
    if schema.enum:
      return {'type': 'literal', 'values': schema.enum, 'id': id}
    known = (
      set(TIMESTAMP_FORMATS) | OPAQUE_STRING_FORMATS | BOOLEAN_STRING_FORMATS
      | INTEGER_STRING_FORMATS | DECIMAL_STRING_FORMATS
    )
    if schema.format is not None and schema.format not in known:
      raise NotImplementedError(f"String format '{schema.format}' is not supported.")
    return scalar('string', schema.format)

  def number(self, schema: Schema, id: str | None = None) -> Type:
    if schema.enum:
      return {'type': 'literal', 'values': schema.enum, 'id': id}
    return scalar('number')

  def integer(self, schema: Schema, id=None) -> Type:
    if schema.enum:
      return {'type': 'literal', 'values': schema.enum, 'id': id}
    if schema.format in TIMESTAMP_FORMATS:
      return scalar('integer', schema.format)
    return scalar('integer')

  def boolean(self, schema: Schema, id: str | None = None) -> Type:
    if schema.enum:
      return {'type': 'literal', 'values': schema.enum, 'id': id}
    return scalar('boolean')

  def null(self, schema: Schema, id: str | None = None) -> Type:
    return scalar('null')

  def array(self, schema: Schema, id: str | None = None) -> Type:
    if schema.prefixItems:
      return self.tuple(schema, id=id)
    return {'type': 'list', 'item': self.inline(schema.items, id=id), 'id': id}

  def tuple(self, schema: Schema, id: str | None = None) -> Type:
    """Parse `prefixItems` as a fixed-length, heterogeneous positional row."""
    return {
      'type': 'tuple',
      'items': [self.inline(item) for item in schema.prefixItems],
      'id': id,
    }

  def object(self, schema: Schema, id: str | None) -> Type:
    if schema.properties is None:
      if isinstance(schema.additionalProperties, bool):
        value = self.unknown()
      else:
        value = self.inline(schema.additionalProperties)
      return {
        'type': 'dict',
        'key': scalar('string'),
        'value': value,
        'id': id,
      }
    else:
      id = id or schema.title
      if id is None:
        raise ValueError('Must provide ID or schema.title for records', schema.model_dump(exclude_defaults=True))
      return {
        'type': 'record',
        'id': id,
        'docstring': schema.description,
        'fields': {
          k: {
            'type': self.inline(v),
            'docstring': v.description if isinstance(v, Schema) else None,
            'required': k in (schema.required or []),
          }
          for k, v in schema.properties.items()
        }
      }

  def all_of(self, schema: Schema, id: str | None = None) -> Type:
    raise NotImplementedError('allOf is not supported.')

  def one_of(self, schema: Schema, id: str | None = None) -> Type:
    raise NotImplementedError('oneOf is not supported.')

  def any_of(self, schema: Schema, id: str | None = None) -> Type:
    return {
      'type': 'union',
      'variants': [
        {
          'type': self.inline(v),
          'docstring': v.description if isinstance(v, Schema) else None,
        }
        for v in schema.anyOf
      ],
      'id': id,
    }

  def enum(self, schema: Schema, id: str | None = None) -> Type:
    return {'type': 'literal', 'values': schema.enum or [], 'id': id}

  def const(self, schema: Schema, id: str | None = None) -> Type:
    """Parse `const` as a closed set of exactly one value."""
    return {'type': 'literal', 'values': [schema.const], 'id': id}

  def other(self, schema: Schema, id: str | None = None) -> Type:
    return self.any
