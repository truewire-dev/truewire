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

[secrets]
required = ["PETSTORE_API_KEY"]

[policy]
rate = 10
retry = false
refuse = ["pets.delete"]

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
import math
import re
import tomllib
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing_extensions import TYPE_CHECKING, Any

if TYPE_CHECKING:
  from truewire.spec.codegen_toml import CodegenConfig

PROJECT_FILE = 'truewire.toml'
"""The file that marks a directory as a Truewire project."""

STATE_DIR = '.truewire'
"""Per-project working directory (generated-file manifests and the like), under `root`."""

CODEGEN_DIR = f'{STATE_DIR}/codegen'
"""Where the generated-file manifests live, one per language; committed, unlike
`.truewire/cache/` (W16)."""


ENV_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
"""What `[secrets]` may list: an environment variable's name, never its value or a path."""


class NotAProject(Exception):
  """Raised when no `truewire.toml` can be found for a path, or the one found is invalid."""


@dataclass(frozen=True)
class Secrets:
  """`[secrets]`: the environment variables a caller sets, by name only (W14).

  The values live in `.env`, which `truewire init` ignores, or in the caller's environment.
  """
  required: tuple[str, ...] = ()
  """`[secrets].required`: variables the client cannot run without."""
  optional: tuple[str, ...] = ()
  """`[secrets].optional`: variables that unlock more of the API when set."""


@dataclass(frozen=True)
class Policy:
  """`[policy]`: what the client may do on its own behalf (W15).

  `refuse` is generated: each endpoint it names fails with the package's `RefusedByPolicy`
  before any request (`truewire.codegen.policy`). `rate` and `retry` are generated as the
  root's `RATE`/`RETRY`, which the hand-written core passes to the runtime's `HttpClient`
  (Python, TypeScript and Rust; packages clause P19). `truewire check` fails on a `refuse`
  id that names no endpoint in the spec.
  """
  rate: float | None = None
  """`[policy].rate`: requests per second the client paces itself to, or `None` for no pacing."""
  retry: bool = False
  """`[policy].retry`: whether the client retries a failed request on its own."""
  refuse: tuple[str, ...] = ()
  """`[policy].refuse`: endpoint function ids (`account.withdraw`) the client refuses to call."""


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
  """The `[cores.*]`, `[python]`, `[typescript]`, `[rust]` and `[go]` sections, validated."""
  python_src: Path
  """`root / [python].src`, `src` by default -- the directory the generated package lives
  under. Meaningful only when `[python]` is declared."""
  secrets: Secrets
  """`[secrets]`: credential variable *names*, never values; empty when absent."""
  policy: Policy
  """`[policy]`: rate, retry and refusals; the defaults when absent."""
  stranger: date | None = None
  """`[score].stranger`: the day a newcomer last completed the quickstart from the published
  package and docs alone (`docs/shape/score.md` S12), or `None` when nobody has."""

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
  def rust(self):
    """The `[rust]` section, or `None` when the project generates no Rust package."""
    return self.config.rust

  @property
  def rust_package_dir(self) -> Path:
    """`root / [rust].src / [rust].package` -- the directory every generated Rust module
    is written under (`lib.rs` at its top).

    Raises:
      NotAProject: When `truewire.toml` declares no `[rust]` section.
    """
    rust = self.config.rust
    if rust is None:
      raise NotAProject(
        f'{self.root / PROJECT_FILE}: no [rust] section, so no Rust package to generate'
      )
    return self.root / rust.src / (rust.package or self.name)

  @property
  def go(self):
    """The `[go]` section, or `None` when the project generates no Go package."""
    return self.config.go

  @property
  def go_package_dir(self) -> Path:
    """`root / [go].src / [go].package` -- the directory of the generated root Go package
    (`client.go`), every other generated package beneath it.

    Raises:
      NotAProject: When `truewire.toml` declares no `[go]` section.
    """
    go = self.config.go
    if go is None:
      raise NotAProject(
        f'{self.root / PROJECT_FILE}: no [go] section, so no Go package to generate'
      )
    return self.root / go.src / (go.package or self.name)

  @property
  def state_dir(self) -> Path:
    """`root / .truewire` -- created on demand by whatever writes into it."""
    return self.root / STATE_DIR

  def manifest_path(self, language: str) -> Path:
    """The generated-file ownership manifest for one language, committed:
    `.truewire/codegen/<language>.json`."""
    return self.root / CODEGEN_DIR / f'{language}.json'

  def legacy_manifest_path(self, language: str) -> Path:
    """Where the manifest lived before W16, git-ignored: `.truewire/<language>-files.json`.
    `generate` moves one it finds to `manifest_path`."""
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
  _table(project, 'project', {'name'}, file=root / PROJECT_FILE)
  spec = _table(data.get('spec'), 'spec', {'dir'}, file=root / PROJECT_FILE)
  spec_value = spec.get('dir', 'spec')
  if not isinstance(spec_value, str) or not spec_value:
    raise NotAProject(f'{root / PROJECT_FILE}: [spec].dir must be a directory path; got {spec_value!r}')
  spec_dir = root / spec_value
  secrets = _secrets(data.get('secrets'), file=root / PROJECT_FILE)
  policy = _policy(data.get('policy'), file=root / PROJECT_FILE)
  stranger = _stranger(data.get('score'), file=root / PROJECT_FILE)
  known = {'project', 'spec', 'secrets', 'policy', 'score', 'cores', 'python', 'typescript', 'rust', 'go'}
  unknown = sorted(set(data) - known)
  if unknown:
    raise NotAProject(f'{root / PROJECT_FILE}: unknown top-level section(s): {", ".join(unknown)}')
  try:
    config = load_codegen_config(
      {key: data[key] for key in ('cores', 'python', 'typescript', 'rust', 'go') if key in data}
    )
  except ValidationError as exc:
    raise NotAProject(f'{root / PROJECT_FILE}: {exc}') from exc
  python_src = root / (config.python.src if config.python is not None else 'src')
  return Project(
    root=root, name=project['name'], spec_dir=spec_dir, config=config,
    python_src=python_src, secrets=secrets, policy=policy, stranger=stranger,
  )


