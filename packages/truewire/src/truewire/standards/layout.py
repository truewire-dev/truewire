"""
The layout `truewire init` writes (`docs/shape/agents.md`, `docs/shape/workspace.md`), one
finding per clause a project breaks, each named by its clause id:

- A7: `AGENTS.md` exists. A8: `CLAUDE.md` is exactly the one line `@AGENTS.md`. A9:
  `.claude/skills` and `.claude/rules` are relative symlinks to `.agents/skills` and
  `.agents/rules`, which exist.
- W10: `docs/docs.yml` has a `$schema` key naming the published schema. W13: `truewire.toml`
  starts with a `#:schema` comment naming it.
- W16: each declared language's `.truewire/codegen/<language>.json` is not git-ignored, is
  not still at its pre-W16 path, and exists once codegen has written that language's
  package; `.truewire/cache/` is git-ignored. Before codegen has run a missing manifest is
  expected, so it is no finding. Once it has, `generate --check` does not notice a missing
  manifest (it lets the plan stand in for it), so this is where that is caught.

Every finding is `error`-severity. `truewire init` writes a missing file and narrows a
`.gitignore` that ignores all of `.truewire/`, but never rewrites a file that is there, so a
wrong `CLAUDE.md`, link or schema reference has to be fixed by hand.
"""
import os
import shutil
import subprocess
from pathlib import Path

import yaml

from truewire.codegen.go.printer import BANNER as GO_BANNER
from truewire.codegen.meta import META_MODULE
from truewire.codegen.rust.printer import BANNER as RUST_BANNER
from truewire.codegen.typescript.printer import BANNER as TYPESCRIPT_BANNER
from truewire.project import PROJECT_FILE, NotAProject, Project
from truewire.score.rows import declared_languages
from truewire.skeleton import AGENT_LINKS, DOCS_SCHEMA_URL, SCHEMA_URL
from truewire.standards.finding import Finding

def banner(language: str) -> str:
  """The first line of every file codegen writes for `language`."""
  from truewire.cli.generate import GENERATED_BANNER  # `truewire.cli` imports this module

  return {
    'python': GENERATED_BANNER.rstrip('\n'), 'typescript': TYPESCRIPT_BANNER,
    'rust': RUST_BANNER, 'go': GO_BANNER,
  }[language]

SCHEMA_PREFIX = SCHEMA_URL.rsplit('/', 1)[0] + '/'
"""Where the published schemas live; W10 and W13 reference one of them."""

CLAUDE_MD = frozenset({'@AGENTS.md', '@AGENTS.md\n', '@AGENTS.md\r\n'})
"""`CLAUDE.md` as A8 allows it: the one line, with or without its line ending."""


def _finding(rule: str, location: str, message: str) -> Finding:
  return Finding(rule=rule, location=location, message=message, severity='error')


def read_utf8(path: Path) -> str | None:
  """`path`'s text, or None when it is not UTF-8."""
  try:
    return path.read_bytes().decode('utf-8')
  except UnicodeDecodeError:
    return None


def check_agents(root: Path) -> list[Finding]:
  """A7, A8 and A9: the agent entrypoint, its one-line alias, and the `.claude/` links."""
  findings: list[Finding] = []
  if not (root / 'AGENTS.md').is_file():
    findings.append(_finding('A7', 'AGENTS.md', 'missing; it is the entrypoint an agent reads'))
  claude = root / 'CLAUDE.md'
  if not claude.is_file():
    findings.append(_finding('A8', 'CLAUDE.md', 'missing; it must be the one line `@AGENTS.md`'))
  elif read_utf8(claude) not in CLAUDE_MD:
    findings.append(_finding('A8', 'CLAUDE.md', 'must be exactly the one line `@AGENTS.md`, in UTF-8'))
  for relative, target in AGENT_LINKS.items():
    link = root / relative
    expected = os.path.normpath(link.parent / target)
    if not link.is_symlink():
      state = 'not a symlink' if os.path.lexists(link) else 'missing'
      findings.append(_finding('A9', relative, f'{state}; it must be a symlink to {target}'))
      continue
    points_at = os.readlink(link)
    if os.path.isabs(points_at) or os.path.normpath(link.parent / points_at) != expected:
      findings.append(_finding('A9', relative, f'links to {points_at}, not to {target}'))
    elif not Path(expected).is_dir():
      findings.append(_finding('A9', relative, f'links to {target}, which is not a directory'))
  return findings


