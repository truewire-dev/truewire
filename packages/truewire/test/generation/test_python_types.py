"""
Pin the Python type backend's handling of `enum`, `const` and `prefixItems`.

These three JSON Schema keywords are the declarative statements a spec makes about
closed sets and positional rows. Whatever the backend fails to express here becomes
hand-written compensation in each client's `codegen/python.py`, so each case below
asserts on the emitted IR and, where the shape is new, on the rendered source.
"""
import pytest

from truewire.generation.python.types import Parser, Renderer, TypeGenerator
from truewire.generation.schema import Schema

def parse(schema: dict, *, id: str | None = None):
  """Parse a raw JSON Schema fragment into the Python backend's IR."""
  return Parser()(Schema.model_validate(schema), id=id)

def render(schemas: dict[str, dict], *, inline: bool = True):
  """Render raw JSON Schema fragments into Python source."""
  parser = Parser()
  return Renderer(parser=parser)({k: Schema.model_validate(v) for k, v in schemas.items()}, inline=inline)

def generate(schemas: dict[str, dict], *, inline: bool = True):
  """Run the full Python type pipeline: normalize, disambiguate, render."""
  return TypeGenerator()({k: Schema.model_validate(v) for k, v in schemas.items()}, inline=inline)

class TestEnum:
  """`enum` declares a closed set and must survive for every scalar type."""

  def test_string_enum_becomes_literal(self):
    assert parse({'type': 'string', 'enum': ['BUY', 'SELL']}) == {
      'type': 'literal', 'values': ['BUY', 'SELL'], 'id': None,
    }

  def test_integer_enum_becomes_literal(self):
    assert parse({'type': 'integer', 'enum': [1, 2, 3, 4]}) == {
      'type': 'literal', 'values': [1, 2, 3, 4], 'id': None,
    }

  def test_number_enum_becomes_literal(self):
    assert parse({'type': 'number', 'enum': [0.5, 1.0]}) == {
      'type': 'literal', 'values': [0.5, 1.0], 'id': None,
    }

  def test_boolean_enum_becomes_literal(self):
    assert parse({'type': 'boolean', 'enum': [True]}) == {
      'type': 'literal', 'values': [True], 'id': None,
    }

  def test_integer_without_enum_stays_int(self):
    assert parse({'type': 'integer'}) == {'type': 'ref', 'id': 'int'}

  def test_boolean_without_enum_stays_bool(self):
    assert parse({'type': 'boolean'}) == {'type': 'ref', 'id': 'bool'}

  def test_number_without_enum_stays_float(self):
    assert parse({'type': 'number'}) == {'type': 'ref', 'id': 'float'}

  def test_integer_enum_renders_literal_field(self):
    rendered = render({'SubmitOrder': {
      'title': 'SubmitOrder',
      'type': 'object',
      'required': ['side'],
      'properties': {'side': {'type': 'integer', 'enum': [1, 2, 3, 4]}},
    }})
    assert 'side: Literal[1, 2, 3, 4]' in rendered.definitions['SubmitOrder']

class TestConst:
  """`const` is a single-value closed set and must render as a one-value literal."""

  def test_string_const_becomes_literal(self):
    assert parse({'type': 'string', 'const': 'spot'}) == {
      'type': 'literal', 'values': ['spot'], 'id': None,
    }

  def test_integer_const_becomes_literal(self):
    assert parse({'type': 'integer', 'const': 7}) == {
      'type': 'literal', 'values': [7], 'id': None,
    }

  def test_untyped_const_becomes_literal(self):
    assert parse({'const': 'spot'}) == {'type': 'literal', 'values': ['spot'], 'id': None}

  def test_falsy_const_is_not_dropped(self):
    assert parse({'type': 'boolean', 'const': False}) == {
      'type': 'literal', 'values': [False], 'id': None,
    }

  def test_string_const_renders_literal_field(self):
    rendered = render({'Market': {
      'title': 'Market',
      'type': 'object',
      'required': ['type'],
      'properties': {'type': {'type': 'string', 'const': 'spot'}},
    }})
    assert "type: Literal['spot']" in rendered.definitions['Market']

