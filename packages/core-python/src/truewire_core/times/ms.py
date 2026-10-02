from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from datetime import datetime, timedelta, timezone
import time

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
"""The Unix epoch, aware, so arithmetic against it is exact integer `timedelta` math."""
from .base import TimeConverter

@dataclass(kw_only=True)
class EpochConverter(TimeConverter[int]):
  """Converter for epoch timestamps in a specific unit and timezone."""

  unit: float
  """Unit of the epoch timestamps, e.g. 1e3 for milliseconds, 1 for seconds."""
  tz: timezone | None = None
  """Timezone of the timestamps. If None, timestamps are naive."""

  @classmethod
  def milliseconds(cls, tz: timezone | None = None):
    """Create a converter for millisecond epoch timestamps."""
    return cls(unit=1e3, tz=tz)

  @classmethod
  def seconds(cls, tz: timezone | None = None):
    """Create a converter for second epoch timestamps."""
    return cls(unit=1, tz=tz)

  @classmethod
  def microseconds(cls, tz: timezone | None = None):
    """Create a converter for microsecond epoch timestamps."""
    return cls(unit=1e6, tz=tz)

  @classmethod
  def nanoseconds(cls, tz: timezone | None = None):
    """Create a converter for nanosecond epoch timestamps."""
    return cls(unit=1e9, tz=tz)

  def parse(self, value: int | float | str | datetime) -> datetime:
    """Parse an epoch timestamp into a `datetime`, or pass an already-parsed `datetime`
    through unchanged (its `tzinfo` as given, not moved to `tz`).

    Args:
      value: The epoch timestamp. Some APIs serialize it as a numeral string rather
        than a bare number (`"timestamp": "1786302600000"`), others as a fractional
        number (kraken's `trades_history` `time`). Exact `Fraction` arithmetic keeps the
        sub-second part; sub-microsecond values round to the nearest microsecond.
        A `datetime` is returned as is, so a request that already holds one validates
        through `BeforeValidator(parse)`.
    """
    if isinstance(value, datetime):
      return value
    try:
      numeric = Fraction(Decimal(value)) if isinstance(value, str) else Fraction(value)
    except (InvalidOperation, TypeError, ValueError) as error:
      raise ValueError(f'epoch timestamp must be a number or numeral string, got {value!r}') from error
    micros = round(numeric * 1_000_000 / Fraction(self.unit))
    aware = EPOCH + timedelta(microseconds=micros)
    if self.tz is None:
      return aware.astimezone().replace(tzinfo=None)
    return aware.astimezone(self.tz)

  def dump(self, dt: datetime) -> int:
    """Convert a `datetime` back into an epoch timestamp, floored to a whole unit (see
    `_micros`). A `type: number` epoch keeps the fraction through `EpochNumberConverter`."""
    return _micros(dt) * int(self.unit) // 1_000_000

  def now(self) -> int:
    """The current time, in the unit specified."""
    return int(self.unit * time.time())


@dataclass(kw_only=True)
class EpochNumberConverter(TimeConverter[int | float]):
  """`EpochConverter` for a `type: number` epoch, whose wire value may carry a fraction.

  Parses exactly as `epoch` does. `dump` keeps the sub-unit part a `datetime` holds, so a
  value read as `1688669448.4712` is sent back as `1688669448.4712`, not `1688669448`: an
  `int` when the count is whole, the nearest `float` otherwise.
  """

  epoch: EpochConverter
  """Converter supplying the unit and timezone."""

  def parse(self, value: int | float | str | datetime) -> datetime:
    """See `EpochConverter.parse`."""
    return self.epoch.parse(value)

  def dump(self, dt: datetime) -> int | float:
    """Convert a `datetime` back into an epoch count, fraction included."""
    count = Fraction(_micros(dt) * int(self.epoch.unit), 1_000_000)
    return count.numerator if count.denominator == 1 else float(count)

  def now(self) -> int | float:
    """The current time, in the unit specified."""
    return self.dump(datetime.now(timezone.utc))


def _micros(dt: datetime) -> int:
  """Whole microseconds from the epoch to `dt`, in integer arithmetic: `dt.timestamp()` is a
  float, and at nanosecond scale its rounding showed up as a ~200ns error on every dumped
  value. A naive `dt` is read as local time, the same convention `datetime.timestamp()` uses."""
  aware = dt if dt.tzinfo is not None else dt.astimezone()
  delta = aware - EPOCH
  return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
