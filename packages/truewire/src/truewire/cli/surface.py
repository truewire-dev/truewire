"""`truewire surface`: reconcile every spec against the callables the package really has."""
import json
from collections import Counter

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from truewire.examples import build_response_status_statistics
from truewire.spec import client_example_coverage
from truewire.surface import BackendUnavailable, Reconciliation, reconcile

from .common import PATH_OPTION, PROJECT_OPTION, resolve_spec_scope

_SURFACE_STATUS_STYLES = {
  'generated': 'green',
  'hand-written': 'cyan',
  'absent': 'yellow',
}
"""Style per non-gap outcome. Anything else — one of `surface.Fault` — styles red."""


def _render_surface(result: Reconciliation, *, verbose: bool) -> None:
  """One row per spec worth reporting: what it resolves to, and why when it doesn't.

  Mirrors the non-verbose default of the plain-text report this replaced: `generated`
  and `hand-written` rows — the passing majority — only appear under `--verbose`;
  `absent` and every gap always do, since those are what a caller actually needs to act on.
  A gap's status is its bare `fault` (`no_module`, `stale`, ...), not prefixed with `gap`;
  the red style already marks it as one.
  """
  rows: list[tuple[str, str, str, str]] = []
  if verbose:
    rows.extend((function, 'generated', '', '') for function in result.generated)
    rows.extend((function, 'hand-written', '', '') for function in result.handwritten)
  rows.extend((function, 'absent', '', 'declared absent') for function in result.absent)
  rows.extend((gap.function, gap.fault, gap.path, gap.detail) for gap in result.gaps)
  rows.extend((
    function, 'no_validate_param', '',
    'accepts no `validate` parameter (docs/production_standards.md S8)',
  ) for function in result.missing_validate)
  if not rows:
    return

  table = Table(header_style='bold')
  table.add_column('function')
  table.add_column('status')
  table.add_column('path')
  table.add_column('detail')
  for function, status, path, detail in rows:
    style = _SURFACE_STATUS_STYLES.get(status, 'red')
    table.add_row(function, Text(status, style=style), path, detail)
  Console().print(table)


def surface(
  project: str | None = PROJECT_OPTION,
  path: str | None = PATH_OPTION,
  verbose: bool = typer.Option(False, '--verbose', '-v'),
  language: str = typer.Option('python', '--language', help='Codegen backend to resolve the layout with.'),
):
  """Check that every in-scope spec produces something a caller can actually call.

  A backend may exclude an endpoint it cannot emit, and that is legitimate — but the
  exclusion has to be said out loud. A backend that dropped every WebSocket endpoint by
  design once let seven new WebSocket specs pass every gate with no method anywhere in
  the package, because a silent skip is indistinguishable from a success. This asks the
  question none of the gates asks, and every answer is one of three: the backend generated
  the method, the spec's `surface` names a hand-written one that is really there, or the
  spec's `surface` records that there is none and why.

  **Example coverage is never consulted.** An unverified endpoint still generates a method
  — what is missing there is evidence, and `truewire examples` is where that is asked.
  Conflating the two would fail every unverified endpoint, and a check that fires on
  correct work gets switched off.

  Args:
    project: Project directory holding `truewire.toml`; defaults to the nearest one.
    path: Project root, or one subdivision of its `spec/endpoints`, to scope the report to.
    verbose: List every generated and hand-written endpoint, not only the gaps.
    language: Codegen backend to resolve the layout with.
  """
  class SurfaceError(Exception):
    pass

  loaded, scoped = resolve_spec_scope(project, path)
  client = loaded.name
  try:
    try:
      result = reconcile(loaded, scope=scoped.scope, language=language)
    except BackendUnavailable as exc:
      raise SurfaceError(f'{exc}\nWithout a backend nothing can be resolved, so nothing was checked.')

    if not result.total:
      # A gate reporting success over zero endpoints is the defect this check was built to
      # close, so it must not be the way this check itself passes.
      raise SurfaceError(f'{scoped.scope}: no endpoint specs found, so nothing was checked')

    typer.echo(f'Project: {client}')
    if scoped.is_subdivision:
      typer.echo(f'Scope: {scoped.scope.relative_to(scoped.endpoints_root)} (subdivision)')

    _render_surface(result, verbose=verbose)

    typer.echo('')
    typer.echo(
      f'Callables: {len(result.generated)} generated, {len(result.handwritten)} hand-written, '
      f'{len(result.absent)} declared absent, over {result.total} spec(s)'
    )
    failed = False
    if result.gaps:
      counts = Counter(gap.fault for gap in result.gaps)
      summary = ', '.join(f'{count} {fault}' for fault, count in sorted(counts.items()))
      typer.echo(
        f'{len(result.gaps)} spec(s) generate nothing a caller can call ({summary}) — see table above'
      )
      failed = True
    if result.missing_validate:
      typer.echo(
        f'{len(result.missing_validate)} generated method(s) accept no `validate` parameter '
        f'(docs/production_standards.md S8) — see table above'
      )
      failed = True
    if failed:
      raise typer.Exit(code=1)

  except SurfaceError as exc:
    typer.echo(str(exc), err=True)
    raise typer.Exit(code=1)
