"""Wire timestamp and date shapes generated code refers to by name.

Each spec `format` renders to one alias here: a real `datetime`/`date` that parses from the
wire form and serializes back to it (`PlainSerializer(..., when_used='json')`, so a request
body dumped through `validator(Type).dump()` carries the declared wire shape, never ISO-8601
by accident).

| format          | alias              | wire value                          |
| --------------- | ------------------ | ----------------------------------- |
| `epoch-seconds` | `TimestampSeconds` | integer seconds since the Unix epoch |
| `epoch-millis`  | `TimestampMillis`  | integer milliseconds                 |
| `epoch-micros`  | `TimestampMicros`  | integer microseconds                 |
| `epoch-nanos`   | `TimestampNanos`   | integer nanoseconds                  |
| `date-time`     | `TimestampIso`     | RFC 3339 date-time string            |
| `date`          | `DateIso`          | RFC 3339 full-date string            |

The module-level converter instances (`timestamp_millis`, ...) are the same objects the
aliases validate through; generated request builders call `.dump()` on them directly for a
timestamp that travels as a query or path parameter rather than in a JSON body.

Every epoch alias is UTC-aware. A project that needs a different zone, or a wire shape not
listed here, defines its own alias beside its core from `truewire_core.times` converters and
keeps the pairing rule above.
"""
from datetime import date, datetime, timezone
from typing_extensions import Annotated

from pydantic import BeforeValidator, PlainSerializer

from .times import DateConverter, EpochConverter, IsoConverter

timestamp_seconds = EpochConverter.seconds(tz=timezone.utc)
"""Converter behind `TimestampSeconds`."""
timestamp_millis = EpochConverter.milliseconds(tz=timezone.utc)
"""Converter behind `TimestampMillis`."""
timestamp_micros = EpochConverter.microseconds(tz=timezone.utc)
"""Converter behind `TimestampMicros`."""
timestamp_nanos = EpochConverter.nanoseconds(tz=timezone.utc)
"""Converter behind `TimestampNanos`."""
timestamp_iso = IsoConverter()
"""Converter behind `TimestampIso`."""
date_iso = DateConverter()
"""Converter behind `DateIso`."""

TimestampSeconds = Annotated[
  datetime, BeforeValidator(timestamp_seconds.parse), PlainSerializer(timestamp_seconds.dump, when_used='json'),
]
"""An `epoch-seconds` field."""
TimestampMillis = Annotated[
  datetime, BeforeValidator(timestamp_millis.parse), PlainSerializer(timestamp_millis.dump, when_used='json'),
]
"""An `epoch-millis` field."""
TimestampMicros = Annotated[
  datetime, BeforeValidator(timestamp_micros.parse), PlainSerializer(timestamp_micros.dump, when_used='json'),
]
"""An `epoch-micros` field."""
TimestampNanos = Annotated[
  datetime, BeforeValidator(timestamp_nanos.parse), PlainSerializer(timestamp_nanos.dump, when_used='json'),
]
"""An `epoch-nanos` field."""
TimestampIso = Annotated[
  datetime, BeforeValidator(timestamp_iso.parse), PlainSerializer(timestamp_iso.dump, when_used='json'),
]
"""A `date-time` (RFC 3339) field."""
DateIso = Annotated[
  date, BeforeValidator(date_iso.parse), PlainSerializer(date_iso.dump, when_used='json'),
]
"""A `date` (RFC 3339 full-date) field."""

__all__ = [
  'timestamp_seconds', 'timestamp_millis', 'timestamp_micros', 'timestamp_nanos',
  'timestamp_iso', 'date_iso',
  'TimestampSeconds', 'TimestampMillis', 'TimestampMicros', 'TimestampNanos',
  'TimestampIso', 'DateIso',
]
