from dataclasses import dataclass
from datetime import date, datetime

@dataclass(kw_only=True)
class DateConverter:
  """Converter for a plain calendar date, with no time component.

  Not a `TimeConverter` -- that base class's contract is fixed to `datetime` (see
  `base.py`), and a calendar date has no time-of-day to round-trip through one. Widening
  `TimeConverter` itself to cover this would ripple its return type into every existing
  subclass for the sake of this one converter; standing alone keeps the blast radius to
  just this file.
  """

  pattern: str = '%Y-%m-%d'
  """`datetime.strptime`/`strftime` directive for the wire string, e.g. `'%Y%m%d'` for a
  compact `YYYYMMDD` date with no separators (`"date": "20260101"`). Defaults to RFC
  3339's `YYYY-MM-DD`. Generic on the pattern the same way `EpochConverter` is generic on
  `unit`/`tz` -- an API's exact wire encoding is a detail each project's own
  `core_package` supplies, not something this converter should special-case per format.
  """

  def parse(self, value: str | date) -> date:
    """Parse a wire calendar date, or pass an already-parsed one through.

    A `date` comes back unchanged, so a request `TypedDict` that holds the real `date` its
    generated signature asks for validates through `BeforeValidator(parse)` the same way a
    wire string does. A `datetime` (a `date` subclass) is accepted only at midnight, when
    dropping its time loses nothing; any other time of day raises rather than truncating
    silently -- the same rule pydantic applies when a `datetime` meets a `date` field.

    Args:
      value: The wire date, e.g. `'2026-08-03'` for the default pattern, or a `date`.

    Raises:
      ValueError: `value` is a `datetime` with a non-midnight time.
    """
    if isinstance(value, datetime):
      if (value.hour, value.minute, value.second, value.microsecond) != (0, 0, 0, 0):
        raise ValueError(f'a date has no time of day; got {value.isoformat()} -- call .date() to choose')
      return value.date()
    if isinstance(value, date):
      return value
    return datetime.strptime(value, self.pattern).date()

  def dump(self, d: date) -> str:
    """Render a `date` back to the wire pattern."""
    return d.strftime(self.pattern)

  def now(self) -> date:
    """Today's date."""
    return date.today()
