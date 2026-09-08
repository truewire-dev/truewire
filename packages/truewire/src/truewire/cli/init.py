"""`truewire init`: scaffold a new project directory."""
import re
from pathlib import Path

import typer

from truewire.codegen.meta import META_MODULE, meta_module
from truewire.project import PROJECT_FILE
from truewire.spec.codegen_toml import load_codegen_toml

from .core_templates import DEFAULT_TEMPLATE, TEMPLATES, template_help
from .generate import GENERATED_BANNER

TYPES_TEMPLATE = '''"""Wire timestamp shapes, re-exported from the runtime.

Generated code imports `truewire_core.types` directly; this module stays so a caller that
imports `{package}.core.types` keeps working. Add a project-specific alias here (a
non-UTC epoch, say) built from `truewire_core.times` converters.
"""
from truewire_core.types import (  # noqa: F401
  DateIso, TimestampIso, TimestampMicros, TimestampMillis, TimestampNanos, TimestampSeconds,
  date_iso, timestamp_iso, timestamp_micros, timestamp_millis, timestamp_nanos, timestamp_seconds,
)
'''

PYPROJECT_TEMPLATE = '''[project]
name = "{package}"
version = "0.1.0"
description = "Typed client generated with Truewire."
requires-python = ">=3.11"
dependencies = ["truewire-core>=0.2.0,<0.3"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-data]
{package} = ["py.typed"]
'''
"""The project's own packaging file, so `pip install -e .` makes the generated client importable."""


SCAFFOLD_NEIGHBOURS = {'.git', '.venv'}
"""What an otherwise empty directory may hold and still count as the empty target of
`truewire init <name>` run from inside it: the checkout and the environment made for it."""

GITIGNORE_LINES = ['.truewire/', '__pycache__/', '.venv/']
"""What `init` ignores: `generate`'s own state (its manifest; `--check` does not need it
on a fresh clone), bytecode and the environment."""


def snake_case(name: str) -> str:
  """The package name a directory name implies: `open-meteo` -> `open_meteo`, `MyApi` ->
  `myapi`. Anything but letters and digits becomes one underscore."""
  return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')


def is_empty_apart_from(directory: Path, neighbours: set[str]) -> bool:
  """Whether `directory` holds nothing but entries named in `neighbours`."""
  return directory.is_dir() and all(entry.name in neighbours for entry in directory.iterdir())


def ensure_gitignore(root: Path, lines: list[str]):
  """Write `.gitignore` with `lines`, or add the ones an existing file lacks to its end."""
  target = root / '.gitignore'
  if not target.exists():
    target.write_text(''.join(f'{line}\n' for line in lines))
    return
  existing = target.read_text()
  present = {line.strip() for line in existing.splitlines()}
  missing = [line for line in lines if line not in present]
  if missing:
    separator = '' if existing == '' or existing.endswith('\n') else '\n'
    target.write_text(existing + separator + ''.join(f'{line}\n' for line in missing))


def default_ws_url(base_url: str) -> str:
  """The socket URL a `ws` project starts with: the base URL with its scheme swapped for the WebSocket one."""
  return re.sub(r'^http(s?)://', r'ws\1://', base_url)


