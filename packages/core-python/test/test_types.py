"""`truewire_core.types`: the timestamp aliases generated code imports by name.

Each alias must parse the declared wire form into an aware `datetime`/`date` and dump back
to that same form under `mode='json'`; ADR 0008 relies on the serializer half.
"""
from datetime import date, datetime, timezone
from typing import Union

from pydantic import TypeAdapter
from typing_extensions import Literal, NamedTuple, TypedDict

from truewire_core import types
from truewire_core.types import (
  DateIso, TimestampIso, TimestampMicros, TimestampMillis, TimestampNanos, TimestampSeconds, wire_row,
)

INSTANT = datetime(2024, 5, 30, 12, 34, 56, 123456, tzinfo=timezone.utc)


class TestAliases:
  def test_epoch_aliases_round_trip(self):
    cases = [
      (TimestampSeconds, 1717072496, INSTANT.replace(microsecond=0)),
      (TimestampMillis, 1717072496123, INSTANT.replace(microsecond=123000)),
      (TimestampMicros, 1717072496123456, INSTANT),
      (TimestampNanos, 1717072496123456000, INSTANT),
    ]
    for alias, wire, expected in cases:
      adapter = TypeAdapter(alias)
      parsed = adapter.validate_python(wire)
      assert parsed == expected, alias
      assert parsed.tzinfo is not None
      assert adapter.dump_python(parsed, mode='json') == wire

  def test_number_epoch_aliases_keep_the_fraction(self):
    """A `type: number` epoch sends back the fraction it was read with (TRU-480); the
    integer alias floors it."""
    cases = [
      (types.TimestampSecondsFloat, 1688669448.4712, 1688669448),
      (types.TimestampMillisFloat, 1688669448471.25, 1688669448471),
      (types.TimestampMicrosFloat, 1688669448471200, 1688669448471200),
      (types.TimestampNanosFloat, 1688669448471200000, 1688669448471200000),
    ]
    for alias, wire, _ in cases:
      adapter = TypeAdapter(alias)
      dumped = adapter.dump_python(adapter.validate_python(wire), mode='json')
      assert dumped == wire and type(dumped) is type(wire), alias
    assert TypeAdapter(TimestampSeconds).dump_python(
      TypeAdapter(TimestampSeconds).validate_python(1688669448.4712), mode='json',
    ) == 1688669448

  def test_a_whole_number_epoch_dumps_an_int(self):
    adapter = TypeAdapter(types.TimestampSecondsFloat)
    assert adapter.dump_json(INSTANT.replace(microsecond=0)) == b'1717072496'
    assert adapter.dump_json(INSTANT) == b'1717072496.123456'
    assert types.timestamp_seconds_float.dump(INSTANT) == 1717072496.123456

  def test_iso_alias_round_trips_rfc3339(self):
    adapter = TypeAdapter(TimestampIso)
    parsed = adapter.validate_python('2024-05-30T12:34:56.123456Z')
    assert parsed == INSTANT
    assert adapter.dump_python(parsed, mode='json') == '2024-05-30T12:34:56.123456Z'

  def test_date_alias(self):
    adapter = TypeAdapter(DateIso)
    parsed = adapter.validate_python('2024-05-30')
    assert parsed == date(2024, 5, 30)
    assert adapter.dump_python(parsed, mode='json') == '2024-05-30'

  def test_alias_inside_a_typed_dict_dumps_the_wire_form(self):
    """The shape generated request builders rely on: a body dumped through the type
    carries the declared wire form, not `datetime.isoformat()`."""
    class Request(TypedDict):
      since: TimestampMillis

    dumped = TypeAdapter(Request).dump_json({'since': INSTANT})
    assert dumped == b'{"since":1717072496123}'

  def test_converters_are_exported_beside_the_aliases(self):
    """Generated code calls `timestamp_millis.dump(...)` for a query/path timestamp."""
    assert types.timestamp_millis.dump(INSTANT) == 1717072496123
    assert types.timestamp_seconds.dump(INSTANT) == 1717072496
    assert types.date_iso.dump(date(2024, 5, 30)) == '2024-05-30'
    for name in types.__all__:
      assert hasattr(types, name)


class Candle(NamedTuple):
  at: TimestampMillis
  close: float

  __get_pydantic_core_schema__ = wire_row


class Level(NamedTuple):
  price: float

  __get_pydantic_core_schema__ = wire_row


class Trade(NamedTuple):
  """A second timestamped row, so a union of rows tries `Candle`'s serializer on it first."""
  side: Literal['b', 's']
  at: TimestampMillis

  __get_pydantic_core_schema__ = wire_row


class Book(NamedTuple):
  """Recursive: a row holding rows of its own type."""
  level: Level
  children: list['Book']

  __get_pydantic_core_schema__ = wire_row


class TestWireRow:
  """A generated `NamedTuple` row dumps the JSON array it was read from on every pydantic the
  core admits (2.9 and later); before 2.14 the `TimestampMillis` position dumped ISO-8601
  without `wire_row`."""

  def test_row_dumps_its_positions_wire_form(self):
    adapter = TypeAdapter(Candle)
    row = adapter.validate_json('[1717072496123,1.5]')
    assert row == Candle(INSTANT.replace(microsecond=123000), 1.5)
    assert adapter.dump_json(row) == b'[1717072496123,1.5]'
    assert adapter.dump_python(row, mode='json') == [1717072496123, 1.5]
    assert adapter.dump_python(row) == (INSTANT.replace(microsecond=123000), 1.5)

  def test_row_inside_a_union_and_a_list(self):
    adapter = TypeAdapter(list[Union[Level, Candle]])
    rows = adapter.validate_json('[[2.5],[1717072496123,1.5]]')
    assert [type(row) for row in rows] == [Level, Candle]
    assert adapter.dump_json(rows) == b'[[2.5],[1717072496123,1.5]]'

  def test_union_of_two_timestamped_rows(self):
    adapter = TypeAdapter(list[Union[Candle, Trade]])
    wire = b'[[1717072496123,1.5],["b",1717072496123]]'
    rows = adapter.validate_json(wire)
    assert [type(row) for row in rows] == [Candle, Trade]
    assert adapter.dump_json(rows) == wire
    assert adapter.dump_python(rows, mode='json') == [[1717072496123, 1.5], ['b', 1717072496123]]

  def test_recursive_row(self):
    adapter = TypeAdapter(Book)
    wire = b'[[1.0],[[[2.0],[]]]]'
    assert adapter.dump_json(adapter.validate_json(wire)) == wire
