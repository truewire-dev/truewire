"""
`docs/shape/workspace.md` W14: a file in the tree that holds the value of a `[secrets]`
variable fails the build.

`secrets.py` (S16) guesses from field names and stays a warning. This check compares
values: what each variable named by `[secrets].required` or `.optional` holds in the
environment and in `.env` at the project root, searched for in every file that can enter
the tree (I5) -- recordings first, but also `endpoint.json` notes and `dev/capture/`
scripts. A hit is a live credential about to be committed, so it is an `error`.
`truewire capture` runs the same search over the pair it is about to write, and writes
nothing on a hit.

A value is found however a recording spells it: as written, percent-encoded in either
hex case or with `/` left raw, with `+` for a space, JSON-escaped, or inside a base64
blob (a `Basic` credential a test endpoint echoes back; see `base64_cores`). Nothing
here ever prints a value: a location is built from redacted keys and paths, and `redact`
is what `standards` and `capture` pass all their output through. A value shorter than
`MIN_SECRET_LENGTH` is skipped with a note: `us` or `true` would match half the recordings in a project.
"""
import base64
import json
import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing_extensions import Any

from truewire.call import read_dotenv
from truewire.project import Project, resolve
from truewire.standards.finding import Finding

MIN_SECRET_LENGTH = 8
"""Values shorter than this are not searched for; see this module's docstring."""

MAX_FILE_BYTES = 64 * 1024 * 1024
"""Files larger than this are not read: no recording or script is that size, and a
stray binary should not stall the check."""

SKIPPED_DIRS = frozenset({'.git', '.venv', 'node_modules', 'target', '__pycache__', '.truewire'})
"""Directories left out when the project is not in a git repository and the tree has to
be walked; what git ignores is left out when it is."""


BASE64_SPELLING = {'/': r'(?:/|\\/|%2[fF])', '+': r'(?:\+|%2[bB])'}
"""The two base64 characters a recording can respell (`\\/` in JSON, percent-encoded in a
URL), as regexes; the rest of the alphabet only ever appears as itself (`-`/`_` are
regex-safe outside a character class), which keeps the search a near-literal scan."""


def spelling(char: str) -> str:
  """
  A regex matching one character of a value in any spelling a recording can carry it in:
  itself, percent-encoded (either hex case), `+` for a space, or JSON-escaped (`\\"`,
  `\\/` as PHP's `json_encode` writes it, `\\u00e9` in either hex case).

  Args:
    char: One character of a secret's value.
  """
  def hex_pattern(text: str) -> str:
    return ''.join(f'[{c.lower()}{c.upper()}]' if c.isalpha() else c for c in text)
  options = [re.escape(char)]
  options.append(''.join('%' + hex_pattern(f'{byte:02x}') for byte in char.encode()))
  if char == ' ':
    options.append(r'\+')
  if char == '/':
    options.append(r'\\/')
  escaped = json.dumps(char)[1:-1]
  if escaped != char and not escaped.startswith('\\u'):
    options.append(re.escape(escaped))
  if ord(char) <= 0xFFFF:
    options.append(r'\\u' + hex_pattern(f'{ord(char):04x}'))
  return '(?:' + '|'.join(options) + ')'


@dataclass(frozen=True)
class HeldSecrets:
  """The values `[secrets]` variables hold right now, and the names too short to search for."""
  values: tuple[tuple[str, str], ...]
  """`(name, value)` pairs, one per distinct value: a variable set differently in the
  environment and in `.env` appears twice. Longest value first, so a value that is a
  prefix of another never leaves the other's tail behind in `redact`."""
  short: tuple[str, ...]
  """Names holding a value shorter than `MIN_SECRET_LENGTH`, which are not searched for."""
  patterns: tuple[tuple[str, 're.Pattern[str]'], ...] = field(init=False, repr=False, compare=False)
  """`(name, pattern)` per value, each matching every spelling `spelling` knows."""
  wrapped: tuple[tuple[str, 're.Pattern[str]'], ...] = field(init=False, repr=False, compare=False)
  """`(name, pattern)` per value, matching its `base64_cores`: the value inside a base64 blob."""
  combined: 're.Pattern[str] | None' = field(init=False, repr=False, compare=False)
  """Every value's pattern in one alternation, longest first, one capturing group each (the
  only groups: `spelling`'s are non-capturing), or `None` when there are none."""

  def __post_init__(self):
    patterns = tuple((name, re.compile(''.join(spelling(c) for c in value))) for name, value in self.values)
    object.__setattr__(self, 'patterns', patterns)
    object.__setattr__(self, 'wrapped', tuple(
      (name, re.compile('|'.join(''.join(BASE64_SPELLING.get(c, c) for c in core) for core in base64_cores(value))))
      for name, value in self.values
    ))
    object.__setattr__(
      self, 'combined',
      re.compile('|'.join(f'({p.pattern})' for _, p in patterns)) if patterns else None,
    )


