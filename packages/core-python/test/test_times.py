"""
Pins `IsoConverter`'s RFC 3339 handling and the missing `EpochConverter.microseconds()`
factory. `IsoConverter` previously round-tripped only `datetime.isoformat()`'s own output
(no `Z`, no over-precision handling) -- venues send `Z`-suffixed timestamps with anywhere
from no fractional digits to nanoseconds (kraken's `post_trade`: `...123456789Z`), and
`datetime.fromisoformat` only understands `Z` and arbitrary fraction lengths from Python
3.11 on, one minor version above this package's declared floor (`>=3.10`).
"""
from datetime import date, datetime, timezone, timedelta
import pytest
from pydantic import TypeAdapter

from truewire_core.times.date import DateConverter
from truewire_core.times.iso import IsoConverter
from truewire_core.times.ms import EpochConverter


class TestIsoConverterParse:
  def test_z_suffixed_nine_digit_fraction(self):
    """Kraken's `post_trade` example: nanosecond precision, more than `datetime` holds."""
    dt = IsoConverter().parse('2024-05-30T12:34:56.123456789Z')
    assert dt == datetime(2024, 5, 30, 12, 34, 56, 123456, tzinfo=timezone.utc)

  def test_z_suffixed_two_digit_fraction(self):
    """Under-precision, not just over: pre-3.11 `fromisoformat` accepts only exactly 3 or
    6 fractional digits, so a 2-digit wire value has to be padded, not just truncated."""
    dt = IsoConverter().parse('2024-05-30T12:34:56.12Z')
    assert dt == datetime(2024, 5, 30, 12, 34, 56, 120000, tzinfo=timezone.utc)

  def test_z_suffixed_no_fraction(self):
    dt = IsoConverter().parse('2024-05-30T12:34:56Z')
    assert dt == datetime(2024, 5, 30, 12, 34, 56, tzinfo=timezone.utc)

  def test_explicit_offset_is_preserved(self):
    """A non-UTC offset isn't `Z`-normalized on parse -- nothing is lost."""
    dt = IsoConverter().parse('2024-05-30T14:34:56+02:00')
    assert dt == datetime(2024, 5, 30, 12, 34, 56, tzinfo=timezone.utc)


class TestIsoConverterDump:
  def test_naive_datetime_treated_as_already_utc(self):
    """Not converted through the process's local timezone -- attached, not shifted."""
    assert IsoConverter().dump(datetime(2024, 5, 30, 12, 34, 56)) == '2024-05-30T12:34:56Z'

  def test_aware_non_utc_datetime_is_converted(self):
    aware = datetime(2024, 5, 30, 14, 34, 56, tzinfo=timezone(timedelta(hours=2)))
    assert IsoConverter().dump(aware) == '2024-05-30T12:34:56Z'

  def test_bit2me_shape_round_trips(self):
    """Confirmed-working shape from the bit2me handoff: `isoformat().replace('+00:00', 'Z')`."""
    conv = IsoConverter()
    assert conv.dump(conv.parse('2024-05-07T14:08:30.961Z')) == '2024-05-07T14:08:30.961000Z'


class TestIsoConverterPydantic:
  def test_validates_through_annotated_type(self):
    from typing_extensions import Annotated
    from pydantic import BeforeValidator
    converter = IsoConverter()
    TimestampIso = Annotated[datetime, BeforeValidator(converter.parse)]
    validated = TypeAdapter(TimestampIso).validate_python('2024-05-30T12:34:56.123456789Z')
    assert validated == datetime(2024, 5, 30, 12, 34, 56, 123456, tzinfo=timezone.utc)


class TestEpochConverterMicroseconds:
  def test_factory_exists_and_round_trips(self):
    """`epoch-micros` is declared in `EPOCH_FORMATS` (Task 2) but had no factory."""
    conv = EpochConverter.microseconds(tz=timezone.utc)
    dt = datetime(2024, 5, 30, 12, 34, 56, 123456, tzinfo=timezone.utc)
    assert conv.dump(dt) == int(dt.timestamp() * 1_000_000)
    assert conv.parse(conv.dump(dt)) == dt


class TestEpochConverterNanoseconds:
  def test_factory_exists_and_round_trips(self):
    """`epoch-nanos` (deribit's `starbase_timestamp`/`starbase_last_update_timestamp`) had
    no factory -- `EpochConverter` was already generic on `unit`, just missing this one."""
    conv = EpochConverter.nanoseconds(tz=timezone.utc)
    dt = datetime(2024, 5, 30, 12, 34, 56, 123456, tzinfo=timezone.utc)
    assert conv.dump(dt) == int(dt.timestamp() * 1_000_000_000)
    assert conv.parse(conv.dump(dt)) == dt


