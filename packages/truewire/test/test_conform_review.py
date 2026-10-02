"""Reproductions for the TRU-18 review of `truewire conform` (PR #6).

Every test here asserts what the PR and `docs/conform.md` promise, and failed on the PR
head (66fd6849). One still open is marked `xfail(strict=True)` with its ticket; the fix
removes the mark. Run: `.venv/bin/python -m pytest packages/truewire/test/test_conform_review.py -q`.
"""

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import threading
from types import SimpleNamespace

from jsonschema import Draft202012Validator
import pydantic
import pytest
from typer.testing import CliRunner
from typing_extensions import Annotated, Literal, Union

from truewire.cli import app
from truewire.conform.run import client_findings
from truewire.conform.schema import SchemaView
from truewire.conform.shape import schema_findings, shape_findings
from truewire_core.exceptions import ValidationError as ClientValidationError

from test_capture import quickstart_project
from test_conform import GET_PET, endpoint, findings, mutate, report, run_conform, served_copy


def view(schema):
  return SchemaView.root({**schema, '$defs': {}})


# -- 1. The drift/client split --------------------------------------------------------


def test_a_client_finding_does_not_resolve_on_a_night_the_client_was_not_judged(tmp_path: Path, monkeypatch):
  """run.py:300 drops client findings whenever the body also fails the schema, but the
  example still counts as exercised (report.py:49), so the open `client:python` finding is
  resolved, then reopened with a new first_seen the night the drift goes away."""
  project = quickstart_project(tmp_path, monkeypatch)
  schemas = project / 'src' / 'petstore' / 'schemas.py'
  schemas.write_text(schemas.read_text().replace(
    "  status: Literal['available', 'pending', 'sold']\n", "  status: Literal['available', 'pending']\n",
  ))
  served = served_copy(project, tmp_path)
  state = tmp_path / 'state'
  run_conform(project, served, state, '2026-10-01', '--only', 'pets.get_pet')
  [client] = findings(report(state, '2026-10-01'), 'pets.get_pet')
  assert client['kind'] == 'client:python'

  mutate(served, GET_PET, lambda r: r['payload'].pop('created_at'))
  run_conform(project, served, state, '2026-10-02', '--only', 'pets.get_pet')

  assert report(state, '2026-10-02')['resolved'] == []


# -- 2. Noise from the shape differ --------------------------------------------------


def test_switching_anyof_branch_is_not_a_removed_required_key():
  """schema.py:92 `required = required or ...`: one branch requiring `x` makes `x`
  required everywhere, though the docstring says "every alternative"."""
  schema = {'type': 'object', 'properties': {'result': {'anyOf': [
    {'type': 'object', 'properties': {'x': {'type': 'number'}}, 'required': ['x']},
    {'type': 'object', 'properties': {'y': {'type': 'number'}}, 'required': ['y']},
  ]}}}
  recorded, live = {'result': {'x': 1}}, {'result': {'y': 1}}
  assert list(Draft202012Validator(schema).iter_errors(live)) == []  # the schema accepts it

  assert shape_findings(recorded, live, view(schema)) == []


def test_a_key_required_by_a_nullable_object_stays_required():
  """The `null` branch of a nullable object cannot hold keys, so it does not make a
  required key optional (guard for the TRU-25 fix)."""
  schema = {'type': 'object', 'properties': {'result': {'anyOf': [
    {'type': 'object', 'properties': {'x': {'type': 'number'}, 'y': {'type': 'number'}}, 'required': ['x']},
    {'type': 'null'},
  ]}}}
  recorded, live = {'result': {'x': 1, 'y': 1}}, {'result': {'y': 1}}

  [finding] = shape_findings(recorded, live, view(schema))

  assert (finding.check, finding.pointer) == ('key_removed', '/result/x')
  assert finding.message.endswith('and the schema requires it')


def test_a_new_value_of_a_nullable_enum_is_an_enum_value_finding():
  """`anyOf: [{enum}, {type: null}]` is how Kraken writes a nullable enum (trade_volume's
  `asset_class`, deposit_methods' `limit`). jsonschema reports the `anyOf`, so the finding
  is `schema:anyOf` with expected `<array>`: the value and the allowed set are both lost,
  and the fingerprint no longer tells two new values apart."""
  schema = {'type': 'object', 'properties': {'asset_class': {'anyOf': [
    {'type': 'string', 'enum': ['currency']}, {'type': 'null'},
  ]}}}
  errors = Draft202012Validator(schema).iter_errors({'asset_class': 'forex'})

  [finding] = schema_findings(errors, view(schema))

  assert (finding.check, finding.pointer, finding.actual) == ('enum_value', '/asset_class', 'forex')
  assert finding.expected == ['currency']


