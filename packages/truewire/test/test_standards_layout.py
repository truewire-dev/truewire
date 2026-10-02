"""`truewire standards` checks the layout `truewire init` writes (S8): A7-A9, W10, W13, W16.

A project `init` just wrote has no layout finding; each clause broken on its own gives one
finding naming its id, and a finding fails the command.
"""
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
from typer.testing import CliRunner

from test_score import finish, fixture_project
from truewire.cli import app
from truewire.project import load_project
from truewire.score import rows as score_rows
from truewire.standards.layout import check_layout


@pytest.fixture
def project(tmp_path: Path) -> Path:
  assert CliRunner().invoke(app, ['init', 'demo', '--dir', str(tmp_path / 'demo')]).exit_code == 0
  return tmp_path / 'demo'


def findings(project: Path) -> list[tuple[str, str]]:
  return [(f['rule'], f['location']) for f in check_layout(load_project(project))]


def git(project: Path, *args: str):
  executable = shutil.which('git')
  if executable is None:
    pytest.skip('git is not installed')
  subprocess.run([executable, *args], cwd=project, check=True, capture_output=True)


def test_a_project_init_wrote_has_no_layout_finding(project: Path):
  assert findings(project) == []


def test_the_command_passes_on_it(project: Path):
  result = CliRunner().invoke(app, ['standards', '--project', str(project), '--only', 'layout'])
  assert result.exit_code == 0, result.output


def test_deleting_claude_md_fails_the_command_naming_a8(project: Path):
  (project / 'CLAUDE.md').unlink()
  result = CliRunner().invoke(app, ['standards', '--project', str(project), '--only', 'layout'])
  assert result.exit_code == 1
  assert '[error] A8  CLAUDE.md' in result.output


def test_no_agents_md_is_a7(project: Path):
  (project / 'AGENTS.md').unlink()
  assert findings(project) == [('A7', 'AGENTS.md')]


@pytest.mark.parametrize('text', ['@AGENTS.md\nAlso read README.md\n', '@README.md\n', '', '@AGENTS.md\n\n\n'])
def test_claude_md_that_is_not_exactly_the_redirect_is_a8(project: Path, text: str):
  (project / 'CLAUDE.md').write_text(text)
  assert findings(project) == [('A8', 'CLAUDE.md')]


def test_a_utf16_claude_md_is_a8_not_a_crash(project: Path):
  """Windows PowerShell 5 `echo @AGENTS.md > CLAUDE.md` writes UTF-16LE with a BOM."""
  (project / 'CLAUDE.md').write_bytes('@AGENTS.md\r\n'.encode('utf-16'))
  assert findings(project) == [('A8', 'CLAUDE.md')]


def test_claude_md_without_a_final_newline_is_fine(project: Path):
  (project / 'CLAUDE.md').write_text('@AGENTS.md')
  assert findings(project) == []


def test_a_claude_directory_instead_of_a_link_is_a9(project: Path):
  (project / '.claude' / 'rules').unlink()
  (project / '.claude' / 'rules').mkdir()
  assert findings(project) == [('A9', '.claude/rules')]


def test_a_link_elsewhere_is_a9(project: Path):
  (project / '.claude' / 'skills').unlink()
  (project / '.claude' / 'skills').symlink_to('../docs')
  assert findings(project) == [('A9', '.claude/skills')]


def test_a_symlink_loop_is_a9_not_a_crash(project: Path):
  (project / '.claude' / 'skills').unlink()
  (project / '.claude' / 'skills').symlink_to('skills')
  assert findings(project) == [('A9', '.claude/skills')]


def test_an_absolute_link_is_a9(project: Path):
  """It resolves here and nowhere else: every other clone has another path."""
  (project / '.claude' / 'skills').unlink()
  (project / '.claude' / 'skills').symlink_to(project / '.agents' / 'skills')
  assert findings(project) == [('A9', '.claude/skills')]


def test_a_dangling_link_is_a9(project: Path):
  shutil.rmtree(project / '.agents' / 'rules')
  assert findings(project) == [('A9', '.claude/rules')]


def test_no_claude_links_is_a9_twice(project: Path):
  shutil.rmtree(project / '.claude')
  assert findings(project) == [('A9', '.claude/skills'), ('A9', '.claude/rules')]


def test_no_schema_line_is_w13(project: Path):
  toml = project / 'truewire.toml'
  toml.write_text(toml.read_text().split('\n', 1)[1])
  assert findings(project) == [('W13', 'truewire.toml:1')]


