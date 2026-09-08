"""Verify codegen owns and formats only the files recorded in its manifest."""

from pathlib import Path
from types import SimpleNamespace
import importlib
import json
import sys

import pytest

from truewire.project import resolve

codegen_module = importlib.import_module('truewire.cli.generate')

BACKEND_TOML = (
  '[python]\npackage = "venue"\nsrc = "pkg/src"\nbackend = "backend.py"\n'
  '[python.cores.default]\nbase = "venue.core:Endpoint"\n'
)
"""A project whose codegen runs through a per-project backend module."""


def test_skip_router_reads_the_backend_hook():
  """A backend with no `skip_router` override never skips; one that has it is consulted."""
  from truewire.codegen import layout

  class NoHook:
    pass

  class WithHook:
    def skip_router(self, base, node):
      return node[:1] == ('streams',)

  assert layout.skip_router(NoHook(), 'spot', ('streams',)) is False
  assert layout.skip_router(WithHook(), 'spot', ('streams',)) is True
  assert layout.skip_router(WithHook(), 'spot', ('streams', 'listen_keys')) is True
  assert layout.skip_router(WithHook(), 'spot', ('market',)) is False


def test_format_generated_files_runs_ruff_only_on_python_targets(
  tmp_path: Path,
  monkeypatch,
):
  """Formatting receives generated Python files, not the complete client package."""
  root = tmp_path / 'venue'
  output_root = root / 'pkg' / 'src' / 'venue'
  calls: list[tuple[tuple[str, ...], Path]] = []

  def run(command: tuple[str, ...], *, cwd: Path, capture_output: bool, text: bool):
    """Record the formatter invocation and report success."""
    assert capture_output
    assert text
    calls.append((command, cwd))
    return SimpleNamespace(returncode=0, stdout='', stderr='')

  monkeypatch.setattr(codegen_module.subprocess, 'run', run)
  codegen_module.format_generated_files(
    root,
    output_root,
    {Path('market/ticker.py'), Path('market/__init__.py'), Path('schemas.json')},
    config=root / 'ruff.toml',
  )

  assert calls == [
    (
      (
        sys.executable,
        '-m',
        'ruff',
        'format',
        '--config',
        str(root / 'ruff.toml'),
        'pkg/src/venue/market/__init__.py',
        'pkg/src/venue/market/ticker.py',
      ),
      root,
    )
  ]


def test_format_generated_files_skips_an_output_without_python(
  tmp_path: Path,
  monkeypatch,
):
  """A backend emitting no Python file does not invoke Ruff."""
  monkeypatch.setattr(
    codegen_module.subprocess,
    'run',
    lambda *args, **kwargs: pytest.fail('Ruff should not run'),
  )
  codegen_module.format_generated_files(
    tmp_path, tmp_path / 'pkg', {Path('schema.json')}, config=tmp_path / 'ruff.toml'
  )


def test_format_generated_files_turns_a_ruff_failure_into_a_codegen_failure(
  tmp_path: Path,
  monkeypatch,
):
  """Codegen must not report success after Ruff rejects generated files."""
  root = tmp_path / 'venue'
  output_root = root / 'pkg' / 'src' / 'venue'

  def run(command: tuple[str, ...], *, cwd: Path, capture_output: bool, text: bool):
    """Return one representative Ruff failure."""
    return SimpleNamespace(returncode=2, stdout='', stderr='invalid configuration')

  monkeypatch.setattr(codegen_module.subprocess, 'run', run)
  with pytest.raises(codegen_module.CodegenError, match='invalid configuration'):
    codegen_module.format_generated_files(
      root, output_root, {Path('market/ticker.py')}, config=root / 'ruff.toml'
    )


def test_typecheck_client_runs_the_client_pyright_project(
  tmp_path: Path,
  monkeypatch,
  capsys,
):
  """Pyright uses the current interpreter and the client's checked-in project config."""
  client_root = tmp_path / 'venue'
  root = client_root
  client_root.mkdir(parents=True)
  (client_root / 'pyrightconfig.json').write_text('{}')
  calls: list[tuple[tuple[str, ...], Path]] = []

  def run(command: tuple[str, ...], *, cwd: Path, capture_output: bool, text: bool):
    """Record the type-check invocation and report success."""
    assert capture_output
    assert text
    calls.append((command, cwd))
    return SimpleNamespace(
      returncode=0,
      stdout='0 errors, 1 warning, 0 informations\n',
      stderr='',
    )

  monkeypatch.setattr(codegen_module.subprocess, 'run', run)
  assert codegen_module.typecheck_project(resolve(client_root)) is True

  assert calls == [
    (
      (
        sys.executable,
        '-m',
        'pyright',
        '--project',
        str(client_root),
      ),
      root,
    )
  ]
  assert capsys.readouterr().out == '0 errors, 1 warning, 0 informations\n'


def test_typecheck_client_requires_a_checked_in_config(tmp_path: Path, monkeypatch):
  """Without a `pyrightconfig.json` or `[python].pyright`, no type check runs at all."""
  client_root = tmp_path / 'venue'
  root = client_root
  client_root.mkdir(parents=True)
  monkeypatch.setattr(
    codegen_module.subprocess,
    'run',
    lambda *args, **kwargs: pytest.fail('Pyright should not run'),
  )

  assert codegen_module.typecheck_project(resolve(client_root)) is False


