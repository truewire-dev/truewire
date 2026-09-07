"""
Exercise the rule 3 `timestamp-format` check against synthetic fixtures, never `clients/`.

The check fires on a field *name* that usually names a wire timestamp (`startTime`,
`createdAt`, a bare `datetime`) but declares no `epoch-*`/`date-time` format
(`docs/spec/authoring.md` rule 3). Before this check existed, deribit shipped 218/218
endpoints and kucoin ~117/310 with no declared format anywhere, invisible to `spec test`
(`docs/production_standards.md` S7) -- every one of those raw ints/strs was a silent rule 3
violation `spec test` reported as `0 errors`.

Like rule 2's `enum` heuristic, the name match is a lower bound, not a proof, so it reports
as a `warning` (`truewire.spec.authoring.WARNING_RULES`) rather than an `error`. The
half of this suite worth reading closely is the false-positive guard:
`test_a_time_in_force_field_does_not_warn` pins the exact case that motivated matching only
the *last* word of a split field name -- `timeInForce` is a documented closed set (rule 2),
never a timestamp, and it is real: every venue in this repo that has it names it that way.

Fixtures live entirely under `tmp_path`, per the isolation contract `test_mock.py`'s module
docstring records -- the shared suites must pass in a checkout with zero clients.
"""
import json
from pathlib import Path

from truewire.cli.check import check as spec_test


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
      'method': 'GET',
      'path': f'/{function}',
      'openapi': {
        'description': f'Get {function}.',
        'parameters': parameters,
        'responses': {'200': {'description': 'OK.'}},
      },
    },
  }
  (endpoint_dir / 'endpoint.json').write_text(json.dumps(endpoint))


def test_an_undeclared_start_time_warns_and_gate_stays_green(tmp_path, capsys):
  """A `startTime` with no format is the deribit/kucoin shape this check exists to catch."""
  write_endpoint(tmp_path, function='trades.get', parameters=[
    {'name': 'startTime', 'in': 'query', 'description': 'Start time.',
     'schema': {'type': 'integer'}},
  ])

  spec_test(path=str(tmp_path), verbose=False)

  captured = capsys.readouterr()
  assert 'Spec authoring:\n  pagination  0\n  violations 0\n  warnings   1' in captured.out
  assert 'Timestamp parameters carry their real wire format [warning]' in captured.out
  assert 'trades.get' in captured.err
  assert 'parameters[0].schema' in captured.err
  assert 'Result: OK' in captured.out


def test_a_declared_epoch_millis_format_is_clean(tmp_path, capsys):
  """The fix: declare the real wire format and the warning disappears."""
  write_endpoint(tmp_path, function='trades.get', parameters=[
    {'name': 'startTime', 'in': 'query', 'description': 'Start time.',
     'schema': {'type': 'integer', 'format': 'epoch-millis'}},
  ])

  spec_test(path=str(tmp_path), verbose=False)

  out = capsys.readouterr().out
  assert 'Spec authoring:\n  pagination  0\n  violations 0\n  warnings   0' in out
  assert 'Result: OK' in out


def test_a_declared_date_time_string_format_is_clean(tmp_path, capsys):
  """rule 3 accepts `date-time` on a string the same way it accepts `epoch-*` on an int."""
  write_endpoint(tmp_path, function='trades.get', parameters=[
    {'name': 'createdAt', 'in': 'query', 'description': 'Created at.',
     'schema': {'type': 'string', 'format': 'date-time'}},
  ])

  spec_test(path=str(tmp_path), verbose=False)

  out = capsys.readouterr().out
  assert 'Spec authoring:\n  pagination  0\n  violations 0\n  warnings   0' in out
  assert 'Result: OK' in out


def test_a_declared_epoch_nanos_format_is_clean(tmp_path, capsys):
  """rule 3 accepts `epoch-nanos` on an int the same way it accepts `epoch-millis` -- added
  after a deribit review found genuine epoch-nanosecond `starbase_timestamp`/
  `starbase_last_update_timestamp` fields the original vocabulary couldn't express."""
  write_endpoint(tmp_path, function='trades.get', parameters=[
    {'name': 'startTime', 'in': 'query', 'description': 'Start time.',
     'schema': {'type': 'integer', 'format': 'epoch-nanos'}},
  ])

  spec_test(path=str(tmp_path), verbose=False)

  out = capsys.readouterr().out
  assert 'Spec authoring:\n  pagination  0\n  violations 0\n  warnings   0' in out
  assert 'Result: OK' in out


def test_a_declared_date_string_format_is_clean(tmp_path, capsys):
  """rule 3 accepts `date` on a string for a plain calendar date with no time component --
  added after a deribit review found `market_data.get_delivery_prices.date`."""
  write_endpoint(tmp_path, function='delivery_prices.get', parameters=[
    {'name': 'date', 'in': 'query', 'description': 'Delivery date.',
     'schema': {'type': 'string', 'format': 'date'}},
  ])

  spec_test(path=str(tmp_path), verbose=False)

  out = capsys.readouterr().out
  assert 'Spec authoring:\n  pagination  0\n  violations 0\n  warnings   0' in out
  assert 'Result: OK' in out


def test_a_time_in_force_field_does_not_warn(tmp_path, capsys):
  """`timeInForce` splits to `time`/`in`/`force`; the last word is `force`, not `time` -- the
  false positive that would fire on this field, and every venue in this repo, if the check
  matched any word instead of only the last one."""
  write_endpoint(tmp_path, function='orders.place', parameters=[
    {'name': 'timeInForce', 'in': 'query', 'description': 'Time in force.',
     'schema': {'type': 'string', 'enum': ['GTC', 'IOC']}},
  ])

  spec_test(path=str(tmp_path), verbose=False)

  out = capsys.readouterr().out
  assert 'Spec authoring:\n  pagination  0\n  violations 0\n  warnings   0' in out
  assert 'Result: OK' in out


def test_a_response_property_is_checked_the_same_as_a_request_parameter(tmp_path, capsys):
  """Rule 3 applies identically on both sides of the wire -- this checks the response half."""
  endpoint_dir = tmp_path / 'spec' / 'endpoints' / 'http' / 'trades.get'
  endpoint_dir.mkdir(parents=True)
  endpoint = {
    'function': 'trades.get',
    'spec': {
      'kind': 'rpc', 'transports': ['http'], 'method': 'GET', 'path': '/trades.get',
      'openapi': {
        'description': 'Get trades.',
        'responses': {
          '200': {
            'description': 'The trade.',
            'content': {'application/json': {'schema': {
              'title': 'Trade', 'type': 'object', 'description': 'One trade.',
              'properties': {
                'blockTimestamp': {'type': 'string', 'description': 'Block timestamp.'},
              },
            }}},
          },
        },
      },
    },
  }
  (endpoint_dir / 'endpoint.json').write_text(json.dumps(endpoint))

  spec_test(path=str(tmp_path), verbose=False)

  captured = capsys.readouterr()
  assert 'Spec authoring:\n  pagination  0\n  violations 0\n  warnings   1' in captured.out
  assert 'properties.blockTimestamp' in captured.err
