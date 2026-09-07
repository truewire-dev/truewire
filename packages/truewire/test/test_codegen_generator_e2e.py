"""End-to-end smoke test for the universal `Generator` (codegen mechanization, Phase 4),
Task 20 -- the last task of that phase, validating it as a whole before any real client
migrates onto it.

`_resolve_core` (Task 14), `rpc_endpoint` (Task 15), `stream_endpoint` (Task 16), `router`
(Task 18), and `_root_class` (Task 19) were each unit-tested in isolation against the
fixture client (`test_codegen_generator.py`), calling one method at a time with hand-built
arguments. Nothing before this test ever drove them *together*, the way a real generation
run has to: schemas, then every leaf endpoint, then every router, then the root, writing
real files a real Python interpreter and a real `pyright` run then have to accept. Running
that full pipeline surfaced four real integration gaps this file's own `generate_client`
and the shared `Generator`/CLI code had to close (see each fix's own docstring/comment for
the full reasoning):

1. `Generator.endpoint` (the `rpc`/`stream`/`grpc` dispatcher) never forwarded
   `endpoint_dir` to `rpc_endpoint`/`stream_endpoint`, even though both have required it
   since Tasks 15/16 -- calling the dispatcher for any request/response-shaped endpoint
   raised `ValueError` immediately. Fixed to forward it only when the resolved method
   actually declares the parameter (so every real client's still-legacy `rpc_endpoint`/
   `stream_endpoint` override, none of which declare it, is unaffected).
2. `Generator.endpoint_schemas` (feeding `type_names`/`class_name`'s collision-avoidance
   naming, run by the CLI *before* `rpc_endpoint`/`stream_endpoint` are even called)
   unconditionally read `endpoint.openapi`, which is `None` for every new-shape
   (`request`/`response`) endpoint -- crashed with `AttributeError` on the very first
   fixture endpoint. Fixed to build the same `$request`/`$response`/`$parameters`/
   `$payload` schema map `rpc_endpoint`/`stream_endpoint` themselves already use.
3. `stream_endpoint`'s generated method annotated its own return type as the bare payload
   type (`-> Ticker`), while `self.subscribe(...)` -- per design §2/§8's own stated
   contract -- actually returns a stream/manager object, not a single payload. A real
   `core.subscribe()` returning `StreamManager[Ticker, Any, Any]` fails pyright's
   `reportReturnType` against that annotation. Fixed to annotate
   `StreamManager[{payload}, Any, Any]`, with the matching import.
4. `truewire.codegen.layout.output_function`'s "no override" fallback returned
   `endpoint.function` directly, which is `None` for a client whose `function` was never
   authored (Task 3's whole point) -- `Endpoint.resolved_function`, the method that
   actually derives one from directory position, had zero callers anywhere in this repo.
   Fixed additively: `output_function` now takes optional `endpoint_path`/`spec_root` and
   falls through to `resolved_function` when given them; `cli/codegen.py`'s two call sites
   now pass them, `spec surface`'s does not (unaffected, same prior behavior).

This file's own `generate_client` used to carry a three-part self-collision mechanism for
a directory that is both a leaf and a router node (design §3's old "mixed" shape,
`mixed_dir/` here): the node's own leaf output redirected one level deeper so it didn't
collide with the `<node>/` package at the same path, that leaf's class name suffixed
`Endpoint` so it didn't collide with the router class the same node also produces, and a
synthetic self-child added to the router's own `children` map so the leaf was reachable
through the composed class. Task 24a ported all three parts into `cli/codegen.py`'s own
endpoint/router loops, and its own review then found the composed self-child could only
ever render as `__call__` (S29-forbidden), since a self-referential node's own parent can
never qualify as an `aggregate_nodes` parent. Task 24c retires the whole mechanism in
favor of an outright refusal (`docs/spec/authoring.md` rule 16, `docs/production_
standards.md` S30, design §4) -- `router()` now raises unconditionally naming the
offending directory, so this file's own `mixed_dir/` fixture was restructured the same way
rule 16 asks bit2me's 8 real offenders to be: its own `endpoint.json` moved into a new
`get/` subdirectory, leaving `mixed_dir/` a plain aggregate of `get/`/`nested/` -- and
`generate_client` no longer needs any self-collision handling at all, ordinary or
otherwise; a router node reaching its own loop is never also a leaf.

`generate_client` otherwise mirrors `truewire.cli.codegen.codegen()`'s own plan-building
logic (schemas, per-endpoint loop, per-router loop, including the client root -- Task 24a
retired `_root_class`; `router()` now renders every node, root included, the identical
way `cli/codegen.py` itself does too). It is not a thin call into that command: the CLI is
hard-wired to `clients/<name>` layout, a per-client `codegen/python.py` backend, and
manifest/ruff/pyright bookkeeping the fixture has no use for -- none of which this task
needs to duplicate to prove Phase 4's own pieces combine correctly.
"""

