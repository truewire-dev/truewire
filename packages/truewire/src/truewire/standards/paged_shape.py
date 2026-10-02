"""
Check standard S24: every generated `_paged` method returns a
`truewire_core.PaginatedResponse`.

ADR 0013: a bare async generator that raises is dead, so no downstream caller can retry the
one page that failed -- the SDK's own retry middleware wraps only coroutine functions for
exactly that reason. `PaginatedResponse` makes every page one pure `next(state)` call. The
generator renders nothing else now, so a bare `AsyncIterator`-returning `_paged` method can
only come from a hand-written module or a stale, un-regenerated one, and either is a real
gap in a project's public surface.
"""
import ast
from pathlib import Path

from .finding import Finding


def _returns_paginated_response(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
  """Whether a method's return annotation is `PaginatedResponse[...]` (or a bare
  `PaginatedResponse`)."""
  annotation = node.returns
  if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
    try:
      annotation = ast.parse(annotation.value, mode='eval').body
    except SyntaxError:
      return False
  if isinstance(annotation, ast.Subscript):
    annotation = annotation.value
  return (
    isinstance(annotation, ast.Name) and annotation.id == 'PaginatedResponse'
    or isinstance(annotation, ast.Attribute) and annotation.attr == 'PaginatedResponse'
  )


def check_paged_shape(pkg_src_root: Path) -> list[Finding]:
  """
  Rule S24: every `def <name>_paged` method under a project's package directory is annotated to
  return `PaginatedResponse`.

  Args:
    pkg_src_root: The generated package directory.
  """
  out: list[Finding] = []
  modules: dict[str, tuple[Path, ast.Module]] = {}
  classes: dict[str, ast.ClassDef] = {}
  imports: dict[str, str] = {}
  for file in sorted(pkg_src_root.rglob('*.py')):
    try:
      tree = ast.parse(file.read_text(), filename=str(file))
    except SyntaxError:
      continue
    parts = file.relative_to(pkg_src_root.parent).with_suffix('').parts
    module = '.'.join(parts[:-1] if parts[-1] == '__init__' else parts)
    package = module if file.name == '__init__.py' else module.rpartition('.')[0]
    modules[module] = file, tree
    for node in tree.body:
      if isinstance(node, ast.ClassDef):
        classes[f'{module}.{node.name}'] = node
      elif isinstance(node, ast.Import):
        for alias in node.names:
          local = alias.asname or alias.name.split('.')[0]
          imports[f'{module}.{local}'] = alias.name if alias.asname else local
      elif isinstance(node, ast.ImportFrom):
        source = node.module or ''
        if node.level:
          prefix = package.split('.')[:len(package.split('.')) - node.level + 1]
          source = '.'.join([*prefix, *([source] if source else [])])
        for alias in node.names:
          imports[f'{module}.{alias.asname or alias.name}'] = f'{source}.{alias.name}'

  def resolve_class(symbol: str, seen: set[str]) -> str | None:
    """Follow local import aliases and package re-exports without importing client code."""
    if symbol in seen:
      return None
    seen.add(symbol)
    if symbol in classes:
      return symbol
    parts = symbol.split('.')
    for length in range(len(parts), 0, -1):
      prefix = '.'.join(parts[:length])
      if prefix in imports:
        target = '.'.join([imports[prefix], *parts[length:]])
        return resolve_class(target, seen)
    return None

  def methods(node: ast.ClassDef, module: str, seen: set[str]) -> set[str]:
    """Collect methods from this class and statically resolvable package bases."""
    defined = {
      member.name for member in node.body
      if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for base in node.bases:
      if isinstance(base, ast.Subscript):
        base = base.value
      if not isinstance(base, (ast.Name, ast.Attribute)):
        continue
      symbol = resolve_class(f'{module}.{ast.unparse(base)}', set())
      if symbol is not None and symbol not in seen:
        seen.add(symbol)
        defined.update(methods(classes[symbol], symbol.rpartition('.')[0], seen))
    return defined

  for module, (file, tree) in modules.items():
    path = str(file.relative_to(pkg_src_root.parent.parent))
    for node in ast.walk(tree):
      if not isinstance(node, ast.ClassDef):
        continue
      defined = methods(node, module, set())
      for member in node.body:
        if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
          continue
        # A generated walk always has a local or inherited single-request method it drives; a
        # method merely *named* `*_paged` with no such sibling is an endpoint the venue
        # itself calls that, not a walk.
        if not member.name.endswith('_paged') or member.name[: -len('_paged')] not in defined:
          continue
        if _returns_paginated_response(member):
          continue
        shape = 'an async generator' if isinstance(member, ast.AsyncFunctionDef) else 'something else'
        out.append(Finding(
          rule='S24',
          location=f'{path}:{member.lineno}',
          message=(
            f'`{member.name}` returns {shape}, not `PaginatedResponse` -- a page of it can '
            f'never be retried or resumed; regenerate the module, or return '
            f'`truewire_core.PaginatedResponse` (ADR 0013)'
          ),
          severity='error',
        ))
  return out
