"""
Exercise `typed-dev spec surface` — the reconciliation of in-scope specs against the
callables a caller can actually reach — against synthetic trees, never a real client.

The defect it closes: mexc's backend excludes every WebSocket endpoint from generation by
design, and seven new WebSocket specs passed gate 1 through gate 7 with no method anywhere
in the package. Every gate measured something real and none of them measured *that*, so the
skip was silent and silence read as success.

The subtlety that would break a naive version, and the reason half of this suite exists: an
**unverified** endpoint — no `examples/` at all, because it is effectful or the credentials
are out of reach — still generates a method. What is missing there is evidence, not code. A
check that treated "no examples" as "no callable" would fail all 41 of bitget's unverified
endpoints, and a check that fails correct work is a check somebody switches off. So
`test_an_unverified_endpoint_passes` pins the distinction directly: same tree, same method,
`examples/` deliberately absent, and it passes.

Every tree here is built under `tmp_path` — nothing reads `clients/`, because the shared
suites run inside venue worktrees where all but one client is exactly what is supposed to be
missing. The synthetic backend is a plain object rather than a real `Generator`: `reconcile`
reads it through the same `getattr` hooks `typed-dev codegen` does, so the smallest thing
answering them is the honest fixture.
"""

import json
from pathlib import Path

import pytest
import typer
from pydantic import ValidationError

from truewire.cli.surface import surface
from truewire.spec import Endpoint
from truewire.surface import reconcile

HTTP_ENDPOINT = {
  'function': 'widgets.get',
  'spec': {
    'kind': 'rpc',
    'transports': ['http'],
    'method': 'GET',
    'path': '/widgets/{id}',
    'openapi': {
      'description': 'Get one widget by id.',
      'parameters': [
        {'name': 'id', 'in': 'path', 'required': True, 'description': 'Widget id.',
         'schema': {'type': 'string'}},
      ],
      'responses': {'200': {'description': 'The widget.'}},
    },
  },
}
"""Minimal, valid HTTP `endpoint.json` body. Generates `api/widgets/<leaf>.py`."""

WS_ENDPOINT = {
  'function': 'widgets.feed',
  'spec': {
    'kind': 'rpc',
    'transports': ['ws'],
    'path': 'widget_feed',
    'openapi': {
      'description': 'Synthetic widget stream.',
      'responses': {'reply': {'description': 'Subscription acknowledgement.'}},
    },
  },
}
"""Minimal, valid WebSocket `endpoint.json` body — the shape the fixture backend refuses."""

BACKEND = '''
class Backend:
  """The smallest backend `reconcile` can resolve a layout from."""
  core_package = ''

  def skip_endpoint(self, endpoint):
    """Emit HTTP and nothing else, which is mexc's rule and the one that hid T107."""
    return 'http' not in endpoint.transports

  def schemas(self, schemas):
    """Record nothing; a backend that filters endpoints is handed the shared schemas."""
    return {'files': [], 'references': {}}

  def method_name(self, endpoint, *, parent, child_name, is_aggregate_parent):
    """Name every leaf after itself, so a fixture's method name is readable."""
    return child_name

generator = Backend()
'''
"""Fixture codegen backend, written to `<client>/codegen/python.py` and exec'd like a real one."""


def write_client(root: Path, *, package: str = 'widgets') -> Path:
  """Create the skeleton of a client tree: a backend, an empty package, no endpoints yet."""
  (root / 'backend.py').write_text(BACKEND)
  (root / 'truewire.toml').write_text(
    f'[python]\npackage = "{package}"\nsrc = "pkg/src"\nbackend = "backend.py"\n'
    '[python.cores.default]\nbase = "x.core:Endpoint"\n'
  )
  package_root = root / 'pkg' / 'src' / package
  package_root.mkdir(parents=True, exist_ok=True)
  (root / 'spec' / 'endpoints').mkdir(parents=True, exist_ok=True)
  return package_root


def write_endpoint(root: Path, name: str, body: dict, **overrides) -> Path:
  """Write one `endpoint.json` under its own subdivision, and return its directory."""
  directory = root / 'spec' / 'endpoints' / name
  directory.mkdir(parents=True, exist_ok=True)
  (directory / 'endpoint.json').write_text(json.dumps({**body, **overrides}))
  return directory


