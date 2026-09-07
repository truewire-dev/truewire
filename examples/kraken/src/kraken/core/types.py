"""Kraken's wire timestamp shapes, re-exported from the runtime: RFC 3339 strings
(`post_trade`'s `from_ts`/`to_ts`, response fields like `last_ts`/`trade_ts`), Unix epoch
seconds (`api_key_info`'s `createdTime`, `ledgers`'s `time`) and, for a handful of fields
(`order_amends`'s `timestamp`, `market_data.trades`'s `since`/`last`), nanosecond epochs.

Generated code imports `truewire_core.types` directly; this module stays so a caller that
imports `kraken.core.TimestampIso` keeps working.
"""

from truewire_core.types import (  # noqa: F401
  TimestampIso, TimestampNanos, TimestampSeconds, timestamp_iso, timestamp_nanos, timestamp_seconds,
)
