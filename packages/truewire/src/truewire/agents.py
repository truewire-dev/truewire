"""The skills and rules a project vendors from the toolchain (`docs/shape/agents.md`, A10, A11).

The toolchain ships them as package data under `truewire/resources/agents/`: the skills'
`README.md`, one `skills/<name>/SKILL.md` per skill, and one `rules/<language>.md` per
backend that has rules. A project holds copies of them under `.agents/`, so an agent reads
them with no network and no install step. Each copy carries a stamp line naming the
toolchain version that wrote it and a digest of the text it wrote, so a copy edited since
can be told from one a newer toolchain would write differently.

A copy differs from the installed toolchain's in one of two ways. A deliberate one carries
`LOCAL_MARKER`, and is kept. A silent one fails `truewire agents check`, and so
`truewire standards`, the way a hand-edited generated file fails `generate --check`.
"""
import hashlib
import re
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing_extensions import Literal

RESOURCES = Path(__file__).parent / 'resources' / 'agents'
"""Where the package keeps the skills and rules it vendors."""

LOCAL_MARKER = '<!-- truewire: local edit -->'
"""The line that marks a vendored copy as deliberately edited: `agents check` accepts it and
`agents update` leaves it alone, unless `--force`. It counts only as a line of its own that
is not indented, so a skill can quote it indented or inline without marking itself."""

STAMP = re.compile(
  r'<!-- vendored from truewire (?P<version>\S+) sha256:(?P<digest>[0-9a-f]{16}); [^\n]* -->'
)
"""A copy's stamp line, as `stamp_line` writes it."""

FRONT_MATTER_END = '\n---\n'
"""What closes the YAML front matter a `SKILL.md` opens with (`---\\n`)."""

State = Literal['current', 'local', 'missing', 'stale', 'edited', 'retired', 'orphaned']


class OutsideProject(Exception):
  """A copy's directory is a link that leads outside the project, so nothing is written there."""


def is_local(text: str) -> bool:
  """Whether a copy carries `LOCAL_MARKER` as a line of its own, not indented."""
  return any(line.rstrip() == LOCAL_MARKER and not line[:1].isspace() for line in text.split('\n'))


def digest(text: str) -> str:
  """The digest a stamp records of the text it was written with."""
  return hashlib.sha256(text.encode()).hexdigest()[:16]


def stamp_line(toolchain: str, text: str) -> str:
  """The line a copy of `text` written by truewire `toolchain` carries."""
  return f'<!-- vendored from truewire {toolchain} sha256:{digest(text)}; `truewire agents update` rewrites this file -->'


def installed_version() -> str:
  """The installed toolchain's version, `unknown` from a source tree that was never installed."""
  try:
    return version('truewire')
  except PackageNotFoundError:
    return 'unknown'


def shipped(languages: tuple[str, ...]) -> dict[str, str]:
  """What the installed toolchain vendors into a project declaring `languages`, by path
  relative to the project root: the skills' README, every skill, and the rules of each
  declared language the package has rules for."""
  skills = RESOURCES / 'skills'
  files = {'.agents/skills/README.md': (skills / 'README.md').read_text()}
  for skill in sorted(skills.iterdir()):
    if (skill / 'SKILL.md').is_file():
      files[f'.agents/skills/{skill.name}/SKILL.md'] = (skill / 'SKILL.md').read_text()
  for language in languages:
    rules = RESOURCES / 'rules' / f'{language}.md'
    if rules.is_file():
      files[f'.agents/rules/{language}.md'] = rules.read_text()
  return files


def stamp_offset(text: str) -> int:
  """Where a copy's stamp line goes: right after the YAML front matter a `SKILL.md` opens
  with (runners read that block first), else at the start."""
  if text.startswith('---\n'):
    end = text.find(FRONT_MATTER_END, 3)
    if end != -1:
      return end + len(FRONT_MATTER_END)
  return 0


def stamped(text: str, toolchain: str) -> str:
  """`text` as a copy written by truewire `toolchain`: its stamp line at `stamp_offset`."""
  cut = stamp_offset(text)
  return f'{text[:cut]}{stamp_line(toolchain, text)}\n{text[cut:]}'


@dataclass(frozen=True)
class Stamp:
  """What a copy's stamp line records."""
  version: str
  digest: str


def unstamped(text: str) -> tuple[Stamp | None, str]:
  """A copy's stamp, or None when it has none, and the copy with its stamp line taken out.

  The stamp is read only where `stamped` writes it, the line at `stamp_offset`; a stamp
  quoted anywhere else in the text is text.
  """
  cut = stamp_offset(text)
  line_end = text.find('\n', cut)
  line = text[cut:] if line_end == -1 else text[cut:line_end]
  match = STAMP.fullmatch(line)
  if match is None:
    return None, text
  body = text[:cut] + ('' if line_end == -1 else text[line_end + 1:])
  return Stamp(match['version'], match['digest']), body


@dataclass(frozen=True)
class Copy:
  """One vendored file, compared with what the installed toolchain ships."""
  path: str
  """Relative to the project root."""
  state: State
  """`current`; `local` (carries `LOCAL_MARKER`); `missing`; `stale` (untouched since an
  older toolchain wrote it, and the installed one ships different text); `edited` (changed
  since it was written, or never stamped); `retired` (untouched, and no longer shipped);
  `orphaned` (no longer shipped, and changed since it was written)."""
  detail: str = ''

  @property
  def drifted(self) -> bool:
    """Whether `agents check` fails on this copy."""
    return self.state not in ('current', 'local')


