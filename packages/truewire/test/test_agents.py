"""The skills and rules a project vendors from the toolchain (`docs/shape/agents.md`, A10, A11):
`truewire init` writes them, `truewire agents update` rewrites them, and `truewire agents
check` (and so `truewire standards`) fails on silent drift."""
from pathlib import Path

import pytest
from typer.testing import CliRunner

from truewire import agents
from truewire.cli import app
from truewire.lint.run import LANGUAGES

runner = CliRunner()

SKILLS = ('core', 'discover', 'docs', 'implement', 'review', 'spec')


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
  monkeypatch.chdir(tmp_path)
  assert runner.invoke(app, ['init', 'x']).exit_code == 0
  return tmp_path / 'x'


def agents_command(*argv: str, root: Path):
  return runner.invoke(app, ['agents', *argv, '--project', str(root)])


def test_the_package_ships_six_skills_and_their_readme():
  assert sorted(path.parent.name for path in (agents.RESOURCES / 'skills').glob('*/SKILL.md')) == list(SKILLS)
  assert (agents.RESOURCES / 'skills' / 'README.md').is_file()


def test_the_package_ships_rules_for_every_language():
  assert sorted(path.stem for path in (agents.RESOURCES / 'rules').glob('*.md')) == sorted(LANGUAGES)


def test_the_repository_skills_are_the_packaged_ones():
  """`.agents/skills` at the repository root is a link to the package data, not a second copy."""
  repository = Path(__file__).resolve().parents[3]
  assert (repository / '.agents' / 'skills').resolve() == agents.RESOURCES / 'skills'


def test_init_vendors_every_skill_with_the_toolchain_version(project: Path):
  toolchain = agents.installed_version()
  for skill in SKILLS:
    shipped = (agents.RESOURCES / 'skills' / skill / 'SKILL.md').read_text()
    text = (project / '.agents' / 'skills' / skill / 'SKILL.md').read_text()
    assert text.startswith('---\nname: ')
    front_matter_end = text.index('\n---\n', 3) + len('\n---\n')
    assert text[front_matter_end:].startswith(agents.stamp_line(toolchain, shipped) + '\n')
    assert agents.unstamped(text) == (agents.Stamp(toolchain, agents.digest(shipped)), shipped)
  readme = (agents.RESOURCES / 'skills' / 'README.md').read_text()
  assert (project / '.agents' / 'skills' / 'README.md').read_text().startswith(agents.stamp_line(toolchain, readme) + '\n')
  assert not (project / '.agents' / 'skills' / '.gitkeep').exists()


def test_a_fresh_project_checks_clean(project: Path):
  result = agents_command('check', root=project)
  assert result.exit_code == 0, result.output
  assert 'OK: 8 vendored file(s)' in result.output
  assert sorted(path.name for path in (project / '.agents' / 'rules').glob('*.md')) == ['python.md']


def test_update_vendors_the_rules_of_every_declared_language(project: Path):
  with (project / 'truewire.toml').open('a') as config:
    config.write('\n[typescript]\npackage = "x"\nsrc = "src"\n')
    config.write('\n[rust]\npackage = "x"\nsrc = "src"\n')
    config.write('\n[go]\npackage = "x"\nsrc = "src"\nmodule = "example.com/x"\n')
  assert agents_command('check', root=project).exit_code == 1
  result = agents_command('update', root=project)
  assert result.exit_code == 0, result.output
  for language in LANGUAGES:
    shipped = (agents.RESOURCES / 'rules' / f'{language}.md').read_text()
    assert (project / '.agents' / 'rules' / f'{language}.md').read_text().endswith('\n' + shipped)
  assert agents_command('check', root=project).exit_code == 0


def test_an_edited_line_fails_check_naming_the_file(project: Path):
  skill = project / '.agents' / 'skills' / 'spec' / 'SKILL.md'
  skill.write_text(skill.read_text().replace('## Goal', '## Goal, edited', 1))
  result = agents_command('check', root=project)
  assert result.exit_code == 1
  assert 'edited   .agents/skills/spec/SKILL.md' in result.output


def test_update_restores_an_edited_copy(project: Path):
  skill = project / '.agents' / 'skills' / 'spec' / 'SKILL.md'
  original = skill.read_text()
  skill.write_text(original.replace('## Goal', '## Goal, edited', 1))
  result = agents_command('update', root=project)
  assert result.exit_code == 0
  assert 'overwrote .agents/skills/spec/SKILL.md  (edited since it was written' in result.output
  assert skill.read_text() == original
  assert agents_command('check', root=project).exit_code == 0