ASSET_CLASS = {'type': 'object', 'properties': {'asset_class': {'anyOf': [
  {'type': 'string', 'enum': ['currency']}, {'type': 'null'},
]}}}


def anyof_findings(body, schema=ASSET_CLASS):
  return schema_findings(Draft202012Validator(schema).iter_errors(body), view(schema))


def test_two_new_values_of_one_anyof_enum_are_two_findings():
  [forex] = anyof_findings({'asset_class': 'forex'})
  [equity] = anyof_findings({'asset_class': 'equity'})

  assert forex.key() != equity.key()


def test_a_number_in_an_anyof_enum_is_a_type_change_not_an_enum_value():
  [finding] = anyof_findings({'asset_class': 3})

  assert (finding.check, finding.pointer, finding.actual) == ('type_changed', '/asset_class', ['number'])


def test_a_value_no_alternative_types_names_every_declared_type():
  schema = {'type': 'object', 'properties': {'limit': {'anyOf': [{'type': 'string'}, {'type': 'null'}]}}}

  [finding] = anyof_findings({'limit': 3}, schema)

  assert (finding.check, finding.expected, finding.actual) == ('type_changed', ['string', 'null'], ['number'])


def test_a_failure_inside_a_nullable_object_is_named_where_it_is():
  schema = {'type': 'object', 'properties': {'fee': {'anyOf': [
    {'type': 'object', 'properties': {'kind': {'enum': ['flat']}}, 'required': ['kind']}, {'type': 'null'},
  ]}}}

  [finding] = anyof_findings({'fee': {'kind': 'tiered'}}, schema)

  assert (finding.check, finding.pointer, finding.expected, finding.actual) == ('enum_value', '/fee/kind', ['flat'], 'tiered')


# -- 3. Response and request text in reports -----------------------------------------


class Level(pydantic.BaseModel):
  price: float


class Tagged(pydantic.BaseModel):
  kind: Literal['spot']


class Other(pydantic.BaseModel):
  kind: Literal['margin']


class Ticker(pydantic.BaseModel):
  result: dict[str, Level]
  account: Annotated[Union[Tagged, Other], pydantic.Field(discriminator='kind')]


TICKER = SchemaView.root(Ticker.model_json_schema())
"""The response schema the conform run passes: the client's own, for these tests."""


def client_error(body):
  try:
    Ticker.model_validate(body)
  except pydantic.ValidationError as exc:
    try:
      raise ClientValidationError(*exc.args) from exc
    except ClientValidationError as raised:
      return raised
  raise AssertionError('the body validated')


def test_a_client_finding_under_a_map_does_not_name_the_map_key():
  """run.py:330 turns only int `loc` parts into `*`. A map key (a Kraken pair, or on
  another API a user id or an e-mail) goes into the pointer, the report and the ledger,
  and a new fingerprint appears for every key the API lists."""
  raised = client_error({'result': {'acct-7731@example.com': {'price': 'n/a'}}, 'account': {'kind': 'spot'}})

  [finding] = client_findings(raised, SimpleNamespace(envelope=None), ClientValidationError, TICKER)

  assert finding.pointer == '/result/*/price'


def test_a_client_finding_message_does_not_quote_the_response():
  """run.py:338 keeps pydantic's `msg`; for `union_tag_invalid` that quotes the input."""
  raised = client_error({'result': {}, 'account': {'kind': 'acct-7731'}})

  [finding] = client_findings(raised, SimpleNamespace(envelope=None), ClientValidationError, TICKER)

  assert 'acct-7731' not in json.dumps([finding.message, finding.actual, finding.pointer])


def test_a_request_that_no_longer_binds_does_not_quote_the_recorded_request(tmp_path: Path, monkeypatch):
  """run.py:192 puts `str(exc)` of the binding failure in the report."""
  project = quickstart_project(tmp_path, monkeypatch)
  path = project / 'spec' / 'endpoints' / GET_PET / 'examples' / 'default.request.json'
  request = json.loads(path.read_text())
  request['request']['petId'] = {'token': 'tok_live_4eC39HqLyjWDarjtT1zdp7dc'}
  path.write_text(json.dumps(request))
  served = served_copy(project, tmp_path)
  state = tmp_path / 'state'

  run_conform(project, served, state, '2026-10-01', '--only', 'pets.get_pet')

  assert 'tok_live_' not in (state / 'petstore' / '2026-10-01.json').read_text()


