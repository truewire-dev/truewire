"""Kraken's wire timestamp shapes, re-exported from the runtime: RFC 3339 strings
(`post_trade`'s `from_ts`/`to_ts`, response fields like `last_ts`/`trade_ts`), Unix epoch
seconds: integer (`api_key_info`'s `createdTime`) or fractional (`ledgers`'s `time`).
A handful of fields (`order_amends`'s `timestamp`, `market_data.trades`'s `since`/`last`)
use nanosecond epochs.

Generated code imports `truewire_core.types` directly; this module stays so a caller that
imports `kraken.core.TimestampIso` keeps working.
"""

from truewire_core.types import (  # noqa: F401
  TimestampIso,
  TimestampMicrosFloat,
  TimestampMillisFloat,
  TimestampNanos,
  TimestampNanosFloat,
  TimestampSeconds,
  TimestampSecondsFloat,
  timestamp_iso,
  timestamp_micros_float,
  timestamp_millis_float,
  timestamp_nanos,
  timestamp_nanos_float,
  timestamp_seconds,
  timestamp_seconds_float,
)
