"""Type-check the python examples in a project's published docs.

Docs have shipped referencing methods that never existed, because nobody checked them.
Every ```python block in `README.md` and `docs/**/*.md`, plus `docs/quickstart.yaml`'s
`first_call` and every `how_tos[].code`, is handed to pyright against the
project's own package source, so a call to a method the package does not have, an argument
of the wrong shape, or a subscript into an optional is an error before a reader ever
copies it. `quickstart.yaml`'s `install`/`env_setup` steps are shell/dotenv, not python,
and are not checked.

Nothing is executed, and `.env` is never read. The predecessor of this check ran every
block against the live upstream API: that needed real credentials for every project, so
it was flaky, could not run in CI, and could not cover an effectful endpoint without
placing a real order. What execution proved and this cannot — that the API really answers as
documented — now rests entirely on the spec's recorded examples, which are captured from
real responses and gated separately.

The two are not nested. Type-checking one project's docs found 16 errors in blocks the
executing gate had passed 133/133, because a block that never reaches a mistyped line still
runs clean. This catches a different class of defect, not a weaker one.

A block using top-level `await` is not importable python, so it is wrapped in a coroutine
before checking — exactly as the executing gate wrapped it. Positions are mapped back
through the wrapper, because an error at `block_0119.py:13` tells a reader nothing: every
diagnostic names the markdown page, the block, and the line in that page.
"""

from typing_extensions import Iterator, Protocol
from dataclasses import dataclass, field
from pathlib import Path
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import textwrap

import yaml

from truewire.project import Project, resolve

from .lint import code_blocks, doc_files

PYTHON_INFO = re.compile(r'^(python|py)\b')
"""Info strings whose block is python. Matches the first word, so `python title="x"` counts."""

WRAPPER_INDENT = '  '
WRAPPER_LINES = 2
"""Lines the coroutine wrapper prepends: `import asyncio` and the `async def`."""


def _wrap(code: str) -> str:
  """Put a block inside a coroutine, so that top-level `await` becomes valid python."""
  return (
    'import asyncio\nasync def _main():\n'
    + textwrap.indent(code, WRAPPER_INDENT)
    + '\nasyncio.run(_main())\n'
  )


def _compiles(code: str) -> bool:
  """Whether a source string is syntactically valid python."""
  try:
    compile(code, '<block>', 'exec')
  except SyntaxError:
    return False
  return True


@dataclass(frozen=True)
class Example:
  """One python block of one markdown page, prepared for type-checking."""
  path: Path
  index: int
  """1-indexed position of the block among the python blocks of its own page."""
  line: int
  """1-indexed line of the block's first line of code in `path`."""
  code: str
  wrapped: bool
  """Whether the block needed the coroutine wrapper to become importable python."""
  prefix: str = field(default='')
  """Code prepended before `code` purely for type-checking context — never reported on.

  `docs/quickstart.yaml`'s `how_tos[].code` is a bare continuation (`await client...`)
  that assumes `first_call`'s client is already open, exactly as the rendered page shows
  it. Checking it alone would flag `client` as undefined; checking it after `first_call`'s
  own code resolves it the same way pyright would for a reader following the page top to
  bottom. `first_call` is still checked on its own account too, so a real defect in it is
  reported once there, not once per `how_to` that includes it as a prefix — `locate`
  returns `None` for any diagnostic landing inside `prefix`.
  """

  @property
  def full_code(self) -> str:
    """`prefix` followed by the example's own code — what actually gets type-checked."""
    return self.prefix + self.code if self.prefix else self.code

  @property
  def source(self) -> str:
    """The text handed to pyright: `full_code`, or `full_code` inside a coroutine."""
    return _wrap(self.full_code) if self.wrapped else self.full_code

  def locate(self, line: int, column: int) -> tuple[int, int] | None:
    """Map a 1-indexed position in `source` back to a position in `path`.

    A diagnostic landing on one of the wrapper's own lines is clamped into the block, so
    that it still points somewhere a reader can act on rather than off the end of it.
    `None` means the position falls inside `prefix` — the caller drops it, since whatever
    example checks that code on its own account already reports it.
    """
    offset = WRAPPER_LINES if self.wrapped else 0
    indent = len(WRAPPER_INDENT) if self.wrapped else 0
    total_rows = max(len(self.full_code.splitlines()), 1)
    row = min(max(line - offset, 1), total_rows)
    prefix_rows = len(self.prefix.splitlines()) if self.prefix else 0
    if row <= prefix_rows:
      return None
    own_row = row - prefix_rows
    rows = max(len(self.code.splitlines()), 1)
    own_row = min(own_row, rows)
    return self.line + own_row - 1, max(column - indent, 1)