def test_typecheck_client_turns_pyright_errors_into_a_codegen_failure(
  tmp_path: Path,
  monkeypatch,
  capsys,
):
  """Diagnostics remain visible while a failing Pyright exit rejects generation."""
  client_root = tmp_path / 'venue'
  root = client_root
  client_root.mkdir(parents=True)
  (client_root / 'pyrightconfig.json').write_text('{}')

  def run(command: tuple[str, ...], *, cwd: Path, capture_output: bool, text: bool):
    """Return one representative Pyright failure."""
    return SimpleNamespace(
      returncode=1,
      stdout='pkg/src/venue/api.py:1:1 - error: broken\n',
      stderr='',
    )

  monkeypatch.setattr(codegen_module.subprocess, 'run', run)
  with pytest.raises(codegen_module.CodegenError, match=r'Pyright failed.*exit code 1'):
    codegen_module.typecheck_project(resolve(client_root))

  assert 'error: broken' in capsys.readouterr().out


@pytest.mark.parametrize(
  'path', ['', '.', '../outside.py', '/outside.py', 'a/../b.py', r'a\b.py']
)
def test_generated_path_rejects_unsafe_or_non_posix_paths(path: str):
  """Manifest and planned paths must stay canonical and package-relative."""
  with pytest.raises(codegen_module.CodegenError, match='Unsafe generated path'):
    codegen_module.generated_path(path)


def test_missing_manifest_bootstraps_with_no_owned_files(tmp_path: Path):
  """A first run cannot infer ownership and therefore starts with an empty set."""
  assert codegen_module.load_generated_manifest(tmp_path / 'python-files.json') == set()


@pytest.mark.parametrize(
  'content, message',
  [
    ('not json', 'Could not read'),
    ('[]', 'expected an object'),
    ('{"version": 2, "files": []}', 'Unsupported'),
    ('{"version": 1, "files": "api.py"}', '`files` must be a list'),
    ('{"version": 1, "files": ["../api.py"]}', 'Unsafe generated path'),
    ('{"version": 1, "files": ["api.py", "api.py"]}', 'duplicate file path'),
  ],
)
def test_invalid_manifest_fails_closed(tmp_path: Path, content: str, message: str):
  """Malformed ownership data cannot authorize a deletion."""
  manifest = tmp_path / 'python-files.json'
  manifest.write_text(content)
  with pytest.raises(codegen_module.CodegenError, match=message):
    codegen_module.load_generated_manifest(manifest)


def test_manifest_round_trip_is_sorted_and_deterministic(tmp_path: Path):
  """Tracked ownership has stable JSON and reloads to the same paths."""
  manifest = tmp_path / 'codegen' / 'python-files.json'
  files = {Path('spot/time.py'), Path('account/__init__.py')}

  codegen_module.write_generated_manifest(manifest, files)

  assert json.loads(manifest.read_text()) == {
    'version': 1,
    'files': ['account/__init__.py', 'spot/time.py'],
  }
  assert manifest.read_text().endswith('\n')
  assert not manifest.with_suffix('.json.tmp').exists()
  assert codegen_module.load_generated_manifest(manifest) == files


def test_reconciliation_preserves_handwritten_siblings_byte_for_byte(tmp_path: Path):
  """Only a stale manifested file is removed from a mixed generated/manual directory."""
  output_root = tmp_path / 'pkg'
  account = output_root / 'account'
  account.mkdir(parents=True)
  generated = account / 'accounts.py'
  stale = account / 'removed.py'
  handwritten = account / 'user_info.py'
  generated.write_text('old generated\n')
  stale.write_text('stale generated\n')
  handwritten_bytes = b'hand-written\r\ncontent\r\n'
  handwritten.write_bytes(handwritten_bytes)
  planned = {
    Path('account/accounts.py'): 'new generated\n',
    Path('account/__init__.py'): 'router\n',
  }

  codegen_module.reconcile_generated_output(
    output_root,
    {Path('account/accounts.py'), Path('account/removed.py')},
    set(planned),
  )
  codegen_module.write_planned_files(output_root, planned)

  assert generated.read_text() == codegen_module.GENERATED_BANNER + 'new generated\n'
  assert not stale.exists()
  assert handwritten.read_bytes() == handwritten_bytes


def test_reconciliation_prunes_only_empty_directories(tmp_path: Path):
  """Stale generated directories disappear while directories with manual files remain."""
  output_root = tmp_path / 'pkg'
  emptying = output_root / 'old' / 'nested'
  mixed = output_root / 'mixed'
  emptying.mkdir(parents=True)
  mixed.mkdir(parents=True)
  (emptying / 'stale.py').write_text('stale\n')
  (mixed / 'stale.py').write_text('stale\n')
  (mixed / 'manual.py').write_text('manual\n')

  codegen_module.reconcile_generated_output(
    output_root,
    {Path('old/nested/stale.py'), Path('mixed/stale.py')},
    set(),
  )

  assert not (output_root / 'old').exists()
  assert mixed.is_dir()
  assert (mixed / 'manual.py').is_file()


def test_reconciliation_rejects_a_symlink_escape(tmp_path: Path):
  """A malicious manifest cannot delete through a package-internal directory symlink."""
  output_root = tmp_path / 'pkg'
  outside = tmp_path / 'outside'
  output_root.mkdir()
  outside.mkdir()
  safe = output_root / 'a.py'
  safe.write_text('keep too\n')
  victim = outside / 'victim.py'
  victim.write_text('keep\n')
  (output_root / 'linked').symlink_to(outside, target_is_directory=True)

  with pytest.raises(codegen_module.CodegenError, match='escapes package'):
    codegen_module.reconcile_generated_output(
      output_root, {Path('a.py'), Path('linked/victim.py')}, set()
    )

  assert safe.read_text() == 'keep too\n'
  assert victim.read_text() == 'keep\n'


