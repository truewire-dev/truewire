"""Where a spec lands in a package: the mapping codegen writes by, and a check reads back.

Two commands need the same answer to *which module, and which method, does this endpoint
become?* — `truewire generate`, which writes them, and `truewire surface`, which
reconciles a project's specs against the callables anyone can actually reach. A second copy
of that mapping would drift, and a check reading a drifted copy reports on something other
than the tree in front of it. That is the shape of defect the check exists to catch, so it
would be a poor thing to build it out of.

The backend hooks are read through `getattr` rather than an interface, because a project's
optional `[python].backend` module overrides only the ones it needs and inherits the rest.
"""

import importlib.util
import json
from pathlib import Path
from typing_extensions import Any, Callable

from truewire.generation.schema import Schema

from truewire.project import Project, resolve, spec_dir
from truewire.spec import Endpoint


class BackendUnavailable(Exception):
  """Raised when a project's codegen backend cannot be loaded, so nothing resolves."""


def package_root(root: Path | Project) -> Path:
  """Return the project's generated package directory — the root every generated path is under.

  `<python_src>/<package>`, from `truewire.toml`'s `[python].src`/`[python].package`. It
  used to be guessed by scanning `pkg/src` for the first directory holding a real `.py`
  file, which silently picked a stale, `__pycache__`-only sibling left behind by a package
  rename and reported every endpoint as `no_module` -- a declared name has no such failure.

  Args:
    root: Project (or project root).

  Raises:
    BackendUnavailable: When the project declares no `[python]` section.
  """
  project = resolve(root)
  if project.python is None:
    raise BackendUnavailable(f'{project.root}: truewire.toml declares no [python] section')
  return project.package_dir


def load_generator(root: Path | Project, language: str = 'python') -> Any:
  """Load a project's codegen backend and return its `generator`.

  The universal `Generator` handles every position in a project's function tree, root
  included, with no per-project override needed: the whole tree hangs directly off
  `<spec>/endpoints/`, so the returned generator gets `output_base` set to the constant
  `''` -- the single, unsplit function tree the root/`main.py` layout assumes. A project declaring `[python].backend` loads that module instead and uses its
  exported `generator` (a `Generator` subclass overriding whichever hooks it needs); the
  `''` default is still applied unless the backend defines `output_base` itself.

  Args:
    root: Project (or project root).
    language: Backend language. Only `python` is supported today.

  Raises:
    BackendUnavailable: When the project has no `[python]` section, the language is
      unsupported, or the declared backend module is unloadable or exports no `generator`.
  """
  project = resolve(root)
  if language != 'python':
    raise BackendUnavailable(f'Unsupported codegen language: {language}')
  if project.python is None:
    raise BackendUnavailable(f'{project.root}: truewire.toml declares no [python] section')
  module_path = project.backend_path
  if module_path is None:
    from truewire.codegen.python import Generator

    generator = Generator()
    # `output_base` is a duck-typed hook (this module's own docstring) never declared
    # on `Generator` itself -- `setattr` avoids pyright flagging an assignment to an
    # attribute the base class doesn't declare, the same way a real backend's own
    # subclass method definition does at the class level instead.
    setattr(generator, 'output_base', lambda endpoint: '')
    return generator
  if not module_path.is_file():
    raise BackendUnavailable(f'Missing codegen backend: {module_path}')

  spec = importlib.util.spec_from_file_location(
    f'truewire_backend_{project.name}_{language}', module_path,
  )
  if spec is None or spec.loader is None:
    raise BackendUnavailable(f'Could not load generator module: {module_path}')

  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  generator = getattr(module, 'generator', None)
  if generator is None:
    raise BackendUnavailable(f'Missing `generator` symbol in {module_path}')
  if not callable(getattr(generator, 'output_base', None)):
    setattr(generator, 'output_base', lambda endpoint: '')
  return generator