def vendored_paths(root: Path) -> list[str]:
  """Every file under `.agents/` that could be a vendored copy, relative to `root`."""
  agents = root / '.agents'
  found = [*agents.glob('skills/README.md'), *agents.glob('skills/*/SKILL.md'), *agents.glob('rules/*.md')]
  return sorted(path.relative_to(root).as_posix() for path in found if path.is_file())


def survey(root: Path, languages: tuple[str, ...]) -> list[Copy]:
  """Compare the copies under `root` with what the installed toolchain ships.

  A copy is `current` when its text, stamp line aside, is the shipped text: a toolchain
  upgrade that changed nothing in a skill leaves its copy current. A file under `.agents/`
  with no stamp that the toolchain does not ship is the project's own, and not listed.
  """
  toolchain = installed_version()
  files = shipped(languages)
  copies: list[Copy] = []
  for relative, text in files.items():
    target = root / relative
    if not target.is_file():
      copies.append(Copy(relative, 'missing', 'not vendored'))
      continue
    copy = target.read_text()
    if is_local(copy):
      copies.append(Copy(relative, 'local', 'marked as a local edit'))
      continue
    stamp, body = unstamped(copy)
    if body == text:
      copies.append(Copy(relative, 'current'))
    elif stamp is None:
      copies.append(Copy(relative, 'edited', 'has no truewire stamp line right after its front matter (or on line 1), and differs from the shipped copy'))
    elif digest(body) != stamp.digest:
      copies.append(Copy(relative, 'edited', f'changed since truewire {stamp.version} wrote it'))
    else:
      copies.append(Copy(relative, 'stale', f'vendored from truewire {stamp.version}; truewire {toolchain} ships a different copy'))
  for relative in vendored_paths(root):
    if relative in files:
      continue
    copy = (root / relative).read_text()
    stamp, body = unstamped(copy)
    if stamp is None or is_local(copy):
      continue
    if digest(body) == stamp.digest:
      copies.append(Copy(relative, 'retired', f'truewire {toolchain} no longer ships this file'))
    else:
      copies.append(Copy(
        relative, 'orphaned', f'truewire {toolchain} no longer ships this file, and it was changed since '
        'it was written: delete its stamp line to make it your own',
      ))
  return copies


def inside(root: Path, relative: str) -> bool:
  """Whether the directory `root / relative` lives in, links followed, is within `root`."""
  return (root / relative).parent.resolve().is_relative_to(root.resolve())


def write_copy(root: Path, relative: str, text: str) -> None:
  """Write the stamped copy of `text` at `root / relative`, replacing whatever is there, a
  symlink included, so nothing is written through a link to outside the project.

  Raises:
    OutsideProject: A directory on the way is a link leading outside `root`.
  """
  if not inside(root, relative):
    raise OutsideProject(relative)
  target = root / relative
  target.parent.resolve().mkdir(parents=True, exist_ok=True)
  target.unlink(missing_ok=True)
  target.write_text(stamped(text, installed_version()))


@dataclass
class Updated:
  """What `update` did, by path relative to the project root."""
  written: list[str]
  overwrote: list[str]
  """Edited since they were written (or marked, under `--force`), and rewritten."""
  removed: list[str]
  kept: list[str]
  """Marked as local edits."""
  orphaned: list[str]
  """No longer shipped, and edited since: never removed."""
  refused: list[str]
  """Behind a link leading outside the project."""


def update(root: Path, languages: tuple[str, ...], *, force: bool = False) -> Updated:
  """Rewrite every copy under `root` that differs from the installed toolchain's, and
  remove the `retired` ones.

  A copy marked with `LOCAL_MARKER` is left alone unless `force`. An `orphaned` copy is
  never removed: it holds changes the toolchain did not write. A copy behind a link leading
  outside the project is refused only when its text would change or it would be removed; a
  `current` one there is left as it is, its stamp naming the version that wrote it.
  """
  files = shipped(languages)
  toolchain = installed_version()
  result = Updated([], [], [], [], [], [])
  for copy in survey(root, languages):
    target = root / copy.path
    if copy.state == 'orphaned':
      result.orphaned.append(copy.path)
    elif copy.state == 'local' and not force:
      result.kept.append(copy.path)
    elif copy.state != 'retired' and target.is_file() and target.read_text() == stamped(files[copy.path], toolchain):
      continue
    elif not inside(root, copy.path):
      # A current copy differs only in its stamp's version: leave a shared copy as it is.
      if copy.state != 'current':
        result.refused.append(copy.path)
    elif copy.state == 'retired':
      target.unlink()
      if target.name == 'SKILL.md' and not any(target.parent.iterdir()):
        target.parent.rmdir()
      result.removed.append(copy.path)
    else:
      write_copy(root, copy.path, files[copy.path])
      (result.overwrote if copy.state in ('edited', 'local') else result.written).append(copy.path)
  return result


def write_missing(root: Path, languages: tuple[str, ...]) -> tuple[list[str], list[str]]:
  """Write each shipped file `root` has no copy of, as `truewire init` does.

  An existing copy is left as it is, whatever it says: `init` never overwrites a file.

  Returns:
    The paths written, and the paths refused because their directory links outside `root`.
  """
  written: list[str] = []
  refused: list[str] = []
  for relative, text in shipped(languages).items():
    target = root / relative
    if target.exists() or target.is_symlink():
      continue
    if not inside(root, relative):
      refused.append(relative)
      continue
    write_copy(root, relative, text)
    written.append(relative)
  return written, refused