@dataclass(frozen=True)
class Diagnostic:
  """One pyright diagnostic, mapped back to the markdown page it came from."""
  example: Example
  line: int
  """1-indexed line in `example.path`."""
  column: int
  severity: str
  message: str
  rule: str | None


class PyrightUnavailable(RuntimeError):
  """Raised when pyright cannot be run at all, as opposed to running and finding errors."""


def examples(root: Path) -> list[Example]:
  """Every python example in one client's published docs, in page order."""
  found: list[Example] = []
  for path in doc_files(root):
    index = 0
    for block in code_blocks(path.read_text()):
      if PYTHON_INFO.match(block.info.lower()) is None:
        continue
      index += 1
      found.append(Example(
        path=path, index=index, line=block.line, code=block.code,
        wrapped=not _compiles(block.code) and _compiles(_wrap(block.code)),
      ))
  found.extend(quickstart_examples(root))
  return found


def _node_value(mapping: yaml.MappingNode, key: str) -> yaml.Node | None:
  """The value node of one key in a YAML mapping node, or `None` if absent."""
  for key_node, value_node in mapping.value:
    if key_node.value == key:
      return value_node
  return None


def _code_line(node: yaml.ScalarNode) -> int:
  """1-indexed line of a `code:` scalar's first line of actual content.

  A block scalar (`code: |`/`code: >`) has its node's start mark on the `code:` key's own
  line, not the first content line below it — every real `quickstart.yaml`'s `code` is
  this style, so the `+1` past the header matters. A plain single-line scalar's start mark
  already sits on the content line (it shares the key's own line), so it gets no extra
  skip. `+1` alone, common to both, is only the 0-to-1-indexed conversion.
  """
  base = node.start_mark.line + 1
  return base + 1 if node.style in ('|', '>') else base


def quickstart_examples(root: Path) -> list[Example]:
  """The checkable python in one project's `docs/quickstart.yaml`, if it exists.

  Only `first_call` and each `how_tos[]` entry are python — `install` is a shell command
  and `env_setup` is a `.env` file, neither of them checkable as python. `how_tos[]` code
  is checked with `first_call`'s code as `prefix` (see `Example.prefix`), since the page
  renders each `how_to` as a continuation of `first_call`'s already-open `client`, not as
  a standalone script.

  Line numbers come from `yaml.compose`'s node marks rather than a text scan, so authored
  formatting (block-scalar style, indentation, comments) can't drift the mapping out of
  sync the way a regex would.
  """
  path = root / 'docs' / 'quickstart.yaml'
  if not path.is_file():
    return []
  text = path.read_text()
  data = yaml.safe_load(text)
  root_node = yaml.compose(text)
  if not isinstance(data, dict) or not isinstance(root_node, yaml.MappingNode):
    return []

  found: list[Example] = []
  index = 0

  first_call = data.get('first_call')
  first_call_code = first_call.get('code') if isinstance(first_call, dict) else None
  first_call_node = _node_value(root_node, 'first_call')
  if isinstance(first_call_code, str) and isinstance(first_call_node, yaml.MappingNode):
    code_node = _node_value(first_call_node, 'code')
    if isinstance(code_node, yaml.ScalarNode):
      index += 1
      found.append(Example(
        path=path, index=index, line=_code_line(code_node), code=first_call_code,
        wrapped=not _compiles(first_call_code) and _compiles(_wrap(first_call_code)),
      ))
  else:
    first_call_code = None

  how_tos = data.get('how_tos')
  how_tos_node = _node_value(root_node, 'how_tos')
  if isinstance(how_tos, list) and isinstance(how_tos_node, yaml.SequenceNode):
    prefix = first_call_code or ''
    for item, item_node in zip(how_tos, how_tos_node.value):
      code = item.get('code') if isinstance(item, dict) else None
      if not isinstance(code, str) or not isinstance(item_node, yaml.MappingNode):
        continue
      code_node = _node_value(item_node, 'code')
      if not isinstance(code_node, yaml.ScalarNode):
        continue
      index += 1
      full = prefix + code
      found.append(Example(
        path=path, index=index, line=_code_line(code_node), code=code,
        wrapped=not _compiles(full) and _compiles(_wrap(full)), prefix=prefix,
      ))
  return found


def pyright_config(root: Path | Project) -> dict[str, object]:
  """The pyright config for the scratch directory holding the extracted blocks.

  Generated here rather than asked of the project: the project's own `pyrightconfig.json`
  is `truewire generate`'s, it `include`s the package source to get `py.typed` re-export
  checking, and pointing it at a scratch directory instead would silently retire that gate.

  `extraPaths` resolves the project's package the way a reader's install would (its
  `[python].src` directory). `venvPath` is the project's own `.venv`, when it has one --
  where its dependencies actually are. It is omitted when there is no venv there, which
  degrades to missing-import diagnostics rather than to a confusing pyright failure, and
  never quietly substitutes another tree's environment.
  """
  project = resolve(root)
  config: dict[str, object] = {
    'include': ['.'],
    'extraPaths': [str(project.python_src)],
  }
  venv = project.root / '.venv'
  if venv.is_dir():
    config['venvPath'] = str(venv.parent)
    config['venv'] = venv.name
  return config


