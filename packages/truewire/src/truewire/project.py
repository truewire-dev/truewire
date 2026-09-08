"""A Truewire project: the directory holding a `truewire.toml`.

`truewire.toml` is the one file that says where a project's spec lives, which generated
package it renders into, and how each symbolic core name resolves. Every CLI command takes
`--project PATH` (or finds the nearest `truewire.toml` above the working directory, the way
`cargo` finds `Cargo.toml`), and every library function that needs a project's layout takes
either a `Project` or a plain root directory -- see `resolve`.

Example:

```toml
[project]
name = "petstore"

[spec]
dir = "spec"

[cores.default]
meta = { type = "object", additionalProperties = false }

[python]
package = "petstore"
src = "src"
name = "Petstore"

[python.cores.default]
base = "petstore.core:Endpoint"
```
"""
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing_extensions import TYPE_CHECKING, Any

if TYPE_CHECKING:
  from truewire.spec.codegen_toml import CodegenConfig

PROJECT_FILE = 'truewire.toml'
"""The file that marks a directory as a Truewire project."""

STATE_DIR = '.truewire'
"""Per-project working directory (generated-file manifests and the like), under `root`."""


class NotAProject(Exception):
  """Raised when no `truewire.toml` can be found for a path, or the one found is invalid."""


@dataclass(frozen=True)
class Project:
  """One loaded `truewire.toml`, with every path it implies resolved to an absolute `Path`.

  Construct through `load_project`/`find_project`/`resolve`, not by hand -- the paths here
  are derived from the file's own `[spec]`/`[python]` sections and are kept absolute so a
  command can run from any working directory.
  """
  root: Path
  """Directory containing `truewire.toml`."""
  name: str
  """`[project].name`."""
  spec_dir: Path
  """`root / [spec].dir`, `spec` by default -- holds `endpoints/` and `schemas.json`."""
  config: 'CodegenConfig'
  """The `[cores.*]`, `[python]` and `[typescript]` sections, validated."""
  python_src: Path
  """`root / [python].src`, `src` by default -- the directory the generated package lives
  under. Meaningful only when `[python]` is declared."""
  secrets: dict[str, Any]
  """The raw `[secrets]` table (credential variable *names*, never values), or `{}`."""

  @property
  def endpoints_dir(self) -> Path:
    """`spec_dir / 'endpoints'` -- the endpoint tree."""
    return self.spec_dir / 'endpoints'

  @property
  def schemas_path(self) -> Path:
    """`spec_dir / 'schemas.json'` -- the root shared-schemas file (may not exist)."""
    return self.spec_dir / 'schemas.json'

  @property
  def python(self):
    """The `[python]` section, or `None` when the project generates no Python package."""
    return self.config.python

  @property
  def package_name(self) -> str:
    """`[python].package`, the generated package's import name.

    Raises:
      NotAProject: When `truewire.toml` declares no `[python]` section.
    """
    if self.config.python is None:
      raise NotAProject(f'{self.root / PROJECT_FILE}: no [python] section, so no package to generate')
    return self.config.python.package or self.name

  @property
  def package_dir(self) -> Path:
    """`python_src / [python].package` -- the directory every generated file is written under.

    Raises:
      NotAProject: When `truewire.toml` declares no `[python]` section.
    """
    return self.python_src / self.package_name

  @property
  def typescript(self):
    """The `[typescript]` section, or `None` when the project generates no TypeScript package."""
    return self.config.typescript

  @property
  def typescript_package_dir(self) -> Path:
    """`root / [typescript].src / [typescript].package` -- the directory every generated
    TypeScript file is written under.

    Raises:
      NotAProject: When `truewire.toml` declares no `[typescript]` section.
    """
    typescript = self.config.typescript
    if typescript is None:
      raise NotAProject(
        f'{self.root / PROJECT_FILE}: no [typescript] section, so no TypeScript package to generate'
      )
    return self.root / typescript.src / (typescript.package or self.name)

  @property
  def state_dir(self) -> Path:
    """`root / .truewire` -- created on demand by whatever writes into it."""
    return self.root / STATE_DIR

  def manifest_path(self, language: str) -> Path:
    """The generated-file ownership manifest for one language: `.truewire/<language>-files.json`."""
    return self.state_dir / f'{language}-files.json'

  @property
  def ruff_config(self) -> Path:
    """The Ruff config `truewire generate` formats with: `[python].ruff` (relative to
    `root`) when set, else the config shipped in `truewire/resources/ruff.toml`."""
    python = self.config.python
    if python is not None and python.ruff is not None:
      return self.root / python.ruff
    return Path(__file__).parent / 'resources' / 'ruff.toml'

  @property
  def pyright_enabled(self) -> bool:
    """Whether `truewire generate` runs pyright over the package afterwards: `[python].pyright`
    is true, or a `pyrightconfig.json` sits at `root`."""
    python = self.config.python
    if python is not None and python.pyright:
      return True
    return (self.root / 'pyrightconfig.json').is_file()

  @property
  def backend_path(self) -> Path | None:
    """`[python].backend`, resolved against `root`, or `None` -- a Python module exporting a
    `generator` that customizes generation. Most projects need none."""
    python = self.config.python
    if python is None or python.backend is None:
      return None
    return self.root / python.backend


