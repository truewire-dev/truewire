"""Run each declared package's own formatter, linter and type checker (`docs/shape/toolchain.md`
T3, T4; `docs/shape/packages.md` P10; `docs/shape/score.md` S7).

The tools are the languages' real ones, configured by the package and invoked here, so a
project needs no task runner on top of the toolchain:

| language   | formatter           | linter                           | type checker |
|------------|---------------------|----------------------------------|--------------|
| python     | `ruff format --check` | `ruff check`                   | `pyright`    |
| typescript | (none)              | `eslint`, when the package configures it | `tsc --noEmit` |
| rust       | `cargo fmt --check` | `cargo clippy --all-targets -- -D warnings` | (clippy compiles) |
| go         | `gofmt -l`          | `go vet`, `staticcheck`          | (vet compiles) |

**The package** is the directory `truewire test` runs the suite in
(`truewire.test.package_directory`): the nearest one from `[<language>].src` up to the
project root that holds the language's manifest (`pyproject.toml`, `package.json`,
`Cargo.toml`, `go.mod`), or the project root for a Python package with none. Every tool
runs there, so it reads that package's own config: a project whose four packages share one
root, and one whose packages each sit in `packages/<language>`, both lint what they build.

**A tool that cannot be found fails its language, and names the tool.** It is never a pass:
a gate that goes green on a machine without the checker is the silence the scorecard
exists to end (S13). A step the package opts out of by configuration (eslint without an
eslint config) is `skipped` and printed as such, with the reason.

Every language's exit code is its worst step's, and a run's is its worst language's (T4).
A missing tool counts as 127, the shell's "command not found".
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing_extensions import Literal

from truewire.project import PROJECT_FILE, Project
from truewire.test import MANIFESTS, NoSuite, package_directory

LANGUAGES = ('python', 'typescript', 'rust', 'go')
"""Every language a project can declare, in the order a run lints them."""

STATICCHECK = 'honnef.co/go/tools/cmd/staticcheck@v0.8.1'
"""The staticcheck `go run` builds and runs (2026.2.1): pinned, so every machine judges a Go
package by the same checks, and nothing has to be installed beyond Go itself."""

RUFF_LINT_CONFIG = Path(__file__).parent.parent / 'resources' / 'ruff-lint.toml'
"""The Ruff config for a package that declares none: the one `truewire generate` formats
with, plus Ruff's default rule set, which that config's `select` replaces."""

RUFF_CONFIG_FILES = ('ruff.toml', '.ruff.toml')

ESLINT_CONFIG_FILES = (
  'eslint.config.js', 'eslint.config.mjs', 'eslint.config.cjs',
  'eslint.config.ts', 'eslint.config.mts', 'eslint.config.cts',
  '.eslintrc', '.eslintrc.js', '.eslintrc.cjs', '.eslintrc.yaml', '.eslintrc.yml', '.eslintrc.json',
)
"""Every file ESLint reads its configuration from, flat and legacy."""

MISSING = 127
"""The exit code a tool that cannot be found counts as."""

Status = Literal['pass', 'fail', 'missing', 'skipped']


@dataclass(frozen=True)
class Step:
  """One tool run over one package, or the reason it could not be."""
  language: str
  tool: str
  """The tool's own name (`ruff format`, `pyright`, `clippy`), so a failure names it."""
  status: Status
  code: int = 0
  """The tool's exit code; 1 for `gofmt -l` listing a file, `MISSING` for a missing tool."""
  command: tuple[str, ...] = ()
  """What ran; empty when nothing did."""
  output: str = ''
  """What the tool printed, stdout then stderr."""
  detail: str = ''
  """Why a step is `missing`, `skipped` or failed before any tool ran."""


@dataclass
class Report:
  """One language's steps, in the order they ran."""
  language: str
  package: Path | None = None
  """The package directory the tools ran in; `None` when there is none."""
  steps: list[Step] = field(default_factory=list)

  @property
  def code(self) -> int:
    """The worst exit code of any step; 0 when every step passed or was skipped."""
    return max((step.code for step in self.steps), default=0)

  @property
  def failed(self) -> list[Step]:
    """The steps that failed or could not run."""
    return [step for step in self.steps if step.status in ('fail', 'missing')]


class UnknownLanguage(ValueError):
  """Raised for a language `truewire lint` has no tools for, or one the project does not declare."""


def declared_languages(project: Project) -> tuple[str, ...]:
  """The languages `truewire.toml` declares a section for, in `LANGUAGES` order."""
  return tuple(language for language in LANGUAGES if getattr(project.config, language) is not None)


def package_root(project: Project, language: str) -> Path | None:
  """The package `truewire test` runs `language`'s suite in (`truewire.test.package_directory`),
  or `None` when there is none, so the two commands never disagree about what the package is."""
  try:
    return package_directory(project, language)
  except NoSuite:
    return None


