"""Reconcile a project's in-scope specs against the callables anyone can actually call.

Several gates measure a spec, the generated tree, the types, the tests and the docs. Not
one of them asks whether a spec became something a caller can call. A backend that
excluded every WebSocket endpoint from generation by design once let seven new WebSocket
specs pass every gate with no method anywhere in the package. Nothing was broken; nothing
said anything either, and silence reads exactly like success.

So this asks the one question the others do not: **for every in-scope spec, what can a
caller call?** There are three honest answers and no fourth.

- **generated** — the backend emits the module, and the module defines the method. Resolved
  through `truewire.codegen.layout`, the same mapping codegen writes by, so the check
  cannot drift away from the thing it is checking.
- **hand-written** — the backend refuses, and the spec's `surface` names the method someone
  wrote instead. The symbol is looked up, so the record fails when the method is renamed.
- **absent** — there is no callable, and the spec's `surface` says why. Counted and named,
  never hidden.

Anything else is a gap, and the summary names it.

**Examples are not consulted, at all.** An unverified endpoint — specced from the API's
documentation, no `examples/` recorded, because it is effectful or the credentials for it
are out of reach — still *generates a method*. What is missing there is evidence, not code,
and `truewire examples` is where that question is asked. A check that read this
directory would fire on every one of a project's unverified endpoints, which is how a check
gets switched off. It reads the spec tree, the backend, and the package, and nothing else.

There is deliberately no search for a hand-written method that merely looks like it belongs
to a spec. A `streams.market.all_tickers` channel may be served by a method called
`tickers`, and a `streams.user.trades` one by `my_trades`; a name-matching
heuristic would miss both and then report the silence it found as a finding. Guessing is
the failure being fixed, not the fix.
"""

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing_extensions import Any, Literal

from truewire.codegen.layout import (
  BackendUnavailable,
  aggregate_nodes,
  endpoint_output,
  load_generator,
  load_schemas,
  output_base,
  output_function,
  package_root,
  skip_endpoint,
)
from truewire.project import Project, resolve
from truewire.spec import AbsentSurface, Endpoint, HandwrittenSurface, endpoint_records

Fault = Literal['undeclared', 'no_module', 'no_method', 'no_symbol', 'stale']
"""Why a spec reaches nobody, or why its declaration no longer describes the client."""


@dataclass(frozen=True)
class Gap:
  """One in-scope spec that reaches no caller, or whose `surface` has stopped being true."""
  function: str
  path: str
  """Spec directory, relative to `spec/endpoints`, so a report names a file to open."""
  fault: Fault
  detail: str


@dataclass(frozen=True)
class Reconciliation:
  """Every in-scope spec, classified, and the gaps that make the check fail.

  The three outcome lists are kept whole rather than counted, because the summary has to be
  able to *name* what generated nothing. A count alone is how a skipped endpoint stayed
  invisible in the first place.
  """
  generated: list[str] = field(default_factory=list)
  handwritten: list[str] = field(default_factory=list)
  absent: list[str] = field(default_factory=list)
  gaps: list[Gap] = field(default_factory=list)
  missing_validate: list[str] = field(default_factory=list)
  """Generated `rpc` endpoints whose resolved method accepts no `validate` parameter
  (`docs/production_standards.md` S8). Kept separate from `gaps`: the method is a real,
  callable answer to the spec -- `gaps` is reserved for a spec reaching no caller at all,
  and this is a signature-contract defect on one that does. Whole projects failed this
  silently before the check existed."""

  @property
  def total(self) -> int:
    """Return how many in-scope specs were classified, gaps included."""
    return len(self.generated) + len(self.handwritten) + len(self.absent) + len(self.gaps)


def module_callables(path: Path) -> set[str]:
  """Return every name a module binds to something a caller could call.

  Parsed, never imported. A generated package imports its own dependencies, and a check
  that executed the tree it is judging would fail for reasons that have nothing to do with
  the question — and could not run at all against a project whose package is
  mid-generation, which is exactly when this is worth running.

  Both methods and module-level functions count. Generated endpoints always put the method
  on a class, hand-written ones usually do, and nothing here needs to care which.

  Args:
    path: Module to read.
  """
  try:
    tree = ast.parse(path.read_text())
  except (OSError, SyntaxError):
    return set()
  names: set[str] = set()
  for node in ast.walk(tree):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
      names.add(node.name)
  return names


