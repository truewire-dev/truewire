"""`[secrets]` and `[policy]` in `truewire.toml` (docs/shape/workspace.md W14, W15)."""
from pathlib import Path

import pytest

from truewire.project import NotAProject, Policy, Secrets, load_project_data


def load(tmp_path: Path, **tables):
  return load_project_data({'project': {'name': 'demo'}, **tables}, root=tmp_path)


def test_absent_tables_load_as_their_defaults(tmp_path):
  project = load(tmp_path)
  assert project.secrets == Secrets()
  assert project.policy == Policy(rate=None, retry=False, refuse=())


def test_policy_reads_rate_retry_and_refuse(tmp_path):
  project = load(tmp_path, policy={'rate': 10, 'retry': True, 'refuse': ['account.withdraw']})
  assert project.policy == Policy(rate=10.0, retry=True, refuse=('account.withdraw',))
  assert load(tmp_path, policy={'rate': 0.5}).policy.rate == 0.5


def test_secrets_reads_variable_names(tmp_path):
  project = load(tmp_path, secrets={'required': ['DEMO_API_KEY'], 'optional': ['DEMO_API_SECRET']})
  assert project.secrets == Secrets(required=('DEMO_API_KEY',), optional=('DEMO_API_SECRET',))


@pytest.mark.parametrize('policy, message', [
  ({'rate': 0}, '[policy].rate must be a positive number'),
  ({'rate': -1}, '[policy].rate must be a positive number'),
  ({'rate': '10/s'}, '[policy].rate must be a positive number'),
  ({'rate': True}, '[policy].rate must be a positive number'),
  ({'rate': float('inf')}, '[policy].rate must be a positive number'),
  ({'rate': float('nan')}, '[policy].rate must be a positive number'),
  ({'retry': 'yes'}, '[policy].retry must be true or false'),
  ({'refuse': 'account.withdraw'}, '[policy].refuse must be a list of strings'),
  ({'refuse': ['']}, '[policy].refuse must be a list of strings'),
  ({'refuse': ['a.b', 'a.b']}, '[policy].refuse lists a.b more than once'),
  ({'retries': 3}, 'unknown [policy] key(s): retries'),
  ('fast', '[policy] must be a table'),
])
def test_policy_rejects(tmp_path, policy, message):
  with pytest.raises(NotAProject) as raised:
    load(tmp_path, policy=policy)
  assert message in str(raised.value)


@pytest.mark.parametrize('secrets, message', [
  ({'required': ['sk-not-a-name']}, "not a variable name: 'sk-not-a-name'"),
  ({'required': ['$HOME/.key']}, "not a variable name: '$HOME/.key'"),
  ({'required': 'DEMO_API_KEY'}, '[secrets].required must be a list of strings'),
  ({'values': {'DEMO_API_KEY': 'x'}}, 'unknown [secrets] key(s): values'),
  (['DEMO_API_KEY'], '[secrets] must be a table'),
  ({'required': ['DEMO_API_KEY', 'DEMO_API_KEY']}, '[secrets].required lists DEMO_API_KEY more than once'),
  ({'required': ['DEMO_API_KEY'], 'optional': ['DEMO_API_KEY']}, 'lists DEMO_API_KEY as both required and optional'),
])
def test_secrets_rejects_anything_but_variable_names(tmp_path, secrets, message):
  with pytest.raises(NotAProject) as raised:
    load(tmp_path, secrets=secrets)
  assert message in str(raised.value)