def test_writing_rejects_a_symlink_to_a_handwritten_file(tmp_path: Path):
  """A generated target cannot overwrite a manual file through an in-package symlink."""
  output_root = tmp_path / 'pkg'
  output_root.mkdir()
  handwritten = output_root / 'manual.py'
  handwritten.write_text('manual\n')
  (output_root / 'generated.py').symlink_to(handwritten)

  with pytest.raises(codegen_module.CodegenError, match='traverses a symlink'):
    codegen_module.write_planned_files(
      output_root, {Path('generated.py'): 'generated\n'}
    )

  assert handwritten.read_text() == 'manual\n'


def test_write_planned_files_banners_python_output(tmp_path: Path):
  """Every generated .py/.pyi file opens with the contribution banner."""
  planned = {Path('pkg/src/demo/api/orders.py'): 'async def orders(): ...\n'}
  codegen_module.write_planned_files(tmp_path, planned)
  content = (tmp_path / 'pkg' / 'src' / 'demo' / 'api' / 'orders.py').read_text()
  assert content.startswith('# Generated by truewire — do not edit by hand.\n')
  assert 'async def orders(): ...' in content


def test_add_planned_file_rejects_collisions():
  """No generated source may silently overwrite another source's planned output."""
  planned: dict[Path, str] = {}
  codegen_module.add_planned_file(planned, path='api/time.py', content='first\n')

  with pytest.raises(codegen_module.CodegenError, match='same file'):
    codegen_module.add_planned_file(planned, path='api/time.py', content='second\n')


def test_check_generated_manifest_reports_plan_and_existence_differences(
  tmp_path: Path,
):
  """Check mode distinguishes unowned, stale, and missing generated paths."""
  output_root = tmp_path / 'pkg'
  output_root.mkdir()
  (output_root / 'current.py').write_text('current\n')

  issues = codegen_module.check_generated_manifest(
    output_root,
    {Path('current.py'), Path('missing.py'), Path('stale.py')},
    {Path('current.py'), Path('new.py'), Path('missing.py')},
  )

  assert issues == [
    'not recorded in manifest: new.py',
    'no longer planned: stale.py',
    'owned file is missing: missing.py',
    'owned file is missing: stale.py',
  ]


def test_check_reports_an_owned_file_whose_content_differs_from_the_plan(
  tmp_path: Path, monkeypatch, capsys,
):
  """`--check` compares content, not only ownership: a stale body is `out of date`."""
  client_root = write_backend_project(tmp_path)
  output_root = client_root / 'pkg' / 'src' / 'venue'
  monkeypatch.setattr(codegen_module, 'format_generated_files', lambda *a, **k: None)
  monkeypatch.setattr(codegen_module, 'typecheck_project', lambda project: None)
  codegen_module.generate('python', project=str(client_root), verbose=0)
  codegen_module.generate('python', project=str(client_root), verbose=0, check=True)
  assert 'Generated files match the plan for venue (3 files).' in capsys.readouterr().out

  stale = output_root / 'market' / 'time.py'
  stale.write_text(stale.read_text() + '\nHAND_EDIT = True\n')
  with pytest.raises(codegen_module.typer.Exit) as raised:
    codegen_module.generate('python', project=str(client_root), verbose=0, check=True)

  assert raised.value.exit_code == 1
  err = capsys.readouterr().err
  assert 'Generated files for venue differ from the plan:' in err
  assert '- out of date: market/time.py' in err
  assert 'main.py' not in err
  assert stale.read_text().endswith('HAND_EDIT = True\n')  # check never writes


def test_check_without_a_manifest_takes_the_plan_as_the_owned_files(
  tmp_path: Path, monkeypatch, capsys,
):
  """A fresh clone has no `.truewire/` (gitignored); `--check` still verifies existence
  and content, saying the plan stood in, and never writes the manifest itself."""
  client_root = write_backend_project(tmp_path)
  output_root = client_root / 'pkg' / 'src' / 'venue'
  monkeypatch.setattr(codegen_module, 'format_generated_files', lambda *a, **k: None)
  monkeypatch.setattr(codegen_module, 'typecheck_project', lambda project: None)
  codegen_module.generate('python', project=str(client_root), verbose=0)
  manifest = resolve(client_root).manifest_path('python')
  manifest.unlink()

  codegen_module.generate('python', project=str(client_root), verbose=0, check=True)
  out = capsys.readouterr().out
  assert 'Generated files match the plan for venue (3 files).' in out
  assert 'No manifest at .truewire/python-files.json; the plan stood in for it' in out
  assert not manifest.exists()

  (output_root / 'market' / 'time.py').write_text('class Time:\n  edited = True\n')
  (output_root / 'main.py').unlink()
  with pytest.raises(codegen_module.typer.Exit) as raised:
    codegen_module.generate('python', project=str(client_root), verbose=0, check=True)
  assert raised.value.exit_code == 1
  err = capsys.readouterr().err
  assert '- owned file is missing: main.py' in err
  assert '- out of date: market/time.py' in err
  assert 'no longer planned' not in err
  assert not manifest.exists()


def test_check_renders_the_plan_the_way_generate_writes_it(tmp_path: Path, monkeypatch):
  """With the real formatter: what `generate` wrote (banner, Ruff-formatted) is exactly
  what `--check` expects, so a clean tree passes and only a hand edit is reported."""
  from typer.testing import CliRunner
  from truewire.cli import app
  from test_init import _seed_endpoint

  runner = CliRunner()
  monkeypatch.chdir(tmp_path)
  assert runner.invoke(app, ['init', 'demo']).exit_code == 0
  project = tmp_path / 'demo'
  _seed_endpoint(project)
  assert runner.invoke(app, ['generate', 'python', '--project', str(project)]).exit_code == 0
  module = project / 'src' / 'demo' / 'pets' / 'get.py'
  assert module.read_text().startswith(codegen_module.GENERATED_BANNER)

  checked = runner.invoke(app, ['generate', 'python', '--check', '--project', str(project)])
  assert checked.exit_code == 0, checked.output
  assert 'Generated files match the plan for demo' in checked.output

  module.write_text(module.read_text().replace('Get a pet.', 'Get a cat.'))
  checked = runner.invoke(app, ['generate', 'python', '--check', '--project', str(project)])
  assert checked.exit_code == 1
  assert '- out of date: pets/get.py' in checked.output
  assert 'main.py' not in checked.output