def _node_bin(package: Path, name: str) -> str | None:
  """`name` from the nearest `node_modules/.bin` at or above `package`, else from PATH."""
  for directory in (package, *package.parents):
    candidate = directory / 'node_modules' / '.bin' / name
    if candidate.is_file():
      return str(candidate)
  return shutil.which(name)


def _python_tool(module: str) -> tuple[str, ...] | None:
  """`module` run by this interpreter when it can import it, else its script on PATH."""
  if importlib.util.find_spec(module) is not None:
    return (sys.executable, '-m', module)
  found = shutil.which(module)
  return (found,) if found else None


def _cargo() -> str | None:
  found = shutil.which('cargo')
  if found:
    return found
  home = Path(os.environ.get('CARGO_HOME') or Path.home() / '.cargo')
  candidate = home / 'bin' / 'cargo'
  return str(candidate) if candidate.is_file() else None


def _gofmt(go: str) -> str | None:
  """`gofmt` from PATH, else the one installed beside `go`."""
  found = shutil.which('gofmt')
  if found:
    return found
  candidate = Path(go).resolve().parent / 'gofmt'
  return str(candidate) if candidate.is_file() else None


def _run(
  language: str, tool: str, command: Sequence[str], cwd: Path, *,
  env: dict[str, str] | None = None, fail_on_output: bool = False,
) -> Step:
  """Run one tool; `pass` exactly when it exits zero (and, with `fail_on_output`, prints nothing)."""
  try:
    done = subprocess.run(
      list(command), cwd=cwd, capture_output=True, text=True,
      env={**os.environ, **env} if env else None,
    )
  except FileNotFoundError:
    return Step(language, tool, 'missing', MISSING, tuple(command), detail=f'{command[0]} not found')
  output = done.stdout + done.stderr
  code = done.returncode
  if code == 0 and fail_on_output and done.stdout.strip():
    code = 1
  return Step(language, tool, 'pass' if code == 0 else 'fail', code, tuple(command), output)


def _missing(language: str, tool: str, where: str) -> Step:
  return Step(language, tool, 'missing', MISSING, detail=f'{tool} not found ({where})')


def _has_tool_section(pyproject: Path, tool: str) -> bool:
  try:
    data = tomllib.loads(pyproject.read_text())
  except (OSError, tomllib.TOMLDecodeError):
    return False
  return tool in data.get('tool', {})


def ruff_config(project: Project, package: Path) -> Path | None:
  """The config `ruff` is given with `--config`, or `None` to let Ruff find the package's own.

  `[python].ruff` wins, since `truewire generate` formats with it too. Otherwise a package
  with its own Ruff config (`ruff.toml`, `.ruff.toml`, or `[tool.ruff]` in `pyproject.toml`)
  is linted by it. Otherwise `RUFF_LINT_CONFIG`: without an explicit config Ruff would
  climb past the package and pick up whatever config a parent directory holds.
  """
  python = project.config.python
  if python is not None and python.ruff is not None:
    return project.root / python.ruff
  if any((package / name).is_file() for name in RUFF_CONFIG_FILES) or _has_tool_section(package / 'pyproject.toml', 'ruff'):
    return None
  return RUFF_LINT_CONFIG


def _python(project: Project, package: Path) -> Iterator[Step]:
  ruff = _python_tool('ruff')
  config = ruff_config(project, package)
  flags = ('--config', str(config)) if config is not None else ()
  if ruff is None:
    yield _missing('python', 'ruff format', 'not importable by this interpreter, not on PATH')
    yield _missing('python', 'ruff', 'not importable by this interpreter, not on PATH')
  else:
    yield _run('python', 'ruff format', (*ruff, 'format', '--check', '--no-cache', *flags, '.'), package)
    yield _run('python', 'ruff', (*ruff, 'check', '--no-cache', *flags, '.'), package)
  pyright = _python_tool('pyright') or ((found,) if (found := _node_bin(package, 'pyright')) else None)
  if pyright is None:
    yield _missing('python', 'pyright', 'not importable by this interpreter, not on PATH, not in node_modules/.bin')
  else:
    # Run from the package, so pyright reads `pyrightconfig.json` or `[tool.pyright]` there.
    # A package with neither is checked with pyright's defaults, against the interpreter
    # running truewire: pyright would otherwise take whichever `python` is first on PATH.
    configured = (package / 'pyrightconfig.json').is_file() or _has_tool_section(package / 'pyproject.toml', 'pyright')
    interpreter = () if configured else ('--pythonpath', sys.executable)
    yield _run('python', 'pyright', (*pyright, *interpreter), package, env={'PYRIGHT_PYTHON_IGNORE_WARNINGS': '1'})