def write_method(package: Path, module: str, method: str, *, validate: bool = True) -> None:
  """Write a module defining one method, standing in for generated or hand-written code.

  Args:
    package: The client's inner package directory.
    module: Dotted module path the method is written under.
    method: Name of the method to define.
    validate: Whether the signature accepts `validate` — every real generated `rpc`
      endpoint method does (`docs/production_standards.md` S8), so this defaults to `True`
      and a test exercising the S8 gap itself opts out explicitly.
  """
  path = package.joinpath(*module.split('.')).with_suffix('.py')
  path.parent.mkdir(parents=True, exist_ok=True)
  params = 'self, *, validate: bool | None = None' if validate else 'self'
  path.write_text(f'class Thing:\n  async def {method}({params}):\n    ...\n')


@pytest.fixture
def client(tmp_path: Path) -> Path:
  """A client whose one HTTP spec generates the method it is supposed to generate."""
  package = write_client(tmp_path)
  write_endpoint(tmp_path, 'get', HTTP_ENDPOINT)
  write_method(package, 'widgets.get', 'get')
  return tmp_path


def test_a_spec_that_generates_a_callable_passes(client: Path):
  """The ordinary case: the backend emits the module and the module defines the method."""
  result = reconcile(client)
  assert result.gaps == []
  assert result.generated == ['widgets.get']


def test_a_generated_rpc_method_missing_validate_is_flagged(client: Path):
  """S8: every generated `rpc` endpoint method must accept `validate`. bitget (515/515) and
  coinbase (85/85) both failed this silently before this check existed."""
  write_method(client / 'pkg' / 'src' / 'widgets', 'widgets.get', 'get', validate=False)
  result = reconcile(client)
  assert result.gaps == []
  assert result.generated == ['widgets.get']
  assert result.missing_validate == ['widgets.get']


def test_a_generated_rpc_method_accepting_validate_passes(client: Path):
  """The ordinary, conformant case: `validate` is part of the resolved signature."""
  result = reconcile(client)
  assert result.missing_validate == []


def test_the_summary_names_generated_methods_missing_validate(client: Path, capsys):
  """The CLI reports the S8 gap by name and fails the gate, distinctly from a surface gap."""
  write_method(client / 'pkg' / 'src' / 'widgets', 'widgets.get', 'get', validate=False)
  with pytest.raises(typer.Exit) as exit:
    surface(verbose=False, path=str(client), language='python')
  assert exit.value.exit_code == 1
  out = capsys.readouterr().out
  assert 'widgets.get' in out
  assert 'no_validate_param' in out
  assert '1 generated method(s) accept no `validate` parameter' in out


def test_a_spec_that_generates_nothing_fails(client: Path):
  """A WebSocket spec the backend refuses, with nothing recording where a caller reaches it."""
  write_endpoint(client, 'feed', WS_ENDPOINT)
  result = reconcile(client)
  assert [(gap.function, gap.fault) for gap in result.gaps] == [('widgets.feed', 'undeclared')]
  assert result.generated == ['widgets.get']


def test_a_generated_module_missing_its_method_fails(client: Path):
  """A module regenerated out from under its spec is a gap too, not only an absent file."""
  write_endpoint(client, 'list', HTTP_ENDPOINT, function='widgets.list')
  write_method(client / 'pkg' / 'src' / 'widgets', 'widgets.list', 'something_else')
  result = reconcile(client)
  assert [(gap.function, gap.fault) for gap in result.gaps] == [('widgets.list', 'no_method')]
  assert "defines no 'list'" in result.gaps[0].detail


def test_a_spec_whose_module_was_never_generated_fails(client: Path):
  """A spec written but never regenerated reaches nobody, and says which module is missing."""
  write_endpoint(client, 'list', HTTP_ENDPOINT, function='widgets.list')
  result = reconcile(client)
  assert [(gap.function, gap.fault) for gap in result.gaps] == [('widgets.list', 'no_module')]
  assert 'widgets/list.py' in result.gaps[0].detail


def test_an_unverified_endpoint_passes(client: Path):
  """Spec present, method present, `examples/` absent — verification's problem, not this one.

  This is the distinction the whole check turns on. bitget carries 41 endpoints in exactly
  this state, every one of them generating a method, and a check that conflated the two
  would fail all of them and be turned off within the week.
  """
  assert not (client / 'spec' / 'endpoints' / 'get' / 'examples').exists()
  result = reconcile(client)
  assert result.gaps == []
  assert result.generated == ['widgets.get']