def test_delete_and_check_are_mutually_exclusive():
  """The two non-default modes cannot describe one invocation together."""
  with pytest.raises(codegen_module.typer.Exit) as raised:
    codegen_module.generate('python', project='/nonexistent', verbose=0, delete=True, check=True)

  assert raised.value.exit_code == 1


def write_backend_project(tmp_path: Path) -> Path:
  """A one-endpoint project generating through a per-project backend module; returns its root."""
  client_root = tmp_path / 'venue'
  output_root = client_root / 'pkg' / 'src' / 'venue'
  endpoint_root = client_root / 'spec' / 'endpoints' / 'market' / 'time'
  endpoint_root.mkdir(parents=True)
  output_root.mkdir(parents=True)
  (client_root / 'truewire.toml').write_text(BACKEND_TOML)
  (client_root / 'backend.py').write_text(
    """from truewire.codegen.python import Generator

class Backend(Generator):
  def schemas(self, schemas):
    return {'files': [{'path': 'types.py', 'content': 'TYPE = 1\\n'}], 'references': {}}

  def rpc_endpoint(self, endpoint, references, *, class_name, method_name):
    return f'class {class_name}:\\n  pass'

  def router(self, section, children):
    return 'class Router:\\n  pass'

generator = Backend()
"""
  )
  (endpoint_root / 'endpoint.json').write_text(
    json.dumps(
      {
        'function': 'market.time',
        'spec': {
          'kind': 'rpc',
          'transports': ['http'],
          'path': '/time',
          'method': 'GET',
          'openapi': {
            'summary': 'Get time',
            'responses': {
              '200': {
                'description': 'Success',
                'content': {'application/json': {'schema': {'type': 'integer'}}},
              },
            },
          },
        },
      }
    )
  )
  return client_root


def test_codegen_manifest_covers_every_output_and_drives_the_next_cleanup(
  tmp_path: Path,
  monkeypatch,
):
  """A real CLI run bootstraps ownership, preserves manual code, then removes owned stale code."""
  client_root = write_backend_project(tmp_path)
  root = client_root
  output_root = client_root / 'pkg' / 'src' / 'venue'
  handwritten = output_root / 'market' / 'manual.py'
  handwritten.parent.mkdir(parents=True)
  handwritten_bytes = b'manual\r\n'
  handwritten.write_bytes(handwritten_bytes)
  finalizers: list[str] = []
  # `--check` formats its scratch render of the plan, never the project tree: the stub
  # tells the two apart by where it was asked to run.
  monkeypatch.setattr(
    codegen_module,
    'format_generated_files',
    lambda root, output_root, files, config: finalizers.append(
      'ruff' if root == client_root else 'ruff(scratch)'
    ),
  )
  monkeypatch.setattr(
    codegen_module,
    'typecheck_project',
    lambda project: finalizers.append('pyright'),
  )

  codegen_module.generate('python', project=str(client_root), verbose=0)

  assert finalizers == ['ruff', 'pyright']
  manifest = resolve(client_root).manifest_path('python')
  assert codegen_module.load_generated_manifest(manifest) == {
    Path('main.py'),
    Path('market/__init__.py'),
    Path('market/time.py'),
  }
  assert handwritten.read_bytes() == handwritten_bytes

  stale = output_root / 'market' / 'stale.py'
  stale.write_text('stale\n')
  owned = codegen_module.load_generated_manifest(manifest) | {
    Path('market/stale.py')
  }
  codegen_module.write_generated_manifest(manifest, owned)
  mismatched_manifest = manifest.read_bytes()

  with pytest.raises(codegen_module.typer.Exit) as raised:
    codegen_module.generate('python', project=str(client_root), verbose=0, check=True)

  assert raised.value.exit_code == 1
  assert finalizers == ['ruff', 'pyright', 'ruff(scratch)']
  assert stale.read_text() == 'stale\n'
  assert manifest.read_bytes() == mismatched_manifest

  codegen_module.generate('python', project=str(client_root), verbose=0)

  assert finalizers == ['ruff', 'pyright', 'ruff(scratch)', 'ruff', 'pyright']
  finalizers.clear()
  assert not stale.exists()
  assert handwritten.read_bytes() == handwritten_bytes

  generated = codegen_module.load_generated_manifest(manifest)
  generated_bytes = {path: (output_root / path).read_bytes() for path in generated}
  manifest_bytes = manifest.read_bytes()

  codegen_module.generate('python', project=str(client_root), verbose=0, check=True)

  assert finalizers == ['ruff(scratch)']
  assert manifest.read_bytes() == manifest_bytes
  assert {
    path: (output_root / path).read_bytes() for path in generated
  } == generated_bytes
  assert handwritten.read_bytes() == handwritten_bytes

  already_absent = sorted(generated)[0]
  (output_root / already_absent).unlink()
  (client_root / 'backend.py').unlink()
  codegen_module.generate('python', project=str(client_root), verbose=0, delete=True)

  assert finalizers == ['ruff(scratch)']
  assert not manifest.exists()
  assert all(not (output_root / path).exists() for path in generated)
  assert handwritten.read_bytes() == handwritten_bytes

  with pytest.raises(codegen_module.typer.Exit) as raised:
    codegen_module.generate('python', project=str(client_root), verbose=0, delete=True)

  assert raised.value.exit_code == 1


