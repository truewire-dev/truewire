"""`truewire_core.types`: the timestamp aliases generated code imports by name.

Each alias must parse the declared wire form into an aware `datetime`/`date` and dump back
to that same form under `mode='json'`; ADR 0008 relies on the serializer half.
"""
from datetime import date, datetime, timezone

from pydantic import TypeAdapter
from typing_extensions import TypedDict

from truewire_core import types
from truewire_core.types import (
  DateIso, TimestampIso, TimestampMicros, TimestampMillis, TimestampNanos, TimestampSeconds,
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