def check_schemas(root: Path) -> list[Finding]:
  """W13 and W10: the published schemas the two config files are validated by."""
  findings: list[Finding] = []
  first = (read_utf8(root / PROJECT_FILE) or '').split('\n', 1)[0].rstrip('\r')
  if not first.startswith(f'#:schema {SCHEMA_PREFIX}'):
    findings.append(_finding('W13', f'{PROJECT_FILE}:1', f'the first line must be `#:schema {SCHEMA_URL}`'))
  docs = root / 'docs' / 'docs.yml'
  if not docs.is_file():
    findings.append(_finding('W10', 'docs/docs.yml', 'missing; it carries the nav and a `$schema` key'))
    return findings
  text = read_utf8(docs)
  if text is None:
    findings.append(_finding('W10', 'docs/docs.yml', 'not UTF-8'))
    return findings
  try:
    data = yaml.safe_load(text)
  except yaml.YAMLError as error:
    findings.append(_finding('W10', 'docs/docs.yml', f'not YAML: {error}'))
    return findings
  schema = data.get('$schema') if isinstance(data, dict) else None
  if not isinstance(schema, str) or not schema.startswith(SCHEMA_PREFIX):
    findings.append(_finding('W10', 'docs/docs.yml', f'needs `$schema: {DOCS_SCHEMA_URL}`'))
  return findings


def ignored_by_git(root: Path, relative: str) -> bool | str | None:
  """Whether git ignores `relative` under `root`; None outside a git work tree (no `.git`
  in `root` or above) or with no git installed, where nothing is ignored; git's own error
  when it could not say inside one. A tracked file is never ignored."""
  git = shutil.which('git')
  if git is None:
    return None
  process = subprocess.run([git, 'check-ignore', '-q', relative], cwd=root, capture_output=True, text=True)
  if process.returncode in (0, 1):
    return process.returncode == 0
  if not any((directory / '.git').exists() for directory in (root, *root.parents)):
    return None
  return process.stderr.strip() or f'`git check-ignore` exited {process.returncode}'


def generated_into(directory: Path, banner: str, *, besides: Path | None = None) -> bool:
  """Whether any file under `directory` but `besides` starts with codegen's `banner`."""
  for parent, _, names in os.walk(directory):
    for name in names:
      path = Path(parent) / name
      if path == besides:
        continue
      try:
        with path.open('rb') as file:
          first = file.readline().decode('utf-8', errors='replace').rstrip('\r\n')
      except OSError:
        continue
      if first == banner:
        return True
  return False


def output_dir(project: Project, language: str) -> Path:
  """Where codegen writes `language`'s package."""
  return {
    'python': lambda: project.package_dir,
    'typescript': lambda: project.typescript_package_dir,
    'rust': lambda: project.rust_package_dir,
    'go': lambda: project.go_package_dir,
  }[language]()


def check_manifests(project: Project) -> list[Finding]:
  """W16: each declared language's codegen manifest is committed at its W16 path, and the
  cache beside it is not."""
  findings: list[Finding] = []
  root = project.root
  for language in declared_languages(project):
    manifest = project.manifest_path(language)
    relative = manifest.relative_to(root).as_posix()
    ignored = ignored_by_git(root, relative)
    if isinstance(ignored, str):
      findings.append(_finding('W16', relative, f'could not ask git whether it is ignored: {ignored}'))
    elif ignored:
      findings.append(_finding('W16', relative, 'git-ignored; the codegen manifest is committed'))
    legacy = project.legacy_manifest_path(language)
    if legacy.exists():
      findings.append(_finding(
        'W16', legacy.relative_to(root).as_posix(),
        f'the manifest is still at its old path; `truewire generate {language}` moves it to {relative}',
      ))
      continue
    if manifest.exists():
      continue
    try:
      directory = output_dir(project, language)
    except NotAProject:
      continue
    meta = directory / f'{META_MODULE}.py' if language == 'python' else None
    if generated_into(directory, banner(language), besides=meta):
      findings.append(_finding(
        'W16', relative, f'missing, though codegen wrote {directory.relative_to(root).as_posix()}; '
        f'`truewire generate {language}` writes it, then commit it',
      ))
  if ignored_by_git(root, '.truewire/cache/x') is False:
    findings.append(_finding('W16', '.truewire/cache/', 'not git-ignored; it holds fetched schemas'))
  return findings


def check_layout(project: Project) -> list[Finding]:
  """Every layout clause `project` breaks, as one finding each."""
  return [*check_agents(project.root), *check_schemas(project.root), *check_manifests(project)]