def test_skip_router_leaves_a_hand_written_subtree_alone(tmp_path: Path, monkeypatch):
  """A backend can mark a whole router node hand-written, not just individual leaves."""
  client_root = tmp_path / 'venue'
  root = client_root
  output_root = client_root / 'pkg' / 'src' / 'venue'
  market_root = client_root / 'spec' / 'endpoints' / 'market' / 'time'
  stream_root = client_root / 'spec' / 'endpoints' / 'streams' / 'market' / 'ping'
  market_root.mkdir(parents=True)
  stream_root.mkdir(parents=True)
  output_root.mkdir(parents=True)
  (client_root / 'truewire.toml').write_text(BACKEND_TOML)
  (client_root / 'backend.py').write_text(
    """from truewire.codegen.python import Generator

class Backend(Generator):
  def schemas(self, schemas):
    return {'files': [], 'references': {}}

  def rpc_endpoint(self, endpoint, references, *, class_name, method_name):
    return f'class {class_name}:\\n  pass'

  def router(self, section, children):
    return 'class Router:\\n  pass'

  def skip_endpoint(self, endpoint):
    return endpoint.function.startswith('streams.')

  def skip_router(self, base, node):
    return node[:1] == ('streams',)

generator = Backend()
"""
  )
  body = {
    'spec': {
      'kind': 'rpc', 'transports': ['http'], 'method': 'GET',
      'openapi': {
        'summary': 'Op',
        'responses': {'200': {
          'description': 'Success',
          'content': {'application/json': {'schema': {'type': 'integer'}}},
        }},
      },
    },
  }
  (market_root / 'endpoint.json').write_text(
    json.dumps({'function': 'market.time', 'spec': {**body['spec'], 'path': '/time'}})
  )
  (stream_root / 'endpoint.json').write_text(
    json.dumps({'function': 'streams.market.ping', 'spec': {**body['spec'], 'path': '/ping'}})
  )
  hand_written = output_root / 'streams' / '__init__.py'
  hand_written.parent.mkdir(parents=True)
  hand_written_bytes = b'class Streams:\n  ...\n'
  hand_written.write_bytes(hand_written_bytes)
  monkeypatch.setattr(codegen_module, 'format_generated_files', lambda *a, **k: None)
  monkeypatch.setattr(codegen_module, 'typecheck_project', lambda *a, **k: None)

  codegen_module.generate('python', project=str(client_root), verbose=0)

  manifest = resolve(client_root).manifest_path('python')
  owned = codegen_module.load_generated_manifest(manifest)
  assert Path('market/__init__.py') in owned
  assert Path('market/time.py') in owned
  assert not any(p.parts[:1] == ('streams',) for p in owned)
  assert hand_written.read_bytes() == hand_written_bytes


def test_codegen_generates_a_codegen_toml_client_end_to_end(tmp_path: Path, monkeypatch):
  """A real CLI run against a client with `codegen/config.toml` and no `codegen/python.py`
  backend at all -- `load_generator`'s new fallback (design §5c/Task 24a) drives the
  whole thing through the universal `Generator`, root included, with no special
  dispatch anywhere in this command. Proves Step 8's own end-to-end bar: the fixture
  shape (a fully mechanized client) generates correctly through the *real* `codegen()`
  command, not just `test_codegen_generator_e2e.py`'s own hand-rolled pipeline mirror."""
  client_root = tmp_path / 'chain'
  root = client_root
  output_root = client_root / 'pkg' / 'src' / 'chainvenue'
  market_dir = client_root / 'spec' / 'endpoints' / 'market'
  time_dir = market_dir / 'time'
  time_dir.mkdir(parents=True)
  output_root.mkdir(parents=True)

  (client_root / 'spec' / 'endpoints' / 'router.json').write_text(json.dumps({
    'description': 'Chain venue root.', 'upstream': 'https://example.com/docs',
    'core': 'root',
  }))
  (market_dir / 'router.json').write_text(json.dumps({
    'description': 'Market endpoints.', 'upstream': 'https://example.com/docs/market',
    'core': 'default',
  }))
  (time_dir / 'endpoint.json').write_text(json.dumps({
    'meta': {},
    'spec': {
      'kind': 'rpc', 'transports': ['http'], 'path': '/time', 'method': 'GET',
      'request': {'title': 'TimeRequest', 'type': 'object', 'properties': {}},
      'response': {
        'title': 'TimeResponse', 'type': 'object',
        'properties': {'time': {'type': 'integer', 'description': 'Server time.'}},
      },
    },
  }))
  (client_root / 'truewire.toml').write_text(
    '[python]\n'
    'package = "chainvenue"\n'
    'src = "pkg/src"\n'
    'name = "ChainVenue"\n'
    '\n'
    '[python.cores.root]\n'
    'base = "chainvenue.core:ClientBase"\n'
    '\n'
    '[python.cores.default]\n'
    'base = "chainvenue.core:RpcEndpoint"\n'
  )
  # No `chainvenue/__init__.py` on purpose -- a bare namespace package (no top-level
  # `from .main import ...` dependency) sidesteps the real bootstrap-ordering
  # requirement design §5a's own `_import_core_class` otherwise has (a client already
  # migrated once always has a real, if stale, `main.py` on disk before any
  # regeneration -- see `test_codegen_generator_e2e.py`'s own `generate_client` for how
  # that's reproduced for a client, like this one, starting from nothing).
  (output_root / 'core.py').write_text(
    'from dataclasses import dataclass\n'
    'from typing_extensions import Any\n'
    '\n'
    '\n'
    '@dataclass(kw_only=True)\n'
    'class ClientBase:\n'
    '  client: Any = None\n'
    '\n'
    '\n'
    '@dataclass(kw_only=True, frozen=True)\n'
    'class RpcEndpoint:\n'
    '  client: Any = None\n'
    '\n'
    '  async def request(self, request=None, *, method, path, validate=None, '
    'request_type=None, response_type=None, **meta):\n'
    '    return None\n'
  )
  monkeypatch.setattr(codegen_module, 'format_generated_files', lambda root, output_root, files, config: None)
  monkeypatch.setattr(codegen_module, 'typecheck_project', lambda project: None)

  codegen_module.generate('python', project=str(client_root), verbose=0)

  main_code = (output_root / 'main.py').read_text()
  assert 'class ChainVenue(ClientBase):' in main_code
  assert 'from chainvenue.core import ClientBase' in main_code
  assert 'def market(self) -> Market:' in main_code
  assert 'return Market(client=self.client)' in main_code
  assert not (output_root / '__init__.py').exists()  # never generated (design §4)

  market_code = (output_root / 'market' / '__init__.py').read_text()
  assert 'class Market(Time):' in market_code  # aggregate: one leaf, multiply-inherited
  time_code = (output_root / 'market' / 'time.py').read_text()
  assert 'class Time(RpcEndpoint):' in time_code
  assert 'from chainvenue.core import RpcEndpoint' in time_code

  manifest = resolve(client_root).manifest_path('python')
  owned = codegen_module.load_generated_manifest(manifest)
  assert Path('main.py') in owned
  assert Path('__init__.py') not in owned


