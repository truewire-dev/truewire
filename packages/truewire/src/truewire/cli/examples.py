"""`truewire examples`: paired-example coverage per endpoint."""
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


def examples(
  project: str | None = PROJECT_OPTION,
  path: str | None = PATH_OPTION,
  verbose: bool = typer.Option(False, '--verbose', '-v'),
  require_verified: bool = typer.Option(
    False,
    '--require-verified',
    help=(
      'Fail when any endpoint lacks paired examples and is not declared `unverified` in '
      'its `endpoint.json` — see ADR 0001 (endpoint outcome taxonomy).'
    ),
  ),
):
  """Report paired-example coverage for a project's endpoints.

  For each endpoint in scope, checks whether it has a paired example — matching
  `*.request.json`/`*.response.json` for rpc over http, or `*.parameters.json` plus
  `*.reply.json`/`*.messages.json` for rpc over ws or stream — and reports totals by
  kind, auth, and a stale-`unverified`/partial-pair breakdown. Unlike `truewire check`,
  examples are never validated here, only counted.

  Args:
    project: Project directory holding `truewire.toml`; defaults to the nearest one.
    path: Project root, or one subdivision of its `spec/endpoints`, to scope the report to.
    verbose: List every endpoint missing examples or carrying a partial pair.
    require_verified: Fail when an endpoint lacks paired examples and is not declared
      `unverified`.
  """
  class ExamplesError(Exception):
    pass

  loaded, scoped = resolve_spec_scope(project, path)
  client = loaded.name
  try:
    endpoints_root = scoped.endpoints_root
    coverage = client_example_coverage(loaded, scope=scoped.scope)
    if not coverage:
      # A gate that reports success over zero endpoints is worse than no gate, because the
      # skill instructs agents to run it per subdivision and record the result.
      raise ExamplesError(f'{scoped.scope}: no endpoint specs found, so nothing was checked')

    totals = {
      'all': 0,
      'rpc': 0,
      'stream': 0,
      'grpc': 0,
      'with_examples': 0,
      'without_examples': 0,
      'unverified': 0,
      'stale_unverified': 0,
      'partial_examples': 0,
      'public': 0,
      'authed': 0,
      'public_with_examples': 0,
      'authed_with_examples': 0,
      'request_files': 0,
      'response_files': 0,
      'reply_files': 0,
      'message_files': 0,
      'parameter_files': 0,
      'protobuf_message_files': 0,
    }
    missing_no_files: list[str] = []
    partial: list[str] = []
    stale_unverified: list[str] = []
    response_statuses: list[int] = []

    for item in coverage:
      endpoint_path = item.endpoint_path
      endpoint = item.endpoint
      kind = item.kind
      meta = endpoint.meta or (endpoint.openapi.security if endpoint.openapi is not None else None)
      totals['all'] += 1
      totals[kind] += 1
      totals['request_files'] += len(item.request_files)
      totals['response_files'] += len(item.response_files)
      totals['parameter_files'] += len(item.parameter_files)
      totals['reply_files'] += len(item.reply_files)
      totals['message_files'] += len(item.message_files)
      totals['protobuf_message_files'] += len(item.protobuf_message_files)
      for response_file in item.response_files:
        try:
          payload = json.loads(response_file.read_text())
        except json.JSONDecodeError:
          continue
        if isinstance(payload, dict) and isinstance(payload.get('status'), int):
          response_statuses.append(payload['status'])

      if meta:
        totals['authed'] += 1
      else:
        totals['public'] += 1

      rel = str(endpoint_path.parent.relative_to(endpoints_root))
      if item.has_examples:
        totals['with_examples'] += 1
        if meta:
          totals['authed_with_examples'] += 1
        else:
          totals['public_with_examples'] += 1
        if endpoint.unverified is not None:
          # A captured example is evidence the call was made; a lingering `unverified`
          # declaration next to it is stale, the same way `surface` is checked against
          # the backend so a declaration the project has outgrown fails too.
          totals['stale_unverified'] += 1
          stale_unverified.append(rel)
      else:
        totals['without_examples'] += 1
        if endpoint.unverified is not None:
          totals['unverified'] += 1
        if item.is_partial:
          totals['partial_examples'] += 1
          partial.append(rel)
        else:
          missing_no_files.append(rel)

    typer.echo(f'Project: {client}')
    if scoped.is_subdivision:
      typer.echo(f'Scope: {scoped.scope.relative_to(scoped.endpoints_root)} (subdivision)')
    kind_breakdown = f'rpc={totals["rpc"]}, stream={totals["stream"]}'
    if totals['grpc']:
      kind_breakdown += f', grpc={totals["grpc"]}'
    typer.echo(f'Endpoints: {totals["all"]} ({kind_breakdown})')
    coverage_pct = totals['with_examples'] / totals['all']
    coverage_style = 'green' if coverage_pct >= 0.9 else 'yellow' if coverage_pct >= 0.5 else 'red'
    Console().print(
      Text('Coverage (paired examples): ')
      + Text(f'{totals["with_examples"]}/{totals["all"]} ({coverage_pct:.0%})', style=coverage_style)
    )
    typer.echo()

    typer.echo(f'Public  {totals["public_with_examples"]:>4}/{totals["public"]:<4} with examples')
    typer.echo(f'Authed  {totals["authed_with_examples"]:>4}/{totals["authed"]:<4} with examples')

    if totals['partial_examples'] or totals['unverified']:
      typer.echo()
      if totals['partial_examples']:
        typer.echo(
          f'Incomplete: {totals["partial_examples"]} endpoint(s) have example files but no full pair'
        )
      if totals['unverified']:
        typer.echo(
          f'Unverified: {totals["unverified"]} endpoint(s) declared `unverified` (see ADR 0001)'
        )

    file_counts = {
      'requests': totals['request_files'],
      'responses': totals['response_files'],
      'parameters': totals['parameter_files'],
      'replies': totals['reply_files'],
      'messages': totals['message_files'],
      'protobuf': totals['protobuf_message_files'],
    }
    present_files = {name: count for name, count in file_counts.items() if count}
    typer.echo()
    typer.echo('Example files:')
    if present_files:
      for name, count in present_files.items():
        typer.echo(f'  {name:<11} {count}')
    else:
      typer.echo('  none')

    status_stats = build_response_status_statistics(response_statuses)['all']
    typer.echo()
    typer.echo('Response codes:')
    if status_stats['count']:
      for code, count in status_stats['by_status'].items():
        typer.echo(f'  {code:<11} {count}')
    else:
      typer.echo('  none')

    if verbose and (missing_no_files or partial):
      typer.echo('')
      if missing_no_files:
        typer.echo('No example files:')
        for path in missing_no_files:
          typer.echo(path)
      if partial:
        typer.echo('')
        typer.echo('Incomplete examples (orphan request/response or ws files):')
        for path in partial:
          typer.echo(path)

    if totals['stale_unverified']:
      # Unconditional: a stale `unverified` declaration is a spec correctness bug, not a
      # coverage gap, so it fails regardless of `--require-verified`.
      if verbose:
        typer.echo('')
        typer.echo('Stale `unverified` declarations (paired examples already exist):')
        for path in stale_unverified:
          typer.echo(path)
      raise ExamplesError(
        f'{totals["stale_unverified"]} endpoint(s) declare `unverified` despite having '
        f'paired examples; remove the stale declaration (rerun with --verbose to list them)'
      )

    if require_verified:
      # `unverified` is populated from `Endpoint.unverified` — see
      # `truewire.spec.endpoint.Unverified` and ADR 0001 (endpoint outcome
      # taxonomy). A declared-unverified endpoint is
      # excused from this gate; everything else lacking paired examples still fails it.
      unverifiable = totals['unverified']
      remaining = totals['without_examples'] - unverifiable
      if remaining > 0:
        raise ExamplesError(
          f'{remaining} endpoint(s) without paired examples, '
          f'{unverifiable} marked unverified; --require-verified failed'
        )

  except ExamplesError as exc:
    typer.echo(str(exc), err=True)
    raise typer.Exit(code=1)
