"""
Exercise `truewire.standards.docstrings.check_docstrings` against synthetic `.py` fixtures,
never `clients/` -- per the isolation contract `test_mock.py`'s module docstring records,
this must pass in a checkout with zero clients.

S3 (`docs/production_standards.md`) only checks two mechanizable failure modes here: a
multi-line docstring's closing triple-quote sharing a line with prose, and a section header
outside `{Args, Returns, Raises, Examples, References}`. Docstring *presence* and the
blockquote-only-link clause are out of scope, per `docs/TODO.md` T10.
"""
from pathlib import Path

from truewire.standards.docstrings import check_docstrings


def write_module(root: Path, *, name: str, source: str) -> None:
  """
  Write one `.py` file under a synthetic `pkg/src` tree.

  Args:
    root: `pkg/src` directory; created if absent.
    name: File name, e.g. `'widget.py'`.
    source: Full file contents.
  """
  root.mkdir(parents=True, exist_ok=True)
  (root / name).write_text(source)


def test_clean_multiline_docstring_produces_nothing(tmp_path):
  """Closing quotes alone on their own line, only standard sections -- no findings."""
  write_module(
    tmp_path / 'pkg' / 'src', name='widget.py', source='''"""Widget module."""


def get_widget(name: str) -> str:
  """
  Fetch one widget by name.

  Args:
    name: Widget name.

  Returns:
    The widget.
  """
  return name
''',
  )

  findings = check_docstrings(tmp_path / 'pkg' / 'src')

  assert findings == []


def test_closing_quotes_sharing_a_line_with_prose_is_flagged(tmp_path):
  """A multi-line docstring whose last line has prose right before its closing quotes."""
  write_module(
    tmp_path / 'pkg' / 'src', name='widget.py', source='''def get_widget(name: str) -> str:
  """
  Fetch one widget by name.
  Returns the widget."""
  return name
''',
  )

  findings = check_docstrings(tmp_path / 'pkg' / 'src')

  assert len(findings) == 1
  assert findings[0]['rule'] == 'S3'
  assert findings[0]['severity'] == 'warning'
  assert 'closing' in findings[0]['message']
  assert findings[0]['location'] == 'pkg/src/widget.py:2'


def test_nonstandard_section_header_is_flagged(tmp_path):
  """A `Note:` section is not in the standard vocabulary."""
  write_module(
    tmp_path / 'pkg' / 'src', name='widget.py', source='''def get_widget(name: str) -> str:
  """
  Fetch one widget by name.

  Note:
    This is a side note.
  """
  return name
''',
  )

  findings = check_docstrings(tmp_path / 'pkg' / 'src')

  assert len(findings) == 1
  assert findings[0]['rule'] == 'S3'
  assert '`Note:`' in findings[0]['message']


def test_args_bullet_with_inline_description_is_not_a_section_header(tmp_path):
  """`name: Description.` has content after its colon -- never mistaken for a header."""
  write_module(
    tmp_path / 'pkg' / 'src', name='widget.py', source='''def get_widget(name: str) -> str:
  """
  Fetch one widget by name.

  Args:
    name: Widget name.
    validate: Validate the response against the generated schema.
  """
  return name
''',
  )

  findings = check_docstrings(tmp_path / 'pkg' / 'src')

  assert findings == []


def test_single_line_docstring_never_flagged_for_closing_quotes(tmp_path):
  """A one-line docstring has no 'own line' for the closing quote to be on."""
  write_module(
    tmp_path / 'pkg' / 'src', name='widget.py', source='''def get_widget(name: str) -> str:
  """Fetch one widget by name."""
  return name
''',
  )

  findings = check_docstrings(tmp_path / 'pkg' / 'src')

  assert findings == []


def test_module_and_class_docstrings_are_checked_too(tmp_path):
  """The module- and class-level docstrings get the same two checks as functions."""
  write_module(
    tmp_path / 'pkg' / 'src', name='widget.py', source='''"""
Widget module.
Trailing prose butts against the quotes."""


class Widget:
  """
  A widget.

  Params:
    name: Widget name.
  """
''',
  )

  findings = check_docstrings(tmp_path / 'pkg' / 'src')

  locations = {f['location'] for f in findings}
  assert 'pkg/src/widget.py:1' in locations
  assert any('`Params:`' in f['message'] for f in findings)
