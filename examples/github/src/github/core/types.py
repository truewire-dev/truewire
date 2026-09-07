"""Wire timestamp shapes the generated code refers to by name.

Each spec `format` (`epoch-seconds`, `epoch-millis`, `epoch-micros`, `epoch-nanos`,
`date-time`, `date`) renders to one of these: a real `datetime`/`date` that parses from
the wire form and serializes back to it.
"""
from datetime import date, datetime, timezone
from typing_extensions import Annotated

from pydantic import BeforeValidator, PlainSerializer

from truewire_core.times import DateConverter, EpochConverter, IsoConverter

timestamp_seconds = EpochConverter.seconds(tz=timezone.utc)
timestamp_millis = EpochConverter.milliseconds(tz=timezone.utc)
timestamp_micros = EpochConverter.microseconds(tz=timezone.utc)
timestamp_nanos = EpochConverter.nanoseconds(tz=timezone.utc)
timestamp_iso = IsoConverter()
date_iso = DateConverter()

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