def project_file(path: Path) -> Path:
  """Return the `truewire.toml` a path names: the file itself, or `<dir>/truewire.toml`."""
  path = path.expanduser()
  return path if path.name == PROJECT_FILE else path / PROJECT_FILE


def load_project_data(data: dict[str, Any], *, root: Path) -> Project:
  """Build a `Project` from an already-decoded `truewire.toml` mapping.

  Args:
    data: The whole file, as `tomllib.load` returns it.
    root: The directory the file lives in; every relative path is resolved against it.

  Raises:
    NotAProject: When a required section or field is missing or invalid.
  """
  from pydantic import ValidationError

  from truewire.spec.codegen_toml import load_codegen_config

  root = root.resolve()
  project = data.get('project')
  if project is None:
    project = {'name': root.name or 'project'}
  if not isinstance(project, dict) or not isinstance(project.get('name'), str) or not project['name']:
    raise NotAProject(f'{root / PROJECT_FILE}: [project].name is required')
  spec = data.get('spec') or {}
  if not isinstance(spec, dict):
    raise NotAProject(f'{root / PROJECT_FILE}: [spec] must be a table')
  spec_dir = root / str(spec.get('dir', 'spec'))
  secrets = data.get('secrets') or {}
  if not isinstance(secrets, dict):
    raise NotAProject(f'{root / PROJECT_FILE}: [secrets] must be a table')
  known = {'project', 'spec', 'secrets', 'cores', 'python', 'typescript'}
  unknown = sorted(set(data) - known)
  if unknown:
    raise NotAProject(f'{root / PROJECT_FILE}: unknown top-level section(s): {", ".join(unknown)}')
  try:
    config = load_codegen_config(
      {key: data[key] for key in ('cores', 'python', 'typescript') if key in data}
    )
  except ValidationError as exc:
    raise NotAProject(f'{root / PROJECT_FILE}: {exc}') from exc
  python_src = root / (config.python.src if config.python is not None else 'src')
  return Project(
    root=root, name=project['name'], spec_dir=spec_dir, config=config,
    python_src=python_src, secrets=dict(secrets),
  )


def load_project(path: Path) -> Project:
  """Load the `truewire.toml` at `path` (a directory holding one, or the file itself).

  Raises:
    NotAProject: When the file is missing or invalid.
  """
  file = project_file(path)
  if not file.is_file():
    raise NotAProject(f'{file}: no such file -- run `truewire init` to create a project here')
  try:
    with file.open('rb') as handle:
      data = tomllib.load(handle)
  except tomllib.TOMLDecodeError as exc:
    raise NotAProject(f'{file}: invalid TOML: {exc}') from exc
  return load_project_data(data, root=file.parent)


def find_project(start: Path | None = None) -> Project:
  """Find the nearest `truewire.toml` at or above `start` (the working directory by default).

  Raises:
    NotAProject: When no ancestor holds one.
  """
  origin = (start if start is not None else Path.cwd()).expanduser().resolve()
  for candidate in (origin, *origin.parents):
    if (candidate / PROJECT_FILE).is_file():
      return load_project(candidate)
  raise NotAProject(
    f'no {PROJECT_FILE} found in {origin} or any parent directory -- pass --project, '
    'or run `truewire init` to create one'
  )


def resolve(root: 'Path | Project') -> Project:
  """Return the `Project` a library call was handed, loading it from a plain root when needed.

  A `Project` is returned as-is. A `Path` holding a `truewire.toml` is loaded. A `Path`
  holding none is treated as a bare spec tree with the default layout (`spec/` beneath
  it, no `[python]` section) so read-only spec tooling -- and tests that build a spec in
  a temporary directory -- work without a config file.
  """
  if isinstance(root, Project):
    return root
  if (root / PROJECT_FILE).is_file():
    return load_project(root)
  return load_project_data({'project': {'name': root.name or 'project'}}, root=root)


def spec_dir(root: 'Path | Project') -> Path:
  """The `spec/` directory of a project, or of a bare root with the default layout."""
  if isinstance(root, Project):
    return root.spec_dir
  if (root / PROJECT_FILE).is_file():
    return load_project(root).spec_dir
  return root / 'spec'


def root_of(root: 'Path | Project') -> Path:
  """The root directory of a project, given either it or the `Project` itself."""
  return root.root if isinstance(root, Project) else root
