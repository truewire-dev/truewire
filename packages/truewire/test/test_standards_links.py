"""
Exercise `truewire.standards.links.check_links` against synthetic fixtures with the
network layer mocked, per the isolation contract `test_mock.py`'s module docstring records
-- these must pass in a checkout with zero clients and must never touch a real network.
"""
import urllib.error
from pathlib import Path
from unittest.mock import patch

from truewire.standards.links import (
  check_links,
  extract_urls,
  is_landing_page_redirect,
)


class FakeResponse:
  """Stand-in for `http.client.HTTPResponse`, usable as a context manager."""

  def __init__(self, status: int, final_url: str):
    self.status = status
    self._final_url = final_url

  def geturl(self) -> str:
    return self._final_url

  def __enter__(self):
    return self

  def __exit__(self, *exc):
    return False


def write_upstream(root: Path, *, function: str, source: str) -> Path:
  """Write one `upstream.md` citing `source` as its `**Source:**` line."""
  endpoint_dir = root / 'spec' / 'endpoints' / function
  endpoint_dir.mkdir(parents=True)
  path = endpoint_dir / 'upstream.md'
  path.write_text(f'# {function}\n\n**Source:** {source}\n')
  return path


def write_router_json(root: Path, *, section: str, upstream: str) -> Path:
  """Write one `router.json` declaring `upstream` for `section`."""
  section_dir = root / 'spec' / 'endpoints' / section
  section_dir.mkdir(parents=True, exist_ok=True)
  path = section_dir / 'router.json'
  path.write_text(f'{{"description": "A grouping.", "upstream": "{upstream}"}}')
  return path


def test_extract_urls_ignores_wire_templates():
  """A `{placeholder}`-templated URL (an upstream.md `Base URL` line) isn't fetchable."""
  text = (
    '**Source:** https://docs.example.com/api/widgets\n'
    '**Base URL:** https://{network}.example.com/v2/{apiKey}\n'
  )
  assert extract_urls(text) == {'https://docs.example.com/api/widgets'}


def test_extract_urls_strips_markdown_and_html_wrapping():
  """A markdown link and an HTML `<a href>` both yield the bare URL, no trailing syntax."""
  text = (
    '- [Widgets](https://docs.example.com/api/widgets)\n'
    '<a href="https://docs.example.com/other">link</a>\n'
  )
  assert extract_urls(text) == {
    'https://docs.example.com/api/widgets', 'https://docs.example.com/other',
  }


def test_is_landing_page_redirect_flags_domain_level_landing():
  assert is_landing_page_redirect(
    'https://docs.example.com/api/widgets/list', 'https://docs.example.com/',
  )


def test_is_landing_page_redirect_allows_a_real_section_redirect():
  assert not is_landing_page_redirect(
    'https://docs.example.com/api/widgets/list', 'https://docs.example.com/api/widgets/v2/list',
  )


def test_clean_url_produces_no_finding(tmp_path):
  write_upstream(tmp_path, function='widgets.get', source='https://docs.example.com/widgets')

  with patch('urllib.request.urlopen', return_value=FakeResponse(200, 'https://docs.example.com/widgets')):
    findings = check_links(tmp_path)

  assert findings == []


def test_404_produces_a_finding(tmp_path):
  write_upstream(tmp_path, function='widgets.get', source='https://docs.example.com/widgets')

  def fake_urlopen(request, timeout=None):
    raise urllib.error.HTTPError('https://docs.example.com/widgets', 404, 'Not Found', {}, None)

  with patch('urllib.request.urlopen', side_effect=fake_urlopen):
    findings = check_links(tmp_path)

  assert len(findings) == 1
  assert findings[0]['rule'] == 'S1'
  assert findings[0]['severity'] == 'warning'
  assert 'widgets.get/upstream.md' in findings[0]['location']
  assert 'HTTP 404' in findings[0]['message']


def test_head_405_falls_back_to_get(tmp_path):
  write_upstream(tmp_path, function='widgets.get', source='https://docs.example.com/widgets')

  calls = []

  def fake_urlopen(request, timeout=None):
    calls.append(request.get_method())
    if request.get_method() == 'HEAD':
      raise urllib.error.HTTPError('https://docs.example.com/widgets', 405, 'Not Allowed', {}, None)
    return FakeResponse(200, 'https://docs.example.com/widgets')

  with patch('urllib.request.urlopen', side_effect=fake_urlopen):
    findings = check_links(tmp_path)

  assert calls == ['HEAD', 'GET']
  assert findings == []


def test_landing_page_redirect_produces_a_finding(tmp_path):
  write_upstream(tmp_path, function='widgets.get', source='https://docs.example.com/api/widgets/get')

  with patch('urllib.request.urlopen', return_value=FakeResponse(200, 'https://docs.example.com/')):
    findings = check_links(tmp_path)

  assert len(findings) == 1
  assert 'landing page' in findings[0]['message']


def test_connection_error_produces_a_finding_instead_of_crashing(tmp_path):
  write_upstream(tmp_path, function='widgets.get', source='https://docs.example.com/widgets')

  def fake_urlopen(request, timeout=None):
    raise TimeoutError('timed out')

  with patch('urllib.request.urlopen', side_effect=fake_urlopen):
    findings = check_links(tmp_path)

  assert len(findings) == 1
  assert 'could not resolve' in findings[0]['message']
  assert 'timed out' in findings[0]['message']


def test_same_url_cited_twice_is_fetched_once_but_reported_per_file(tmp_path):
  write_upstream(tmp_path, function='widgets.get', source='https://docs.example.com/widgets')
  write_upstream(tmp_path, function='widgets.list', source='https://docs.example.com/widgets')

  calls = []

  def fake_urlopen(request, timeout=None):
    calls.append(request.full_url)
    raise urllib.error.HTTPError(request.full_url, 500, 'Server Error', {}, None)

  with patch('urllib.request.urlopen', side_effect=fake_urlopen):
    findings = check_links(tmp_path)

  assert len(calls) == 1
  assert len(findings) == 2
  assert {f['location'].split(': ', 1)[0] for f in findings} == {
    'spec/endpoints/widgets.get/upstream.md', 'spec/endpoints/widgets.list/upstream.md',
  }


def test_readme_and_docs_external_links_are_in_scope(tmp_path):
  (tmp_path / 'README.md').write_text('See [docs](https://docs.example.com/readme-link).\n')
  docs_dir = tmp_path / 'docs'
  docs_dir.mkdir()
  (docs_dir / 'index.md').write_text('[More](https://docs.example.com/docs-link)\n')

  def fake_urlopen(request, timeout=None):
    raise urllib.error.HTTPError(request.full_url, 404, 'Not Found', {}, None)

  with patch('urllib.request.urlopen', side_effect=fake_urlopen):
    findings = check_links(tmp_path)

  urls = {f['location'].split(': ', 1)[1] for f in findings}
  assert urls == {'https://docs.example.com/readme-link', 'https://docs.example.com/docs-link'}


def test_router_json_upstream_link_is_checked(tmp_path):
  write_router_json(tmp_path, section='classic/mix', upstream='https://docs.example.com/mix')

  with patch(
    'urllib.request.urlopen',
    return_value=FakeResponse(404, 'https://docs.example.com/mix'),
  ):
    findings = check_links(tmp_path)

  assert len(findings) == 1
  assert 'router.json' in findings[0]['location']
  assert 'HTTP 404' in findings[0]['message']