def _write_chain_venue_client(tmp_path: Path) -> tuple[Path, Path, Path]:
  """Build the same minimal `codegen/config.toml`-driven client
  `test_codegen_generates_a_codegen_toml_client_end_to_end` uses, factored out so the
  schemas.json (design §5b, Task 24b) tests below can extend it with a root and a nested
  scope without duplicating the whole fixture inline.

  Returns `(root, client_root, output_root)`.
  """
  client_root = tmp_path / 'chain'
  root = client_root
  output_root = client_root / 'pkg' / 'src' / 'chainvenue'
  market_dir = client_root / 'spec' / 'endpoints' / 'market'
  time_dir = market_dir / 'time'
  time_dir.mkdir(parents=True)
  output_root.mkdir(parents=True)

  (client_root / 'spec' / 'endpoints' / 'router.json').write_text(json.dumps({
    'description': 'Chain venue root.', 'upstream': 'https://example.com/docs',
    'core': 'root',
  }))
  (market_dir / 'router.json').write_text(json.dumps({
    'description': 'Market endpoints.', 'upstream': 'https://example.com/docs/market',
    'core': 'default',
  }))
  (time_dir / 'endpoint.json').write_text(json.dumps({
    'meta': {},
    'spec': {
      'kind': 'rpc', 'transports': ['http'], 'path': '/time', 'method': 'GET',
      'request': {'title': 'TimeRequest', 'type': 'object', 'properties': {}},
      'response': {
        'title': 'TimeResponse', 'type': 'object',
        'properties': {
          'time': {'type': 'integer', 'description': 'Server time.'},
          'unit': {'$ref': 'TimeUnit'},
        },
        'required': ['time', 'unit'],
      },
    },
  }))
  (client_root / 'truewire.toml').write_text(
    '[python]\n'
    'package = "chainvenue"\n'
    'src = "pkg/src"\n'
    'name = "ChainVenue"\n'
    '\n'
    '[python.cores.root]\n'
    'base = "chainvenue.core:ClientBase"\n'
    '\n'
    '[python.cores.default]\n'
    'base = "chainvenue.core:RpcEndpoint"\n'
  )
  (output_root / 'core.py').write_text(
    'from dataclasses import dataclass\n'
    'from typing_extensions import Any\n'
    '\n'
    '\n'
    '@dataclass(kw_only=True)\n'
    'class ClientBase:\n'
    '  client: Any = None\n'
    '\n'
    '\n'
    '@dataclass(kw_only=True, frozen=True)\n'
    'class RpcEndpoint:\n'
    '  client: Any = None\n'
    '\n'
    '  async def request(self, request=None, *, method, path, validate=None, '
    'request_type=None, response_type=None, **meta):\n'
    '    return None\n'
  )
  (client_root / 'spec' / 'schemas.json').write_text(json.dumps({
    'TimeUnit': {'title': 'TimeUnit', 'type': 'string', 'enum': ['seconds', 'millis']},
  }))
  return root, client_root, output_root


