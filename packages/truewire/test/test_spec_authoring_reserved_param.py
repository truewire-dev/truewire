"""
Exercise the `reserved-param` check against synthetic fixtures, never `clients/`.

`validate` is the keyword every generated `rpc` endpoint method reserves for the per-call
response-validation override (`docs/production_standards.md` S8). A wire parameter sharing
that name is silently shadowed rather than sent: kraken's own `AddOrder`/`EditOrder`/
`AddOrderBatch` reuse `validate` for a venue dry-run flag, and the flag was never threaded
to `authed_request`'s own `validate` at all -- `validate=True` on those endpoints meant
"don't submit the order," not "validate the response," the opposite of every other
generated method's contract.

Unlike `timestamp-format`, this is never a heuristic overshoot -- a parameter really is
named `validate`, or it is not. It still reports as a `warning` rather than an `error`,
because the fix is a codegen-side rename of the *generated* Python parameter, and no
backend in this repo has a declarative way to record that rename yet; the spec is right to
use the venue's own wire name. See `truewire.spec.authoring.WARNING_RULES`.

Fixtures live entirely under `tmp_path`, per the isolation contract `test_mock.py`'s module
docstring records -- the shared suites must pass in a checkout with zero clients.
"""
import json
from pathlib import Path

from truewire.cli.check import check as spec_test
from truewire.spec.authoring import request_parameters


def write_endpoint(root: Path, *, function: str, parameters: list[dict]) -> None:
  """
  Write one minimal HTTP `endpoint.json`, contract-clean except for the parameters given.

  Args:
    root: Client root; the endpoint lands under `spec/endpoints/http/<function>`.
    function: Endpoint's dotted function name, and its directory name.
    parameters: `spec.openapi.parameters` list, exactly as the test wants it.
  """
  endpoint_dir = root / 'spec' / 'endpoints' / 'http' / function
  endpoint_dir.mkdir(parents=True)
  endpoint = {
    'function': function,
    'spec': {
      'kind': 'rpc',
      'transports': ['http'],
      'method': 'POST',
      'path': f'/{function}',
      'openapi': {
        'description': f'{function}.',
        'parameters': parameters,
        'responses': {'200': {'description': 'OK.'}},
      },
    },
  }
  (endpoint_dir / 'endpoint.json').write_text(json.dumps(endpoint))


def test_a_validate_query_parameter_warns_and_gate_stays_green(tmp_path, capsys):
  """kraken's real shape: a wire dry-run flag literally named `validate`."""
  write_endpoint(tmp_path, function='orders.add', parameters=[
    {'name': 'validate', 'in': 'query', 'description': 'Validate inputs only; do not submit.',
     'schema': {'type': 'boolean'}},
  ])

  spec_test(path=str(tmp_path), verbose=False)

  captured = capsys.readouterr()
  assert 'Spec authoring:\n  pagination  0\n  violations 0\n  warnings   1' in captured.out
  assert (
    'production_standards.md S8 — a wire parameter must not collide with a name the '
    'generated method reserves for itself [warning]'
  ) in captured.out
  assert 'orders.add' in captured.err
  assert 'parameters.validate' in captured.err
  assert 'Result: OK' in captured.out


def test_a_validate_request_body_property_is_flagged_too(tmp_path, capsys):
  """The collision is checked on `requestBody` properties, not only query parameters."""
  endpoint_dir = tmp_path / 'spec' / 'endpoints' / 'http' / 'orders.add'
  endpoint_dir.mkdir(parents=True)
  endpoint = {
    'function': 'orders.add',
    'spec': {
      'kind': 'rpc', 'transports': ['http'], 'method': 'POST', 'path': '/orders.add',
      'openapi': {
        'description': 'Add an order.',
        'requestBody': {
          'description': 'Order to place.',
          'content': {'application/json': {'schema': {
            'title': 'AddOrderRequest', 'type': 'object', 'description': 'Order body.',
            'properties': {
              'validate': {
                'type': 'boolean', 'description': 'Validate inputs only; do not submit.',
              },
            },
          }}},
        },
        'responses': {'200': {'description': 'OK.'}},
      },
    },
  }
  (endpoint_dir / 'endpoint.json').write_text(json.dumps(endpoint))

  spec_test(path=str(tmp_path), verbose=False)

  captured = capsys.readouterr()
  assert 'warnings   1' in captured.out
  assert 'parameters.validate' in captured.err


def test_an_unrelated_parameter_name_does_not_warn(tmp_path, capsys):
  """Only an exact `validate` collision fires -- an unrelated boolean flag is not flagged."""
  write_endpoint(tmp_path, function='orders.add', parameters=[
    {'name': 'postOnly', 'in': 'query', 'description': 'Post-only order.',
     'schema': {'type': 'boolean'}},
  ])

  spec_test(path=str(tmp_path), verbose=False)

  out = capsys.readouterr().out
  assert 'Spec authoring:\n  pagination  0\n  violations 0\n  warnings   0' in out
  assert 'Result: OK' in out


def test_request_parameters_reads_new_shape():
  """`request_parameters` reads the new-shape `request` schema's `properties`, not just
  the legacy `parameters`/`requestBody` sources -- design §7."""
  operation = {'request': {'type': 'object', 'properties': {'validate': {'type': 'boolean'}}}}
  assert request_parameters(operation) == {'validate'}


def test_migrated_endpoint_finding_names_the_real_endpoint_not_none(tmp_path, capsys):
  """A fully mechanized endpoint (design §1: `function` dropped, derived from directory
  position) must still print its real dotted function path in a finding, not a bare
  `None`. `report_authoring` (`truewire.cli.test`) used to read `record.endpoint.function`
  directly -- always `None` once `function` is dropped -- so every warning/error on a
  migrated client's spec printed unattributably. kraken's own migration (Task 34) is the
  real, motivating case: 9 real warnings, all printing `None` before this fix.

  Mirrors `test_a_validate_query_parameter_warns_and_gate_stays_green` above, but with the
  new `request`/`response` shape and no `function` field at all, exercising
  `Endpoint.resolved_function`'s directory-derivation fallback instead of the legacy
  authored-`function` path that test already covers."""
  endpoint_dir = tmp_path / 'spec' / 'endpoints' / 'orders' / 'add'
  endpoint_dir.mkdir(parents=True)
  endpoint = {
    'meta': {},
    'spec': {
      'kind': 'rpc', 'transports': ['http'], 'method': 'POST', 'path': '/orders.add',
      'description': 'Add an order.',
      'request': {
        'title': 'AddOrderRequest', 'type': 'object',
        'properties': {
          'validate': {
            'type': 'boolean', 'description': 'Validate inputs only; do not submit.',
          },
        },
      },
      'response': {'title': 'AddOrderResponse', 'type': 'object', 'properties': {}},
    },
  }
  (endpoint_dir / 'endpoint.json').write_text(json.dumps(endpoint))

  spec_test(path=str(tmp_path), verbose=False)

  captured = capsys.readouterr()
  assert 'warnings   1' in captured.out
  # The real, directory-derived function name (`orders.add`) appears in the finding --
  # never a bare `None`.
  assert 'orders.add' in captured.err
  assert 'None  parameters.validate' not in captured.err
