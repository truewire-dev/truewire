"""The workspace skeleton `truewire init` lays down (`docs/shape/workspace.md`).

Every file here is written only where none exists, so running `init` again over a workspace
fills in what is missing and changes nothing else. An empty directory the shape names gets a
`.gitkeep`, so that git keeps it and a fresh clone has the same tree.
"""
import os
from pathlib import Path
import re

from truewire.schemas import SCHEMA_BASE

SCHEMA_URL = SCHEMA_BASE + 'truewire.toml.json'
"""The published `truewire.toml` schema, named on the file's first line for editors (W13)."""

DOCS_SCHEMA_URL = SCHEMA_BASE + 'docs.yml.json'
"""The published `docs/docs.yml` schema, named by its `$schema` key (W10)."""

GITIGNORE_LINES = ['.env', '.truewire/cache/', '.venv/', '__pycache__/']
"""What a workspace ignores: local credentials, fetched schemas and scratch, the environment
and bytecode. Not the rest of `.truewire/`: the codegen manifest is committed (W16)."""

KEPT_DIRECTORIES = ('packages', '.agents/rules', 'dev/capture', '.truewire/codegen')
"""Directories that start out holding nothing of their own, each kept by a `.gitkeep`.
`.agents/skills/` is not one: `init` vendors the skills into it (`truewire.agents`)."""

AGENT_LINKS = {'.claude/skills': '../.agents/skills', '.claude/rules': '../.agents/rules'}
"""The symlinks into `.agents/` a Claude Code runner reads (A9), by path relative to the
root, each to its target relative to the link."""

STUB_MARKER = '<!-- truewire init: a stub; replace this page -->'
"""The line every docs page `init` writes carries until someone writes the page. `truewire
score` counts a page that still carries it as missing (S9): a stub is not documentation."""

AGENTS_MD = '''# {class_name}

A typed client for the {class_name} API, built with [Truewire](https://truewire.dev). This
file is the entrypoint an agent reads; `CLAUDE.md` points here.

## Layout

- `spec/`: the specification. One `endpoint.json` per endpoint, recordings in `examples/`
  beside it. The spec is the product; everything else is rendered from it or describes it.
- `packages/`: one package per language, each generated from the spec over a hand-written core.
- `docs/`: the documentation site. `docs.yml` is its nav and quickstart.
- `dev/capture/`: how each recording was produced; never a credential.
- `.agents/skills/`, `.agents/rules/`: the skills to follow and the rules per language.
  `.claude/skills` and `.claude/rules` are symlinks to them.
- `truewire.toml`: the one project file.

## Credentials

`[secrets].required` in `truewire.toml` names the environment variables the client reads.
Their values go in `.env`, which is git-ignored, and nowhere else.

## Gates

Run these before pushing, in this order; CI runs the same ones.

```
truewire check
truewire examples
truewire surface
truewire standards
truewire docs check
```
'''

DOCS_YML = '''$schema: {docs_schema}
title: {class_name}
nav:
  - index.md
  - api-keys.md
  - how-to/index.md
  - reference/index.md
quickstart:{quickstart}
'''

QUICKSTART_STUB = 'truewire init: a stub; replace this block with the first call'
"""The code of each quickstart block `init` writes, as a comment in the block's language."""

COMMENT = {'python': '#', 'typescript': '//', 'rust': '//', 'go': '//'}
"""The line comment of each language a quickstart block may be in."""


def quickstart_yaml(languages: tuple[str, ...]) -> str:
  """The `quickstart:` value of `docs.yml` as `init` writes it: one stub block per language
  `truewire.toml` declares (W10), or an empty mapping when it declares none."""
  if not languages:
    return ' {}'
  return ''.join(
    f'\n  {language}:\n    code: |\n      {COMMENT[language]} {QUICKSTART_STUB}' for language in languages
  )

INDEX_MD = '''# {class_name}

{stub}

What the {class_name} API is, and why a caller wants a validated client for it.
'''

API_KEYS_MD = '''# API keys

{stub}

Where a caller gets a key for the {class_name} API, which permissions to grant it, and which
to refuse.
'''

HOW_TO_MD = '''# How-to

{stub}

Task-shaped pages: one page per thing a caller sets out to do.
'''

REFERENCE_MD = '''# Reference

{stub}

Surface-shaped pages, generated from the spec.
'''