def test_codegen_generates_root_and_nested_schemas_json_end_to_end(
  tmp_path: Path, monkeypatch,
):
  """A real CLI run (Task 24b step 6) through a client declaring both a root
  `spec/schemas.json` (`TimeUnit`, used by `market/time`) and a nested `spec/endpoints/
  market/schemas.json` (`MarketKind`, used by a new sibling `market/kind` endpoint) --
  proving `.schemas()` is called once per discovered file and each endpoint resolves only
  the scopes actually visible from its own directory (design §5b), through the *real*
  `codegen()` command, not just `test_codegen_generator_e2e.py`'s own pipeline mirror or
  the unit-level `Generator` tests."""
  root, client_root, output_root = _write_chain_venue_client(tmp_path)
  market_dir = client_root / 'spec' / 'endpoints' / 'market'
  kind_dir = market_dir / 'kind'
  kind_dir.mkdir(parents=True)
  (kind_dir / 'endpoint.json').write_text(json.dumps({
    'meta': {},
    'spec': {
      'kind': 'rpc', 'transports': ['http'], 'path': '/market/kind', 'method': 'GET',
      'request': {'title': 'KindRequest', 'type': 'object', 'properties': {}},
      'response': {
        'title': 'KindResponse', 'type': 'object',
        'properties': {'kind': {'$ref': 'MarketKind'}},
        'required': ['kind'],
      },
    },
  }))
  (market_dir / 'schemas.json').write_text(json.dumps({
    'MarketKind': {'title': 'MarketKind', 'type': 'string', 'enum': ['spot', 'futures']},
  }))
  monkeypatch.setattr(codegen_module, 'format_generated_files', lambda root, output_root, files, config: None)
  monkeypatch.setattr(codegen_module, 'typecheck_project', lambda project: None)

  codegen_module.generate('python', project=str(client_root), verbose=0)

  root_schemas_code = (output_root / 'schemas.py').read_text()
  assert "TimeUnit = Literal['seconds', 'millis']" in root_schemas_code

  market_schemas_code = (output_root / 'market' / 'schemas.py').read_text()
  assert "MarketKind = Literal['spot', 'futures']" in market_schemas_code

  # `market/time` (root scope only) sees the root-scope id.
  time_code = (output_root / 'market' / 'time.py').read_text()
  assert 'from chainvenue.schemas import TimeUnit' in time_code
  assert '  unit: TimeUnit' in time_code

  # `market/kind` (under the same `market/` nested scope) sees the nested-scope id.
  kind_code = (output_root / 'market' / 'kind.py').read_text()
  assert 'from chainvenue.market.schemas import MarketKind' in kind_code
  assert '  kind: MarketKind' in kind_code
  # The root scope's own id is never imported here -- `market/kind` never uses it.
  assert 'TimeUnit' not in kind_code


def test_codegen_raises_on_schemas_shadowing_collision(
  tmp_path: Path, monkeypatch, capsys,
):
  """Two `schemas.json` files on the same path to root declaring the same id -- design
  §5b's "refused as a collision" -- fails the real `codegen()` command (its own top-level
  `except CodegenError` turns every such failure into `typer.Exit(code=1)`, the same
  outcome every other `CodegenError` case in this file asserts, e.g.
  `test_delete_and_check_are_mutually_exclusive`) -- the backstop the CLI's own discovery
  loop keeps even though `check_schemas_no_shadowing` is meant to catch this earlier, at
  spec-test time."""
  root, client_root, output_root = _write_chain_venue_client(tmp_path)
  market_dir = client_root / 'spec' / 'endpoints' / 'market'
  # Same id (`TimeUnit`) the root `spec/schemas.json` already declares -- collides on the
  # client root's own `market/` subtree, which always sees the root scope too.
  (market_dir / 'schemas.json').write_text(json.dumps({
    'TimeUnit': {'title': 'TimeUnit', 'type': 'string', 'enum': ['ns']},
  }))
  monkeypatch.setattr(codegen_module, 'format_generated_files', lambda root, output_root, files, config: None)
  monkeypatch.setattr(codegen_module, 'typecheck_project', lambda project: None)

  with pytest.raises(codegen_module.typer.Exit) as raised:
    codegen_module.generate('python', project=str(client_root), verbose=0)

  assert raised.value.exit_code == 1
  assert 'TimeUnit' in capsys.readouterr().err


def test_codegen_allows_two_unrelated_sibling_scopes_to_share_a_schema_id(
  tmp_path: Path, monkeypatch,
):
  """Two `schemas.json` files at unrelated sibling scopes (`market/`, `futures/` -- neither
  an ancestor of the other) declaring the *same* id is not a collision at all (design §5b,
  `check_schemas_no_shadowing`): no single endpoint's ancestor walk ever sees both, so
  nothing downstream can confuse the two. Task 24b review finding 1: the CLI's own
  discovery-loop backstop used to reject this globally, with no path-to-root relationship
  test at all -- `check_schemas_no_shadowing` (which gets this right) reports the tree
  clean while real codegen failed it. Fixed by reusing `schemas_on_same_path_to_root`."""
  root, client_root, output_root = _write_chain_venue_client(tmp_path)
  market_dir = client_root / 'spec' / 'endpoints' / 'market'
  futures_dir = client_root / 'spec' / 'endpoints' / 'futures'
  futures_dir.mkdir(parents=True)
  (futures_dir / 'router.json').write_text(json.dumps({
    'description': 'Futures endpoints.', 'upstream': 'https://example.com/docs/futures',
    'core': 'default',
  }))
  (futures_dir / 'position' / 'endpoint.json').parent.mkdir(parents=True)
  (futures_dir / 'position' / 'endpoint.json').write_text(json.dumps({
    'meta': {},
    'spec': {
      'kind': 'rpc', 'transports': ['http'], 'path': '/futures/position', 'method': 'GET',
      'request': {'title': 'PositionRequest', 'type': 'object', 'properties': {}},
      'response': {
        'title': 'PositionResponse', 'type': 'object',
        'properties': {'side': {'$ref': 'Side'}}, 'required': ['side'],
      },
    },
  }))
  (futures_dir / 'schemas.json').write_text(json.dumps({
    'Side': {'title': 'Side', 'type': 'string', 'enum': ['long', 'short']},
  }))
  # `market/` declares the identical id `Side` -- a genuine collision only if some
  # endpoint's ancestor walk could see both, which none can here.
  (market_dir / 'schemas.json').write_text(json.dumps({
    'Side': {'title': 'Side', 'type': 'string', 'enum': ['buy', 'sell']},
  }))
  monkeypatch.setattr(codegen_module, 'format_generated_files', lambda root, output_root, files, config: None)
  monkeypatch.setattr(codegen_module, 'typecheck_project', lambda project: None)

  codegen_module.generate('python', project=str(client_root), verbose=0)

  futures_schemas_code = (output_root / 'futures' / 'schemas.py').read_text()
  assert "Side = Literal['long', 'short']" in futures_schemas_code
  market_schemas_code = (output_root / 'market' / 'schemas.py').read_text()
  assert "Side = Literal['buy', 'sell']" in market_schemas_code


