"""
Exercise the `title-empty-object` half of rule 1 (`docs/spec/authoring.md` rule 1,
`docs/production_standards.md` S6) against synthetic fixtures, never `clients/`.

`check_titles` always required a `title` on a schema with a non-empty `properties` map.
binance's `ping` response (`{"type": "object", "properties": {}}`) sits outside that: zero
properties, so the old check skipped it outright, and codegen invented a placeholder name
(`Response200`) for it. The widened half reports this as its own `title-empty-object` rule,
`warning`-severity while the fleet migrates — every currently-clean title-less empty-object
schema across all sixteen clients would otherwise flip to a violation the moment this
landed. Fixtures live entirely under `tmp_path`, per the isolation contract
`common/lib/test/test_mock.py`'s module docstring records.
"""
import json
from pathlib import Path

import pytest
import typer

from truewire.cli.check import check as spec_test


def write_ping_endpoint(root: Path, *, response_schema: dict) -> None:
  """
  Write one minimal HTTP `endpoint.json` whose 200 response is exactly `response_schema`.

  Args:
    root: Client root; the endpoint lands under `spec/endpoints/http/ping`.
    response_schema: Response object schema to test, varied by the caller.
  """
  endpoint_dir = root / 'spec' / 'endpoints' / 'http' / 'ping'
  endpoint_dir.mkdir(parents=True)
  endpoint = {
    'function': 'ping',
    'spec': {
      'kind': 'rpc',
      'transports': ['http'],
      'method': 'GET',
      'path': '/ping',
      'openapi': {
        'description': 'Test connectivity.',
        'responses': {
          '200': {
            'description': 'Connectivity confirmed.',
            'content': {'application/json': {'schema': response_schema}},
          },
        },
      },
    },
  }
  (endpoint_dir / 'endpoint.json').write_text(json.dumps(endpoint))


def test_untitled_empty_object_response_warns_and_gate_stays_green(tmp_path, capsys):
  """binance `ping`'s exact shape: `type: object`, `properties: {}`, no title."""
  write_ping_endpoint(
    tmp_path,
    response_schema={
      'type': 'object', 'description': 'Always empty on success.', 'properties': {},
    },
  )

  spec_test(path=str(tmp_path), verbose=False)

  captured = capsys.readouterr()
  assert 'Spec authoring:\n  pagination  0\n  violations 0\n  warnings   1' in captured.out
  assert 'Title every object schema — including an empty one' in captured.out
  assert 'ping' in captured.err
  assert 'Result: OK' in captured.out


def test_untitled_object_with_no_properties_key_also_warns(tmp_path, capsys):
  """`type: object` with no `properties` key at all is the same gap, not a different one."""
  write_ping_endpoint(
    tmp_path, response_schema={'type': 'object', 'description': 'Always empty on success.'},
  )

  spec_test(path=str(tmp_path), verbose=False)

  out = capsys.readouterr().out
  assert 'warnings   1' in out
  assert 'Result: OK' in out


def test_titled_empty_object_is_clean(tmp_path, capsys):
  """A title on the empty-object schema satisfies rule 1 outright."""
  write_ping_endpoint(
    tmp_path,
    response_schema={
      'title': 'PingResponse', 'type': 'object', 'description': 'Always empty on success.',
      'properties': {},
    },
  )

  spec_test(path=str(tmp_path), verbose=False)

  out = capsys.readouterr().out
  assert 'Spec authoring:\n  pagination  0\n  violations 0\n  warnings   0' in out
  assert 'Result: OK' in out


def test_map_schema_with_additional_properties_is_exempt(tmp_path, capsys):
  """A map (`additionalProperties`, no `properties`) is not a record and needs no title."""
  write_ping_endpoint(
    tmp_path,
    response_schema={
      'type': 'object', 'description': 'Arbitrary key/value map.',
      'additionalProperties': {'type': 'string'},
    },
  )

  spec_test(path=str(tmp_path), verbose=False)

  out = capsys.readouterr().out
  assert 'Spec authoring:\n  pagination  0\n  violations 0\n  warnings   0' in out
  assert 'Result: OK' in out
