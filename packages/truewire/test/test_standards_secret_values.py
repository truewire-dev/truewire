"""`truewire.standards.secret_values` (`docs/shape/workspace.md` W14): a file in the tree
holding the value of a `[secrets]` variable fails `truewire standards`, and the value is
never printed. Synthetic projects only, built with `init` and a hand-written example. The
tests from `test_standards_location_never_quotes_a_key_holding_the_value` on are the
review probes of PR #26 (TRU-193, then TRU-252), kept as regressions."""
import base64
import json
import os
import subprocess
from pathlib import Path
from urllib.parse import quote

from typer.testing import CliRunner

from truewire.cli import app
from truewire.mock import running_mock_servers
from truewire.project import load_project
from truewire.standards.secret_values import (
  check_secret_values,
  held_secrets,
  leaks_in_text,
  redact,
)

from test_capture import echoing_project, quickstart_project

KEY = 'abcd1234efgh'


def project_with_secrets(tmp_path: Path, monkeypatch, table: str = 'required = ["PETSTORE_KEY"]') -> Path:
  """An `init`-ed project whose `[secrets]` is `table`, with one endpoint and one example."""
  monkeypatch.chdir(tmp_path)
  assert CliRunner().invoke(app, ['init', 'petstore', '--base-url', 'https://petstore.example/v1']).exit_code == 0
  root = tmp_path / 'petstore'
  toml = root / 'truewire.toml'
  toml.write_text(toml.read_text().replace('[secrets]\nrequired = []\n', f'[secrets]\n{table}\n'))
  return root


def write_example(root: Path, payload: object, *, name: str = 'default.response.json') -> Path:
  examples = root / 'spec' / 'endpoints' / 'pets' / 'get_pet' / 'examples'
  examples.mkdir(parents=True, exist_ok=True)
  path = examples / name
  path.write_text(json.dumps({'status': 200, 'payload': payload}, indent=2) + '\n')
  return path


def held_secrets_of(environ: dict[str, str]):
  """`held_secrets` over a project-less `[secrets]` naming every key of `environ`."""
  class Stub:
    root = Path('/nonexistent')
    secrets = type('S', (), {'required': tuple(environ), 'optional': ()})()
  return held_secrets(Stub(), environ)  # type: ignore[arg-type]


def test_a_value_in_a_response_body_is_an_error_naming_the_variable_and_file(tmp_path, monkeypatch):
  root = project_with_secrets(tmp_path, monkeypatch)
  write_example(root, {'id': 42, 'owner': {'note': f'key={KEY}'}})

  findings = check_secret_values(root, {'PETSTORE_KEY': KEY})

  assert [(f['rule'], f['severity'], f['location']) for f in findings] == [
    ('W14', 'error', 'spec/endpoints/pets/get_pet/examples/default.response.json: payload.owner.note'),
  ]
  assert '`PETSTORE_KEY`' in findings[0]['message']
  assert KEY not in json.dumps(findings)


def test_the_value_is_read_from_dotenv_too(tmp_path, monkeypatch):
  root = project_with_secrets(tmp_path, monkeypatch, 'optional = ["PETSTORE_KEY"]')
  (root / '.env').write_text(f'export PETSTORE_KEY="{KEY}"  # local only\n')
  write_example(root, {'token': KEY})

  assert [f['location'] for f in check_secret_values(root, {})] == [
    'spec/endpoints/pets/get_pet/examples/default.response.json: payload.token',
  ]


def test_environment_and_dotenv_values_are_both_searched(tmp_path, monkeypatch):
  root = project_with_secrets(tmp_path, monkeypatch)
  (root / '.env').write_text('PETSTORE_KEY=old-key-still-live\n')
  write_example(root, {'token': 'old-key-still-live'})

  assert len(check_secret_values(root, {'PETSTORE_KEY': KEY})) == 1


def test_an_unset_variable_passes(tmp_path, monkeypatch):
  """CI runs with no credentials: nothing to compare, nothing to report."""
  root = project_with_secrets(tmp_path, monkeypatch)
  write_example(root, {'token': KEY})

  assert check_secret_values(root, {}) == []