def project_toml(name: str, rest: str) -> str:
  """`truewire.toml` as `init` writes it: the schema line, `[project]`, `[spec]`,
  `[secrets]` and `[policy]` (W13), then `rest` (the cores and language blocks)."""
  return f'''#:schema {SCHEMA_URL}
[project]
name = "{name}"

[spec]
dir = "spec"

[secrets]
required = []

[policy]
# rate = 10               # requests per second the client paces itself to; unpaced when absent
retry = false
refuse = []               # endpoint ids the client refuses to call, e.g. "account.withdraw"

{rest}'''


def skeleton_files(class_name: str, languages: tuple[str, ...] = ()) -> dict[str, str]:
  """Every file of the skeleton but `truewire.toml` and `.gitignore`, by path relative to the
  root; `docs/docs.yml` has a quickstart block for each of `languages`."""
  fill = dict(
    class_name=class_name, docs_schema=DOCS_SCHEMA_URL, stub=STUB_MARKER, quickstart=quickstart_yaml(languages),
  )
  files = {
    'AGENTS.md': AGENTS_MD.format(**fill),
    'CLAUDE.md': '@AGENTS.md\n',
    'docs/docs.yml': DOCS_YML.format(**fill),
    'docs/index.md': INDEX_MD.format(**fill),
    'docs/api-keys.md': API_KEYS_MD.format(**fill),
    'docs/how-to/index.md': HOW_TO_MD.format(**fill),
    'docs/reference/index.md': REFERENCE_MD.format(**fill),
  }
  files.update({f'{directory}/.gitkeep': '' for directory in KEPT_DIRECTORIES})
  return files


def write_new(root: Path, relative: str, text: str) -> bool:
  """Write `root / relative` unless something is there already; whether it wrote.

  The file is created exclusively, so a symlink in its place, dangling or not, counts as
  something there: `init` never writes through a link to outside the workspace.
  """
  target = root / relative
  target.parent.mkdir(parents=True, exist_ok=True)
  try:
    with target.open('x') as file:
      file.write(text)
  except FileExistsError:
    return False
  return True


def add_schema_line(file: Path) -> bool:
  """Put `#:schema SCHEMA_URL` on line 1 of `file`, an existing `truewire.toml`, when the
  file names no schema; whether it did (W13).

  The line is prepended in the file's own line ending and every other byte stays as it was.
  A file with a `#:schema` line anywhere, whatever its URL, is left alone: a second one
  would contradict it, and `truewire standards` reports one that is wrong or not on line 1.
  So is a symlink: `init` never writes through a link.
  """
  if file.is_symlink():
    return False
  existing = file.read_bytes()
  if any(line.startswith(b'#:schema') for line in existing.splitlines()):
    return False
  newline = b'\r\n' if existing.split(b'\n', 1)[0].endswith(b'\r') else b'\n'
  file.write_bytes(f'#:schema {SCHEMA_URL}'.encode() + newline + existing)
  return True


def link_new(root: Path, relative: str, target: str) -> bool:
  """Make `root / relative` a symlink to `target` unless something is there already, a
  dangling symlink included; whether it linked. A filesystem that refuses symlinks leaves
  it unlinked: `init` says so and `truewire standards` reports it (A9)."""
  link = root / relative
  if os.path.lexists(link):
    return False
  link.parent.mkdir(parents=True, exist_ok=True)
  try:
    link.symlink_to(target, target_is_directory=True)
  except OSError:
    return False
  return True


BROAD_STATE_LINES = frozenset({'.truewire', '.truewire/', '/.truewire', '/.truewire/'})
"""`.gitignore` lines that ignore all of `.truewire/`, as an older `init` wrote, and with it
the codegen manifest meant to be committed (W16)."""

CACHE_LINES = frozenset({'.truewire/cache', '.truewire/cache/', '/.truewire/cache', '/.truewire/cache/'})
"""`.gitignore` lines that ignore `.truewire/cache/`, the one part of `.truewire/` a workspace
keeps out of git; `.truewire/cache/` is how `init` writes it."""

NARROWED = '.gitignore (`.truewire/` narrowed to `.truewire/cache/`)'
"""How `written` records a `.gitignore` whose broad `.truewire/` line was rewritten."""