def init(
  name: str = typer.Argument(
    ..., help='Project name; also the directory and Python package name. `.` writes into the '
    'current directory and names the package after it (`open-meteo` -> `open_meteo`).',
  ),
  directory: Path | None = typer.Option(
    None, '--dir', help='Where to create the project; defaults to ./<name>, or to the current '
    'directory when it is named <name> and holds nothing but .git/.venv.',
  ),
  base_url: str = typer.Option('https://api.example.com', '--base-url', help='Upstream base URL baked into the core template.'),
  template: str = typer.Option(DEFAULT_TEMPLATE, '--template', help=template_help()),
  ws_url: str | None = typer.Option(None, '--ws-url', help='Socket URL baked into the `ws` template; defaults to --base-url with a ws/wss scheme.'),
):
  """Create a new Truewire project: `truewire.toml`, `pyproject.toml`, an empty `spec/`, and a core skeleton.

  `truewire init petstore` creates `./petstore`; `truewire init .` writes into the current
  directory and derives the package name from its name, as does `truewire init petstore`
  run inside an empty `petstore` (a `.git` or `.venv` there does not count as content).

  `--template` picks the core skeleton: `bearer` (the default), `hmac`, `jsonrpc` or `ws`.
  Each is a working core for one common API shape, documented in `docs/cores.md`; adapt
  it to the API, the generated code never changes when you do. Follow with
  `truewire import openapi <doc>` to seed the spec, or author
  `spec/endpoints/<group>/<name>/endpoint.json` by hand, then `truewire check` and
  `truewire generate python`.
  """
  cwd = Path.cwd().resolve()
  if directory is not None:
    root = directory.resolve()
  elif name == '.' or (snake_case(cwd.name) == name and is_empty_apart_from(cwd, SCAFFOLD_NEIGHBOURS)):
    root = cwd
  else:
    root = (cwd / name).resolve()
  if name == '.':
    name = snake_case(root.name)
    if not re.fullmatch(r'[a-z][a-z0-9_]*', name):
      typer.echo(
        f'{root.name!r} does not give a package name (got {name!r}); pass one instead of `.`',
        err=True,
      )
      raise typer.Exit(code=1)
  if not re.fullmatch(r'[a-z][a-z0-9_]*', name):
    typer.echo('name must be a lowercase identifier: letters, digits, underscores', err=True)
    raise typer.Exit(code=1)
  if template not in TEMPLATES:
    typer.echo(f'unknown template {template!r}; one of: {", ".join(TEMPLATES)}', err=True)
    raise typer.Exit(code=1)
  core = TEMPLATES[template]
  if (root / PROJECT_FILE).exists():
    typer.echo(f'{root / PROJECT_FILE} already exists', err=True)
    raise typer.Exit(code=1)
  class_name = ''.join(part.capitalize() for part in name.split('_'))
  fill = dict(package=name, base_url=base_url, ws_url=ws_url or default_ws_url(base_url), class_name=class_name)
  (root / 'spec' / 'endpoints').mkdir(parents=True, exist_ok=True)
  (root / 'src' / name / 'core').mkdir(parents=True, exist_ok=True)
  (root / PROJECT_FILE).write_text(f'''[project]
name = "{name}"

[spec]
dir = "spec"

{core.render(core.cores_toml, **fill)}
[python]
package = "{name}"
src = "src"
name = "{class_name}"

{core.render(core.python_cores_toml, **fill)}''')
  (root / 'spec' / 'endpoints' / 'router.json').write_text(
    '{\n  "description": "' + class_name + ' API.",\n  "upstream": "' + base_url + '",\n  "core": "root"\n}\n'
  )
  (root / 'src' / name / '__init__.py').write_text(f'from .main import {class_name}\n')
  (root / 'src' / name / 'py.typed').write_text('')
  meta_source = meta_module(load_codegen_toml(root).cores)
  assert meta_source is not None  # the template's [cores.default] declares a schema
  (root / 'src' / name / f'{META_MODULE}.py').write_text(GENERATED_BANNER + meta_source)
  (root / 'src' / name / 'core' / '__init__.py').write_text(core.render(core.core, **fill))
  for relative, text in core.files.items():
    target = root / core.render(relative, **fill)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(core.render(text, **fill))
  (root / 'src' / name / 'core' / 'types.py').write_text(TYPES_TEMPLATE.format(package=name))
  if not (root / 'pyproject.toml').exists():
    (root / 'pyproject.toml').write_text(PYPROJECT_TEMPLATE.format(package=name))
  ensure_gitignore(root, GITIGNORE_LINES)
  typer.echo(f'Created {root}')
  typer.echo('Next: `truewire import openapi <document>` or write spec/endpoints/**/endpoint.json, then `truewire check` and `truewire generate python`.')
