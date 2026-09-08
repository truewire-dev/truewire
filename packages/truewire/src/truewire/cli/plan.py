"""`truewire plan`: print the plan a backend renders from -- a summary, or the JSON."""
import json

import typer

from .common import PROJECT_OPTION, resolve_project


def _describe(plan) -> list[str]:
  """One line per router and per endpoint: what a reader wants before opening the JSON."""
  from truewire.plan.model import EndpointPlan, PackagePlan

  assert isinstance(plan, PackagePlan)
  lines = [
    f'{plan.name}: root class {plan.root_class}, {len(plan.routers)} routers, '
    f'{len(plan.endpoints)} endpoints, {sum(len(s) for s in plan.schemas.values())} shared '
    f'types in {len(plan.schemas)} scope(s), cores: {", ".join(sorted(plan.cores)) or "none"}',
    '',
    'routers:',
  ]
  for router in plan.routers:
    children = ', '.join(
      f'{child.name}/' if child.kind == 'router' else child.name for child in router.children
    )
    lines.append(f'  {".".join(router.path) or "<root>"}  core={router.core}  children: {children}')
  lines.append('')
  lines.append('endpoints:')
  for endpoint in plan.endpoints:
    lines.append('  ' + _endpoint_line(endpoint))
  return lines


def _endpoint_line(endpoint) -> str:
  from truewire.plan.model import EndpointPlan

  assert isinstance(endpoint, EndpointPlan)
  wire = endpoint.wire
  if endpoint.kind == 'stream':
    where = f'channel {wire.channel}'
  else:
    where = f'{wire.method + " " if wire.method else ""}{wire.path}'
  request = endpoint.request
  shape = request.shape if request.shape != 'fields' else f'{len(request.fields)} fields'
  parts = [
    endpoint.function, endpoint.kind, '+'.join(endpoint.transports), where,
    f'core={endpoint.core}', f'request={shape}', f'returns={endpoint.response.payload or "nothing"}',
  ]
  if endpoint.response.selector:
    parts.append(f'selector={endpoint.response.selector}')
  if endpoint.response.optional:
    parts.append('nullable')
  pagination = endpoint.pagination
  if pagination is not None:
    parts.append(
      f'paged={pagination.strategy}/{pagination.done.get("kind")} walker={pagination.walker} '
      f'driver={pagination.driver}'
    )
  if endpoint.stream is not None:
    if endpoint.stream.connect_only:
      parts.append('connect-only')
    if endpoint.stream.direct_channel:
      parts.append('direct-channel')
  if endpoint.deprecated:
    parts.append('deprecated')
  return '  '.join(parts)


def plan(
  project: str | None = PROJECT_OPTION,
  as_json: bool = typer.Option(False, '--json', help='Print the whole plan as JSON.'),
):
  """Print the plan: every decision a backend renders, computed from the spec and `truewire.toml`.

  Args:
    project: Project directory (holding `truewire.toml`); the nearest one by default.
    as_json: Print the full plan as JSON (`docs/plan.md` describes the shape) instead of
      the one-line-per-endpoint summary.
  """
  from truewire.plan.build import build_plan

  loaded = resolve_project(project)
  try:
    built = build_plan(loaded)
  except ValueError as exc:
    typer.echo(str(exc), err=True)
    raise typer.Exit(code=1)
  if as_json:
    typer.echo(json.dumps(built.to_json(), indent=2))
    return
  for line in _describe(built):
    typer.echo(line)