def test_no_schema_key_in_docs_yml_is_w10(project: Path):
  docs = project / 'docs' / 'docs.yml'
  docs.write_text(docs.read_text().split('\n', 1)[1])
  assert findings(project) == [('W10', 'docs/docs.yml')]


def test_a_schema_line_naming_another_schema_is_w13(project: Path):
  toml = project / 'truewire.toml'
  toml.write_text('#:schema ./nope.json\n' + toml.read_text().split('\n', 1)[1])
  assert findings(project) == [('W13', 'truewire.toml:1')]


def test_a_schema_key_naming_another_schema_is_w10(project: Path):
  docs = project / 'docs' / 'docs.yml'
  docs.write_text('$schema: http://nope.invalid/docs.json\n' + docs.read_text().split('\n', 1)[1])
  assert findings(project) == [('W10', 'docs/docs.yml')]


def test_no_docs_yml_is_w10(project: Path):
  (project / 'docs' / 'docs.yml').unlink()
  assert findings(project) == [('W10', 'docs/docs.yml')]


def test_an_ignored_manifest_is_w16(project: Path):
  git(project, 'init', '-q')
  with (project / '.gitignore').open('a') as file:
    file.write('.truewire/\n')
  assert findings(project) == [('W16', '.truewire/codegen/python.json')]


def test_a_tracked_manifest_under_an_ignore_line_is_committed(project: Path):
  git(project, 'init', '-q')
  manifest = project / '.truewire' / 'codegen' / 'python.json'
  manifest.write_text('{"version": 1, "files": []}\n')
  git(project, 'add', 'truewire.toml', str(manifest))
  with (project / '.gitignore').open('a') as file:
    file.write('.truewire/\n')
  assert findings(project) == []


def test_a_manifest_at_its_pre_w16_path_is_w16(project: Path):
  (project / '.truewire' / 'python-files.json').write_text('{"version": 1, "files": []}\n')
  assert findings(project) == [('W16', '.truewire/python-files.json')]


def test_a_missing_manifest_before_codegen_has_run_is_fine(project: Path):
  """`init` wrote only the hand-written core and `meta.py`, which carries codegen's banner."""
  assert not (project / '.truewire' / 'codegen' / 'python.json').exists()
  assert findings(project) == []


def test_a_missing_manifest_after_codegen_is_w16(tmp_path: Path):
  """`generate --check` lets the plan stand in for a missing manifest, so S4 passes: W16
  is what catches a manifest that was never committed."""
  project = finish(fixture_project(tmp_path))
  assert findings(project) == []
  (project / '.truewire' / 'codegen' / 'python.json').unlink()
  assert score_rows.generated(load_project(project)).status == 'pass'
  assert findings(project) == [('W16', '.truewire/codegen/python.json')]


def test_an_unignored_cache_is_w16(project: Path):
  git(project, 'init', '-q')
  gitignore = project / '.gitignore'
  gitignore.write_text(gitignore.read_text().replace('.truewire/cache/\n', ''))
  assert findings(project) == [('W16', '.truewire/cache/')]


def test_git_failing_inside_a_repository_is_w16_not_a_pass(project: Path, monkeypatch: pytest.MonkeyPatch):
  git(project, 'init', '-q')
  with (project / '.gitignore').open('a') as file:
    file.write('.truewire/\n')
  monkeypatch.setenv('GIT_DIR', str(project / 'no-such-git-dir'))
  monkeypatch.setenv('GIT_WORK_TREE', str(project))
  assert ('W16', '.truewire/codegen/python.json') in findings(project)


def test_init_without_symlinks_still_ignores_env_and_says_so(tmp_path: Path):
  """No symlink privilege (Windows without developer mode): `init` still finishes."""
  with mock.patch.object(Path, 'symlink_to', side_effect=OSError(1, 'A required privilege is not held')):
    result = CliRunner().invoke(app, ['init', 'demo', '--dir', str(tmp_path / 'demo')])
  assert result.exit_code == 0, result.output
  assert '.env' in (tmp_path / 'demo' / '.gitignore').read_text()
  assert 'Could not link .claude/skills' in result.output
  assert findings(tmp_path / 'demo') == [('A9', '.claude/skills'), ('A9', '.claude/rules')]


def test_the_module_imports_on_its_own():
  """`truewire.cli` imports it, so it must not need `truewire.cli` loaded first."""
  result = subprocess.run([sys.executable, '-c', 'import truewire.standards.layout'], capture_output=True, text=True)
  assert result.returncode == 0, result.stderr
