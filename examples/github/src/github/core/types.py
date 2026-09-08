"""Wire timestamp shapes, re-exported from the runtime.

Generated code imports `truewire_core.types` directly; this module stays so a caller that
imports `github.core.types` keeps working. Add a project-specific alias here (a
non-UTC epoch, say) built from `truewire_core.times` converters.
"""
from truewire_core.types import (  # noqa: F401
  DateIso, TimestampIso, TimestampMicros, TimestampMillis, TimestampNanos, TimestampSeconds,
  date_iso, timestamp_iso, timestamp_micros, timestamp_millis, timestamp_nanos, timestamp_seconds,
)