def load_schema_file(path: Path) -> dict[str, Schema]:
  """Decode one `schemas.json` file (the project root's own, or a nested scope's, design
  §5b) as id -> `Schema` -- the single decode `load_schemas` used to inline for the root
  file alone, now shared with `discover_schemas_files`' nested files too.

  Args:
    path: A real `schemas.json` file (caller already confirmed it exists).
  """
  data = json.loads(path.read_text())
  return {schema_id: Schema.model_validate(schema) for schema_id, schema in data.items()}


def load_schemas(root: Path | Project) -> dict[str, Schema]:
  """Load `spec/schemas.json` as the shared schemas a backend is handed before it runs.

  Handing them over is not optional for a reader that only wants the layout: a backend's
  `skip_endpoint` may render a subscription payload to judge whether it is typed, and those
  payloads `$ref` shared schemas that do not resolve until `Generator.schemas` has recorded
  them. A caller that skipped this step would be told a channel is unemittable because it
  did not supply the map, which is a wrong answer arrived at confidently.

  Root-only -- see `discover_schemas_files` for a project's nested `spec/endpoints/**/
  schemas.json` scopes too. Kept as its own function (rather than folded into
  `discover_schemas_files`) since its pre-existing caller (`surface/check.py`'s
  `skip_endpoint` probe) only ever wants the one file.

  Args:
    root: Project (or project root).
  """
  schemas_path = spec_dir(root) / 'schemas.json'
  if not schemas_path.is_file():
    return {}
  return load_schema_file(schemas_path)


def discover_schemas_files(root: Path | Project) -> list[Path]:
  """Every `schemas.json` belonging to one project: the project root's own
  `spec/schemas.json`, if present, followed by every nested `spec/endpoints/**/
  schemas.json` scope, in a deterministic (sorted) order.

  A directory under `spec/endpoints/` may carry its own `schemas.json` beside its
  `router.json`, scoping visibility to itself and everything beneath it -- this is the
  full inventory a codegen run needs before it can resolve any of them: `Generator.
  schemas()` renders each file found here into its own types module,
  and `Generator._resolve_schemas` walks a leaf's ancestors to decide which of these
  files it can actually see.

  Args:
    root: Project (or project root).
  """
  paths: list[Path] = []
  spec = spec_dir(root)
  root_schemas = spec / 'schemas.json'
  if root_schemas.is_file():
    paths.append(root_schemas)
  endpoints_root = spec / 'endpoints'
  if endpoints_root.is_dir():
    paths.extend(sorted(endpoints_root.rglob('schemas.json')))
  return paths


def schemas_scope(path: Path, root: Path | Project) -> tuple[str, ...]:
  """Directory segments (relative to `spec/endpoints/`) a discovered `schemas.json`'s own
  scope lives under -- the empty tuple for the project root's own `spec/schemas.json` (or
  for a `schemas.json` placed directly at `spec/endpoints/schemas.json`, which resolves to
  the identical scope; `add_planned_file`'s own path-collision check is what refuses two
  files that both claim it).

  Args:
    path: One entry from `discover_schemas_files`.
    root: Project (or project root).
  """
  spec = spec_dir(root)
  if path == spec / 'schemas.json':
    return ()
  endpoints_root = spec / 'endpoints'
  return path.parent.relative_to(endpoints_root).parts


def output_base(generator: Any, endpoint: Endpoint) -> str:
  """Return the package subtree a backend generates one endpoint into."""
  base: Callable[[Endpoint], str] | None = getattr(generator, 'output_base', None)
  if callable(base):
    return base(endpoint)
  return 'api'