def held_secrets(project: Project, environ: Mapping[str, str] | None = None) -> HeldSecrets:
  """
  Read the value of every `[secrets].required` and `.optional` variable, from the
  environment and from `.env` at the project root. Unset and empty ones are left out.

  Args:
    project: The project whose `[secrets]` names the variables.
    environ: The environment to read, `os.environ` by default.
  """
  environ = os.environ if environ is None else environ
  dotenv = read_dotenv(project.root / '.env')
  values: list[tuple[str, str]] = []
  short: list[str] = []
  for name in dict.fromkeys((*project.secrets.required, *project.secrets.optional)):
    for value in dict.fromkeys(v for v in (environ.get(name), dotenv.get(name)) if v):
      if len(value) < MIN_SECRET_LENGTH:
        if name not in short:
          short.append(name)
      else:
        values.append((name, value))
  values.sort(key=lambda pair: -len(pair[1]))
  return HeldSecrets(values=tuple(values), short=tuple(short))


def base64_cores(value: str) -> tuple[str, ...]:
  """
  The base64 characters that encode `value` alone, at each of the three byte alignments
  it can sit at inside a longer base64 blob (`user:<value>` in a `Basic` credential),
  standard and URL-safe alphabets. Any blob that holds the value holds one of these, so a
  text search finds it without decoding anything.

  A character is kept only when all six of its bits come from the value: the first ones
  mix in the bytes before it, the last ones the bytes after.

  Args:
    value: A secret's value.
  """
  data = value.encode()
  cores: list[str] = []
  for offset in range(3):
    encoded = base64.b64encode(bytes(offset) + data).decode()
    core = encoded[-(-8 * offset // 6):(8 * (offset + len(data))) // 6]
    cores += [core, core.translate(str.maketrans('+/', '-_'))]
  return tuple(dict.fromkeys(cores))


def names_in(text: str, secrets: HeldSecrets) -> list[str]:
  """
  The variables whose value `text` holds in any spelling, in `secrets` order and without
  duplicates.

  Args:
    text: Any string: a JSON key or value, a path, or a whole file.
    secrets: What to look for.
  """
  if secrets.combined is None:
    return []
  found: list[str] = []
  for (name, pattern), (_, wrapped) in zip(secrets.patterns, secrets.wrapped):
    if name not in found and (pattern.search(text) or wrapped.search(text)):
      found.append(name)
  return found


def redact(text: str, secrets: HeldSecrets) -> str:
  """
  Replace every value `text` holds, in any spelling, with `<NAME>`: for printing a path,
  a JSON key, a response body or another command's output. A base64-wrapped value is not
  replaced; nothing that prints goes through base64.

  Args:
    text: Text that may quote a secret.
    secrets: What to replace.
  """
  if secrets.combined is None:
    return text
  names = [name for name, _ in secrets.patterns]
  return secrets.combined.sub(lambda match: f'<{names[(match.lastindex or 1) - 1]}>', text)


def leaks_in_json(value: Any, secrets: HeldSecrets, path: str = '') -> list[tuple[str, str]]:
  """
  Every `(name, path)` where a decoded JSON tree holds a secret's value: in a string, in
  an object key, or as a number. The path is redacted, so a key that is itself the value
  reads as `<NAME>`.

  Args:
    value: Decoded JSON value.
    secrets: What to look for.
    path: Redacted dotted/bracketed path of `value` in the file, `''` at the root.
  """
  if isinstance(value, dict):
    out: list[tuple[str, str]] = []
    for key, child in value.items():
      shown = redact(str(key), secrets)
      child_path = f'{path}.{shown}' if path else shown
      out.extend((name, child_path) for name in names_in(str(key), secrets))
      out.extend(leaks_in_json(child, secrets, child_path))
    return out
  if isinstance(value, list):
    return [leak for index, child in enumerate(value) for leak in leaks_in_json(child, secrets, f'{path}[{index}]')]
  if isinstance(value, str):
    return [(name, path) for name in names_in(value, secrets)]
  if isinstance(value, (int, float)) and not isinstance(value, bool):
    return [(name, path) for name in names_in(json.dumps(value), secrets)]
  return []


def leaks_in_text(text: str, secrets: HeldSecrets) -> list[tuple[str, str]]:
  """
  Every `(name, path)` where one file holds a secret's value. The whole text is searched
  first; only a JSON file with a hit is walked value by value, so the path says where. A
  hit the walk cannot place (a spelling that only the raw text has) keeps an empty path.

  Args:
    text: The file's content.
    secrets: What to look for.
  """
  whole = names_in(text, secrets)
  if not whole:
    return []
  try:
    decoded = json.loads(text)
  except ValueError:
    return [(name, '') for name in whole]
  placed = list(dict.fromkeys(leaks_in_json(decoded, secrets)))
  return placed + [(name, '') for name in whole if name not in {n for n, _ in placed}]


def tree_files(root: Path) -> list[Path]:
  """
  Every file under `root` that can enter the tree: what `git ls-files` lists as tracked
  or untracked-but-not-ignored when `root` is in a git repository, else every file
  outside `SKIPPED_DIRS` except `.env` itself.

  Args:
    root: The project root.
  """
  try:
    listed = subprocess.run(
      ['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard', '--', '.'],
      cwd=root, capture_output=True, check=True,
    ).stdout
  except (OSError, subprocess.CalledProcessError):
    return sorted(
      path for path in root.rglob('*')
      if path.is_file() and path.name != '.env'
      and not SKIPPED_DIRS.intersection(path.relative_to(root).parts[:-1])
    )
  # Names are bytes: `os.fsdecode` keeps a non-UTF-8 one as the same file on disk.
  names = (os.fsdecode(name) for name in listed.split(b'\0') if name)
  return sorted({root / name for name in names if (root / name).is_file()})


def short_note(name: str) -> str:
  """The note for a variable whose value is too short to search for."""
  return (
    f'`{name}` holds a value shorter than {MIN_SECRET_LENGTH} characters; not searched for, '
    'to avoid false hits'
  )


def check_secret_values(
  client_root: Path | Project, environ: Mapping[str, str] | None = None,
) -> list[Finding]:
  """
  W14: an `error` for every file that can enter the tree and holds the value of a
  `[secrets]` variable, in its content or its path, and a `warning` note for each variable
  whose value is too short to search for.

  A project whose variables are all unset passes: there is no value to compare, which is
  the normal state of a CI job with no credentials.

  Args:
    client_root: Project (or project root).
    environ: The environment to read, `os.environ` by default.
  """
  project = resolve(client_root)
  secrets = held_secrets(project, environ)
  out: list[Finding] = [
    Finding(rule='W14', location='[secrets]', message=short_note(name), severity='warning')
    for name in secrets.short
  ]
  if not secrets.values:
    return out
  for path in tree_files(project.root):
    relative = str(path.relative_to(project.root))
    # A non-UTF-8 name is searched as decoded and printed with `?` for its bad bytes.
    shown = redact(os.fsencode(relative).decode('utf-8', errors='replace'), secrets)
    leaks = [(name, 'file name') for name in names_in(relative, secrets)]
    try:
      if path.stat().st_size <= MAX_FILE_BYTES:
        leaks += leaks_in_text(path.read_text(errors='replace'), secrets)
    except OSError:
      pass
    for name, where in leaks:
      out.append(Finding(
        rule='W14',
        location=f'{shown}: {where}' if where else shown,
        message=(
          f'holds the value `{name}` has in the environment or .env; no live credential may '
          'enter the tree (I5). Remove it (in a recording, re-capture with `--scrub <key>`), '
          'and rotate the credential if the file was ever pushed'
        ),
        severity='error',
      ))
  return out
