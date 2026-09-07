from typing_extensions import Sequence, Mapping
from dataclasses import dataclass, field

from truewire.generation.schema import Reference, Schema, DataType
from .schema import Type, InlineType

TIMESTAMP_FORMATS: Mapping[str, str] = {
  'epoch-seconds': 'TimestampSeconds',
  'epoch-millis': 'TimestampMillis',
  'epoch-micros': 'TimestampMicros',
  'epoch-nanos': 'TimestampNanos',
  'date-time': 'TimestampIso',
  'date': 'DateIso',
}
"""Wire timestamp format -> the render id the project's own `core_package` is expected to
export. Uniform across every project: a core only defines the pairs its spec actually
uses, and the generator only ever asks for a name from this table."""

OPAQUE_STRING_FORMATS = {'uuid', 'hostname', 'uri'}
"""Standard OpenAPI string formats that document a value without narrowing its Python type.

A `uuid` is a `str` to every caller: nothing in the generated surface parses it, and a
`Literal` cannot enumerate it. The format is worth keeping in the spec because it tells a
reader what the API sends, and unsupported formats raise rather than render, so it has to
be named here to be allowed through as the `str` it already is.

`hostname`/`uri` were once handled by per-project `Parser` subclasses. `truewire.toml`
deliberately offers no Parser-customization escape hatch, so a project declaring either
format has no way to render it at all unless the shared table admits it. Both are standard
OpenAPI string formats with the identical "documents shape, doesn't narrow the type"
reasoning `uuid` already has -- a webhook URL or a DNS hostname is a `str` to every
caller the same way a UUID is -- so widening the shared table is the same call this
constant already makes for `uuid`, not a new one.
"""

BOOLEAN_STRING_FORMATS = {'boolean-string'}
"""String formats that narrow to the builtin `bool`, `docs/spec/authoring.md` rule 12.

Unlike `TIMESTAMP_FORMATS`, this needs no `core_package` type: `pydantic`'s default (lax)
coercion already turns the wire strings `"true"`/`"false"` into a real `bool` with no custom
`BeforeValidator`, so this renders straight to the builtin the same way `uuid` above renders
straight to `str`.
"""

INTEGER_STRING_FORMATS = {'integer-string'}
"""String formats that narrow to the builtin `int`, `docs/spec/authoring.md` rule 13.

Same reasoning as `BOOLEAN_STRING_FORMATS`: `pydantic`'s default (lax) coercion already
turns a wire string like `"12"` into a real `int` with no custom `BeforeValidator`, so this
renders straight to the builtin the same way `uuid`/`boolean-string` above do.
"""

DECIMAL_STRING_FORMATS = {'decimal-string'}
"""String formats that narrow to stdlib `decimal.Decimal`, `docs/spec/authoring.md` rule 15.

Same reasoning as `BOOLEAN_STRING_FORMATS`/`INTEGER_STRING_FORMATS`: `pydantic`'s default
(lax) coercion already turns a wire string like `"1.23"` into a real `Decimal` with no
custom `BeforeValidator`, so this needs no `core_package` type either -- it renders to
`decimal.Decimal`, a stdlib import rather than a builtin, the same way `Any` renders to a
`typing_extensions` import.
"""

@dataclass(kw_only=True)
class Parser:
  typing_package: str = 'typing_extensions'
  core_package: str | None = None
  """Package exporting the `TIMESTAMP_FORMATS` render ids (`TimestampMillis`, etc.), e.g.
  `petstore.core`.

  Required before any timestamp `format` can be rendered — a `TimestampX` type with
  nowhere to be imported from emits a `NameError` at import time rather than a type error
  at generation.
  """
  any: InlineType = field(default_factory=lambda: {'type': 'ref', 'id': 'Any', 'package': 'typing_extensions'})

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
    elif schema.format in TIMESTAMP_FORMATS:
      return self.timestamp(schema, id=id)
    elif schema.format in OPAQUE_STRING_FORMATS:
      return {'type': 'ref', 'id': 'str'}
    elif schema.format in BOOLEAN_STRING_FORMATS:
      return {'type': 'ref', 'id': 'bool'}
    elif schema.format in INTEGER_STRING_FORMATS:
      return {'type': 'ref', 'id': 'int'}
    elif schema.format in DECIMAL_STRING_FORMATS:
      return {'type': 'ref', 'id': 'Decimal', 'package': 'decimal'}
    elif schema.format is not None:
      raise NotImplementedError(f"String format '{schema.format}' is not supported.")
    else:
      return {'type': 'ref', 'id': 'str'}

  def number(self, schema: Schema, id: str | None = None) -> Type:
    if schema.enum:
      return {'type': 'literal', 'values': schema.enum, 'id': id}
    return {'type': 'ref', 'id': 'float'}

  def integer(self, schema: Schema, id=None) -> Type:
    if schema.enum:
      return {'type': 'literal', 'values': schema.enum, 'id': id}
    if schema.format in TIMESTAMP_FORMATS:
      return self.timestamp(schema, id=id)
    return {'type': 'ref', 'id': 'int'}

  def timestamp(self, schema: Schema, *, id: str | None = None) -> Type:
    """Render a timestamp-formatted field as the project core's matching `TimestampX` alias."""
    if self.core_package is None:
      raise ValueError(
        f"Schema declares format '{schema.format}' but this Parser has no `core_package` "
        'to import a Timestamp type from; pass one via the backend\'s `type_generator()`'
      )
    return {'type': 'ref', 'id': TIMESTAMP_FORMATS[schema.format], 'package': self.core_package}

  def boolean(self, schema: Schema, id: str | None = None) -> Type:
    if schema.enum:
      return {'type': 'literal', 'values': schema.enum, 'id': id}
    return {'type': 'ref', 'id': 'bool'}

  def null(self, schema: Schema, id: str | None = None) -> Type:
    return {'type': 'ref', 'id': 'None'}

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
        'key': {'type': 'ref', 'id': 'str'},
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