def pyright_command() -> list[str]:
  """The pyright executable to run: the `pyright` Python package when it is installed in
  the running interpreter (`python -m pyright`), else `npx --yes pyright`."""
  if importlib.util.find_spec('pyright') is not None:
    return [sys.executable, '-m', 'pyright']
  return ['npx', '--yes', 'pyright']


def run_pyright(directory: Path, *, cwd: Path) -> list[dict]:
  """Run pyright over one directory and return its raw diagnostics.

  Raises:
    PyrightUnavailable: when pyright could not be run or produced no report. A non-zero
      exit is not itself a failure — pyright exits 1 whenever it finds an error.
  """
  command = [*pyright_command(), '--outputjson', '--project', str(directory)]
  try:
    result = subprocess.run(command, capture_output=True, text=True, cwd=cwd)
  except (FileNotFoundError, NotADirectoryError) as e:
    raise PyrightUnavailable(
      'pyright could not be run: neither the `pyright` Python package (`pip install '
      'pyright`) nor `npx` is available. Install one and check with `pyright --version`.'
    ) from e
  try:
    report = json.loads(result.stdout)
  except json.JSONDecodeError as e:
    detail = (result.stderr or result.stdout).strip().splitlines()
    raise PyrightUnavailable(
      f'pyright produced no report (exit {result.returncode}): '
      f'{detail[-1] if detail else "no output"}'
    ) from e
  return report.get('generalDiagnostics', [])


def _map_back(raw: list[dict], files: dict[Path, Example]) -> list[Diagnostic]:
  """Turn pyright's file-and-offset diagnostics into markdown page positions.

  A diagnostic `example.locate` maps into `example.prefix` (see `Example.prefix`) is
  dropped here — it belongs to whichever example checks that code on its own account,
  and is already reported there.
  """
  found: list[Diagnostic] = []
  for item in raw:
    severity = item.get('severity')
    if severity not in ('error', 'warning'):
      continue
    example = files.get(Path(item.get('file', '')).resolve())
    if example is None:
      continue
    start = item.get('range', {}).get('start', {})
    located = example.locate(start.get('line', 0) + 1, start.get('character', 0) + 1)
    if located is None:
      continue
    line, column = located
    found.append(Diagnostic(
      example=example, line=line, column=column, severity=severity,
      message=' '.join(item.get('message', '').split()),
      rule=item.get('rule'),
    ))
  return sorted(found, key=lambda d: (str(d.example.path), d.line, d.column))


class Runner(Protocol):
  """The pyright invocation, as `check_docs` makes it. Substitutable in tests."""
  def __call__(self, directory: Path, *, cwd: Path) -> list[dict]: ...


def check_docs(
  root: Path | Project, *, runner: Runner | None = None,
) -> tuple[list[Example], list[Diagnostic]]:
  """Type-check every python example in one project's published docs.

  Args:
    root: The project (or project root), holding `README.md`, `docs/` and the package source.
    runner: Replaces the pyright invocation. For tests only; the default is the real one.

  Returns:
    Every example found, and every error or warning against it. An empty doc set is not
    an error — a project with no python examples has nothing to get wrong.

  Raises:
    PyrightUnavailable: when pyright could not be run.
  """
  project = resolve(root)
  root = project.root
  found = examples(root)
  if not found:
    return [], []
  with tempfile.TemporaryDirectory(prefix='truewire-docs-check-') as name:
    directory = Path(name).resolve()
    files: dict[Path, Example] = {}
    for number, example in enumerate(found):
      file = directory / f'block_{number:04d}.py'
      file.write_text(example.source)
      files[file] = example
    (directory / 'pyrightconfig.json').write_text(
      json.dumps(pyright_config(project), indent=2) + '\n'
    )
    raw = (runner or run_pyright)(directory, cwd=root)
  return found, _map_back(raw, files)


def report(root: Path, diagnostics: list[Diagnostic]) -> Iterator[str]:
  """Render each diagnostic as one line naming the page, the block, and the position."""
  for diagnostic in diagnostics:
    rel = diagnostic.example.path.relative_to(root)
    rule = f' [{diagnostic.rule}]' if diagnostic.rule else ''
    yield (
      f'{rel}:{diagnostic.line}:{diagnostic.column}: block {diagnostic.example.index}: '
      f'{diagnostic.severity}{rule}: {diagnostic.message}'
    )
