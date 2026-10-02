"""`truewire surface --language go`: the same three answers as the Python check, for a Go
package.

- **generated**: the Go backend renders the endpoint, and its package on disk defines the
  method on its `Endpoint` struct.
- **hand-written**: the spec's `surface` names a method, and a `.go` file in the package of
  the endpoint's parent router defines a method of that name (`PascalCase` of the symbol's
  method part) on some type: `spot.account.retrieve_export:retrieve_export` is
  `func (r *Account) RetrieveExport(` in `spot/account/`. The Go convention for a
  hand-written method is a file beside the generated router, whose struct keeps its core.
- **absent**: the spec's `surface` says there is none.

Files are read, never compiled, for the same reason the Python check parses rather than
imports: the question has to be answerable against a package mid-generation.
"""
import re
from pathlib import Path

from truewire.codegen.go import render_package
from truewire.codegen.go.endpoint import endpoint_dirs
from truewire.codegen.go.names import pascal_ident
from truewire.plan.build import build_plan
from truewire.project import Project, resolve
from truewire.spec import AbsentSurface, HandwrittenSurface, endpoint_records

from .check import BackendUnavailable, Gap, Reconciliation


def _defines(directory: Path, method: str) -> bool:
  """Whether a non-test `.go` file in `directory` declares a method named `method`."""
  pattern = re.compile(rf'^func \(\w+ \*?\w+(?:\[[^\]]*\])?\) {re.escape(method)}\(', re.MULTILINE)
  if not directory.is_dir():
    return False
  for file in sorted(directory.glob('*.go')):
    if file.name.endswith('_test.go'):
      continue
    try:
      if pattern.search(file.read_text()):
        return True
    except OSError:
      continue
  return False


def reconcile_go(root: Path | Project, *, scope: Path | None = None) -> Reconciliation:
  """Classify every in-scope spec by what a caller of the Go package can call.

  Raises:
    BackendUnavailable: The project declares no `[go]` section.
  """
  project = resolve(root)
  if project.go is None:
    raise BackendUnavailable(f'{project.root}: truewire.toml declares no [go] section')
  package = project.go_package_dir
  rendered = render_package(build_plan(project), project)
  skipped = {note.split(':', 1)[0]: note.split(':', 1)[1].strip() for note in rendered.skipped if ':' in note}
  endpoints_root = project.endpoints_dir
  out = Reconciliation()
  for record in endpoint_records(project, scope=scope):
    endpoint = record.endpoint
    function = endpoint.resolved_function(record.path, project.spec_dir)
    path = function.split('.')
    rel = record.path.parent.relative_to(endpoints_root).as_posix()
    surface = endpoint.surface
    if isinstance(surface, AbsentSurface):
      out.absent.append(function)
      continue
    if isinstance(surface, HandwrittenSurface):
      method = pascal_ident(surface.symbol.partition(':')[2] or path[-1])
      directory = package.joinpath(*endpoint_dirs(path[:-1]))
      if _defines(directory, method):
        out.handwritten.append(function)
      else:
        where = directory.relative_to(package).as_posix() or '.'
        out.gaps.append(Gap(
          function=function, path=rel, fault='no_symbol',
          detail=f'{surface.symbol}: no method {method} in the Go package {where}/',
        ))
      continue
    dirs = endpoint_dirs(path)
    file = '/'.join([*dirs, f'{dirs[-1]}.go'])
    if file not in rendered.files:
      out.gaps.append(Gap(
        function=function, path=rel, fault='undeclared',
        detail=f'skipped by the go backend ({skipped.get(function, "not rendered")}); no `surface` declared',
      ))
    elif not (package / file).is_file():
      out.gaps.append(Gap(function=function, path=rel, fault='no_module', detail=f'{file} not in package; needs regenerating'))
    elif not _defines(package.joinpath(*dirs), pascal_ident(path[-1], fallback='Call')):
      out.gaps.append(Gap(function=function, path=rel, fault='no_method', detail=f'{file} defines no {pascal_ident(path[-1])!r}'))
    else:
      out.generated.append(function)
  return out


__all__ = ['reconcile_go']