def resolve_signature(path: Path, method: str) -> set[str] | None:
  """Return one function/method's parameter names, or `None` if it is not defined.

  Walked the same way `module_callables` is, so it shares that function's tolerance for a
  package mid-generation. The *last* definition under the given name wins: a
  `@typing.overload` stub is always written before the real implementation, so this never
  resolves to a stub's signature instead of the callable one.

  Args:
    path: Module to read.
    method: Function or method name to resolve.
  """
  try:
    tree = ast.parse(path.read_text())
  except (OSError, SyntaxError):
    return None
  found: set[str] | None = None
  for node in ast.walk(tree):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == method:
      args = node.args
      found = {arg.arg for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
  return found


def resolve_symbol(package: Path, symbol: str) -> tuple[Path, str]:
  """Split a `module.path:name` symbol into the file it names and the method it names.

  Args:
    package: The client's inner package directory, which the module path is relative to.
    symbol: Symbol exactly as the spec declares it.
  """
  module, _, name = symbol.partition(':')
  return package.joinpath(*module.split('.')).with_suffix('.py'), name


def _generated(
  generator: Any, endpoint: Endpoint, package: Path, aggregates: dict[str, set[tuple[str, ...]]],
  *, endpoint_path: Path | None = None, spec_root: Path | None = None,
) -> tuple[Path, str]:
  """Return the module and method one endpoint generates, per the backend's own layout.

  `endpoint_path`/`spec_root` thread through to `output_function` so a fully mechanized
  project (`endpoint.function` dropped, derived from directory position instead)
  resolves correctly here too, not just from `truewire generate`'s own loop -- omitting
  them silently returned `None` and crashed on the first `.split('.')` below the moment a
  real project stopped authoring `function` at all.
  """
  base = output_base(generator, endpoint)
  function = output_function(
    generator, endpoint, endpoint_path=endpoint_path, spec_root=spec_root,
  )
  node = tuple(function.split('.'))
  parent = node[:-1]
  method = generator.method_name(
    endpoint,
    parent=parent,
    child_name=node[-1],
    is_aggregate_parent=parent in aggregates.get(base, set()),
  )
  return package / endpoint_output(function, base=base), method


def reconcile(root: Path | Project, *, scope: Path | None = None, language: str = 'python') -> Reconciliation:
  """Classify every in-scope spec of one client by what a caller can call.

  The backend is loaded and handed its shared schemas exactly as `truewire generate` does,
  because `skip_endpoint` is a backend decision and there is no way to ask it from outside.
  Every endpoint is loaded even when `scope` narrows the report to one subdivision: whether
  a leaf is generated as `__call__` or under its own name is a property of the whole base,
  so a subdivision judged on its own would be judged against the wrong method names.

  Args:
    root: Project (or project root) being judged.
    scope: Subdivision under `<spec>/endpoints` to restrict the report to.
    language: Codegen backend to resolve the layout with.

  Raises:
    BackendUnavailable: When the project has no loadable codegen backend, so no layout can
      be resolved and every answer would be a guess.
  """
  project = resolve(root)
  if language == 'go':
    from .go import reconcile_go

    return reconcile_go(project, scope=scope)
  if language == 'typescript':
    return reconcile_typescript(project, scope=scope)

  if language == 'rust':
    return _reconcile_rust(project, scope=scope)
  package = package_root(project)
  generator = load_generator(project, language)
  generator.core_package = f'{package.name}.core'
  if getattr(generator, 'skip_endpoint', None) is not None:
    # Handed over only to a backend that filters endpoints, because only such a backend can
    # need them: a `skip_endpoint` may render a subscription payload whose `$ref`s do
    # not resolve until `schemas` has recorded the map. Every other backend is left alone,
    # and that is not tidiness — `schemas` is a *generation* hook and some of them write to
    # disk (one clears two package subtrees from inside it), and a check that
    # deleted part of the tree it was about to read would report the damage it had just
    # done. A backend with no `skip_endpoint` skips nothing, so there is nothing to ask it.
    generator.schemas(load_schemas(project))

  spec_root = project.spec_dir
  every = endpoint_records(project)
  functions_by_base: dict[str, list[str]] = {}
  for record in every:
    # `skip_endpoint` alone decides whether a node feeds `functions_by_base` -- it now
    # answers for `AbsentSurface` too (`Generator.skip_endpoint`'s own base
    # implementation, `codegen/python.py`), mirroring `cli/codegen.py`'s own up-front
    # filter exactly. Deliberately not special-cased here: a synthetic test backend
    # that (unlike the real `Generator`) doesn't recognize `AbsentSurface` needs this to
    # still fall through and get counted, so `reconcile`'s own `stale` check below can
    # tell a genuinely-excluded endpoint from a declaration that's quietly gone stale
    # (`test_a_declaration_left_behind_after_the_backend_learned_to_emit_fails`) --
    # baking an unconditional `AbsentSurface` short-circuit in here, bypassing whatever
    # the actual backend says, defeats that check outright.
    if skip_endpoint(generator, record.endpoint):
      continue
    functions_by_base.setdefault(output_base(generator, record.endpoint), []).append(
      output_function(
        generator, record.endpoint, endpoint_path=record.path, spec_root=spec_root,
      )
    )
  aggregates = {base: aggregate_nodes(functions) for base, functions in functions_by_base.items()}

  endpoints_root = project.endpoints_dir
  out = Reconciliation()
  for record in endpoint_records(project, scope=scope):
    endpoint = record.endpoint
    rel = record.path.parent.relative_to(endpoints_root).as_posix()
    surface = endpoint.surface
    if skip_endpoint(generator, endpoint):
      if surface is None:
        out.gaps.append(Gap(
          function=endpoint.function, path=rel, fault='undeclared',
          detail=f'skipped by the {language} backend; no `surface` declared',
        ))
      elif isinstance(surface, HandwrittenSurface):
        module, method = resolve_symbol(package, surface.symbol)
        if method in module_callables(module):
          # `endpoint.function` is `None` for a fully mechanized endpoint (dropped,
          # derived from directory position) -- the same gap `_generated` already
          # closes, reached here by a `handwritten`-surface endpoint (a bytes-typed
          # response with no JSON schema to derive one from, say).
          out.handwritten.append(
            endpoint.function
            or output_function(generator, endpoint, endpoint_path=record.path, spec_root=spec_root)
          )
        else:
          out.gaps.append(Gap(
            function=endpoint.function, path=rel, fault='no_symbol',
            detail=f'{surface.symbol}: no such method in {module.relative_to(package).as_posix()}',
          ))
      else:
        # `endpoint.function` is `None` for a fully mechanized endpoint (dropped,
        # derived from directory position) -- the same fallback `out.handwritten`'s
        # own branch above already needs, and an `AbsentSurface` endpoint is exactly as
        # likely to be fully mechanized as a generated one.
        out.absent.append(
          endpoint.function
          or output_function(generator, endpoint, endpoint_path=record.path, spec_root=spec_root)
        )
      continue

    if surface is not None:
      kind = 'hand-written' if isinstance(surface, HandwrittenSurface) else 'absent'
      out.gaps.append(Gap(
        function=endpoint.function, path=rel, fault='stale',
        detail=f'declared {kind}, but the backend now generates it; drop the declaration',
      ))
      continue

    module, method = _generated(
      generator, endpoint, package, aggregates,
      endpoint_path=record.path, spec_root=spec_root,
    )
    if not module.is_file():
      out.gaps.append(Gap(
        function=endpoint.function, path=rel, fault='no_module',
        detail=f'{module.relative_to(package).as_posix()} not in package; needs regenerating',
      ))
    elif method not in module_callables(module):
      out.gaps.append(Gap(
        function=endpoint.function, path=rel, fault='no_method',
        detail=f'{module.relative_to(package).as_posix()} defines no {method!r}',
      ))
    else:
      out.generated.append(endpoint.function)
      if endpoint.spec.kind == 'rpc':
        # `stream`/`grpc` endpoints are exempt: a stream method returns a subscription, not
        # one validated response, and there is no operation to validate a gRPC message
        # against. Only a single-request/response `rpc` method is the one the guideline's
        # own worked example (`Depth.depth`) and every real defect found were about.
        params = resolve_signature(module, method)
        if params is not None and 'validate' not in params:
          out.missing_validate.append(endpoint.function)
  return out


_TS_MEMBER = re.compile(
  r'^[ \t]*(?:export[ \t]+)?(?:(?:public|private|protected|static|readonly|abstract|override|declare|async|function)[ \t]+)*'
  r'\*?[ \t]*([A-Za-z_$][\w$]*)[ \t]*(?:<[^>\n]*>)?[ \t]*[(:=]',
  re.MULTILINE,
)
"""A class member or top-level function declaration at the start of a line: `name(`, `async
name(`, `readonly name:`, `function name(`. Parsed as text, never compiled, for the same
reason `module_callables` never imports: the package may be mid-generation."""


def typescript_callables(path: Path) -> set[str]:
  """Return every name a TypeScript module declares as a method, property or function.

  Args:
    path: Module to read.
  """
  try:
    text = path.read_text()
  except OSError:
    return set()
  return set(_TS_MEMBER.findall(text))


def typescript_parameters(path: Path, method: str) -> set[str] | None:
  """Return the parameter names of a TypeScript method's last declaration (its
  implementation, written after its overloads), or `None` when it is not declared.

  Args:
    path: Module to read.
    method: Method name.
  """
  try:
    text = path.read_text()
  except OSError:
    return None
  pattern = re.compile(
    r'^[ \t]*(?:(?:async|public|private|protected|static)[ \t]+)*\*?[ \t]*' + re.escape(method) + r'[ \t]*\(([^)]*)\)',
    re.MULTILINE,
  )
  found = pattern.findall(text)
  if not found:
    return None
  return {part.split(':')[0].split('=')[0].strip().rstrip('?') for part in found[-1].split(',') if part.strip()}


def reconcile_typescript(project: Project, *, scope: Path | None = None) -> Reconciliation:
  """Classify every in-scope spec by what a TypeScript caller can call.

  The same three answers as `reconcile`, from the TypeScript backend's own rendering:

  - **generated**: the plan renders the endpoint (nothing in `Rendered.skipped` names it)
    and its module under `[typescript].src/<package>` declares the camelCase method.
  - **hand-written**: the spec declares `surface: handwritten`, which the backend does not
    render, and a `[typescript.extras]` entry serves the spec's symbol: the symbol's module
    path (`spot.account.retrieve_export`) names the router node (`spot.account`) and the
    entry's `file` (`retrieve_export`), its name the method (`retrieve_export` ->
    `retrieveExport`) the entry lists and the file declares.
  - **absent**: the spec declares `surface: absent`.

  A generated `rpc` method whose implementation takes no `options` (where `validate` is
  passed) is reported in `missing_validate`, as in Python.

  Args:
    project: The project being judged.
    scope: Subdivision under `<spec>/endpoints` to restrict the report to.

  Raises:
    BackendUnavailable: The project declares no `[typescript]` section.
  """
  from truewire.codegen.typescript import render_package
  from truewire.codegen.typescript.endpoint import endpoint_file
  from truewire.codegen.typescript.names import camel_case
  from truewire.plan.build import build_plan

  config = project.typescript
  if config is None:
    raise BackendUnavailable(f'{project.root}: truewire.toml declares no [typescript] section')
  plan = build_plan(project)
  rendered = render_package(plan, project)
  package = project.typescript_package_dir
  extras = config.extras or {}
  out = Reconciliation()
  for record in endpoint_records(project, scope=scope):
    endpoint = record.endpoint
    function = endpoint.resolved_function(record.path, project.spec_dir)
    rel = record.path.parent.relative_to(project.endpoints_dir).as_posix()
    surface = endpoint.surface
    if isinstance(surface, AbsentSurface):
      out.absent.append(function)
      continue
    if isinstance(surface, HandwrittenSurface):
      module_path, _, name = surface.symbol.partition(':')
      parts = module_path.split('.')
      node, file = '.'.join(parts[:-1]), parts[-1]
      method = camel_case(name)
      target = package.joinpath(*parts).with_suffix('.ts')
      entry = next((e for e in extras.get(node, []) if e.file == file and method in e.methods), None)
      if entry is None:
        out.gaps.append(Gap(
          function=function, path=rel, fault='no_symbol',
          detail=f'{surface.symbol}: no [typescript.extras.{node!r}] entry with file {file!r} lists {method!r}',
        ))
      elif method not in typescript_callables(target):
        out.gaps.append(Gap(
          function=function, path=rel, fault='no_symbol',
          detail=f'{surface.symbol}: {target.relative_to(package).as_posix()} declares no {method!r}',
        ))
      else:
        out.handwritten.append(function)
      continue

    planned = plan.endpoint(function)
    skipped = any(note.startswith(f'{function}:') for note in rendered.skipped)
    segments = function.split('.')
    target = package / endpoint_file(segments)
    method = camel_case(segments[-1])
    if planned is None or skipped:
      out.gaps.append(Gap(
        function=function, path=rel, fault='undeclared',
        detail='skipped by the typescript backend; no `surface` declared',
      ))
    elif not target.is_file():
      out.gaps.append(Gap(
        function=function, path=rel, fault='no_module',
        detail=f'{target.relative_to(package).as_posix()} not in package; needs regenerating',
      ))
    elif method not in typescript_callables(target):
      out.gaps.append(Gap(
        function=function, path=rel, fault='no_method',
        detail=f'{target.relative_to(package).as_posix()} defines no {method!r}',
      ))
    else:
      out.generated.append(function)
      if planned.kind == 'rpc':
        params = typescript_parameters(target, method)
        if params is not None and 'options' not in params:
          out.missing_validate.append(function)
  return out

_RUST_FN = r'\bfn\s+{name}\s*[<(]'


def _rust_defines(text: str, name: str) -> bool:
  """Whether Rust source `text` defines a function or method called `name`."""
  import re
  return re.search(_RUST_FN.format(name=re.escape(name)), text) is not None


def _reconcile_rust(project: Project, *, scope: Path | None) -> Reconciliation:
  """`reconcile` for the Rust backend, which renders from the plan rather than through a
  Python `Generator`.

  What the backend emits is asked of `render_package` in memory, and what the package
  holds is read off disk, so a package that is missing files or was edited by hand shows
  up. A generated method lives in `<router>/<endpoint>.rs` as `fn <method>`; the Rust form
  of `validate: false` is its `_raw` twin, so a generated `rpc` method returning a value
  with no twin is reported as `missing_validate`.

  Hand-written Rust methods are inherent `impl` blocks on the generated structs, written in
  the crate beside them (`impl crate::spot::account::Account { pub async fn ... }`), so no
  table declares them: a `surface.kind == "handwritten"` endpoint is satisfied when a
  function named after its `symbol` exists anywhere in the package. A `surface` written
  for another backend never makes a Rust-generated endpoint `stale`: it describes that
  backend's refusal, not this one's.

  Raises:
    BackendUnavailable: When `truewire.toml` declares no `[rust]` section.
  """
  from truewire.codegen.rust import render_package
  from truewire.codegen.rust.endpoint import endpoint_file, raw_name
  from truewire.codegen.rust.names import snake_ident
  from truewire.plan.build import build_plan

  if project.rust is None:
    raise BackendUnavailable(f'{project.root}: truewire.toml declares no [rust] section')
  package = project.rust_package_dir
  plan = build_plan(project)
  rendered = render_package(plan, project)
  skipped = {note.split(':', 1)[0]: note.split(':', 1)[1].strip() for note in rendered.skipped if ':' in note}
  by_function = {endpoint.function: endpoint for endpoint in plan.endpoints}
  sources = {path: path.read_text() for path in package.rglob('*.rs')} if package.is_dir() else {}

  out = Reconciliation()
  endpoints_root = project.endpoints_dir
  for record in endpoint_records(project, scope=scope):
    endpoint = record.endpoint
    function = endpoint.resolved_function(record.path, project.spec_dir)
    rel = record.path.parent.relative_to(endpoints_root).as_posix()
    surface = endpoint.surface
    if surface is not None and surface.kind == 'absent':
      out.absent.append(function)
      continue
    if isinstance(surface, HandwrittenSurface):
      _, name = resolve_symbol(package, surface.symbol)
      if any(_rust_defines(text, name) for text in sources.values()):
        out.handwritten.append(function)
      else:
        out.gaps.append(Gap(function=function, path=rel, fault='no_symbol', detail=f'no `fn {name}` in the Rust package'))
      continue
    planned = by_function.get(function)
    file = endpoint_file(function.split('.'))
    if planned is None or file not in rendered.files:
      reason = skipped.get(function) or 'not rendered by the rust backend'
      out.gaps.append(Gap(function=function, path=rel, fault='undeclared', detail=f'{reason}; no `surface` declared'))
      continue
    module = package / file
    if module not in sources:
      out.gaps.append(Gap(function=function, path=rel, fault='no_module', detail=f'{file} not in package; needs regenerating'))
      continue
    fallback = 'subscribe' if planned.kind == 'stream' else 'call'
    method = snake_ident(planned.path[-1], fallback=fallback)
    if not _rust_defines(sources[module], method):
      out.gaps.append(Gap(function=function, path=rel, fault='no_method', detail=f'{file} defines no `fn {method}`'))
      continue
    out.generated.append(function)
    if planned.kind == 'rpc' and planned.response.payload is not None and not _rust_defines(sources[module], raw_name(method)):
      out.missing_validate.append(function)
  return out

