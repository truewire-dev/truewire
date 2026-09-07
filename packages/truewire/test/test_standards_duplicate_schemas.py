"""
Exercise `truewire.standards.duplicate_schemas.check_duplicate_schemas` (S6, the
"unreduced duplicate of a shared shape" half) against synthetic fixtures, never `clients/`
-- per the isolation contract `common/lib/test/test_mock.py`'s module docstring records,
these must pass in a checkout with zero clients.
"""
import json
from pathlib import Path

from truewire.standards.duplicate_schemas import check_duplicate_schemas


def write_endpoint(root: Path, *, function: str, response_schema: dict) -> None:
  """
  Write one minimal HTTP `endpoint.json` whose 200 response is exactly `response_schema`.

  Args:
    root: Client root; the endpoint lands under `spec/endpoints/http/<function>`.
    function: Endpoint's dotted function name, and its directory name.
    response_schema: Response schema to test, varied by the caller.
  """
  endpoint_dir = root / 'spec' / 'endpoints' / 'http' / function
  endpoint_dir.mkdir(parents=True)
  endpoint = {
    'function': function,
    'spec': {
      'kind': 'rpc',
      'transports': ['http'],
      'method': 'GET',
      'path': f'/{function}',
      'openapi': {
        'description': f'Get {function}.',
        'responses': {
          '200': {
            'description': 'Response.',
            'content': {'application/json': {'schema': response_schema}},
          },
        },
      },
    },
  }
  (endpoint_dir / 'endpoint.json').write_text(json.dumps(endpoint))


def money_schema(title: str) -> dict:
  """A `Money`-shaped inline schema, individually titled -- coinbase's real worked case."""
  return {
    'title': title,
    'type': 'object',
    'description': 'An amount of a currency.',
    'properties': {
      'amount': {'type': 'string', 'description': 'Numeric amount.'},
      'currency': {'type': 'string', 'description': 'Currency code.'},
    },
  }


def test_two_differently_titled_copies_of_the_same_shape_produce_one_finding(tmp_path):
  """Coinbase's real case: same property set, individually titled, never `$ref`'d."""
  write_endpoint(tmp_path, function='orders.create', response_schema=money_schema('AmountCreate'))
  write_endpoint(tmp_path, function='orders.cancel', response_schema=money_schema('AmountCancel'))

  findings = check_duplicate_schemas(tmp_path)

  assert len(findings) == 1
  finding = findings[0]
  assert finding['rule'] == 'S6'
  assert finding['severity'] == 'warning'
  assert '2 occurrences' in finding['location']
  assert 'orders.create' in finding['location']
  assert 'orders.cancel' in finding['location']
  assert "'amount'" in finding['message'] and "'currency'" in finding['message']


def test_a_shape_that_appears_once_produces_no_finding(tmp_path):
  """A single inline occurrence has nothing to collide with."""
  write_endpoint(tmp_path, function='orders.create', response_schema=money_schema('Amount'))

  assert check_duplicate_schemas(tmp_path) == []


def test_shared_ref_to_the_same_schemas_json_entry_produces_no_finding(tmp_path):
  """Two endpoints `$ref`-ing the same shared schema are doing exactly the right thing."""
  ref_schema = {'$ref': '#/$defs/Money'}
  write_endpoint(tmp_path, function='orders.create', response_schema=ref_schema)
  write_endpoint(tmp_path, function='orders.cancel', response_schema=ref_schema)

  assert check_duplicate_schemas(tmp_path) == []


def test_single_property_signature_stays_below_threshold(tmp_path):
  """A one-property schema carries no real structural signature -- never flagged."""
  def id_schema(title: str) -> dict:
    return {
      'title': title, 'type': 'object', 'description': 'Bare id wrapper.',
      'properties': {'id': {'type': 'string', 'description': 'Identifier.'}},
    }

  write_endpoint(tmp_path, function='orders.create', response_schema=id_schema('CreateId'))
  write_endpoint(tmp_path, function='orders.cancel', response_schema=id_schema('CancelId'))

  assert check_duplicate_schemas(tmp_path) == []