def eslint_configured(package: Path) -> bool:
  """Whether the package configures ESLint: a config file, or `eslintConfig` in `package.json`."""
  if any((package / name).is_file() for name in ESLINT_CONFIG_FILES):
    return True
  try:
    return 'eslintConfig' in json.loads((package / 'package.json').read_text())
  except (OSError, ValueError):
    return False


def _typescript(project: Project, package: Path) -> Iterator[Step]:
  tsc = _node_bin(package, 'tsc')
  if not (package / 'tsconfig.json').is_file():
    yield Step('typescript', 'tsc', 'fail', 1, detail=f'no tsconfig.json in {package}')
  elif tsc is None:
    yield _missing('typescript', 'tsc', 'not in node_modules/.bin, not on PATH; install the package first')
  else:
    yield _run('typescript', 'tsc', (tsc, '-p', 'tsconfig.json', '--noEmit'), package)
  if not eslint_configured(package):
    yield Step('typescript', 'eslint', 'skipped', detail='the package has no eslint config')
    return
  eslint = _node_bin(package, 'eslint')
  if eslint is None:
    yield _missing('typescript', 'eslint', 'not in node_modules/.bin, not on PATH; install the package first')
  else:
    yield _run('typescript', 'eslint', (eslint, '.'), package)


def _cargo_component(cargo: str, subcommand: str) -> bool:
  try:
    return subprocess.run([cargo, subcommand, '--version'], capture_output=True).returncode == 0
  except OSError:
    return False


def _rust(project: Project, package: Path) -> Iterator[Step]:
  cargo = _cargo()
  if cargo is None:
    yield _missing('rust', 'rustfmt', 'cargo not on PATH or in $CARGO_HOME/bin')
    yield _missing('rust', 'clippy', 'cargo not on PATH or in $CARGO_HOME/bin')
    return
  manifest = ('--manifest-path', str(package / 'Cargo.toml'))
  if _cargo_component(cargo, 'fmt'):
    yield _run('rust', 'rustfmt', (cargo, 'fmt', '--check', *manifest), package)
  else:
    yield _missing('rust', 'rustfmt', '`cargo fmt` unavailable; `rustup component add rustfmt`')
  if _cargo_component(cargo, 'clippy'):
    yield _run('rust', 'clippy', (cargo, 'clippy', *manifest, '--all-targets', '--', '-D', 'warnings'), package)
  else:
    yield _missing('rust', 'clippy', '`cargo clippy` unavailable; `rustup component add clippy`')


def _go(project: Project, package: Path) -> Iterator[Step]:
  go = shutil.which('go')
  if go is None:
    for tool in ('gofmt', 'go vet', 'staticcheck'):
      yield _missing('go', tool, 'go not on PATH')
    return
  gofmt = _gofmt(go)
  if gofmt is None:
    yield _missing('go', 'gofmt', 'not on PATH or beside go')
  else:
    yield _run('go', 'gofmt', (gofmt, '-l', '.'), package, fail_on_output=True)
  yield _run('go', 'go vet', (go, 'vet', './...'), package)
  yield _run('go', 'staticcheck', (go, 'run', STATICCHECK, './...'), package)


_LANGUAGE_STEPS = {'python': _python, 'typescript': _typescript, 'rust': _rust, 'go': _go}


def steps(project: Project, language: str) -> Iterator[Step]:
  """Run `language`'s tools over its package, yielding each step as it finishes.

  Raises:
    UnknownLanguage: `language` is not one of `LANGUAGES`, or the project does not declare it.
  """
  if language not in LANGUAGES:
    raise UnknownLanguage(f'no such language: {language!r}; expected one of {", ".join(LANGUAGES)}')
  if getattr(project.config, language) is None:
    raise UnknownLanguage(f'{project.root / PROJECT_FILE} declares no [{language}] section')
  try:
    package = package_directory(project, language)
  except NoSuite as exc:
    yield Step(language, 'package', 'fail', 1, detail=str(exc))
    return
  yield from _LANGUAGE_STEPS[language](project, package)


def lint_language(project: Project, language: str) -> Report:
  """Run every step for one declared language and collect them."""
  return Report(language, package_root(project, language), list(steps(project, language)))


def lint(project: Project, languages: Sequence[str] | None = None) -> list[Report]:
  """Lint each of `languages`, every declared one by default.

  Raises:
    UnknownLanguage: One of `languages` has no tools here or is not declared.
  """
  return [lint_language(project, language) for language in (languages or declared_languages(project))]


def worst(reports: Sequence[Report]) -> int:
  """The exit code of a run: its worst language's (T4)."""
  return max((report.code for report in reports), default=0)
