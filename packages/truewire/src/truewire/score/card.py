"""The scorecard's shape: rows of cells, the summary line, and how each prints.

Nothing here measures anything; `truewire.score.rows` does. This is the part `docs/shape/
score.md`'s rules S13 and S15 live in: what a cell may say, how a row adds up, and when
the card as a whole is `done`.
"""
from dataclasses import dataclass, field
from typing_extensions import Literal

Status = Literal['pass', 'fail', 'unchecked', 'date']
"""What one cell says. `date` is `stranger`'s alone: S12 is a date, not a check, so it
counts as neither a pass nor a fail, and only its absence (`unchecked`) holds `done` back."""

PROJECT = ''
"""The key of a project-wide row's one cell, where a per-language row keys by language."""

ROWS: tuple[tuple[str, str], ...] = (
  ('coverage', 'every documented endpoint is specified'),
  ('recorded', 'every endpoint has a recording or reason'),
  ('check', 'the spec satisfies the authoring rules'),
  ('generated', 'codegen is current and has no orphans'),
  ('surface', 'every endpoint is reachable as a method'),
  ('tests', 'the suite passes against the mock'),
  ('lint', 'format, lint and type-check'),
  ('standards', 'docstrings, schemas, secrets, coverage'),
  ('docs', 'required pages, every snippet type-checks'),
  ('published', 'the current version is on every registry'),
  ('conform', 'a dated report from the last run'),
  ('stranger', 'a newcomer completed the quickstart'),
)
"""S1-S12, in order: each row's name and its one-line description, as `score.md` prints them."""

_NAME_WIDTH = max(len(name) for name, _ in ROWS) + 3
_DESCRIPTION_WIDTH = max(len(description) for _, description in ROWS) + 2
_DETAIL_LIMIT = 120


@dataclass(frozen=True)
class Cell:
  """One row's result for one language, or for the whole project."""
  status: Status
  detail: str = ''
  """The checker's count, warning, failure reason, or which checker does not exist yet."""
  output: str = ''
  """Everything the checker printed, for `truewire score --verbose`."""
  shown: str = ''
  """What prints in place of the status, when that is not the status itself (`stranger`'s date)."""

  @property
  def text(self) -> str:
    """The cell as printed: `pass`, `FAIL`, `unchecked`, or a date."""
    if self.shown:
      return self.shown
    return 'FAIL' if self.status == 'fail' else self.status


@dataclass(frozen=True)
class Row:
  """One of S1-S12: a cell per declared language, or one `PROJECT` cell."""
  name: str
  description: str
  cells: dict[str, Cell]

  @property
  def status(self) -> Status:
    """`fail` when any cell fails, else `unchecked` when any is, else `date` or `pass`.

    A row is `pass` only when every one of its cells is: one language left unmeasured
    leaves the row unmeasured (S13, S15).
    """
    statuses = {cell.status for cell in self.cells.values()}
    for status in ('fail', 'unchecked', 'date'):
      if status in statuses:
        return status  # type: ignore[return-value]
    return 'pass' if statuses else 'unchecked'


@dataclass
class Scorecard:
  """A project's rows, filled in as they are measured."""
  project: str
  languages: tuple[str, ...]
  """The declared languages, one column each (S14: no column for an undeclared one)."""
  rows: list[Row] = field(default_factory=list)

  def counts(self) -> tuple[int, int, int]:
    """How many rows `pass`, `fail` and are `unchecked`. A dated `stranger` is none of them."""
    statuses = [row.status for row in self.rows]
    return statuses.count('pass'), statuses.count('fail'), statuses.count('unchecked')

  @property
  def done(self) -> bool:
    """S15: every row present, every row `pass`, and `stranger` carries a date."""
    names = [row.name for row in self.rows]
    if names != [name for name, _ in ROWS]:
      return False
    return all(row.status in ('pass', 'date') for row in self.rows)


def _column_width(language: str) -> int:
  return max(len(language), len('unchecked')) + 2


def header(card: Scorecard) -> str:
  """The first line: the project's name, then one column heading per declared language."""
  width = 2 + _NAME_WIDTH + _DESCRIPTION_WIDTH
  columns = ''.join(language.ljust(_column_width(language)) for language in card.languages)
  return (card.project.ljust(width - 1) + ' ' + columns).rstrip()


def _details(row: Row) -> str:
  """The trailing detail: one when every cell gives the same, else per column."""
  reasons = {key: cell.detail for key, cell in row.cells.items() if cell.detail}
  if not reasons:
    return ''
  if len(set(reasons.values())) == 1:
    detail = next(iter(reasons.values()))
  else:
    detail = '; '.join(f'{key}: {reason}' for key, reason in reasons.items())
  return detail if len(detail) <= _DETAIL_LIMIT else detail[:_DETAIL_LIMIT - 3] + '...'


def render_row(row: Row, languages: tuple[str, ...]) -> str:
  """One row: name, description, a cell per language (or the one project cell), the reason."""
  line = '  ' + row.name.ljust(_NAME_WIDTH) + row.description.ljust(_DESCRIPTION_WIDTH)
  if PROJECT in row.cells:
    cell = row.cells[PROJECT]
    line += cell.text.ljust(_column_width(languages[0] if languages else ''))
  else:
    line += ''.join(row.cells[language].text.ljust(_column_width(language)) for language in languages)
  return (line + _details(row)).rstrip()


def summary(card: Scorecard) -> str:
  """The last line: `N pass, N fail, N unchecked -> done|not done`."""
  passed, failed, unchecked = card.counts()
  return f'{passed} pass, {failed} fail, {unchecked} unchecked -> {"done" if card.done else "not done"}'


def render(card: Scorecard) -> list[str]:
  """The whole card as printed lines: the header, one line per row, the summary."""
  return [header(card), *(render_row(row, card.languages) for row in card.rows), summary(card)]