def _table(value: Any, section: str, keys: set[str], *, file: Path) -> dict[str, Any]:
  """A `truewire.toml` table holding only `keys`, `{}` when absent.

  Raises:
    NotAProject: When `value` is not a table, or carries a key outside `keys`.
  """
  if value is None:
    return {}
  if not isinstance(value, dict):
    raise NotAProject(f'{file}: [{section}] must be a table')
  unknown = sorted(set(value) - keys)
  if unknown:
    raise NotAProject(f'{file}: unknown [{section}] key(s): {", ".join(unknown)}')
  return value


def _names(value: Any, where: str, *, file: Path, pattern: re.Pattern[str] | None = None) -> tuple[str, ...]:
  """A list of distinct non-empty strings, each matching `pattern` when given, as a tuple.

  Raises:
    NotAProject: When `value` is not such a list.
  """
  if value is None:
    return ()
  if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
    raise NotAProject(f'{file}: {where} must be a list of strings; got {value!r}')
  if pattern is not None:
    bad = [item for item in value if not pattern.fullmatch(item)]
    if bad:
      raise NotAProject(
        f'{file}: {where} names environment variables only; not a variable name: '
        + ', '.join(repr(item) for item in bad)
      )
  repeated = sorted(item for item, count in Counter(value).items() if count > 1)
  if repeated:
    raise NotAProject(f'{file}: {where} lists {", ".join(repeated)} more than once')
  return tuple(value)


def _secrets(value: Any, *, file: Path) -> Secrets:
  """Read `[secrets]`: `required` and `optional`, lists of environment variable names."""
  table = _table(value, 'secrets', {'required', 'optional'}, file=file)
  required = _names(table.get('required'), '[secrets].required', file=file, pattern=ENV_NAME)
  optional = _names(table.get('optional'), '[secrets].optional', file=file, pattern=ENV_NAME)
  both = sorted(set(required) & set(optional))
  if both:
    raise NotAProject(f'{file}: [secrets] lists {", ".join(both)} as both required and optional')
  return Secrets(required=required, optional=optional)


def _policy(value: Any, *, file: Path) -> Policy:
  """Read `[policy]`: `rate` (a positive number), `retry` (a bool), `refuse` (endpoint ids).

  Whether each `refuse` id names an endpoint is `truewire check`'s to say; the spec is not
  loaded here.
  """
  table = _table(value, 'policy', {'rate', 'retry', 'refuse'}, file=file)
  rate = table.get('rate')
  if rate is not None and (
    isinstance(rate, bool) or not isinstance(rate, int | float) or not math.isfinite(rate) or not rate > 0
  ):
    raise NotAProject(f'{file}: [policy].rate must be a positive number of requests per second; got {rate!r}')
  retry = table.get('retry', False)
  if not isinstance(retry, bool):
    raise NotAProject(f'{file}: [policy].retry must be true or false; got {retry!r}')
  refuse = _names(table.get('refuse'), '[policy].refuse', file=file)
  return Policy(rate=None if rate is None else float(rate), retry=retry, refuse=refuse)


def _stranger(score: Any, *, file: Path) -> date | None:
  """Read `[score].stranger`: a TOML date (`2026-10-12`) or the same as a string.

  Raises:
    NotAProject: When `[score]` is not a table, carries another key, or the date is not one.
  """
  if score is None:
    return None
  if not isinstance(score, dict):
    raise NotAProject(f'{file}: [score] must be a table')
  unknown = sorted(set(score) - {'stranger'})
  if unknown:
    raise NotAProject(f'{file}: unknown [score] key(s): {", ".join(unknown)}')
  value = score.get('stranger')
  if value is None or (isinstance(value, date) and not isinstance(value, datetime)):
    return value
  if isinstance(value, str):
    try:
      return date.fromisoformat(value)
    except ValueError:
      pass
  raise NotAProject(f'{file}: [score].stranger must be a date, YYYY-MM-DD; got {value!r}')


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
