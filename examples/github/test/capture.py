"""Record the examples in this project from the live GitHub API.

Run from the project root: `python test/capture.py [--token TOKEN]`. Every recorded pair
is a real response from `api.github.com`; only fields that would leak a credential
(`temp_clone_token`) are replaced by an obviously fake placeholder (authoring rule 6).
Re-running overwrites the examples with fresh captures, so the recorded values move with
the repository they describe.
"""

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
ENDPOINTS = ROOT / 'spec' / 'endpoints'
BASE_URL = 'https://api.github.com'
OWNER, REPO = 'truewire-dev', 'truewire'

CAPTURES: list[tuple[str, str, str, dict]] = [
  # (endpoint directory, example id, description, request)
  ('repos/get', 'default', 'The toolchain repository itself', {'owner': OWNER, 'repo': REPO}),
  ('repos/list_tags', 'default', 'Release tags', {'owner': OWNER, 'repo': REPO, 'per_page': 30, 'page': 1}),
  ('repos/list_releases', 'default', 'Published releases', {'owner': OWNER, 'repo': REPO, 'per_page': 30, 'page': 1}),
  ('repos/list_commits', 'page1', 'First page of three', {'owner': OWNER, 'repo': REPO, 'per_page': 3, 'page': 1}),
  ('repos/list_commits', 'page2', 'Second page of three', {'owner': OWNER, 'repo': REPO, 'per_page': 3, 'page': 2}),
  ('repos/list_commits', 'page3', 'Third page of three', {'owner': OWNER, 'repo': REPO, 'per_page': 3, 'page': 3}),
  ('repos/list_commits', 'page4', 'Last, short page', {'owner': OWNER, 'repo': REPO, 'per_page': 3, 'page': 4}),
  ('repos/get_commit', 'default', 'The 0.1.0 release merge commit', {'owner': OWNER, 'repo': REPO, 'ref': '934a718509b0cfd1244db90821201afbbb728797'}),
  ('issues/list', 'page1', 'First closed pull request, one per page', {'owner': OWNER, 'repo': REPO, 'state': 'all', 'per_page': 1, 'page': 1}),
  ('issues/list', 'page2', 'Second closed pull request', {'owner': OWNER, 'repo': REPO, 'state': 'all', 'per_page': 1, 'page': 2}),
  ('issues/list', 'page3', 'Past the end: an empty page', {'owner': OWNER, 'repo': REPO, 'state': 'all', 'per_page': 1, 'page': 3}),
]

PATHS = {
  'repos/get': ('GET', '/repos/{owner}/{repo}'),
  'repos/list_tags': ('GET', '/repos/{owner}/{repo}/tags'),
  'repos/list_releases': ('GET', '/repos/{owner}/{repo}/releases'),
  'repos/list_commits': ('GET', '/repos/{owner}/{repo}/commits'),
  'repos/get_commit': ('GET', '/repos/{owner}/{repo}/commits/{ref}'),
  'issues/list': ('GET', '/repos/{owner}/{repo}/issues'),
}

SECRET_FIELDS = {'temp_clone_token': 'REDACTED_TEMP_CLONE_TOKEN'}


def scrub(value):
  """Replace credential-shaped fields with placeholders, recursively."""
  if isinstance(value, dict):
    return {
      k: (SECRET_FIELDS[k] if k in SECRET_FIELDS and v is not None else scrub(v))
      for k, v in value.items()
    }
  if isinstance(value, list):
    return [scrub(v) for v in value]
  return value


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--token', default=os.environ.get('GITHUB_TOKEN'))
  args = parser.parse_args()
  headers = {
    'Accept': 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28',
    'User-Agent': 'truewire-example-github',
  }
  if args.token:
    headers['Authorization'] = f'Bearer {args.token}'
  with httpx.Client(base_url=BASE_URL, headers=headers, timeout=30) as http:
    for endpoint, example_id, description, request in CAPTURES:
      method, path = PATHS[endpoint]
      params = dict(request)
      for name in list(params):
        if f'{{{name}}}' in path:
          path = path.replace(f'{{{name}}}', str(params.pop(name)))
      response = http.request(method, path, params=params)
      if response.status_code != 200:
        print(f'{endpoint}[{example_id}]: HTTP {response.status_code}: {response.text[:200]}', file=sys.stderr)
        return 1
      out = ENDPOINTS / endpoint / 'examples'
      out.mkdir(parents=True, exist_ok=True)
      (out / f'{example_id}.request.json').write_text(
        json.dumps({'description': description, 'request': request}, indent=2) + '\n'
      )
      (out / f'{example_id}.response.json').write_text(
        json.dumps({'status': response.status_code, 'payload': scrub(response.json())}, indent=2) + '\n'
      )
      print(f'{endpoint}[{example_id}]: {response.status_code}, {len(response.content)} bytes')
  return 0


if __name__ == '__main__':
  raise SystemExit(main())