def test_a_declared_unemittable_endpoint_passes(client: Path):
  """A backend that cannot emit an endpoint says so in the spec, and the spec is believed."""
  write_endpoint(client, 'feed', WS_ENDPOINT, surface={
    'kind': 'absent',
    'reason': 'The venue pushes protobuf this backend cannot type yet.',
  })
  result = reconcile(client)
  assert result.gaps == []
  assert result.absent == ['widgets.feed']


def test_a_declared_handwritten_endpoint_passes_when_the_method_is_there(client: Path):
  """A hand-written callable is accepted once the spec names it and the name resolves."""
  package = client / 'pkg' / 'src' / 'widgets'
  write_method(package, 'streams.feed', 'subscribe_feed')
  write_endpoint(client, 'feed', WS_ENDPOINT, surface={
    'kind': 'handwritten',
    'symbol': 'streams.feed:subscribe_feed',
    'reason': 'Written by hand against the socket core.',
  })
  result = reconcile(client)
  assert result.gaps == []
  assert result.handwritten == ['widgets.feed']


def test_a_declared_handwritten_endpoint_fails_when_the_method_is_renamed(client: Path):
  """The record is falsifiable, which is the entire reason it names a symbol and not a place."""
  write_method(client / 'pkg' / 'src' / 'widgets', 'streams.feed', 'feed')
  write_endpoint(client, 'feed', WS_ENDPOINT, surface={
    'kind': 'handwritten',
    'symbol': 'streams.feed:subscribe_feed',
    'reason': 'Written by hand against the socket core.',
  })
  result = reconcile(client)
  assert [(gap.function, gap.fault) for gap in result.gaps] == [('widgets.feed', 'no_symbol')]


def test_a_declaration_left_behind_after_the_backend_learned_to_emit_fails(client: Path):
  """`absent` is checked against the backend too, so a stale exclusion cannot sit unnoticed."""
  write_endpoint(client, 'list', HTTP_ENDPOINT, function='widgets.list', surface={
    'kind': 'absent', 'reason': 'stale',
  })
  result = reconcile(client)
  assert [(gap.function, gap.fault) for gap in result.gaps] == [('widgets.list', 'stale')]


def test_a_symbol_that_names_no_method_is_refused_by_the_spec(client: Path):
  """A symbol nothing can be looked up in is prose, and prose is what the check replaces."""
  with pytest.raises(ValidationError):
    Endpoint.model_validate({
      **WS_ENDPOINT,
      'surface': {'kind': 'handwritten', 'symbol': 'streams.feed', 'reason': 'over there'},
    })


def test_the_summary_names_the_specs_that_generated_nothing(client: Path, capsys):
  """A count is how the skip stayed invisible; the report names every spec behind it."""
  write_endpoint(client, 'feed', WS_ENDPOINT)
  write_endpoint(client, 'ticks', WS_ENDPOINT, function='widgets.ticks')
  with pytest.raises(typer.Exit) as exit:
    surface(verbose=False, path=str(client), language='python')
  assert exit.value.exit_code == 1
  out = capsys.readouterr().out
  assert 'widgets.feed' in out
  assert 'widgets.ticks' in out
  assert '2 spec(s) generate nothing a caller can call (2 undeclared)' in out
  assert 'Callables: 1 generated, 0 hand-written, 0 declared absent, over 3 spec(s)' in out


def test_a_clean_client_exits_zero_and_says_what_it_counted(client: Path, capsys):
  """A passing report still states its numbers, so nobody has to trust a bare OK."""
  surface(verbose=False, path=str(client), language='python')
  out = capsys.readouterr().out
  assert 'Callables: 1 generated, 0 hand-written, 0 declared absent, over 1 spec(s)' in out


def test_a_scope_holding_no_specs_fails_rather_than_reporting_success(tmp_path: Path):
  """The gate that reports OK over nothing is the defect; it must not be this one's pass."""
  write_client(tmp_path)
  with pytest.raises(typer.Exit) as exit:
    surface(verbose=False, path=str(tmp_path), language='python')
  assert exit.value.exit_code == 1
