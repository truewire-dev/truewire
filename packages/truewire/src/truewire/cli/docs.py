from collections import Counter
from pathlib import Path
import json
import shutil

import typer

from truewire.docs import Finding, PyrightUnavailable, check_docs, check_docs_yml, lint_docs, report
from truewire.project import PROJECT_FILE, NotAProject, Project, load_project
from truewire.schemas.config import LANGUAGES
from .common import PROJECT_OPTION, resolve_project

def _root(project: str | None, path: str | None) -> tuple[str, Path]:
  """Resolve the directory a docs command runs against: `--path` verbatim, else the project root."""
  if path is not None:
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
      typer.echo(f'Not a directory: {root}')
      raise typer.Exit(code=1)
    return root.name, root
  loaded = resolve_project(project)
  return loaded.name, loaded.root


def _target(project: str | None, path: str | None) -> tuple[str, Path, Project | None]:
  """The name and directory a docs command runs against, and the project there: `--path`
  verbatim with the `truewire.toml` it holds, if any, else the resolved project."""
  if path is None:
    loaded = resolve_project(project)
    return loaded.name, loaded.root, loaded
  root = Path(path).expanduser().resolve()
  if not root.is_dir():
    typer.echo(f'Not a directory: {root}')
    raise typer.Exit(code=1)
  if not (root / PROJECT_FILE).is_file():
    return root.name, root, None
  try:
    return root.name, root, load_project(root)
  except NotAProject as error:
    typer.echo(str(error))
    raise typer.Exit(code=1)


def _declared(project: Project | None) -> tuple[str, ...] | None:
  """The languages `project` declares, or None when there is no project to hold docs to."""
  if project is None:
    return None
  return tuple(language for language in LANGUAGES if getattr(project.config, language) is not None)


def check(
  project: str | None = PROJECT_OPTION,
  path: str | None = typer.Option(None, '--path', help='Directory holding README.md and docs/; defaults to the project root.'),
):
  """Validate `docs/docs.yml` and type-check every python example in a project's published docs.

  `docs/docs.yml` is checked against its schema (an unknown key, a wrong type), every page
  its nav names must exist under `docs/`, and its quickstart needs one block per language
  `truewire.toml` declares (W10). Then pyright runs over the blocks of `README.md` and
  `docs/`, against the project's own package source. Nothing is executed and no credential is read: a doc example that calls
  a method the package does not have is a type error, not a failed request.
  """
  client, root, loaded = _target(project, path)

  problems = check_docs_yml(root, _declared(loaded))
  for problem in problems:
    typer.echo(problem)

  try:
    examples, diagnostics = check_docs(root)
  except PyrightUnavailable as e:
    typer.echo(str(e))
    raise typer.Exit(code=1)

  for line in report(root, diagnostics):
    typer.echo(line)

  errors = [d for d in diagnostics if d.severity == 'error']
  warnings = len(diagnostics) - len(errors)
  pages = len({example.path for example in examples})
  wrapped = sum(1 for example in examples if example.wrapped)
  summary = (
    f'{len(examples)} block(s) across {pages} page(s), {wrapped} wrapped for top-level await'
  )
  if errors or problems:
    typer.echo('')
    if problems:
      typer.echo(f'{len(problems)} problem(s) in docs/docs.yml')
    typer.echo(f'{summary}: {len(errors)} error(s), {warnings} warning(s)')
    raise typer.Exit(code=1)

  typer.echo(f'Docs type check: {client}: {summary}: OK')


def lint(
  project: str | None = PROJECT_OPTION,
  path: str | None = typer.Option(None, '--path', help='Directory holding README.md and docs/; defaults to the project root.'),
):
  """Report audience, export, verbosity, and internal-link defects in published docs."""
  client, root = _root(project, path)

  findings = lint_docs(root)
  _print_lint_findings(root, findings)

  if findings:
    counts = Counter(finding.rule for finding in findings)
    typer.echo('')
    summary = ', '.join(f'{count} {rule}' for rule, count in sorted(counts.items()))
    typer.echo(f'{len(findings)} finding(s): {summary}')
    raise typer.Exit(code=1)

  typer.echo(f'Docs lint: {client}: OK')


def _print_lint_findings(root: Path, findings: list[Finding]) -> None:
  """Print docs-lint findings in the same format as the standalone command."""
  for finding in findings:
    rel = finding.path.relative_to(root)
    typer.echo(f'{rel}:{finding.line}: [{finding.rule}] {finding.detail}')
    typer.echo(f'    {finding.text}')


app = typer.Typer(help="Checks over a project's docs: type-check code blocks, lint prose and links.")
app.command('check')(check)
app.command('lint')(lint)
