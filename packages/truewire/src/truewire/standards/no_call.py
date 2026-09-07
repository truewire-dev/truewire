"""
Check `docs/production_standards.md` S29: no generated method is named `__call__`.

`docs/spec/authoring.md` rule 0 and S11 already require one endpoint method per module,
composed onto its parent through inheritance (`Trade(GetOrder, PlaceOrder, ...)`) rather
than exposed through a wrapping property. A `__call__`-only leaf class, reached through a
`cached_property` (`client.spot.trade.cancel_order()` where `cancel_order` returns a
callable object rather than being the method itself), is never necessary: a leaf endpoint
class already subclasses its family's shared, undecorated dataclass base directly and
defines no `__init__` of its own, so multiply-inheriting several of them onto their parent
-- giving each a real, distinctly-named method instead -- works at any depth, including a
client's outermost transport root. The mixed-children case that first exposed the bug this
check guards against: a leaf with both a named sibling method and a `__call__` produced two
separate, disagreeing docstrings for the same call.
"""
import ast
from pathlib import Path

from .finding import Finding


def _call_methods(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
  """Every `def __call__`/`async def __call__` method defined directly inside a class body
  in `tree` -- a module-level function literally named `__call__` isn't a method and isn't
  this rule's concern."""
  out: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
  for node in ast.walk(tree):
    if not isinstance(node, ast.ClassDef):
      continue
    for member in node.body:
      if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) and member.name == '__call__':
        out.append(member)
  return out


def check_no_call_methods(pkg_src_root: Path) -> list[Finding]:
  """
  Rule S29: no class under a project's package defines `__call__`.

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
    for method in _call_methods(tree):
      out.append(Finding(
        rule='S29',
        location=f'{path}:{method.lineno}',
        message=(
          'class defines `__call__` -- give it a real, distinctly-named method and '
          'inherit it onto its parent directly instead (production_standards.md S29)'
        ),
        severity='error',
      ))
  return out