class TestDateConverter:
  def test_parse(self):
    """Plain calendar date, no time component -- deribit's
    `market_data.get_delivery_prices.date` (e.g. `'2026-08-03'`)."""
    assert DateConverter().parse('2026-08-03') == date(2026, 8, 3)

  def test_dump(self):
    assert DateConverter().dump(date(2026, 8, 3)) == '2026-08-03'

  def test_round_trips(self):
    conv = DateConverter()
    assert conv.parse(conv.dump(date(2026, 8, 3))) == date(2026, 8, 3)


class TestDateConverterPydantic:
  def test_validates_through_annotated_type(self):
    from typing_extensions import Annotated
    from pydantic import BeforeValidator
    converter = DateConverter()
    DateIso = Annotated[date, BeforeValidator(converter.parse)]
    validated = TypeAdapter(DateIso).validate_python('2026-08-03')
    assert validated == date(2026, 8, 3)


class TestDateConverterCustomPattern:
  def test_compact_pattern_exists_and_round_trips(self):
    """bitget's broker-commission endpoints send a compact `YYYYMMDD` date with no
    separators (`"date": "20260101"`), which the default RFC 3339 pattern can't parse --
    `DateConverter` was already a single fixed pattern, this makes it generic the same
    way `EpochConverter` is generic on `unit`."""
    conv = DateConverter(pattern='%Y%m%d')
    assert conv.parse('20260101') == date(2026, 1, 1)
    assert conv.dump(date(2026, 1, 1)) == '20260101'
    assert conv.parse(conv.dump(date(2026, 1, 1))) == date(2026, 1, 1)

  def test_default_pattern_is_unchanged(self):
    """Omitting `pattern` still means RFC 3339 `YYYY-MM-DD` -- existing callers see no
    behavior change."""
    assert DateConverter().pattern == '%Y-%m-%d'


class TestEpochConverterRoundTrips:
  dt = datetime(2024, 5, 30, 12, 34, 56, 123000, tzinfo=timezone.utc)

  def test_milliseconds(self):
    conv = EpochConverter.milliseconds(tz=timezone.utc)
    assert conv.dump(self.dt) == 1717072496123
    assert conv.parse(1717072496123) == self.dt

  def test_seconds(self):
    """Sub-second precision is lost on dump, by construction of the unit."""
    conv = EpochConverter.seconds(tz=timezone.utc)
    assert conv.dump(self.dt) == 1717072496
    assert conv.parse(1717072496) == self.dt.replace(microsecond=0)

  def test_microseconds(self):
    conv = EpochConverter.microseconds(tz=timezone.utc)
    assert conv.dump(self.dt) == 1717072496123000
    assert conv.parse(1717072496123000) == self.dt

  def test_nanoseconds(self):
    """Exact at nanosecond scale: integer arithmetic, no float `timestamp()` rounding."""
    conv = EpochConverter.nanoseconds(tz=timezone.utc)
    assert conv.dump(self.dt) == 1717072496123000000
    assert conv.parse(1717072496123000000) == self.dt
    assert conv.parse(conv.dump(self.dt)) == self.dt

  def test_parse_accepts_numeral_string(self):
    """Some APIs send the epoch as a string; `parse` coerces with `int()` first."""
    conv = EpochConverter.milliseconds(tz=timezone.utc)
    assert conv.parse('1717072496123') == self.dt

  def test_naive_when_no_tz(self):
    """Without `tz`, parse yields a naive local datetime and dump treats input as local."""
    conv = EpochConverter.seconds()
    parsed = conv.parse(1717072496)
    assert parsed.tzinfo is None
    assert conv.dump(parsed) == 1717072496

  def test_now_is_in_unit(self):
    conv = EpochConverter.milliseconds(tz=timezone.utc)
    now = conv.now()
    assert isinstance(now, int)
    assert abs(conv.parse(now) - datetime.now(timezone.utc)).total_seconds() < 5


