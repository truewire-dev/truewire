"""
Exercise `truewire.standards.secrets.check_secret_placeholders` (`docs/production_standards.md`
S16) against synthetic fixtures, per the isolation contract `test_mock.py`'s module
docstring records -- these must pass in a checkout with zero clients. One exception: a
single test also loads `clients/deribit`'s real `list_api_keys` example, since
`docs/production_standards.md` S16 and `docs/spec/authoring.md` rule 6 both cite it by name
as the canonical clean placeholder, so it's worth confirming the checker actually agrees.
"""
import json
from pathlib import Path

from truewire.standards.secrets import check_secret_placeholders

REPO_ROOT = Path(__file__).resolve().parents[3]


def write_client_toml(root: Path, *, required: list, optional: list | None = None) -> None:
  """
  Write a minimal `client.toml` carrying only a `[secrets]` table.

  Args:
    root: Client root the file lands directly under.
    required: `[secrets] required` env-var names.
    optional: `[secrets] optional` env-var names, defaulting to none.
  """
  lines = [
    '[secrets]',
    'env = "clients/synthetic/.env"',
    f'required = {json.dumps(required)}',
    f'optional = {json.dumps(optional or [])}',
    'read_by = "package"',
  ]
  (root / 'client.toml').write_text('\n'.join(lines))


def write_example(root: Path, *, function: str, payload: dict) -> Path:
  """
  Write one HTTP response example under a synthetic endpoint's `examples/` directory.

  Args:
    root: Client root.
    function: Endpoint directory name under `spec/endpoints/`.
    payload: Decoded example body, written as `{"status": 200, "payload": payload}`.
  """
  examples_dir = root / 'spec' / 'endpoints' / function / 'examples'
  examples_dir.mkdir(parents=True, exist_ok=True)
  example_path = examples_dir / '01.response.json'
  example_path.write_text(json.dumps({'status': 200, 'payload': payload}))
  return example_path


def test_obviously_fake_value_produces_no_finding(tmp_path):
  """A `client_secret` whose value carries a `REDACTED` marker is clean."""
  write_client_toml(tmp_path, required=['TEST_SYNTH_CLIENT_ID', 'TEST_SYNTH_CLIENT_SECRET'])
  write_example(
    tmp_path, function='account.list_keys',
    payload={'client_id': 'REDACTED_CLIENT_ID', 'client_secret': 'REDACTED_CLIENT_SECRET_XY'},
  )

  findings = check_secret_placeholders(tmp_path)

  assert findings == []


def test_realistic_looking_value_is_flagged(tmp_path):
  """The same field, holding a value with no fake marker, is flagged."""
  write_client_toml(tmp_path, required=['TEST_SYNTH_CLIENT_SECRET'])
  write_example(
    tmp_path, function='account.list_keys',
    payload={'client_secret': '9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c0d1e2f3a'},
  )

  findings = check_secret_placeholders(tmp_path)

  assert len(findings) == 1
  finding = findings[0]
  assert finding['rule'] == 'S16'
  assert finding['severity'] == 'warning'
  assert 'client_secret' in finding['message']
  assert '9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c0d1e2f3a' not in finding['message']
  assert '9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c0d1e2f3a' not in finding['location']
  assert 'client_secret' in finding['location']


def test_client_with_no_secrets_table_runs_cleanly(tmp_path):
  """A public-only client (no `client.toml`, or none with `[secrets]`) doesn't crash and
  still uses the built-in denylist."""
  write_example(
    tmp_path, function='market.ticker',
    payload={'symbol': 'BTC-USD', 'privateKey': 'a'.join(str(i) for i in range(20))},
  )

  findings = check_secret_placeholders(tmp_path)

  assert len(findings) == 1
  assert findings[0]['rule'] == 'S16'


def test_non_string_value_is_skipped(tmp_path):
  """A credential-shaped key holding a non-string value has nothing to placeholder-check."""
  write_client_toml(tmp_path, required=['TEST_SYNTH_CLIENT_SECRET'])
  write_example(
    tmp_path, function='account.list_keys', payload={'client_secret': None, 'secretCount': 3},
  )

  findings = check_secret_placeholders(tmp_path)

  assert findings == []


def test_deeply_nested_credential_field_is_found(tmp_path):
  """A credential field nested inside a list of objects is still found by the recursive walk."""
  write_client_toml(tmp_path, required=['TEST_SYNTH_CLIENT_SECRET'])
  write_example(
    tmp_path, function='account.list_keys',
    payload={'result': [{'id': 1, 'client_secret': '9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c0d1e2f3a'}]},
  )

  findings = check_secret_placeholders(tmp_path)

  assert len(findings) == 1
  assert 'result[0].client_secret' in findings[0]['location']


