"""Validate a project's `docs/docs.yml` against its model and the project around it (W10, S9).

The model (`truewire.schemas.docs.DocsYml`) rejects an unknown key, a wrong type and a
missing field. What it cannot see needs the project: every page the nav names exists under
`docs/`, and the quickstart has one block per language `truewire.toml` declares and none for
a language it does not.

A project without `docs/docs.yml` has nothing to validate here; the layout check reports the
missing file (W10).
"""
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing_extensions import Any

import yaml
from pydantic import ValidationError

from truewire.schemas.config import LANGUAGES
from truewire.schemas.docs import DocsYml, NavEntry, NavSection

DOCS_YML = 'docs/docs.yml'
"""Where the file lives, relative to the project root."""

TAGS = frozenset({'page', 'section'})
"""The kinds of nav entry, which pydantic writes into an error's location after the index."""


def check_docs_yml(root: Path, languages: Iterable[str] | None) -> list[str]:
  """Every problem with `root`'s `docs/docs.yml`, each a line naming the file and the key.

  Args:
    root: The project root.
    languages: The languages `truewire.toml` declares, or None when there is no project
      file to hold the quickstart to.

  Returns:
    The problems, in file order; empty when the file is valid or absent.
  """
  path = root / DOCS_YML
  if not path.is_file():
    return []
  try:
    data = yaml.safe_load(path.read_text(encoding='utf-8'))
  except UnicodeDecodeError:
    return [f'{DOCS_YML}: not UTF-8']
  except yaml.MarkedYAMLError as error:
    mark = error.problem_mark
    where = f':{mark.line + 1}' if mark is not None else ''
    return [f'{DOCS_YML}{where}: not YAML: {error.problem or error.context}']
  except yaml.YAMLError as error:
    return [f'{DOCS_YML}: not YAML: {error}']
  if data is None:
    return [f'{DOCS_YML}: empty; it needs at least `title` and `nav`']
  if not isinstance(data, dict):
    return [f'{DOCS_YML}: must be a mapping of keys, not a {type(data).__name__}']
  try:
    docs = DocsYml.model_validate(data)
  except ValidationError as error:
    return [f'{DOCS_YML}: {_problem(detail)}' for detail in error.errors()]
  problems = [
    f'{DOCS_YML}: nav: {page}: no such page under docs/'
    for page in _pages(docs.nav) if not _is_page(root / 'docs', page)
  ]
  if languages is not None:
    declared = [language for language in LANGUAGES if language in set(languages)]
    problems += [
      f'{DOCS_YML}: quickstart: no block for {language}, which truewire.toml declares'
      for language in declared if language not in docs.quickstart
    ]
    problems += [
      f'{DOCS_YML}: quickstart.{language}: truewire.toml declares no [{language}]'
      for language in docs.quickstart if language not in declared
    ]
  return problems


def _problem(detail: Any) -> str:
  """One pydantic error as `<key path>: <what is wrong>`."""
  loc = [part for index, part in enumerate(detail['loc']) if not (
    part in TAGS and index > 0 and isinstance(detail['loc'][index - 1], int)
  )]
  if loc and loc[-1] == '[key]':
    loc.pop()
    return f'{_path(loc)}: not a language; one of {", ".join(LANGUAGES)}'
  if detail['type'] == 'extra_forbidden':
    return f'{_path(loc)}: unknown key'
  if detail['type'] == 'missing':
    return f'{_path(loc)}: required'
  if detail['type'] == 'string_pattern_mismatch':
    return f'{_path(loc)}: {detail["input"]!r} is not a page; name one by its path relative to docs/, ending in .md'
  if detail['type'] == 'string_type' and loc[:1] == ['nav'] and isinstance(loc[-1], int):
    return f'{_path(loc)}: a nav entry is a page path under docs/ or a section {{title, pages}}; got {detail["input"]!r}'
  if detail['type'] in ('model_type', 'model_attributes_type', 'dict_type'):
    what = 'a quickstart block {code, install}' if loc[:1] == ['quickstart'] else 'a mapping'
    return f'{_path(loc)}: must be {what}; got {detail["input"]!r}'
  if detail['type'] == 'too_short':
    return f'{_path(loc)}: must not be empty'
  return f'{_path(loc)}: {detail["msg"]}'


def _path(loc: list[Any]) -> str:
  """A location as a key path: `nav[2].pages[0]`."""
  text = ''
  for part in loc:
    text += f'[{part}]' if isinstance(part, int) else f'.{part}' if text else str(part)
  return text or '(top level)'


def _pages(nav: list[NavEntry]) -> Iterator[str]:
  """Every page the nav names, sections walked in order."""
  for entry in nav:
    if isinstance(entry, NavSection):
      yield from _pages(entry.pages)
    else:
      yield entry


def _is_page(docs: Path, page: str) -> bool:
  """Whether `page` names a file under `docs`, not one reached by leaving it."""
  target = (docs / page).resolve()
  return target.is_file() and target.is_relative_to(docs.resolve())