from pathlib import Path
import asyncio
import importlib
import json
import shutil
import subprocess
import sys

from truewire.codegen.layout import (
  aggregate_nodes,
  class_name,
  discover_schemas_files,
  endpoint_output,
  load_schema_file,
  output_base,
  output_function,
  router_nodes,
  schemas_scope,
)
from truewire.codegen.python import (
  Generator,
  RouterChild,
  endpoint_transport,
  subtree_transport,
)
from truewire.spec import Endpoint, load_router
from truewire.spec.codegen_toml import load_codegen_toml
from dataclasses import replace

from truewire.project import resolve

FIXTURE_ROOT = Path(__file__).parent / 'fixtures' / 'codegen_fixture_client'


def relative_import(from_file: Path, to_file: Path) -> str:
  """Return the relative import one generated module writes to reach a sibling module.

  Copied from `truewire.cli.codegen.codegen`'s own closure of the same name (not
  exported there -- it's a local helper of that command) rather than reused, since
  reaching into another command's closure isn't possible from here.

  Args:
    from_file: Path of the module doing the importing, relative to the package root.
    to_file: Path of the module being imported, relative to the package root.
  """
  from_parts = from_file.parts[:-1]
  to_parts = to_file.parts[:-1]
  common = 0
  while (
    common < min(len(from_parts), len(to_parts))
    and from_parts[common] == to_parts[common]
  ):
    common += 1
  up = len(from_parts) - common
  down = list(to_parts[common:])
  dots = '.' * (up + 1)
  target = down[:]
  if to_file.stem != '__init__':
    target.append(to_file.stem)
  suffix = '.'.join(target)
  return dots + suffix if suffix else dots


