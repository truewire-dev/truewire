"""
Exercise the rule 2 error/warning split against synthetic fixtures, never `clients/`.

Rule 2's `enum` check (`docs/spec/authoring.md` rule 2) fires on field *names*
that usually denote a closed set — `type`, `state`, `side`, `status` — not on ones the
venue actually documents as closed. Before this suite the overshoot failed gate 1
outright, so a spec that correctly left an undocumented `state` bare had no way to reach
a green gate short of inventing an enum (T101). `enum` violations now report as
`warning`s (`truewire.spec.authoring.WARNING_RULES`) that ask the author to check rather
than errors that fail the build, while every other rule stays an `error`. Fixtures live
entirely under `tmp_path`, per the isolation contract `common/lib/test/test_mock.py`'s
module docstring records — the shared suites must pass in a checkout with zero clients.
"""
import json
from pathlib import Path

import pytest
import typer

from truewire.cli.check import check as spec_test

BASE_PROPERTIES = {
  'name': {'type': 'string', 'description': 'Widget name.'},
}
"""Response object properties every fixture endpoint carries beside whatever a test adds."""


def write_endpoint(
  root: Path, *, function: str, properties: dict, titled: bool = True,
) -> None:
  """
  Write one minimal HTTP `endpoint.json`, contract-clean except for what the caller varies.

  Args:
    root: Client root; the endpoint lands under `spec/endpoints/http/<function>`.
    function: Endpoint's dotted function name, and its directory name.
    properties: Response object properties, merged over `BASE_PROPERTIES`'s `name`.
    titled: Whether the response object schema carries a `title`. `False` produces a
      genuine rule 1 violation, for the test that pins errors still fail the gate.
  """
  endpoint_dir = root / 'spec' / 'endpoints' / 'http' / function
  endpoint_dir.mkdir(parents=True)
  schema: dict = {
    'type': 'object',
    'description': 'One widget.',
    'properties': dict(BASE_PROPERTIES, **properties),
  }
  if titled:
    schema['title'] = 'Widget'
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
            'description': 'The widget.',
            'content': {'application/json': {'schema': schema}},
          },
        },
      },
    },
  }
  (endpoint_dir / 'endpoint.json').write_text(json.dumps(endpoint))


def test_undocumented_state_field_warns_and_gate_stays_green(tmp_path, capsys):
  """A `state` field with no `enum` is reported by default but does not fail."""
  write_endpoint(
    tmp_path, function='widgets.get',
    properties={'state': {'type': 'string', 'description': 'Widget state.'}},
  )

  spec_test(path=str(tmp_path), verbose=False)

  captured = capsys.readouterr()
  out = captured.out
  assert 'Spec authoring:\n  pagination  0\n  violations 0\n  warnings   1' in out
  assert 'Closed sets use `enum` [warning]' in out
  assert 'widgets.get' in captured.err
  assert 'properties.state' in captured.err
  assert 'Result: OK' in out


def test_verbose_warning_output_expands_the_default_sample(tmp_path, capsys):
  """Default output samples warning locations; `--verbose` exposes all of them."""
  for index in range(6):
    write_endpoint(
      tmp_path, function=f'widgets.get_{index}',
      properties={'state': {'type': 'string', 'description': 'Widget state.'}},
    )

  spec_test(path=str(tmp_path), verbose=False)

  captured = capsys.readouterr()
  assert 'widgets.get_4' in captured.err
  assert 'widgets.get_5' not in captured.err
  assert '... 1 more, rerun with --verbose' in captured.err

  spec_test(path=str(tmp_path), verbose=True)

  captured = capsys.readouterr()
  assert 'widgets.get_5' in captured.err
  assert 'more, rerun with --verbose' not in captured.err


def test_state_field_declaring_enum_is_neither_error_nor_warning(tmp_path, capsys):
  """A `state` that names its values plainly satisfies rule 2 outright."""
  write_endpoint(
    tmp_path, function='widgets.get',
    properties={
      'state': {
        'type': 'string', 'description': 'Widget state.', 'enum': ['open', 'closed'],
      },
    },
  )

  spec_test(path=str(tmp_path), verbose=False)

  out = capsys.readouterr().out
  assert 'Spec authoring:\n  pagination  0\n  violations 0\n  warnings   0' in out
  assert 'Result: OK' in out


def test_genuine_violation_still_errors_and_fails_the_gate(tmp_path, capsys):
  """A real contract breach — an untitled object schema, rule 1 — still errors and exits 1."""
  write_endpoint(tmp_path, function='widgets.get', properties={}, titled=False)

  with pytest.raises(typer.Exit) as exc_info:
    spec_test(path=str(tmp_path), verbose=False)

  assert exc_info.value.exit_code == 1
  out = capsys.readouterr().out
  assert 'violations 1\n  warnings   0' in out
  assert 'Result: FAILED' in out


def test_summary_line_separates_errors_from_warnings_when_both_fire(tmp_path, capsys):
  """An endpoint carrying both a genuine violation and a rule 2 warning reports both counts,
  and the gate still fails on the error alone."""
  write_endpoint(
    tmp_path, function='widgets.get', titled=False,
    properties={'state': {'type': 'string', 'description': 'Widget state.'}},
  )

  with pytest.raises(typer.Exit) as exc_info:
    spec_test(path=str(tmp_path), verbose=False)

  assert exc_info.value.exit_code == 1
  out = capsys.readouterr().out
  assert 'violations 1\n  warnings   1' in out
  assert 'Result: FAILED' in out
