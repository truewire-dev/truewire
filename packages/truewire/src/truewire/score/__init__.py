"""`truewire score`: the distance between a project and `docs/shape/`, as one table.

`docs/shape/score.md` defines the rows (S1-S12) and the rules (S13-S17). `measure` yields
them for a project, `Scorecard` holds them, and `render` prints them.
"""
from .card import PROJECT, ROWS, Cell, Row, Scorecard, Status, header, render, render_row, summary
from .rows import LANGUAGES, declared_languages, measure, missing_pages, run_command, score

__all__ = [
  'LANGUAGES', 'PROJECT', 'ROWS', 'Cell', 'Row', 'Scorecard', 'Status', 'declared_languages',
  'header', 'measure', 'missing_pages', 'render', 'render_row', 'run_command', 'score', 'summary',
]