class TestEpochConverterPydantic:
  def test_validates_and_dumps_through_annotated_type(self):
    """The shape a generated client uses: `BeforeValidator(parse)` + `PlainSerializer(dump)`."""
    from typing_extensions import Annotated
    from pydantic import BeforeValidator, PlainSerializer
    conv = EpochConverter.milliseconds(tz=timezone.utc)
    TimestampMillis = Annotated[
      datetime, BeforeValidator(conv.parse), PlainSerializer(conv.dump, when_used='json'),
    ]
    adapter = TypeAdapter(TimestampMillis)
    dt = adapter.validate_python(1717072496123)
    assert dt == datetime(2024, 5, 30, 12, 34, 56, 123000, tzinfo=timezone.utc)
    assert adapter.dump_json(dt) == b'1717072496123'


class TestParseAcceptsAlreadyParsedValues:
  """A request `TypedDict` holds the real `date`/`datetime` its generated signature asks
  for, and `BeforeValidator(parse)` runs on it all the same -- so every converter's `parse`
  returns a parsed value unchanged instead of failing on `strptime`/`endswith`/`int()`."""

  def test_date_converter_returns_a_date_unchanged(self):
    d = date(2024, 1, 1)
    assert DateConverter().parse(d) is d
    assert DateConverter(pattern='%Y%m%d').parse(d) is d

  def test_date_converter_takes_a_midnight_datetime_as_its_date(self):
    """`datetime` subclasses `date`; at midnight dropping the time loses nothing."""
    assert DateConverter().parse(datetime(2024, 1, 1)) == date(2024, 1, 1)
    assert DateConverter().parse(datetime(2024, 1, 1, tzinfo=timezone.utc)) == date(2024, 1, 1)

  def test_date_converter_refuses_a_datetime_with_a_time_of_day(self):
    """Truncating `2024-01-01T13:00` to a date silently would be a lie about the input."""
    with pytest.raises(ValueError, match='no time of day'):
      DateConverter().parse(datetime(2024, 1, 1, 13, 0))

  def test_iso_converter_returns_a_datetime_unchanged(self):
    aware = datetime(2024, 5, 30, 12, 34, 56, tzinfo=timezone.utc)
    naive = datetime(2024, 5, 30, 12, 34, 56)
    assert IsoConverter().parse(aware) is aware
    assert IsoConverter().parse(naive) is naive

  def test_epoch_converter_returns_a_datetime_unchanged(self):
    """Not moved to the converter's `tz` either: the caller's value is the caller's."""
    other = datetime(2024, 5, 30, 14, 34, 56, tzinfo=timezone(timedelta(hours=2)))
    assert EpochConverter.milliseconds(tz=timezone.utc).parse(other) is other
    assert EpochConverter.seconds().parse(other) is other

  def test_dump_is_unchanged(self):
    """Passing parsed values through `parse` changes nothing on the way out."""
    assert DateConverter().dump(date(2024, 1, 1)) == '2024-01-01'
    assert IsoConverter().dump(datetime(2024, 5, 30, 12, 34, 56)) == '2024-05-30T12:34:56Z'
    dt = datetime(2024, 5, 30, 12, 34, 56, 123000, tzinfo=timezone.utc)
    assert EpochConverter.milliseconds(tz=timezone.utc).dump(dt) == 1717072496123

  def test_a_request_typed_dict_holding_parsed_values_validates_and_dumps(self):
    """The Open-Meteo shape: `TypeAdapter(Request).validate_python(request)` before the
    query is rendered, with `start_date` already a `date`. Serializing still renders the
    wire form, and a wire string still parses."""
    from typing_extensions import TypedDict
    from truewire_core.types import DateIso, TimestampIso, TimestampMillis

    class Request(TypedDict):
      start_date: DateIso
      since: TimestampIso
      at: TimestampMillis

    adapter = TypeAdapter(Request)
    request = {
      'start_date': date(2024, 1, 1),
      'since': datetime(2024, 5, 30, 12, 34, 56, tzinfo=timezone.utc),
      'at': datetime(2024, 5, 30, 12, 34, 56, 123000, tzinfo=timezone.utc),
    }
    assert adapter.validate_python(request) == request
    assert adapter.dump_python(request, mode='json') == {
      'start_date': '2024-01-01', 'since': '2024-05-30T12:34:56Z', 'at': 1717072496123,
    }
    assert adapter.validate_python({
      'start_date': '2024-01-01', 'since': '2024-05-30T12:34:56Z', 'at': 1717072496123,
    }) == request

  def test_a_datetime_with_a_time_of_day_is_a_validation_error_on_a_date_field(self):
    from pydantic import ValidationError
    from truewire_core.types import DateIso
    with pytest.raises(ValidationError, match='no time of day'):
      TypeAdapter(DateIso).validate_python(datetime(2024, 1, 1, 13, 0))