def test_a_short_value_is_skipped_with_a_note(tmp_path, monkeypatch):
  root = project_with_secrets(tmp_path, monkeypatch, 'required = ["PETSTORE_KEY", "PETSTORE_REGION"]')
  write_example(root, {'region': 'eu-west', 'token': KEY})

  findings = check_secret_values(root, {'PETSTORE_KEY': KEY, 'PETSTORE_REGION': 'eu-west'})

  assert [(f['severity'], f['location']) for f in findings] == [
    ('warning', '[secrets]'),
    ('error', 'spec/endpoints/pets/get_pet/examples/default.response.json: payload.token'),
  ]
  assert '`PETSTORE_REGION`' in findings[0]['message']
  assert 'shorter than 8' in findings[0]['message']
  assert 'eu-west' not in json.dumps(findings)


def test_encoded_spellings_are_found():
  """A key echoed inside a URL is percent-encoded; one inside a JSON string is escaped."""
  secrets = held_secrets_of({'PETSTORE_KEY': 'a/b c"d+e=f'})

  in_url = json.dumps({'next': 'https://x.example/?key=a%2Fb%20c%22d%2Be%3Df'})
  in_form = 'key=a%2Fb+c%22d%2Be%3Df'
  in_raw_json = '{"k": "a/b c\\"d+e=f"}'
  assert leaks_in_text(in_url, secrets) == [('PETSTORE_KEY', 'next')]
  assert leaks_in_text(in_form, secrets) == [('PETSTORE_KEY', '')]
  assert leaks_in_text(in_raw_json, secrets) == [('PETSTORE_KEY', 'k')]


def test_keys_numbers_and_request_parameters_are_searched():
  secrets = held_secrets_of({'NUMERIC_ID': '9876543210'})

  assert leaks_in_text(json.dumps({'request': {'account': 9876543210}}), secrets) == [
    ('NUMERIC_ID', 'request.account'),
  ]
  assert leaks_in_text(json.dumps({'payload': {'9876543210': True}}), secrets) == [
    ('NUMERIC_ID', 'payload.<NUMERIC_ID>'),
  ]


def test_redact_replaces_every_spelling_with_the_name():
  secrets = held_secrets_of({'PETSTORE_KEY': 'a/b cdefg'})

  assert redact('raw a/b cdefg, url a%2Fb%20cdefg, form a%2Fb+cdefg', secrets) == (
    'raw <PETSTORE_KEY>, url <PETSTORE_KEY>, form <PETSTORE_KEY>'
  )


def test_standards_exits_1_and_never_prints_the_value(tmp_path, monkeypatch):
  """The ticket's check: `PETSTORE_KEY` set, its value in one example's response body."""
  root = project_with_secrets(tmp_path, monkeypatch)
  write_example(root, {'id': 42, 'name': KEY})
  monkeypatch.setenv('PETSTORE_KEY', KEY)

  result = CliRunner().invoke(app, ['standards', '--only', 'secret-values', '--project', str(root)])

  assert result.exit_code == 1, result.output
  assert 'PETSTORE_KEY' in result.output
  assert 'spec/endpoints/pets/get_pet/examples/default.response.json: payload.name' in result.output
  assert KEY not in result.output


def test_init_project_passes_with_the_check_in_the_default_run(tmp_path, monkeypatch):
  """Registered in the default `truewire standards` run, and quiet on a clean project."""
  root = project_with_secrets(tmp_path, monkeypatch)
  monkeypatch.setenv('PETSTORE_KEY', KEY)

  assert check_secret_values(load_project(root)) == []
  listed = CliRunner().invoke(app, ['standards', '--list-checks'])
  assert 'secret-values' in listed.output
  assert '[default: on]' in next(line for line in listed.output.splitlines() if line.startswith('secret-values'))


def test_standards_location_never_quotes_a_key_holding_the_value(tmp_path, monkeypatch):
  root = project_with_secrets(tmp_path, monkeypatch)
  write_example(root, {'tokens': {KEY: {'scope': 'read'}}})
  findings = check_secret_values(root, {'PETSTORE_KEY': KEY})
  assert findings, 'expected a W14 finding'
  assert KEY not in json.dumps(findings)