def generate_client(root: Path, output_dir: Path):
  """Run the full python codegen pipeline for the client at `root`, writing generated
  files into `output_dir` and copying in `root`'s hand-written `core_impl/fixture_client`
  as the package's own `core/`-equivalent support code.

  See this module's own docstring for why this exists here, rather than as a call into
  `truewire.cli.codegen.codegen`, and for the "mixed" self-collision handling below that
  neither this function nor the CLI generalizes into a real spec-shape concept yet.

  Args:
    root: The client root (a directory holding `spec/`, `codegen/config.toml`, and -- for this
      fixture specifically -- `core_impl/fixture_client`).
    output_dir: Directory the generated `fixture_client` package is written into. Must not
      already exist -- becomes the package root itself (`output_dir/main.py`,
      `output_dir/market/orderbook.py`, ...), not a parent of one named `fixture_client`.
  """
  generator = Generator()
  # No per-client `output_base` override exists for this fixture (design §4 describes a
  # client with one function tree rooted directly at `spec/endpoints/`, no per-surface
  # split like binance's `spot`/`futures`), so every endpoint's base is the package root
  # itself -- the empty string, not `layout.output_base`'s own `'api'` default, which
  # exists only for a legacy client with no override at all.
  generator.output_base = lambda endpoint: ''
  # The generated package lives under `output_dir`, not the fixture's own `core_impl/`:
  # point the project's `python_src` there so core introspection imports the tree being
  # generated (with its placeholder `main.py`), not the committed fixture.
  generator.project = replace(resolve(root), python_src=output_dir.parent)
  generator.codegen_config = generator.project.config
  # Design §5a's own mechanism genuinely imports a resolved core's live class
  # (`Generator._import_core_class`) to introspect `.new()`. Doing that requires
  # `import fixture_client...` to succeed *during* generation, before every planned file
  # has been written -- which in turn requires `fixture_client/__init__.py` (hand-
  # curated, design §4: `from .main import FixtureClient`) to already find a real
  # `main.py` on disk. For a real, already-migrated client this is never a problem --
  # every client in this repo already has a real `main.py` from its own prior codegen
  # backend before it would ever adopt this mechanism -- but this fixture's own
  # `generate_client` always starts from an empty `output_dir`, so it has to reproduce
  # that same "already has a main.py" precondition itself: copy the hand-written
  # `core_impl/fixture_client` support code into `output_dir` (matching the copy this
  # function used to do only at the very end) and seed a throwaway placeholder `main.py`
  # *before* generation runs, both overwritten by the real generated content below.
  shutil.copytree(root / 'core_impl' / 'fixture_client', output_dir, dirs_exist_ok=True)
  (output_dir / 'main.py').write_text(
    'class FixtureClient:\n'
    '  """Placeholder, overwritten by generate_client below."""\n'
  )
  output_dir_parent = str(output_dir.parent)
  sys.path.insert(0, output_dir_parent)
  generator.core_package = 'fixture_client.core'

  planned: dict[Path, str] = {}
  # One `schemas()` call per discovered `schemas.json` file (design §5b, Task 24b step 6)
  # -- the fixture's own root `spec/schemas.json` (`OrderSide`) plus its nested
  # `spec/endpoints/futures/schemas.json` (`FuturesSide`) -- mirroring `cli/codegen.py`'s
  # own multi-file wiring exactly, since this function's whole purpose (this module's own
  # docstring) is proving every mechanized piece combines the same way the CLI drives it.
  schemas_by_path: dict[Path, dict] = {}
  merged_shared_schemas: dict = {}
  for schemas_path in discover_schemas_files(root):
    file_schemas = load_schema_file(schemas_path)
    merged_shared_schemas.update(file_schemas)
    generator.schemas_scope = schemas_scope(schemas_path, root)
    es = generator.schemas(file_schemas)
    schemas_by_path[schemas_path] = es['references']
    for file in es['files']:
      planned[Path(file['path'])] = file['content']
  generator.schemas_scope = None
  generator.shared_schemas = merged_shared_schemas

  endpoints_root = root / 'spec' / 'endpoints'
  spec_root = root / 'spec'
  spec_files = sorted(endpoints_root.rglob('endpoint.json'))
  records = [
    (spec_file, Endpoint.model_validate_json(spec_file.read_text()))
    for spec_file in spec_files
  ]
  records = [
    (spec_file, endpoint)
    for spec_file, endpoint in records
    if endpoint.surface is None or endpoint.surface.kind != 'absent'
  ]

  functions_by_base: dict[str, list[str]] = {}
  for spec_file, endpoint in records:
    base = output_base(generator, endpoint)
    functions_by_base.setdefault(base, []).append(
      output_function(generator, endpoint, endpoint_path=spec_file, spec_root=spec_root)
    )
  aggregates_by_base = {
    base: aggregate_nodes(functions) for base, functions in functions_by_base.items()
  }
  router_nodes_by_base = {
    base: set(router_nodes(functions)) for base, functions in functions_by_base.items()
  }

  endpoint_by_node: dict[tuple[str, tuple[str, ...]], Endpoint] = {}
  path_by_node: dict[tuple[str, tuple[str, ...]], Path] = {}
  class_by_node: dict[tuple[str, tuple[str, ...]], str] = {}
  transport_by_node: dict[tuple[str, tuple[str, ...]], str] = {}

  for spec_file, endpoint in records:
    base = output_base(generator, endpoint)
    function = output_function(
      generator, endpoint, endpoint_path=spec_file, spec_root=spec_root
    )
    node = tuple(function.split('.'))
    # A directory that is *also* a router node -- carries its own `endpoint.json`
    # alongside an endpoint-bearing descendant subdirectory -- is refused outright by
    # `router()` (rule 16/S30, Task 24c), so no node reaching this loop is ever also a
    # router node; `endpoint_output` always produces `<node>.py` with no collision to
    # redirect around.
    out = endpoint_output(function, base=base)
    endpoint_by_node[(base, node)] = endpoint
    path_by_node[(base, node)] = out
    transport_by_node[(base, node)] = endpoint_transport(endpoint)
    parent = node[:-1]
    child_name = node[-1]
    requested_name = class_name(child_name)
    # Per-endpoint-directory reference resolution (design §5b, Task 24b): only the
    # schemas.json scopes actually visible from this endpoint's own directory, mirroring
    # `cli/codegen.py`'s identical `_resolve_schemas` call.
    endpoint_references = generator._resolve_schemas(spec_file.parent, spec_root, schemas_by_path)
    endpoint_class_name = generator.class_name(
      endpoint, endpoint_references, name=requested_name
    )
    class_by_node[(base, node)] = endpoint_class_name
    method_name = generator.method_name(
      endpoint,
      parent=parent,
      child_name=child_name,
      is_aggregate_parent=parent in aggregates_by_base[base],
    )
    planned[out] = (
      generator.endpoint(
        endpoint,
        endpoint_references,
        class_name=endpoint_class_name,
        method_name=method_name,
        endpoint_dir=spec_file.parent,
      )
      + '\n'
    )

  router_records: list[tuple[str, tuple[str, ...]]] = []
  for base, functions in functions_by_base.items():
    for node in router_nodes(functions):
      router_records.append((base, node))

  for base, node in router_records:
    # The true client root (design §4/§5c) writes to `main.py`, not `<base>/__init__.py`
    # -- `__init__.py` is the hand-curated public export surface (design §4), which this
    # generation plan must never overwrite. An ordinary base's own top (`spot/__init__.py`,
    # say) is unaffected -- this only fires for the one base representing the whole,
    # unsplit function tree (`base == ''`, this fixture's own convention).
    out = Path('main.py') if base == '' and not node else Path(base).joinpath(*node, '__init__.py')
    prefix_len = len(node)
    children: dict[str, RouterChild] = {}
    # A router node is never also a leaf (rule 16/S30, Task 24c) -- `(base, node) in
    # endpoint_by_node` never holds here, so there is no self-child to compose; every
    # child below is a real subdirectory.
    child_names = sorted(
      {
        parts[prefix_len]
        for node_base, parts in path_by_node
        if node_base == base and len(parts) > prefix_len and parts[:prefix_len] == node
      }
    )
    for child_name in child_names:
      child_node = (*node, child_name)
      # A child that is itself a router node (has further descendants of its own, e.g.
      # `account/deposits` under `account`) is composed as a `kind: 'router'` child
      # pointing at its own generated composite class -- never as a bare `kind: 'endpoint'`
      # leaf, which would multiply-inherit only its leaf portion directly and silently
      # drop everything under its own subdirectory.
      if child_node in router_nodes_by_base[base]:
        child_out = Path(base).joinpath(*child_node, '__init__.py')
        children[child_name] = {
          'import_path': relative_import(out, child_out),
          'class_name': class_name(child_name),
          'attr_name': child_name,
          'kind': 'router',
          'transport': subtree_transport(transport_by_node, base, child_node),
          'doc': load_router(root / 'spec' / 'endpoints' / Path(base, *child_node)),
          'spec_dir': endpoints_root / Path(base, *child_node),
        }
      elif (base, child_node) in endpoint_by_node:
        child_out = path_by_node[(base, child_node)]
        children[child_name] = {
          'import_path': relative_import(out, child_out),
          'class_name': class_by_node[(base, child_node)],
          'attr_name': child_name,
          'kind': 'endpoint',
          'transport': transport_by_node[(base, child_node)],
        }
      else:
        child_out = Path(base).joinpath(*child_node, '__init__.py')
        children[child_name] = {
          'import_path': relative_import(out, child_out),
          'class_name': class_name(child_name),
          'attr_name': child_name,
          'kind': 'router',
          'transport': subtree_transport(transport_by_node, base, child_node),
          'doc': load_router(root / 'spec' / 'endpoints' / Path(base, *child_node)),
          'spec_dir': endpoints_root / Path(base, *child_node),
        }
    generator.router_context = (base, node)
    section = node[-1] if node else (base or 'root')
    # `router()` handles every node uniformly now, including the true root (design
    # §5c/Task 24a) -- no separate `_root_class` call.
    planned[out] = generator.router(section, children) + '\n'

  for relative, content in planned.items():
    destination = output_dir / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content)

  # Undo the `sys.path` insertion above and drop any `fixture_client` module this
  # function's own generation step happened to import (`_import_core_class`, design §5a)
  # -- otherwise a later real `import fixture_client` (this function's own caller) could
  # resolve a module cached from mid-generation (the placeholder `main.py`, say) instead
  # of the real, now-fully-written one at `output_dir`.
  if output_dir_parent in sys.path:
    sys.path.remove(output_dir_parent)
  for name in [n for n in list(sys.modules) if n == 'fixture_client' or n.startswith('fixture_client.')]:
    del sys.modules[name]


