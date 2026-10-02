"""`truewire init`: write a workspace skeleton and a Python core, or fill in what one lacks."""
import os
import re
from pathlib import Path

import tomllib
import typer

from truewire.agents import write_missing
from truewire.codegen.meta import META_MODULE, meta_module
from truewire.project import PROJECT_FILE
from truewire.score.rows import LANGUAGES
from truewire.skeleton import AGENT_LINKS, NARROWED, SCHEMA_URL, add_schema_line, gitignore_warning, project_toml, write_new, write_skeleton
from truewire.spec.codegen_toml import load_codegen_toml

from .core_templates import DEFAULT_TEMPLATE, TEMPLATES, CoreTemplate, template_help
from .generate import GENERATED_BANNER

TYPES_TEMPLATE = '''"""Wire timestamp shapes, re-exported from the runtime.

Generated code imports `truewire_core.types` directly; this module stays so a caller that
imports `{package}.core.types` keeps working. Add a project-specific alias here (a
non-UTC epoch, say) built from `truewire_core.times` converters.
"""

from truewire_core.types import (  # noqa: F401
  DateIso,
  TimestampIso,
  TimestampMicros,
  TimestampMicrosFloat,
  TimestampMillis,
  TimestampMillisFloat,
  TimestampNanos,
  TimestampNanosFloat,
  TimestampSeconds,
  TimestampSecondsFloat,
  date_iso,
  timestamp_iso,
  timestamp_micros,
  timestamp_micros_float,
  timestamp_millis,
  timestamp_millis_float,
  timestamp_nanos,
  timestamp_nanos_float,
  timestamp_seconds,
  timestamp_seconds_float,
)
'''