def output_function(
  generator: Any, endpoint: Endpoint, *,
  endpoint_path: Path | None = None, spec_root: Path | None = None,
) -> str:
  """Return the dotted function path a backend gives one endpoint inside its base.

  A backend overriding `output_function` wins outright, same as always. Otherwise, an
  authored `endpoint.function` wins too -- but a project that never authors one
  (`Endpoint.function` is derivable, not required) has `endpoint.function is None`, and
  the only place that actually derives one is `Endpoint.resolved_function`. Without
  calling it here, a bare `Generator()` driving a directory-derived project (no
  per-project `output_function` override) would return `None` and crash on the first
  `.split('.')` downstream.

  `endpoint_path`/`spec_root` are optional and additive so existing callers (`spec
  surface`) that don't have them handy keep their exact prior behavior -- `None` when
  unset, same as always.

  Args:
    generator: The project's loaded codegen backend.
    endpoint: The endpoint to resolve a function path for.
    endpoint_path: This endpoint's own `endpoint.json`, needed only to derive a
      function path when none was authored.
    spec_root: The project's `spec/` directory, needed only for the same derivation.
  """
  fn: Callable[[Endpoint], str] | None = getattr(generator, 'output_function', None)
  if callable(fn):
    return fn(endpoint)
  if endpoint.function is not None or endpoint_path is None or spec_root is None:
    return endpoint.function
  return endpoint.resolved_function(endpoint_path, spec_root)


def skip_endpoint(generator: Any, endpoint: Endpoint) -> bool:
  """Return whether a backend excludes one endpoint from generation entirely."""
  skip: Callable[[Endpoint], bool] | None = getattr(generator, 'skip_endpoint', None)
  return bool(skip(endpoint)) if callable(skip) else False


def skip_router(generator: Any, base: str, node: tuple[str, ...]) -> bool:
  """Return whether a backend excludes one router node's file from generation entirely.

  Symmetric with `skip_endpoint`, one level up: `skip_endpoint` marks an individual
  leaf hand-written while still letting the leaf's node feed a parent router that
  shares its directory (the intentional design at `cli/generate.py`'s endpoint-loop
  comment). `skip_router` is for the opposite shape -- a router file itself is
  hand-composed (custom lifecycle code a generic composition class can't express),
  regardless of whether some of its children are genuinely generated.

  Args:
    generator: The project's loaded codegen backend.
    base: The output base the router node lives under (a backend's `output_base`).
    node: The router node's path, root-first, as `router_nodes` returns it.
  """
  skip: Callable[[str, tuple[str, ...]], bool] | None = getattr(generator, 'skip_router', None)
  return bool(skip(base, node)) if callable(skip) else False


def endpoint_output(function: str, *, base: str = 'api') -> Path:
  """Return the module path one function generates into, relative to the package root."""
  *sections, method = function.split('.')
  return Path(base).joinpath(*sections, f'{method}.py')


def router_nodes(functions: list[str]) -> list[tuple[str, ...]]:
  """Return every router node one base's functions hang off, root first."""
  nodes: set[tuple[str, ...]] = {()}
  for function in functions:
    parts = tuple(function.split('.'))
    for i in range(1, len(parts)):
      nodes.add(parts[:i])
  return sorted(nodes)


def class_name(section: str) -> str:
  """Return the PascalCase class name derived from one function segment."""
  parts = section.replace('-', '_').split('_')
  return ''.join(part[:1].upper() + part[1:] for part in parts if part)


def aggregate_nodes(functions: list[str]) -> set[tuple[str, ...]]:
  """Return the router nodes whose children are all leaf endpoints.

  A leaf under such a node is named after itself; a leaf under any other node is reached
  through the attribute it is annotated under. Which of the
  two applies is a property of the whole base, so it cannot be decided one endpoint at a
  time — which is why a caller scoped to one subdivision still has to load them all.
  """
  all_nodes = {tuple(function.split('.')) for function in functions}
  out: set[tuple[str, ...]] = set()
  for node in router_nodes(functions):
    if not node:
      continue
    child_nodes = sorted({
      parts[:len(node) + 1]
      for parts in all_nodes
      if len(parts) > len(node) and parts[:len(node)] == node
    })
    if not child_nodes:
      continue
    if all(
      child in all_nodes and
      not any(other[:len(child)] == child and len(other) > len(child) for other in all_nodes)
      for child in child_nodes
    ):
      out.add(node)
  return out