def _write_pyright_config(tmp_path: Path):
  """Write a `pyrightconfig.json` at `tmp_path` resolving `fixture_client` as an absolute
  import root, against this checkout's own real `.venv` (so `truewire_core`/`pydantic`/
  `typing_extensions` all resolve exactly as they would for a real client)."""
  root = Path(__file__).resolve().parents[3]
  config = {
    'include': ['fixture_client'],
    'extraPaths': ['.'],
    'venvPath': str(root),
    'venv': '.venv',
  }
  (tmp_path / 'pyrightconfig.json').write_text(json.dumps(config, indent=2) + '\n')


def test_fixture_client_generates_and_type_checks(tmp_path: Path):
  """The full generation pipeline, run against the real fixture client, produces a
  `fixture_client` package that both imports (implicitly -- `pyright` cannot type-check
  code it can't parse and resolve) and type-checks cleanly. This is the whole point of
  Task 20 (see this module's own docstring): the four integration gaps it found were each
  invisible to Tasks 14-19's own isolated unit tests, and only surfaced once every piece
  ran together against real, written-to-disk output."""
  output_dir = tmp_path / 'fixture_client'
  generate_client(FIXTURE_ROOT, output_dir)
  _write_pyright_config(tmp_path)

  result = subprocess.run(
    [sys.executable, '-m', 'pyright', str(output_dir)],
    capture_output=True,
    text=True,
    cwd=tmp_path,
  )
  assert result.returncode == 0, result.stdout + result.stderr