class TestPrefixItems:
  """`prefixItems` declares a fixed-length heterogeneous row, i.e. a tuple."""

  def test_prefix_items_becomes_tuple(self):
    assert parse({'type': 'array', 'prefixItems': [{'type': 'integer'}, {'type': 'string'}]}) == {
      'type': 'tuple',
      'items': [{'type': 'ref', 'id': 'int'}, {'type': 'ref', 'id': 'str'}],
      'id': None,
    }

  def test_array_without_prefix_items_stays_list(self):
    assert parse({'type': 'array', 'items': {'type': 'string'}}) == {
      'type': 'list', 'item': {'type': 'ref', 'id': 'str'}, 'id': None,
    }

  def test_tuple_carries_its_id(self):
    parsed = parse({'type': 'array', 'prefixItems': [{'type': 'string'}]}, id='SpotCandle')
    assert parsed['id'] == 'SpotCandle'

  def test_candle_row_renders_as_tuple_alias(self):
    rendered = render({'SpotCandle': {
      'title': 'SpotCandle',
      'type': 'array',
      'prefixItems': [
        {'type': 'string', 'format': 'date-time'},
        {'type': 'string'},
        {'type': 'string'},
        {'type': 'string'},
        {'type': 'string'},
        {'type': 'string'},
        {'type': 'string', 'format': 'date-time'},
        {'type': 'string'},
      ],
    }})
    assert rendered.definitions['SpotCandle'] == (
      'SpotCandle = tuple[TimestampIso, str, str, str, str, str, TimestampIso, str]'
    )
    assert rendered.imports['truewire_core.types'] == {'TimestampIso'}

  def test_tuple_of_records_references_the_unnested_records(self):
    generated = generate({'Row': {
      'title': 'Row',
      'type': 'array',
      'prefixItems': [
        {'title': 'Leg', 'type': 'object', 'required': ['id'], 'properties': {'id': {'type': 'string'}}},
        {'type': 'integer'},
      ],
    }})
    assert generated.definitions['Row'] == 'Row = tuple[Leg, int]'
    assert 'class Leg(TypedDict):' in generated.definitions['Row/prefix/0']


def test_integer_with_epoch_millis_format_renders_as_timestamp_millis():
  parser = Parser()
  result = parser(Schema(type='integer', format='epoch-millis'))
  assert result == {'type': 'ref', 'id': 'TimestampMillis', 'package': 'truewire_core.types'}


def test_string_carried_epoch_renders_as_timestamp_millis():
  """Several venues send millisecond epochs as JSON strings, not numbers."""
  parser = Parser()
  result = parser(Schema(type='string', format='epoch-millis'))
  assert result == {'type': 'ref', 'id': 'TimestampMillis', 'package': 'truewire_core.types'}


def test_epoch_seconds_format_renders_as_timestamp_seconds():
  parser = Parser()
  result = parser(Schema(type='integer', format='epoch-seconds'))
  assert result == {'type': 'ref', 'id': 'TimestampSeconds', 'package': 'truewire_core.types'}


def test_epoch_micros_format_renders_as_timestamp_micros():
  parser = Parser()
  result = parser(Schema(type='integer', format='epoch-micros'))
  assert result == {'type': 'ref', 'id': 'TimestampMicros', 'package': 'truewire_core.types'}


def test_date_time_format_renders_as_timestamp_iso():
  """The actual bug fix: `date-time` used to render to a bare, unconvertible `datetime`
  -- it now goes through the same per-format lookup as every epoch shape."""
  parser = Parser()
  result = parser(Schema(type='string', format='date-time'))
  assert result == {'type': 'ref', 'id': 'TimestampIso', 'package': 'truewire_core.types'}


