"""
Check `docs/production_standards.md` S3: every docstring parses under the Google-compatible
section style (`Args:`/`Returns:`/`Raises:`/`Examples:`/`References:`).

Griffe was considered instead of a hand-rolled checker -- it is not a declared dependency
of truewire, so this stays stdlib-`ast`-only rather than adding one.

Only the two mechanizable failure modes are checked here, not
docstring *presence* (a separate, already-partially-documented concern) and not the
blockquote-only-link clause (no spec-local signal for it -- prose can quote a URL in either
shape and both render fine).
"""
import ast
from pathlib import Path

from truewire.standards.finding import Finding

SECTION_VOCABULARY = frozenset({'Args', 'Returns', 'Raises', 'Examples', 'References'})
"""Section headers the Google-compatible docstring style sanctions."""

DocNode = ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
"""Node kinds `ast.get_docstring` accepts, and the ones this check walks."""


def _docstring_expr(node: DocNode) -> ast.Expr | None:
  """
  Return the docstring's own literal-string statement, or `None` if `node` has none.

  `ast.get_docstring` returns the docstring's *content*; getting its exact source
  formatting (needed for the closing-quote check) requires the statement node itself.

  Args:
    node: Module, class, or function definition to inspect.
  """
  body = node.body
  if not body:
    return None
  first = body[0]
  if (
    isinstance(first, ast.Expr)
    and isinstance(first.value, ast.Constant)
    and isinstance(first.value.value, str)
  ):
    return first
  return None


def _check_closing_quotes(
  source: str, path: str, expr: ast.Expr,
) -> Finding | None:
  """
  Flag a multi-line docstring whose closing triple-quote shares a line with prose.

  Args:
    source: Full text of the file `expr` was parsed from.
    path: File path to report, relative to the scanned root.
    expr: The docstring's own literal-string statement.
  """
  segment = ast.get_source_segment(source, expr)
  if segment is None or '\n' not in segment:
    return None
  quote = segment[-3:]
  if quote not in ('"""', "'''"):
    return None
  last_line = segment.splitlines()[-1]
  before_quote = last_line[:-3] if last_line.endswith(quote) else last_line
  if not before_quote.strip():
    return None
  return Finding(
    rule='S3',
    location=f'{path}:{expr.lineno}',
    message=(
      f'multi-line docstring\'s closing {quote} shares a line with prose; put it alone '
      f'on its own line'
    ),
    severity='warning',
  )


def _section_header_word(line: str) -> str | None:
  """
  Return the header word/phrase of a line shaped like `Word:` or `Word Word:`, or `None`.

  Deliberately narrow: every word must be capitalized and nothing may follow the colon but
  whitespace, so an `Args:`-style bullet (`name: Description.`) never matches -- it has
  real content after its colon.

  Args:
    line: One line of docstring text, indentation included.
  """
  stripped = line.strip()
  if not stripped.endswith(':'):
    return None
  words = stripped[:-1].split(' ')
  if not words or not all(word and word[0].isupper() and word.isalpha() for word in words):
    return None
  return ' '.join(words)


def _check_section_vocabulary(path: str, expr: ast.Expr) -> list[Finding]:
  """
  Flag a section-header-shaped line whose word isn't in `SECTION_VOCABULARY`.

  Args:
    path: File path to report, relative to the scanned root.
    expr: The docstring's own literal-string statement.
  """
  content = expr.value.value
  assert isinstance(content, str)
  out: list[Finding] = []
  for offset, line in enumerate(content.splitlines()):
    header = _section_header_word(line)
    if header is None or header in SECTION_VOCABULARY:
      continue
    out.append(Finding(
      rule='S3',
      location=f'{path}:{expr.lineno + offset}',
      message=(
        f'`{header}:` is not a standard docstring section; use one of '
        f'{sorted(SECTION_VOCABULARY)}'
      ),
      severity='warning',
    ))
  return out


def _check_node(source: str, path: str, node: DocNode) -> list[Finding]:
  """
  Run both checks against one module/class/function's docstring, if it has one.

  Args:
    source: Full text of the file `node` was parsed from.
    path: File path to report, relative to the scanned root.
    node: Module, class, or function definition to inspect.
  """
  if ast.get_docstring(node, clean=False) is None:
    return []
  expr = _docstring_expr(node)
  if expr is None:
    return []
  out: list[Finding] = []
  closing = _check_closing_quotes(source, path, expr)
  if closing is not None:
    out.append(closing)
  out.extend(_check_section_vocabulary(path, expr))
  return out


def check_docstrings(pkg_src_root: Path) -> list[Finding]:
  """
  Rule S3: every docstring under a project's package parses in the standard section style.

  Args:
    pkg_src_root: The directory holding the project's Python package(s).
  """
  out: list[Finding] = []
  for file in sorted(pkg_src_root.rglob('*.py')):
    source = file.read_text()
    try:
      tree = ast.parse(source, filename=str(file))
    except SyntaxError:
      continue
    path = str(file.relative_to(pkg_src_root.parent.parent))
    out.extend(_check_node(source, path, tree))
    for node in ast.walk(tree):
      if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        out.extend(_check_node(source, path, node))
  return out
