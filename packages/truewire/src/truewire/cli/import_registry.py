"""`truewire import registry`: start a project from a spec in the Truewire registry."""

import json
import re
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path

import typer

from .common import PROJECT_OPTION, resolve_project

DEFAULT_REGISTRY = 'https://github.com/truewire-dev/registry'


def registry(
  name: str = typer.Argument(..., help='Spec name in the registry, e.g. `github`.'),
  project: str | None = PROJECT_OPTION,
  source: str = typer.Option(
    DEFAULT_REGISTRY, '--registry',
    help='Registry to read: a git URL (cloned shallowly) or a local checkout path.',
  ),
  force: bool = typer.Option(False, '--force', help='Replace an existing spec/endpoints tree.'),
  check: bool = typer.Option(True, '--check/--no-check', help='Run `truewire check` on the imported tree.'),
):
  """Copy a registry spec into the project: its `spec/` tree, and the `[cores]` sections its
  routers refer to, merged into `truewire.toml` (existing sections are kept).

  `truewire import registry github` inside a fresh `truewire init` project is enough to
  `truewire generate python` a client whose recorded examples replay through `truewire mock`.
  A spec whose routers name a core the project has no `[python.cores.<name>]` entry for is
  reported, since generation needs one.
  """
  loaded = resolve_project(project)
  endpoints = loaded.spec_dir / 'endpoints'
  if endpoints.is_dir() and any(endpoints.rglob('endpoint.json')) and not force:
    typer.echo(f'{endpoints} already holds endpoints; pass --force to replace the spec tree', err=True)
    raise typer.Exit(code=1)

  with tempfile.TemporaryDirectory() as tmp:
    root = _registry_root(source, Path(tmp))
    index = _load_index(root)
    if name not in index:
      known = ', '.join(sorted(index)) or '(none)'
      typer.echo(f'no spec named {name!r} in {source}; known: {known}', err=True)
      raise typer.Exit(code=1)
    entry = root / index[name].get('path', f'specs/{name}')
    spec_src = entry / 'spec'
    if not spec_src.is_dir():
      typer.echo(f'{entry} has no spec/ directory', err=True)
      raise typer.Exit(code=1)

    if loaded.spec_dir.exists():
      shutil.rmtree(loaded.spec_dir)
    shutil.copytree(spec_src, loaded.spec_dir, ignore=shutil.ignore_patterns('__pycache__'))
    added = _merge_cores(entry / 'truewire.toml', loaded.root / 'truewire.toml')

  copied = sum(1 for _ in loaded.spec_dir.rglob('endpoint.json'))
  typer.echo(f'Imported {name}: {copied} endpoints into {loaded.spec_dir}')
  for core in added:
    typer.echo(f'  added [cores.{core}] to truewire.toml')
  missing = _cores_without_python_entry(loaded.root / 'truewire.toml', loaded.spec_dir)
  for core in missing:
    typer.echo(
      f'  note: routers name core {core!r} but truewire.toml has no [python.cores.{core}]; '
      f'add one (base = "<package>.core:<Class>") before `truewire generate python`'
    )
  if check:
    from .check import check as run_check
    run_check(project=str(loaded.root), path=None, verbose=False)


def _registry_root(source: str, tmp: Path) -> Path:
  """A local checkout path as-is; anything else is cloned shallowly into `tmp`."""
  local = Path(source)
  if local.is_dir():
    return local
  target = tmp / 'registry'
  try:
    subprocess.run(
      ['git', 'clone', '--quiet', '--depth', '1', source, str(target)],
      check=True, capture_output=True, text=True,
    )
  except FileNotFoundError:
    typer.echo('git is not installed; pass --registry <local checkout>', err=True)
    raise typer.Exit(code=1)
  except subprocess.CalledProcessError as exc:
    typer.echo(f'could not clone {source}: {exc.stderr.strip()}', err=True)
    raise typer.Exit(code=1)
  return target


def _load_index(root: Path) -> dict:
  index_file = root / 'registry.json'
  if index_file.is_file():
    return json.loads(index_file.read_text()).get('specs', {})
  specs = root / 'specs'
  return {p.name: {'path': f'specs/{p.name}'} for p in specs.iterdir() if p.is_dir()} if specs.is_dir() else {}


def _merge_cores(source_toml: Path, target_toml: Path) -> list[str]:
  """Append every `[cores.<name>]` table the target lacks, verbatim from the source."""
  if not source_toml.is_file():
    return []
  source_text = source_toml.read_text()
  target_text = target_toml.read_text() if target_toml.is_file() else ''
  have = set(tomllib.loads(target_text).get('cores', {})) if target_text else set()
  added: list[str] = []
  pattern = re.compile(r'^\[cores\.([^\]]+)\]\n(?:(?!^\[).*\n?)*', re.M)
  for match in pattern.finditer(source_text):
    core = match.group(1)
    if core in have:
      continue
    block = match.group(0).rstrip() + '\n'
    target_text = target_text.rstrip('\n') + '\n\n' + block
    added.append(core)
  if added:
    target_toml.write_text(target_text)
  return added


def _cores_without_python_entry(project_toml: Path, spec_dir: Path) -> list[str]:
  data = tomllib.loads(project_toml.read_text()) if project_toml.is_file() else {}
  python_cores = set((data.get('python') or {}).get('cores', {}))
  named: set[str] = set()
  for router in spec_dir.rglob('router.json'):
    try:
      core = json.loads(router.read_text()).get('core')
    except ValueError:
      continue
    if isinstance(core, str):
      named.add(core)
  return sorted(named - python_cores)