PYPROJECT_TEMPLATE = '''[project]
name = "{package}"
version = "0.1.0"
description = "Typed client generated with Truewire."
requires-python = ">=3.11"
dependencies = ["truewire-core>=0.3.0,<0.4"]

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


def snake_case(name: str) -> str:
  """The package name a directory name implies: `open-meteo` -> `open_meteo`, `MyApi` ->
  `myapi`. Anything but letters and digits becomes one underscore."""
  return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')


def class_name_of(name: str) -> str:
  """The class name a project name gives: `open-meteo` -> `OpenMeteo`."""
  return ''.join(part.capitalize() for part in snake_case(name).split('_'))


def project_class_name(file: Path) -> str:
  """The class name an existing `truewire.toml` gives its project: `[python].name`, else
  `[project].name` class-cased, else the directory's name class-cased.

  Raises:
    tomllib.TOMLDecodeError: When the file is not TOML.
  """
  data = tomllib.loads(file.read_text())
  python, project = data.get('python'), data.get('project')
  if isinstance(python, dict) and isinstance(python.get('name'), str) and python['name']:
    return python['name']
  if isinstance(project, dict) and isinstance(project.get('name'), str) and snake_case(project['name']):
    return class_name_of(project['name'])
  return class_name_of(file.parent.name)


def toml_languages(file: Path) -> tuple[str, ...]:
  """The languages a `truewire.toml` declares a section for, in `LANGUAGES` order; none when
  the file is not TOML."""
  try:
    data = tomllib.loads(file.read_text())
  except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
    return ()
  return tuple(language for language in LANGUAGES if isinstance(data.get(language), dict))


def write_workspace(root: Path, class_name: str) -> list[str]:
  """The skeleton (`write_skeleton`), then the skills and rules vendored from the installed
  toolchain (`truewire.agents`, A10), each only where no file is; the paths written."""
  languages = toml_languages(root / PROJECT_FILE)
  written = write_skeleton(root, class_name, languages)
  vendored, refused = write_missing(root, languages)
  for relative in refused:
    typer.echo(f'Not written: {relative}, its directory is a link leading outside {root}.', err=True)
  return written + vendored


def is_empty_apart_from(directory: Path, neighbours: set[str]) -> bool:
  """Whether `directory` holds nothing but entries named in `neighbours`."""
  return directory.is_dir() and all(entry.name in neighbours for entry in directory.iterdir())


def is_project_named(directory: Path, name: str) -> bool:
  """Whether `directory` already holds a `truewire.toml` for `name`: the directory is named
  after it, or the file's `[project].name` is `name`. An unreadable file counts only by the
  directory's name."""
  file = directory / PROJECT_FILE
  if not file.is_file():
    return False
  if snake_case(directory.name) == name:
    return True
  try:
    project = tomllib.loads(file.read_text()).get('project')
  except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
    return False
  return isinstance(project, dict) and project.get('name') == name


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
    'directory when it is named <name> and holds nothing but .git/.venv, or already holds '
    'the truewire.toml of project <name>.',
  ),
  base_url: str = typer.Option('https://api.example.com', '--base-url', help='Upstream base URL baked into the core template.'),
  template: str = typer.Option(DEFAULT_TEMPLATE, '--template', help=template_help()),
  ws_url: str | None = typer.Option(None, '--ws-url', help='Socket URL baked into the `ws` template; defaults to --base-url with a ws/wss scheme.'),
):
  """Create a Truewire workspace: `truewire.toml`, the skeleton around it, and a Python core.

  The skeleton is the tree `docs/shape/workspace.md` describes: `spec/`, `packages/`,
  `docs/` (`docs.yml`, `index.md`, `api-keys.md`, `how-to/`, `reference/`), `.agents/`
  with the skills and rules vendored from this toolchain (`truewire agents update` refreshes them),
  `.claude/skills` and `.claude/rules` (symlinks into `.agents/`), `AGENTS.md`, `CLAUDE.md`,
  `dev/capture/`, `.truewire/codegen/` and a `.gitignore`.

  Running it again is safe: an existing file is never overwritten, a missing one is written,
  and a run that finds nothing missing says so. Over a directory that already holds a
  `truewire.toml`, the file and the package scaffold are left as they are and only the
  skeleton is filled in, titled after the file's `[python].name` or `[project].name`. The
  one exception: a `truewire.toml` that names no schema gets `#:schema <url>` as its first
  line, every other byte unchanged.

  `truewire init petstore` creates `./petstore`; `truewire init .` writes into the current
  directory and derives the package name from its name, as does `truewire init petstore`
  run inside an empty `petstore` (a `.git` or `.venv` there does not count as content).
  Run inside the project it names (a `truewire.toml` there, and the directory or the file's
  `[project].name` matches), `truewire init petstore` fills that project in rather than
  nesting a second one.

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
  elif (
    name == '.'
    or (snake_case(cwd.name) == name and is_empty_apart_from(cwd, SCAFFOLD_NEIGHBOURS))
    or is_project_named(cwd, name)
  ):
    root = cwd
  else:
    root = (cwd / name).resolve()
  if template not in TEMPLATES:
    typer.echo(f'unknown template {template!r}; one of: {", ".join(TEMPLATES)}', err=True)
    raise typer.Exit(code=1)
  if (root / PROJECT_FILE).is_file():
    fill_in(root)
    return
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
  class_name = class_name_of(name)
  write_package_scaffold(root, name, class_name, TEMPLATES[template], base_url=base_url, ws_url=ws_url)
  written = write_workspace(root, class_name)
  if warning := gitignore_warning(root):
    typer.echo(warning)
  typer.echo(f'Created {root}')
  say_unlinked(root)
  if NARROWED in written:
    typer.echo(f'Changed {NARROWED}: the codegen manifest is committed.')
  typer.echo('Next: `truewire import openapi <document>` or write spec/endpoints/**/endpoint.json, then `truewire check` and `truewire generate python`.')


def fill_in(root: Path):
  """Write the skeleton files a project with a `truewire.toml` lacks, titled after that file,
  and say what was written. The file gains its `#:schema` line if it names no schema
  (`add_schema_line`); the rest of it and the package scaffold are left as they are."""
  try:
    class_name = project_class_name(root / PROJECT_FILE)
  except tomllib.TOMLDecodeError as error:
    typer.echo(f'{root / PROJECT_FILE}: not TOML ({error}); fix it and run `truewire init` again', err=True)
    raise typer.Exit(code=1) from error
  schema_added = add_schema_line(root / PROJECT_FILE)
  written = write_workspace(root, class_name)
  if warning := gitignore_warning(root):
    typer.echo(warning)
  say_unlinked(root)
  if not written and not schema_added:
    typer.echo(f'Nothing changed: {root} already has every file `truewire init` writes.')
    return
  if schema_added:
    written.insert(0, f'{PROJECT_FILE} (line 1 only: `#:schema {SCHEMA_URL}`; every other byte as it was)')
  typer.echo(f'Filled in {root}: {", ".join(written)}')
  if schema_added:
    typer.echo(f'{PROJECT_FILE} was already there; but for that first line, it and the package scaffold were left as they are.')
  else:
    typer.echo(f'{PROJECT_FILE} was already there; it and the package scaffold were left as they are.')


def say_unlinked(root: Path):
  """Say which `.claude/` link the filesystem refused, so a missing one is not a surprise."""
  for relative, target in AGENT_LINKS.items():
    if not os.path.lexists(root / relative):
      typer.echo(f'Could not link {relative} to {target}: symlinks are refused here. `truewire standards` reports it (A9).', err=True)


def write_package_scaffold(
  root: Path, name: str, class_name: str, core: CoreTemplate, *, base_url: str, ws_url: str | None,
) -> None:
  """Write `truewire.toml`, the root `router.json` and the Python package's core from `core`,
  a template; each file only where none exists."""
  fill = dict(package=name, base_url=base_url, ws_url=ws_url or default_ws_url(base_url), class_name=class_name)
  package = f'src/{name}'
  toml = project_toml(name, f'''{core.render(core.cores_toml, **fill)}
[python]
package = "{name}"
src = "src"
name = "{class_name}"

{core.render(core.python_cores_toml, **fill)}''')
  write_new(root, PROJECT_FILE, toml)
  meta_source = meta_module(load_codegen_toml(root).cores)
  assert meta_source is not None  # the template's [cores.default] declares a schema
  files = {
    'spec/endpoints/router.json':
      '{\n  "description": "' + class_name + ' API.",\n  "upstream": "' + base_url + '",\n  "core": "root"\n}\n',
    f'{package}/__init__.py': f"from .main import {class_name}\n\n__all__ = ['{class_name}']\n",
    f'{package}/py.typed': '',
    f'{package}/{META_MODULE}.py': GENERATED_BANNER + meta_source,
    f'{package}/core/__init__.py': core.render(core.core, **fill),
  }
  for relative, text in core.files.items():
    files.setdefault(core.render(relative, **fill), core.render(text, **fill))
  files.setdefault(f'{package}/core/types.py', TYPES_TEMPLATE.format(package=name))
  files.setdefault('pyproject.toml', PYPROJECT_TEMPLATE.format(package=name))
  for relative, text in files.items():
    write_new(root, relative, text)
