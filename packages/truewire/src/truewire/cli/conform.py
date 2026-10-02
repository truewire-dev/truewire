"""`truewire conform`: one project, one live run, one report (ADR 0012, phase one)."""

import asyncio
from datetime import date, datetime, timezone
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path

import typer

from .common import PROJECT_OPTION, resolve_project


def conform(
  state: Path = typer.Option(..., '--state', help='Directory reports and the ledger are written under, as <state>/<project>/.'),
  only: list[str] = typer.Option(
    [], '--only', help='Glob on the function (`spot.market_data.*`) or the directory under spec/endpoints. Repeatable.',
  ),
  allow: list[str] = typer.Option(
    [], '--allow', help='Glob naming a non-GET endpoint that only reads (a POST query), so it is called. Never a write. Repeatable.',
  ),
  new: list[str] = typer.Option(
    [], '--new', help='key=value passed to the root client\'s `.new(...)`; JSON values are decoded. Repeatable.',
  ),
  interval: float = typer.Option(
    1.0, '--interval', help='Seconds between two calls, at least. The core\'s own rate limiter applies on top.',
  ),
  timeout: float = typer.Option(60.0, '--timeout', help='Seconds one call may take before it is an error.'),
  day: str | None = typer.Option(
    None, '--date', help='The run\'s date, YYYY-MM-DD (UTC today by default). Names the report and dates the ledger.',
  ),
  project: str | None = PROJECT_OPTION,
):
  """Call every recorded HTTP request live, once, and report what changed.

  For each `rpc` endpoint over HTTP with a recorded `<id>.request.json`, the call goes
  through the project's generated Python client and hand-written core with validation on.
  The raw body is validated against the response schema (a failure is `drift`: the API
  changed), the client's own handling of the same response is checked (a body the schema
  accepts and the client rejects is `client:python`: our code is wrong), and the body's
  shape is diffed against the recorded `<id>.response.json` (keys, types, nullability,
  element shape, status; values never). Endpoints with no recording, streams, calls the
  core refuses for want of credentials, and anything but a GET not named by `--allow`
  (phase one sends no writes) are listed as skipped, with the reason.

  Writes `<state>/<project>/<date>.json`, `<date>.md` and `ledger.json`, where every
  finding keeps the date it was first seen. Sequential, one call per recorded request,
  at least `--interval` seconds apart. Nothing under `spec/` is written.

  Exits 1 when any finding exists, 2 when there is none but a call errored, 0 otherwise.
  """
  from truewire.conform.report import Ledger, report_json, report_markdown
  from truewire.conform.run import Conform
  from truewire.mcp import load_client, parse_new_kwargs

  loaded = resolve_project(project)
  try:
    new_kwargs = parse_new_kwargs(new)
  except ValueError as exc:
    typer.echo(str(exc), err=True)
    raise typer.Exit(code=1)
  try:
    today = (date.fromisoformat(day) if day else datetime.now(timezone.utc).date()).isoformat()
  except ValueError:
    typer.echo(f'--date must be YYYY-MM-DD, got {day!r}', err=True)
    raise typer.Exit(code=1)
  out = state.resolve() / loaded.name
  if out.is_relative_to(loaded.spec_dir.resolve()):
    typer.echo('--state must not be inside the spec tree: a conformance run writes nothing there', err=True)
    raise typer.Exit(code=1)

  started = datetime.now(timezone.utc)
  client = load_client(loaded, new_kwargs)
  runner = Conform(loaded, client, interval=interval, timeout=timeout, allow=allow, echo=typer.echo,
                   today=date.fromisoformat(today))
  results = asyncio.run(runner.run(only))
  finished = datetime.now(timezone.utc)

  out.mkdir(parents=True, exist_ok=True)
  ledger_path = out / 'ledger.json'
  ledger = Ledger.load(ledger_path, loaded.name)
  resolved = ledger.record(results, today, complete=not only)
  meta = {
    'truewire': toolchain_version(),
    'started_at': started.isoformat(timespec='seconds'),
    'finished_at': finished.isoformat(timespec='seconds'),
    'only': only or None,
    'allow': allow or None,
  }
  report = report_json(loaded.name, today, meta, results, resolved)
  (out / f'{today}.json').write_text(json.dumps(report, indent=2) + '\n')
  (out / f'{today}.md').write_text(report_markdown(report))
  ledger.save(ledger_path)

  counts = report['counts']
  findings = sum(len(e.get('findings', [])) for p in report['endpoints'] for e in p.get('examples', []))
  typer.echo('')
  typer.echo(' '.join(f'{key}={value}' for key, value in counts.items())
             + f' findings={findings} resolved={len(resolved)}')
  typer.echo(f'  {out / f"{today}.json"}')
  typer.echo(f'  {out / f"{today}.md"}')
  if findings:
    raise typer.Exit(code=1)
  if counts['error']:
    raise typer.Exit(code=2)


def toolchain_version() -> str:
  try:
    return version('truewire')
  except PackageNotFoundError:
    return 'unknown'