def git_lines(text: str) -> list[str]:
  """`text` split into lines with their endings, as git reads an ignore file: only LF ends a
  line, so a lone CR, form feed or U+2028 stays inside one."""
  return re.findall(r'[^\n]*\n|[^\n]+\Z', text)


def narrow_state_lines(text: str) -> str:
  """`text` with each line of `BROAD_STATE_LINES` rewritten to `.truewire/cache/` in place,
  or dropped when a line of `CACHE_LINES` is already there; every other line stays byte for
  byte."""
  bom = '\ufeff' if text.startswith('\ufeff') else ''
  lines = git_lines(text.removeprefix(bom))
  has_cache = any(line.strip() in CACHE_LINES for line in lines)
  kept: list[str] = []
  for line in lines:
    if line.strip() not in BROAD_STATE_LINES:
      kept.append(line)
    elif not has_cache:
      kept.append('.truewire/cache/' + line[len(line.rstrip('\r\n')):])
      has_cache = True
  return bom + ''.join(kept)


def gitignore_warning(root: Path) -> str | None:
  """Explain a broad rule `init` cannot narrow because `.gitignore` is a symlink."""
  target = root / '.gitignore'
  if not target.is_symlink() or not target.is_file():
    return None
  try:
    with target.open(encoding='utf-8', errors='surrogateescape', newline='') as file:
      text = file.read()
  except OSError:
    return None
  if narrow_state_lines(text) != text:
    return 'Left symlinked .gitignore alone; narrow its broad `.truewire/` rule by hand to `.truewire/cache/` so the codegen manifest can be committed.'
  return None


def ensure_gitignore(root: Path, lines: list[str]) -> str | None:
  """Write `.gitignore` with `lines`, or add the ones an existing file lacks to its end;
  how `write_skeleton` records the change, or None when the file is left as it was.

  An existing file keeps its lines and their line endings, but for a broad `.truewire/` one
  an older `init` wrote, which is narrowed to `.truewire/cache/` (`narrow_state_lines`); any
  line of `CACHE_LINES` counts as `.truewire/cache/`. A change is recorded as `.gitignore`,
  or as `NARROWED` when the narrowing was part of it. A symlinked `.gitignore` is left
  alone, like any other link `init` finds (`write_new`).
  """
  target = root / '.gitignore'
  if write_new(root, '.gitignore', ''.join(f'{line}\n' for line in lines)):
    return '.gitignore'
  if target.is_symlink() or not target.is_file():
    return None
  with target.open(encoding='utf-8', errors='surrogateescape', newline='') as file:
    existing = file.read()
  text = narrow_state_lines(existing)
  narrowed = text != existing
  present = {line.strip() for line in git_lines(text.removeprefix('\ufeff'))}
  if present & CACHE_LINES:
    present.add('.truewire/cache/')
  missing = [line for line in lines if line not in present]
  if missing:
    endings = re.findall(r'\r?\n', existing)
    end = endings[-1] if endings else '\n'
    if text in ('', '\ufeff') or text.endswith('\n'):
      separator = ''
    elif text.endswith('\r'):
      separator = '\n'  # `x\r\n` reads as `x`, as git read the unterminated `x\r`
    else:
      separator = end
    text += separator + ''.join(f'{line}{end}' for line in missing)
  if text == existing:
    return None
  target.write_text(text, encoding='utf-8', errors='surrogateescape', newline='')
  return NARROWED if narrowed else '.gitignore'


def write_skeleton(root: Path, class_name: str, languages: tuple[str, ...] = ()) -> list[str]:
  """Lay the workspace skeleton under `root`, leaving every existing file alone.

  `languages` are the ones `truewire.toml` declares, each given a quickstart block.

  `truewire.toml` is not written here: its `[cores]` and `[python]` blocks come from the
  core template, and `init` writes it with the package scaffold.

  Returns:
    The paths written or changed, relative to `root` (`.gitignore` possibly as `NARROWED`);
    empty when nothing was missing.
  """
  written = [relative for relative, text in skeleton_files(class_name, languages).items() if write_new(root, relative, text)]
  if changed := ensure_gitignore(root, GITIGNORE_LINES):
    written.append(changed)
  written += [relative for relative, target in AGENT_LINKS.items() if link_new(root, relative, target)]
  return written