def test_capture_refusal_never_quotes_a_key_holding_the_value(tmp_path, monkeypatch):
  project = echoing_project(tmp_path, monkeypatch, 'Rex')
  served = project / 'spec' / 'endpoints' / 'pets' / 'get_pet' / 'examples' / 'default.response.json'
  body = json.loads(served.read_text())
  body['payload']['tag'] = None
  body['payload'][KEY] = 'x'
  served.write_text(json.dumps(body))
  with running_mock_servers(project) as servers:
    result = CliRunner().invoke(app, [
      'capture', 'pets.get_pet', '--request', '{"petId": 42}', '--id', 'echoed', '--no-check',
      '--new', f'base_url={servers.http_base_url}', '--project', str(project),
    ])
  assert result.exit_code == 1, result.output
  assert KEY not in result.output, result.output


def test_capture_never_prints_a_key_carried_in_the_base_url_path(tmp_path, monkeypatch):
  """Alchemy/Infura style: the key is a path segment of the base URL, read from .env."""
  project = echoing_project(tmp_path, monkeypatch, 'Rex')
  with running_mock_servers(project) as servers:
    result = CliRunner().invoke(app, [
      'capture', 'pets.get_pet', '--request', '{"petId": 42}', '--id', 'p', '--no-check',
      '--new', f'base_url={servers.http_base_url}/v2/{KEY}', '--project', str(project),
    ])
  assert KEY not in result.output, result.output


def test_redact_hides_every_character_of_overlapping_values():
  secrets = held_secrets_of({'A_KEY': 'abcd1234', 'B_KEY': 'abcd1234efgh5678'})
  out = redact('token=abcd1234efgh5678', secrets)
  assert 'efgh5678' not in out, out


def test_a_value_percent_encoded_with_slash_left_raw_is_found():
  """A base64-shaped key (`+`, `/`, `=`) in a URL built by an encoder that keeps `/`."""
  value = 'kQ3+v/Zx9w=='
  secrets = held_secrets_of({'KRAKEN_API_KEY': value})
  text = json.dumps({'next': f'https://api.example/x?key={quote(value, safe="/")}'})
  assert leaks_in_text(text, secrets)


def test_a_value_percent_encoded_in_lower_case_hex_is_found():
  value = 'kQ3+vZx9w=='
  secrets = held_secrets_of({'KRAKEN_API_KEY': value})
  text = json.dumps({'next': 'https://api.example/x?key=' + quote(value, safe='').lower().replace('kq3', 'kQ3').replace('vzx9w', 'vZx9w')})
  assert leaks_in_text(text, secrets), text


def test_a_basic_auth_echo_is_found(tmp_path, monkeypatch):
  root = project_with_secrets(tmp_path, monkeypatch)
  basic = base64.b64encode(f'user:{KEY}'.encode()).decode()
  write_example(root, {'headers': {'Authorization': f'Basic {basic}'}})
  assert check_secret_values(root, {'PETSTORE_KEY': KEY})


def test_a_value_pasted_into_dev_capture_is_found(tmp_path, monkeypatch):
  root = project_with_secrets(tmp_path, monkeypatch)
  (root / 'dev' / 'capture').mkdir(parents=True, exist_ok=True)
  (root / 'dev' / 'capture' / 'pets.sh').write_text(f"truewire capture pets.get_pet --new api_key={KEY}\n")
  assert check_secret_values(root, {'PETSTORE_KEY': KEY})


def test_a_value_pasted_into_endpoint_json_is_found(tmp_path, monkeypatch):
  root = project_with_secrets(tmp_path, monkeypatch)
  path = write_example(root, {'id': 1}).parent.parent / 'endpoint.json'
  path.write_text(json.dumps({'notes': f'tested with key {KEY}'}))
  assert check_secret_values(root, {'PETSTORE_KEY': KEY})


def test_a_file_named_after_the_value_is_found(tmp_path, monkeypatch):
  root = project_with_secrets(tmp_path, monkeypatch)
  write_example(root, {'id': 1}, name=f'{KEY}.response.json')
  assert check_secret_values(root, {'PETSTORE_KEY': KEY})


