"""The docs audience checker: published docs describe the library, not our pipeline."""

from pathlib import Path

import pytest

from truewire.docs.lint import lint_docs


@pytest.fixture
def client(tmp_path: Path) -> Path:
  (tmp_path / 'docs').mkdir()
  (tmp_path / 'client.toml').write_text(
    '[export]\n'
    'include = ["README.md", "docs/**", "pkg/**"]\n'
    'exclude = [".env"]\n'
  )
  (tmp_path / 'README.md').write_text('# Venue\n\nA typed client.\n')
  return tmp_path


def test_a_clean_doc_set_has_no_findings(client: Path):
  (client / 'docs' / 'index.md').write_text(
    '# Venue\n\nRead market data and account state.\n'
  )
  assert lint_docs(client) == []


def test_an_internal_path_reference_is_a_finding(client: Path):
  (client / 'docs' / 'index.md').write_text(
    'See `spec/status.md` for the full per-endpoint record.\n'
  )
  findings = lint_docs(client)
  assert len(findings) == 1
  assert findings[0].rule == 'internal-reference'
  assert findings[0].line == 1


def test_pipeline_voice_is_a_finding(client: Path):
  (client / 'docs' / 'index.md').write_text(
    'These are deliberately deferred, not missing by oversight.\n'
    'The account these docs run against has never placed an order.\n'
  )
  findings = lint_docs(client)
  assert {f.line for f in findings} == {1, 2}
  assert all(f.rule == 'internal-reference' for f in findings)


def test_a_link_to_an_exported_path_is_fine(client: Path):
  (client / 'docs' / 'index.md').write_text(
    'See [the README](https://github.com/tribulnation/venue/blob/main/README.md).\n'
  )
  assert lint_docs(client) == []


def test_a_relative_link_to_a_missing_doc_is_a_finding(client: Path):
  (client / 'docs' / 'index.md').write_text(
    'See [How To](how-to/index.md).\n'
  )
  findings = lint_docs(client)
  assert len(findings) == 1
  assert findings[0].rule == 'broken-doc-link'
  assert findings[0].line == 1
  assert 'docs/how-to/index.md' in findings[0].detail


def test_a_relative_link_to_an_existing_doc_is_fine(client: Path):
  (client / 'docs' / 'how-to').mkdir()
  (client / 'docs' / 'how-to' / 'index.md').write_text('# How To\n')
  (client / 'docs' / 'index.md').write_text(
    'See [How To](how-to/index.md).\n'
  )
  assert lint_docs(client) == []


def test_external_and_anchor_links_do_not_need_local_targets(client: Path):
  (client / 'docs' / 'index.md').write_text(
    '[Upstream](https://example.com/docs) [Section](#usage) '\
    '[Email](mailto:hello@example.com)\n'
  )
  assert lint_docs(client) == []


def test_the_readme_is_checked_too(client: Path):
  (client / 'README.md').write_text('This client was scoped out of the run.\n')
  assert [f.path.name for f in lint_docs(client)] == ['README.md']


def test_a_fenced_code_block_is_not_prose(client: Path):
  """A code example may legitimately mention a path or a variable named `coverage`."""
  (client / 'docs' / 'index.md').write_text(
    '# Venue\n\n```python\n# out of scope for this example\ncoverage = 1\n```\n'
  )
  assert lint_docs(client) == []


def test_coverage_and_not_implemented_are_not_leaks(client: Path):
  """Both were measured against the whole corpus and are legitimate user-facing prose."""
  (client / 'docs' / 'index.md').write_text(
    '## API Coverage\n\nWebSocket support is not implemented in the current package.\n'
  )
  assert lint_docs(client) == []


def test_an_overlong_page_is_a_finding(client: Path):
  (client / 'docs' / 'api-keys.md').write_text('# Keys\n\n' + 'word ' * 500)
  findings = lint_docs(client)
  assert [f.rule for f in findings] == ['verbose-page']
  assert '500 prose words' in findings[0].detail


def test_a_page_within_budget_is_fine(client: Path):
  (client / 'docs' / 'api-keys.md').write_text('# Keys\n\n' + 'word ' * 100)
  assert lint_docs(client) == []


def test_code_blocks_do_not_count_against_the_budget(client: Path):
  """A page should never be penalised for showing more code."""
  body = '# Keys\n\n' + 'word ' * 100 + '\n\n```python\n' + 'x = 1\n' * 500 + '```\n'
  (client / 'docs' / 'api-keys.md').write_text(body)
  assert lint_docs(client) == []


def test_the_generated_api_reference_is_uncapped(client: Path):
  (client / 'docs' / 'reference' / 'api').mkdir(parents=True)
  (client / 'docs' / 'reference' / 'api' / 'market.md').write_text('# Market\n\n' + 'word ' * 5000)
  assert lint_docs(client) == []
