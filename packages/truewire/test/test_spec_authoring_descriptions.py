"""
Exercise rule 6's `check_descriptions` against synthetic plain-JSON operations, never
`clients/`.

Every generic, `nodes()`-based check (`check_titles`, `check_enums`, ...) picks up a
new-shape `request`/`response` schema for free once `operation_json` synthesizes it
(`test_spec_authoring_request_shape.py`). `check_descriptions` is the one exception -- it
loops `operation['parameters']` (list) and `operation['requestBody']` (dict) directly
rather than going through `nodes()`, so rule 6 ("every parameter needs a `description`")
would silently stop being enforced on a migrated endpoint's `request` properties without a
dedicated branch reading that shape too.
"""
from truewire.spec.authoring import check_descriptions


def test_check_descriptions_flags_missing_request_property_description():
  """A new-shape `request` schema's property with no `description` is flagged, the same
  way a legacy `parameters[i]` entry with no `description` already is."""
  operation = {'description': 'Op.', 'request': {'type': 'object', 'properties': {
    'symbol': {'type': 'string'},  # no description -- rule 6 violation
  }}, 'responses': {}}
  violations = check_descriptions(operation)
  assert any(v['location'] == 'request.properties.symbol' for v in violations)


def test_check_descriptions_accepts_a_fully_described_request_schema():
  """A new-shape `request` schema whose every property carries a `description` is clean."""
  operation = {'description': 'Op.', 'request': {'type': 'object', 'properties': {
    'symbol': {'type': 'string', 'description': 'Trading pair.'},
  }}, 'responses': {}}
  violations = check_descriptions(operation)
  assert not any(v['location'].startswith('request.') for v in violations)


def test_check_descriptions_ignores_a_ref_request_property():
  """A `$ref` property carries its description at the referenced schema, so it is not
  required inline -- the same exemption `nodes()`-based checks already give a `$ref`."""
  operation = {'description': 'Op.', 'request': {'type': 'object', 'properties': {
    'symbol': {'$ref': '#/components/schemas/Symbol'},
  }}, 'responses': {}}
  violations = check_descriptions(operation)
  assert not any(v['location'].startswith('request.') for v in violations)


def test_check_descriptions_legacy_shape_unaffected():
  """A legacy `spec.openapi` operation with no `request` key at all is checked exactly as
  before -- `parameters`/`requestBody`, never the new `request` branch."""
  operation = {
    'description': 'Op.',
    'parameters': [{'name': 'symbol', 'in': 'query', 'schema': {'type': 'string'}}],
    'responses': {'200': {'description': 'OK.'}},
  }
  violations = check_descriptions(operation)
  assert any(v['location'] == 'parameters[0]' for v in violations)
  assert not any(v['location'].startswith('request.') for v in violations)