def test_codegen_rejects_a_directory_that_is_both_a_leaf_and_a_router(
  tmp_path: Path, monkeypatch, capsys,
):
  """A directory that is both a leaf and a router node (`docs/spec/authoring.md` rule 16,
  `docs/production_standards.md` S30, confirmed real in 8 bit2me directories) is refused,
  not composed. This used to be `test_codegen_preserves_a_mixed_leaf_and_router_directory`
  (Task 24a's fix round): the CLI's real endpoint/router loops once redirected the node's
  own leaf output one level deeper (`wallet.py` -> `wallet/wallet.py`), named it
  `WalletEndpoint` to avoid colliding with the router class the same node also produces,
  and composed it as a base of the generated `Wallet` class via a synthetic self-child --
  all so the leaf survived rather than being silently shadowed by the `wallet/__init__.py`
  package Python resolves on import. Review of that mechanism found the self-child could
  only ever render as `__call__` (S29-forbidden), since a self-referential node's own
  parent can never qualify as an `aggregate_nodes` parent. Task 24c retires the whole
  mechanism in favor of an outright refusal: this test builds the identical mixed-shape
  fixture and proves the real CLI now raises `CodegenError`, naming the offending
  directory, instead of silently planning the two colliding files."""
  client_root = tmp_path / 'chain'
  root = client_root
  output_root = client_root / 'pkg' / 'src' / 'chainvenue'
  wallet_dir = client_root / 'spec' / 'endpoints' / 'wallet'
  # `history/` itself needs a further subdirectory (`list/`) so it is a genuine router
  # child of `wallet/` (subject to design §3's "compose as cached_property" rule) rather
  # than another bare leaf, which would just become a second multiply-inherited base
  # alongside `wallet`'s own self-leaf -- matching the existing fixture's `mixed_dir/
  # nested/` shape exactly.
  history_dir = wallet_dir / 'history' / 'list'
  history_dir.mkdir(parents=True)
  output_root.mkdir(parents=True)

  (client_root / 'spec' / 'endpoints' / 'router.json').write_text(json.dumps({
    'description': 'Chain venue root.', 'upstream': 'https://example.com/docs',
    'core': 'root',
  }))
  (wallet_dir / 'router.json').write_text(json.dumps({
    'description': 'Wallet endpoints.', 'upstream': 'https://example.com/docs/wallet',
    'core': 'default',
  }))
  wallet_endpoint = {
    'meta': {},
    'spec': {
      'kind': 'rpc', 'transports': ['http'], 'path': '/wallet', 'method': 'GET',
      'request': {'title': 'WalletRequest', 'type': 'object', 'properties': {}},
      'response': {
        'title': 'WalletResponse', 'type': 'object',
        'properties': {'balance': {'type': 'string', 'description': 'Wallet balance.'}},
      },
    },
  }
  (wallet_dir / 'endpoint.json').write_text(json.dumps(wallet_endpoint))
  (history_dir / 'endpoint.json').write_text(json.dumps({
    'meta': {},
    'spec': {
      'kind': 'rpc', 'transports': ['http'], 'path': '/wallet/history', 'method': 'GET',
      'request': {'title': 'WalletHistoryRequest', 'type': 'object', 'properties': {}},
      'response': {
        'title': 'WalletHistoryResponse', 'type': 'object',
        'properties': {'entries': {
          'type': 'array', 'items': {'type': 'string'}, 'description': 'History entries.',
        }},
      },
    },
  }))
  (client_root / 'truewire.toml').write_text(
    '[python]\n'
    'package = "chainvenue"\n'
    'src = "pkg/src"\n'
    'name = "ChainVenue"\n'
    '\n'
    '[python.cores.root]\n'
    'base = "chainvenue.core:ClientBase"\n'
    '\n'
    '[python.cores.default]\n'
    'base = "chainvenue.core:RpcEndpoint"\n'
  )
  (output_root / 'core.py').write_text(
    'from dataclasses import dataclass\n'
    'from typing_extensions import Any\n'
    '\n'
    '\n'
    '@dataclass(kw_only=True)\n'
    'class ClientBase:\n'
    '  client: Any = None\n'
    '\n'
    '\n'
    '@dataclass(kw_only=True, frozen=True)\n'
    'class RpcEndpoint:\n'
    '  client: Any = None\n'
    '\n'
    '  async def request(self, request=None, *, method, path, validate=None, '
    'request_type=None, response_type=None, **meta):\n'
    '    return None\n'
  )
  monkeypatch.setattr(codegen_module, 'format_generated_files', lambda root, output_root, files, config: None)
  monkeypatch.setattr(codegen_module, 'typecheck_project', lambda project: None)

  with pytest.raises(codegen_module.typer.Exit) as raised:
    codegen_module.generate('python', project=str(client_root), verbose=0)

  assert raised.value.exit_code == 1
  err = capsys.readouterr().err
  assert 'wallet' in err
  assert 'rule 16' in err

  # Neither file was ever written -- the raise fires before either is planned.
  assert not (output_root / 'wallet.py').exists()
  assert not (output_root / 'wallet').exists()