def test_epoch_nanos_format_renders_as_timestamp_nanos():
  """Added after a deribit review found genuine epoch-nanosecond `starbase_timestamp`/
  `starbase_last_update_timestamp` fields the original vocabulary couldn't express."""
  parser = Parser()
  result = parser(Schema(type='integer', format='epoch-nanos'))
  assert result == {'type': 'ref', 'id': 'TimestampNanos', 'package': 'truewire_core.types'}


def test_date_format_renders_as_date_iso():
  """Added after a deribit review found `market_data.get_delivery_prices.date`, a genuine
  plain calendar date with no time component."""
  parser = Parser()
  result = parser(Schema(type='string', format='date'))
  assert result == {'type': 'ref', 'id': 'DateIso', 'package': 'truewire_core.types'}


def test_plain_integer_is_unaffected():
  parser = Parser()
  assert parser(Schema(type='integer')) == {'type': 'ref', 'id': 'int'}


def test_uuid_format_renders_as_a_plain_string():
  """A `uuid` narrows nothing in Python, but the spec is right to record it.

  bit2me's `v1.trading.markets` types its market `id` as `{type: string, format: uuid}`,
  which is what the venue sends. Nothing generated parses it, so the rendered type is
  `str` — but an unlisted format raises rather than rendering, so it has to be named.
  """
  parser = Parser()
  assert parser(Schema(type='string', format='uuid')) == {'type': 'ref', 'id': 'str'}


def test_unknown_string_format_still_raises():
  parser = Parser()
  with pytest.raises(NotImplementedError):
    parser(Schema(type='string', format='email'))


def test_hostname_and_uri_formats_render_as_a_plain_string():
  """`hostname`/`uri` are standard OpenAPI string formats, the same "documents shape,
  doesn't narrow the type" case `uuid` already covers -- previously handled only by
  moralis's own per-client `Parser` subclass (`clients/moralis/codegen/python.py`,
  deliberately not upstreamed at the time), which no longer exists once a client migrates
  off its own `codegen/python.py` (codegen-mechanization design §6 has no
  Parser-customization escape hatch). moralis's `auth.challenge.request_evm_challenge`
  types its `domain`/`uri` request properties this way.
  """
  parser = Parser()
  assert parser(Schema(type='string', format='hostname')) == {'type': 'ref', 'id': 'str'}
  assert parser(Schema(type='string', format='uri')) == {'type': 'ref', 'id': 'str'}


def test_a_nested_array_carrying_properties_is_not_unnested():
  """An `array` renders as a list whatever `properties` it also carries.

  `Unnest.records()` used to unnest anything declaring `properties`, so a schema written
  `{type: array, properties: {}, items: {...}}` was replaced by a reference to a name that
  then rendered inline and defined nothing. The parent record annotated its field with that
  undefined name, the name never reached `generation_order`, and no check in the pipeline
  saw it — only `pyright` did, on the generated client. Deribit's `block_rfq.taker`
  subscription carries exactly one such schema, an array of maker aliases.
  """
  rendered = generate({
    'Taker': {
      'title': 'Taker',
      'type': 'object',
      'required': ['makers'],
      'properties': {
        'makers': {
          'type': 'array',
          'properties': {},
          'additionalProperties': True,
          'items': {'type': 'string'},
        },
      },
    },
  }, inline=False)
  assert 'makers: list[str]' in rendered.definitions['Taker']
  assert rendered.generation_order == ['Taker']


def test_a_nested_record_is_still_unnested():
  """The ordinary case is untouched: a nested object still becomes its own class."""
  rendered = generate({
    'Order': {
      'title': 'Order',
      'type': 'object',
      'required': ['leg'],
      'properties': {
        'leg': {
          'title': 'Leg',
          'type': 'object',
          'required': ['symbol'],
          'properties': {'symbol': {'type': 'string'}},
        },
      },
    },
  }, inline=False)
  assert 'leg: Leg' in rendered.definitions['Order']
  assert rendered.definitions['Order/leg'].startswith('class Leg(TypedDict):')