def test_a_missing_copy_fails_check_and_update_writes_it(project: Path):
  (project / '.agents' / 'skills' / 'docs' / 'SKILL.md').unlink()
  result = agents_command('check', root=project)
  assert result.exit_code == 1
  assert 'missing  .agents/skills/docs/SKILL.md' in result.output
  agents_command('update', root=project)
  assert agents_command('check', root=project).exit_code == 0


def test_a_marked_local_edit_passes_and_update_keeps_it(project: Path):
  skill = project / '.agents' / 'skills' / 'core' / 'SKILL.md'
  edited = skill.read_text().replace('## Goal', '## Goal, ours', 1) + agents.LOCAL_MARKER + '\n'
  skill.write_text(edited)
  result = agents_command('check', root=project)
  assert result.exit_code == 0, result.output
  assert 'local    .agents/skills/core/SKILL.md' in result.output
  assert 'kept     .agents/skills/core/SKILL.md' in agents_command('update', root=project).output
  assert skill.read_text() == edited
  agents_command('update', '--force', root=project)
  assert agents.LOCAL_MARKER not in skill.read_text()


def test_the_marker_counts_only_as_a_line_of_its_own():
  assert agents.is_local(f'text\n{agents.LOCAL_MARKER}\n')
  assert agents.is_local(f'text\n{agents.LOCAL_MARKER} \r\n')
  assert not agents.is_local(f'add `{agents.LOCAL_MARKER}` to the file\n')
  assert not agents.is_local(f'    {agents.LOCAL_MARKER}\n')


def test_the_shipped_readme_does_not_mark_itself_local():
  assert not agents.is_local((agents.RESOURCES / 'skills' / 'README.md').read_text())


SHIPPED_REVIEW = agents.RESOURCES / 'skills' / 'review' / 'SKILL.md'


def test_a_copy_from_an_older_toolchain_with_the_same_text_passes(project: Path):
  skill = project / '.agents' / 'skills' / 'review' / 'SKILL.md'
  skill.write_text(agents.stamped(SHIPPED_REVIEW.read_text(), '0.0.1'))
  assert agents_command('check', root=project).exit_code == 0
  agents_command('update', root=project)
  assert skill.read_text() == agents.stamped(SHIPPED_REVIEW.read_text(), agents.installed_version())


def test_an_untouched_copy_an_older_toolchain_wrote_differently_is_stale(project: Path):
  skill = project / '.agents' / 'skills' / 'review' / 'SKILL.md'
  skill.write_text(agents.stamped(SHIPPED_REVIEW.read_text() + 'older text\n', '0.0.1'))
  result = agents_command('check', root=project)
  assert result.exit_code == 1
  assert 'stale    .agents/skills/review/SKILL.md  (vendored from truewire 0.0.1' in result.output
  agents_command('update', root=project)
  assert agents_command('check', root=project).exit_code == 0


def test_an_edited_copy_from_an_older_toolchain_is_edited_not_stale(project: Path):
  skill = project / '.agents' / 'skills' / 'review' / 'SKILL.md'
  skill.write_text(agents.stamped(SHIPPED_REVIEW.read_text(), '0.0.1') + 'our line\n')
  result = agents_command('check', root=project)
  assert result.exit_code == 1
  assert 'edited   .agents/skills/review/SKILL.md  (changed since truewire 0.0.1 wrote it)' in result.output


def test_a_retired_skill_fails_check_and_update_removes_it(project: Path):
  retired = project / '.agents' / 'skills' / 'old' / 'SKILL.md'
  retired.parent.mkdir()
  retired.write_text((project / '.agents' / 'skills' / 'core' / 'SKILL.md').read_text())
  result = agents_command('check', root=project)
  assert result.exit_code == 1
  assert 'retired  .agents/skills/old/SKILL.md' in result.output
  assert 'removed  .agents/skills/old/SKILL.md' in agents_command('update', root=project).output
  assert not retired.parent.exists()