def test_a_body_that_is_not_json_does_not_quote_a_header(tmp_path: Path, monkeypatch):
  """run.py:262 copies the Content-Type header into `actual`, report and ledger."""
  class Html(BaseHTTPRequestHandler):
    def do_GET(self):
      body = b'<html>maintenance</html>'
      self.send_response(200)
      self.send_header('Content-Type', 'text/html; edge=fra1-7f3a9c')
      self.send_header('Content-Length', str(len(body)))
      self.end_headers()
      self.wfile.write(body)

    def log_message(self, *args):
      pass

  server = HTTPServer(('127.0.0.1', 0), Html)
  threading.Thread(target=server.serve_forever, daemon=True).start()
  project = quickstart_project(tmp_path, monkeypatch)
  state = tmp_path / 'state'
  try:
    CliRunner().invoke(app, [
      'conform', '--project', str(project), '--state', str(state), '--date', '2026-10-01', '--interval', '0',
      '--only', 'pets.get_pet', '--new', f'base_url=http://127.0.0.1:{server.server_port}',
    ])
  finally:
    server.shutdown()

  assert endpoint(report(state, '2026-10-01'), 'pets.get_pet')['status'] == 'drift'
  assert 'fra1-7f3a9c' not in (state / 'petstore' / 'ledger.json').read_text()


# -- 4. The ledger's resolve rule and "not now" statuses -----------------------------


def test_a_body_finding_does_not_resolve_on_a_night_the_body_was_not_read(tmp_path: Path, monkeypatch):
  """A non-2xx with a recorded 200 is `drift` (run.py:253), hence `exercised`
  (report.py:49), though the body was never judged. Every open body finding of that
  example is resolved, and comes back the next night with a new first_seen."""
  project = quickstart_project(tmp_path, monkeypatch)
  served = served_copy(project, tmp_path)
  state = tmp_path / 'state'
  mutate(served, GET_PET, lambda r: r['payload'].update(nickname='Fi'))
  run_conform(project, served, state, '2026-10-01', '--only', 'pets.get_pet')

  mutate(served, GET_PET, lambda r: r.update(status=404, payload={'code': 404, 'message': 'x'}))
  run_conform(project, served, state, '2026-10-02', '--only', 'pets.get_pet')

  assert [r['check'] for r in report(state, '2026-10-02')['resolved']] == []


def test_a_403_is_not_drift_when_no_secret_is_missing(tmp_path: Path, monkeypatch):
  """GitHub answers an exhausted primary rate limit with 403 (not 429), and any API
  answers an expired key with 401. Neither project in examples/ declares
  `[secrets].required`, so no secret is ever missing, and both were `drift: status` with
  a first_seen date. They are `error`: "not now" or "not you", not "the API changed"."""
  project = quickstart_project(tmp_path, monkeypatch)
  served = served_copy(project, tmp_path)
  mutate(served, GET_PET, lambda r: r.update(status=403, payload={'message': 'API rate limit exceeded'}))
  state = tmp_path / 'state'

  run_conform(project, served, state, '2026-10-01', '--only', 'pets.get_pet')

  entry = endpoint(report(state, '2026-10-01'), 'pets.get_pet')
  assert entry['status'] == 'error'
  assert 'rate limit' not in json.dumps(entry)


def test_an_expired_key_is_not_drift(tmp_path: Path, monkeypatch):
  project = quickstart_project(tmp_path, monkeypatch)
  served = served_copy(project, tmp_path)
  mutate(served, GET_PET, lambda r: r.update(status=401, payload={'message': 'Bad credentials'}))
  state = tmp_path / 'state'

  run_conform(project, served, state, '2026-10-01', '--only', 'pets.get_pet')

  data = report(state, '2026-10-01')
  assert endpoint(data, 'pets.get_pet')['status'] == 'error'
  assert findings(data, 'pets.get_pet') == []


@pytest.mark.xfail(strict=True, reason='open: TRU-18 review, not yet ticketed')
def test_a_recorded_error_that_still_errors_is_ok(tmp_path: Path, monkeypatch):
  """run.py:250-255: a recorded 404 answered 404 is `error`, with the detail "no recorded
  status to compare" although it just compared equal."""
  project = quickstart_project(tmp_path, monkeypatch)
  gone = lambda r: r.update(status=404, payload={'code': 404, 'message': 'x'})
  mutate(project, GET_PET, gone)
  served = served_copy(project, tmp_path)
  state = tmp_path / 'state'

  run_conform(project, served, state, '2026-10-01', '--only', 'pets.get_pet')

  example = endpoint(report(state, '2026-10-01'), 'pets.get_pet')['examples'][0]
  assert 'no recorded status' not in example.get('detail', '')