def test_fixture_client_generated_methods_work_at_runtime(tmp_path: Path):
  """Beyond type-checking, the generated code actually runs: construct the generated
  root client against the hand-written `core_impl` (real `RpcEndpoint`/`FuturesEndpoint`
  against a trivial in-memory transport, not a stub -- see `core_impl/fixture_client/
  core.py`'s own module docstring), call one HTTP RPC endpoint from each resolved core
  (`market.orderbook` on the default core, `futures.positions` on the futures subcore),
  and drive one WS stream endpoint (`market.ticker_stream`) through a real
  `StreamManager`. Also confirms the shared `InMemoryTransport.send`'s own credential
  check fires for a `signed` futures call made with no `api_key` configured -- every
  fixture endpoint's `auth` is fixed at generation time (spec-declared, not a per-call
  choice), so `futures.positions`'s generated call always passes `signed=True` regardless
  of caller; what a caller actually controls is whether the *client* was built with
  credentials at all (`FixtureClient.new(public=True)` here).
  """
  output_dir = tmp_path / 'fixture_client'
  generate_client(FIXTURE_ROOT, output_dir)
  sys.path.insert(0, str(tmp_path))
  try:
    fixture_client = importlib.import_module('fixture_client')
    FixtureClient = fixture_client.FixtureClient

    async def run():
      client = FixtureClient.new(api_key='test-key')
      async with client:
        client.client.seed_response('GET', '/v1/orderbook', b'{"bids": [["100", "1"]]}')
        orderbook = await client.market.orderbook(symbol='BTC-USD')
        assert orderbook == {'bids': [['100', '1']]}

        client.client.seed_response(
          'GET', '/v1/futures/positions', b'{"positions": ["p1"]}'
        )
        positions = await client.futures.positions(symbol='BTC-USD')
        assert positions == {'positions': ['p1']}

        client.client.seed_channel(
          '/ticker/BTC-USD',
          [b'{"price": "1.5"}', b'{"price": "1.75"}'],
        )
        received = []
        async with client.market.ticker_stream(symbol='BTC-USD') as stream:
          async for message in stream:
            received.append(message)
        assert [str(m['price']) for m in received] == ['1.5', '1.75']

      # A fresh, public-only client (no credentials configured) still generates a
      # `signed=True` call for `futures.positions` -- confirms the shared transport's own
      # credential check actually runs at request time, not just at construction.
      public_client = FixtureClient.new(public=True)
      async with public_client:
        public_client.client.seed_response(
          'GET',
          '/v1/futures/positions',
          b'{"positions": []}',
        )
        try:
          await public_client.futures.positions(symbol='BTC-USD')
        except PermissionError:
          pass
        else:
          raise AssertionError('futures endpoint accepted an unsigned call')

    asyncio.run(run())
  finally:
    sys.path.remove(str(tmp_path))
    for name in [
      n
      for n in list(sys.modules)
      if n == 'fixture_client' or n.startswith('fixture_client.')
    ]:
      del sys.modules[name]