def test_standards_never_prints_the_value_through_an_external_check(tmp_path, monkeypatch):
  """The value sits in a field whose type is wrong: `truewire check` quotes it, and
  `standards` re-prints check's output as is."""
  project = quickstart_project(tmp_path, monkeypatch)
  toml = project / 'truewire.toml'
  toml.write_text(toml.read_text().replace('[secrets]\nrequired = []\n', '[secrets]\nrequired = ["PETSTORE_KEY"]\n'))
  served = project / 'spec' / 'endpoints' / 'pets' / 'get_pet' / 'examples' / 'default.response.json'
  body = json.loads(served.read_text())
  body['payload']['id'] = KEY
  served.write_text(json.dumps(body, indent=2) + '\n')
  monkeypatch.setenv('PETSTORE_KEY', KEY)
  result = CliRunner().invoke(app, ['standards', '--project', str(project), '--only', 'check,secret-values'])
  assert result.exit_code == 1, result.output
  assert 'PETSTORE_KEY' in result.output
  assert KEY not in result.output, result.output


def test_a_gitignored_dotenv_is_not_a_finding_but_a_tracked_file_is(tmp_path, monkeypatch):
  """In a git repository the search covers what can be committed: `.env` is ignored
  (`init` writes the rule), and an untracked, unignored note is not."""
  import subprocess
  root = project_with_secrets(tmp_path, monkeypatch)
  subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
  (root / '.env').write_text(f'PETSTORE_KEY={KEY}\n')
  (root / 'notes.txt').write_text(f'key is {KEY}\n')

  findings = check_secret_values(root, {})

  assert [f['location'] for f in findings] == ['notes.txt']


def test_redacted_names_and_percent_encodings_in_both_hex_cases():
  secrets = held_secrets_of({'KRAKEN_API_KEY': 'kQ3+v/Zx9w=='})

  assert redact('a kQ3%2Bv%2FZx9w%3D%3D b kQ3%2bv/Zx9w%3d%3d c', secrets) == (
    'a <KRAKEN_API_KEY> b <KRAKEN_API_KEY> c'
  )


def test_capture_check_step_never_prints_a_sibling_recordings_value(tmp_path, monkeypatch):
  """`--check` is on by default, and `truewire check` quotes the sibling pair it rejects."""
  project = echoing_project(tmp_path, monkeypatch, 'Rex')
  examples = project / 'spec' / 'endpoints' / 'pets' / 'get_pet' / 'examples'
  request = json.loads((examples / 'default.request.json').read_text())
  request['request'] = {'petId': 7}
  (examples / 'other.request.json').write_text(json.dumps(request, indent=2) + '\n')
  body = json.loads((examples / 'default.response.json').read_text())
  body['payload']['id'] = KEY
  (examples / 'other.response.json').write_text(json.dumps(body, indent=2) + '\n')
  with running_mock_servers(project) as servers:
    result = CliRunner().invoke(app, [
      'capture', 'pets.get_pet', '--request', '{"petId": 42}', '--id', 'captured',
      '--new', f'base_url={servers.http_base_url}', '--project', str(project),
    ])
  assert 'captured.response.json' in result.output, result.output
  assert KEY not in result.output, result.output


def test_a_non_utf8_file_name_does_not_crash_the_search(tmp_path, monkeypatch):
  root = project_with_secrets(tmp_path, monkeypatch)
  subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
  (root / os.fsdecode(b'notes-\xe9.txt')).write_text(f'key is {KEY}\n')
  findings = check_secret_values(root, {'PETSTORE_KEY': KEY})
  assert [f['rule'] for f in findings] == ['W14']


def test_a_json_escaped_slash_is_found_and_redacted():
  """PHP's json_encode (and others) write `/` as `\\/`: a legal JSON escape."""
  value = 'kQ3+v/Zx9w=='
  secrets = held_secrets_of({'KRAKEN_API_KEY': value})
  text = '{"error": "invalid key ' + value.replace('/', '\\/') + '"}'
  assert leaks_in_text(text, secrets)
  assert '<KRAKEN_API_KEY>' in redact(text, secrets)


def test_a_basic_credential_after_a_path_segment_is_found():
  secrets = held_secrets_of({'PETSTORE_KEY': KEY})
  encoded = base64.b64encode(f'user:{KEY}'.encode()).decode()
  text = json.dumps({'url': f'https://httpbin.org/anything/{encoded}'})
  assert leaks_in_text(text, secrets)