def test_a_skill_of_the_projects_own_is_left_alone(project: Path):
  own = project / '.agents' / 'skills' / 'mine' / 'SKILL.md'
  own.parent.mkdir()
  own.write_text('---\nname: mine\ndescription: ours\n---\n')
  assert agents_command('check', root=project).exit_code == 0
  agents_command('update', root=project)
  assert own.read_text() == '---\nname: mine\ndescription: ours\n---\n'


def test_rules_are_vendored_for_declared_languages_only(project: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
  resources = tmp_path / 'resources'
  (resources / 'rules').mkdir(parents=True)
  (resources / 'skills').symlink_to(agents.RESOURCES / 'skills')
  (resources / 'rules' / 'python.md').write_text('# Python rules\n')
  (resources / 'rules' / 'go.md').write_text('# Go rules\n')
  monkeypatch.setattr(agents, 'RESOURCES', resources)
  (project / '.agents' / 'rules' / 'python.md').unlink()
  result = agents_command('check', root=project)
  assert result.exit_code == 1
  assert 'missing  .agents/rules/python.md' in result.output
  assert 'go.md' not in result.output
  agents_command('update', root=project)
  assert (project / '.agents' / 'rules' / 'python.md').read_text().endswith('\n# Python rules\n')
  assert not (project / '.agents' / 'rules' / 'go.md').exists()


def test_init_over_an_existing_project_keeps_an_edited_copy(project: Path, monkeypatch: pytest.MonkeyPatch):
  skill = project / '.agents' / 'skills' / 'spec' / 'SKILL.md'
  skill.write_text('edited\n')
  (project / '.agents' / 'skills' / 'docs' / 'SKILL.md').unlink()
  monkeypatch.chdir(project)
  result = runner.invoke(app, ['init', 'x'])
  assert result.exit_code == 0, result.output
  assert '.agents/skills/docs/SKILL.md' in result.output
  assert skill.read_text() == 'edited\n'


def test_standards_fails_on_a_drifted_copy_naming_it(project: Path):
  skill = project / '.agents' / 'skills' / 'spec' / 'SKILL.md'
  skill.write_text(skill.read_text().replace('## Goal', '## Goal, edited', 1))
  result = runner.invoke(app, ['standards', '--only', 'agents', '--project', str(project)])
  assert result.exit_code == 1
  assert '.agents/skills/spec/SKILL.md' in result.output


# From the Python review of PR #19 (TRU-157).

STAMP_EXAMPLE = agents.stamp_line('0.11.0', 'x')


def test_update_never_deletes_a_skill_the_project_wrote(project: Path):
  """The project's own skill quotes the stamp line in a code block. The toolchain never
  wrote it, so `agents update` must not remove it (README: "never touched")."""
  own = project / '.agents' / 'skills' / 'vendoring' / 'SKILL.md'
  own.parent.mkdir()
  text = f'---\nname: vendoring\ndescription: ours\n---\nEach copy carries:\n\n```\n{STAMP_EXAMPLE}\n```\n'
  own.write_text(text)
  agents_command('update', root=project)
  assert own.read_text() == text


def test_update_keeps_an_edited_fork_of_a_vendored_skill(project: Path):
  """A skill copied from a vendored one and then rewritten keeps the copied stamp line.
  Removing it deletes the project's work; at most `check` should flag it."""
  fork = project / '.agents' / 'skills' / 'our-review' / 'SKILL.md'
  fork.parent.mkdir()
  source = (project / '.agents' / 'skills' / 'review' / 'SKILL.md').read_text()
  text = source.replace('name: truewire-review', 'name: our-review').replace('## ', '## (ours) ')
  fork.write_text(text)
  agents_command('update', root=project)
  assert fork.read_text() == text


def test_update_does_not_write_through_a_linked_skills_directory(project: Path, tmp_path: Path):
  """`write_copy` says nothing is written through a link to outside the project."""
  shared = tmp_path / 'shared'
  (project / '.agents' / 'skills').rename(shared)
  (project / '.agents' / 'skills').symlink_to(shared)
  (shared / 'docs' / 'SKILL.md').unlink()
  agents_command('update', root=project)
  assert not (shared / 'docs' / 'SKILL.md').exists()


def test_an_edited_fork_fails_check_as_orphaned_and_update_says_it_kept_it(project: Path):
  fork = project / '.agents' / 'skills' / 'our-review' / 'SKILL.md'
  fork.parent.mkdir()
  fork.write_text((project / '.agents' / 'skills' / 'review' / 'SKILL.md').read_text() + 'ours\n')
  result = agents_command('check', root=project)
  assert result.exit_code == 1
  assert 'orphaned .agents/skills/our-review/SKILL.md' in result.output
  assert 'kept     .agents/skills/our-review/SKILL.md' in agents_command('update', root=project).output


def test_a_stamp_quoted_where_no_stamp_goes_is_text():
  text = f'---\nname: x\ndescription: y\n---\n# Title\n{STAMP_EXAMPLE}\n'
  assert agents.unstamped(text) == (None, text)


def test_a_linked_skills_directory_outside_the_project_is_refused(project: Path, tmp_path: Path):
  shared = tmp_path / 'shared'
  (project / '.agents' / 'skills').rename(shared)
  (project / '.agents' / 'skills').symlink_to(shared)
  (shared / 'docs' / 'SKILL.md').unlink()
  result = agents_command('update', root=project)
  assert result.exit_code == 1
  assert 'refused  .agents/skills/docs/SKILL.md' in result.output


def test_a_dangling_skills_link_neither_crashes_init_nor_update(project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
  skills = project / '.agents' / 'skills'
  for skill in skills.glob('*/SKILL.md'):
    skill.unlink()
    skill.parent.rmdir()
  (skills / 'README.md').unlink()
  skills.rmdir()
  skills.symlink_to(tmp_path / 'nowhere')
  monkeypatch.chdir(project)
  result = runner.invoke(app, ['init', 'x'])
  assert result.exit_code == 0, result.output
  assert 'Not written: .agents/skills/core/SKILL.md' in result.output
  assert agents_command('update', root=project).exit_code == 1
  assert not (tmp_path / 'nowhere').exists()


# From the Python delta review of PR #19 (TRU-162).

def test_update_over_a_current_shared_skills_link_changes_nothing_and_succeeds(project: Path, tmp_path: Path):
  shared = tmp_path / 'shared'
  (project / '.agents' / 'skills').rename(shared)
  (project / '.agents' / 'skills').symlink_to(shared)
  assert agents_command('check', root=project).exit_code == 0
  result = agents_command('update', root=project)
  assert 'refused' not in result.output, result.output
  assert result.exit_code == 0, result.output


def test_update_says_when_it_overwrites_an_edit(project: Path):
  skill = project / '.agents' / 'skills' / 'core' / 'SKILL.md'
  skill.write_text(skill.read_text() + '\nEvery call needs the header X-Foo.\n')
  assert 'edited' in agents_command('check', root=project).output
  result = agents_command('update', root=project)
  assert 'X-Foo' not in skill.read_text()
  assert 'overwrote .agents/skills/core/SKILL.md' in result.output, result.output
  assert '(1 over an edit)' in result.output


# From the second Python delta review of PR #19 (TRU-166, TRU-168).

def test_update_over_a_shared_link_after_a_toolchain_upgrade_that_changed_nothing_succeeds(project: Path, tmp_path: Path):
  shared = tmp_path / 'shared'
  (project / '.agents' / 'skills').rename(shared)
  (project / '.agents' / 'skills').symlink_to(shared)
  for relative, text in agents.shipped(()).items():
    if relative.startswith('.agents/skills/'):
      (project / relative).write_text(agents.stamped(text, '0.0.1'))
  before = {path: path.read_bytes() for path in shared.rglob('*') if path.is_file()}
  assert agents_command('check', root=project).exit_code == 0
  result = agents_command('update', root=project)
  assert 'refused' not in result.output, result.output
  assert result.exit_code == 0, result.output
  assert {path: path.read_bytes() for path in shared.rglob('*') if path.is_file()} == before


def test_a_stale_copy_behind_a_shared_link_is_still_refused(project: Path, tmp_path: Path):
  shared = tmp_path / 'shared'
  (project / '.agents' / 'skills').rename(shared)
  (project / '.agents' / 'skills').symlink_to(shared)
  skill = shared / 'docs' / 'SKILL.md'
  older = agents.stamped(agents.shipped(())['.agents/skills/docs/SKILL.md'] + 'An older line.\n', '0.0.1')
  skill.write_text(older)
  assert 'stale' in agents_command('check', root=project).output
  result = agents_command('update', root=project)
  assert result.exit_code == 1
  assert 'refused  .agents/skills/docs/SKILL.md' in result.output
  assert skill.read_text() == older
