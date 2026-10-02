"""Run each declared package's own test suite (`docs/shape/toolchain.md` T4, `workspace.md` W8).

Every suite already starts `truewire mock` over the project's recordings from its own
harness: pytest through `truewire.mock.running_mock_servers`, vitest through
`@truewire/testing`, Rust and Go by spawning `$TRUEWIRE_BIN mock`. So nothing here starts
a mock. What this adds is one invocation per language, the way CI runs it, with
`TRUEWIRE_BIN` pointing at the toolchain that is running, so every harness replays with
the same `truewire` that was asked to test.

A package whose suite cannot run (no manifest, no tool on `PATH`, dependencies never
installed) fails with the reason, and so does a suite that ran no test: nothing passes
unmeasured (`docs/shape/score.md` S13).
"""
import codecs
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from truewire.project import PROJECT_FILE, Project

MANIFESTS = {
  'python': 'pyproject.toml',
  'typescript': 'package.json',
  'rust': 'Cargo.toml',
  'go': 'go.mod',
}
"""The file that marks a language's package directory."""

LOCKFILES = (('yarn.lock', 'yarn'), ('pnpm-lock.yaml', 'pnpm'), ('package-lock.json', 'npm'))
"""A TypeScript package's lockfile, and the package manager it names. `npm` without one."""

NO_TESTS_COLLECTED = 5
"""pytest's exit code when it found no test: a package with no suite has not passed."""

_PYTEST_SUMMARY = re.compile(r'^=*\s*(?:\d+ \w+, )*\d+ \w+ in [\d.]+s', re.MULTILINE)
_VITEST_SUMMARY = re.compile(r'^\s*Tests\s+(.*)$', re.MULTILINE)
_RUST_RESULT = re.compile(r'^test result: \w+\. (\d+) passed; \d+ failed; (\d+) ignored', re.MULTILINE)
_GO_TEST_END = re.compile(r'--- (PASS|SKIP): (\S+)')
_COLOUR = re.compile(r'\x1b\[[0-9;]*m')


@dataclass(frozen=True)
class Count:
  """What a suite that exited zero says it ran."""
  passed: int
  skipped: int


def _count(label: str, text: str) -> int:
  return sum(int(number) for number in re.findall(rf'(\d+) {label}\b', text))


class NoSuite(Exception):
  """A declared language whose suite cannot be run; the message says why."""


@dataclass(frozen=True)
class Suite:
  """One package's test command, as CI runs it."""
  language: str
  directory: Path
  """The package directory: the one holding the language's manifest."""
  command: tuple[str, ...]
  env: dict[str, str] = field(default_factory=dict)
  """Added to the environment the suite inherits."""

  @property
  def shown(self) -> str:
    """The command as a person would type it: the interpreter by name, not by path."""
    program = Path(self.command[0]).name
    return ' '.join((program, *self.command[1:]))


@dataclass(frozen=True)
class Outcome:
  """One language's result."""
  language: str
  code: int
  """The suite's exit code; 0 is a pass. A suite that could not start has a non-zero one."""
  detail: str = ''
  """Why, when it failed: the command and its exit code, or why it could not run."""
  output: str = ''
  """Everything the suite printed."""

  @property
  def passed(self) -> bool:
    return self.code == 0


def package_directory(project: Project, language: str) -> Path:
  """The nearest directory holding `language`'s manifest, from its `src` up to the project root.

  `examples/` keep every manifest at the root beside `src/`; the W-layout keeps each under
  `packages/<language>/`. Walking up from `[<language>].src` finds either. pytest needs no
  manifest, so a Python package without a `pyproject.toml` runs from the project root, as
  `examples/kraken`'s CI does.
  """
  section = getattr(project.config, language)
  if section is None:
    raise NoSuite(f'{project.root / PROJECT_FILE}: no [{language}] section')
  root = project.root.resolve()
  directory = (project.root / section.src).resolve()
  manifest = MANIFESTS[language]
  while True:
    if (directory / manifest).is_file():
      return directory
    if directory == root or root not in directory.parents:
      if language == 'python':
        return root
      raise NoSuite(f'no {manifest} between [{language}].src ({section.src}) and the project root')
    directory = directory.parent


def truewire_bin() -> str | None:
  """The `truewire` executable the harnesses should mock with: `$TRUEWIRE_BIN` when set,
  else the one beside the running interpreter, else the first on `PATH`."""
  if bin := os.environ.get('TRUEWIRE_BIN'):
    return bin
  beside = Path(sys.executable).with_name('truewire')
  if beside.is_file():
    return str(beside)
  return shutil.which('truewire')


def suite(project: Project, language: str) -> Suite:
  """How `language`'s package runs its tests: the command its CI job runs."""
  directory = package_directory(project, language)
  # The output goes to a pipe and `count_tests` reads its summary: no colour codes in it.
  # pytest reads `PY_COLORS` before `NO_COLOR`, and CI often exports `PY_COLORS=1`.
  env: dict[str, str] = {'NO_COLOR': '1', 'PY_COLORS': '0'}
  if bin := truewire_bin():
    env['TRUEWIRE_BIN'] = bin
  if language == 'python':
    src = str(project.python_src.resolve())
    env['PYTHONPATH'] = os.pathsep.join(filter(None, (src, os.environ.get('PYTHONPATH'))))
    return Suite(language, directory, (sys.executable, '-m', 'pytest', '--verbosity=-1'), env)
  if language == 'typescript':
    manager = next((tool for lockfile, tool in LOCKFILES if (directory / lockfile).is_file()), 'npm')
    if not (directory / 'node_modules').is_dir():
      raise NoSuite(f'{directory}: no node_modules; run `{manager} install` first')
    return Suite(language, directory, (manager, 'test'), env)
  if language == 'rust':
    return Suite(language, directory, ('cargo', 'test'), env)
  # `-v`, because only a verbose run says how many tests ran, and `[no test files]` exits 0.
  return Suite(language, directory, ('go', 'test', '-v', './...'), env)


def count_tests(language: str, output: str) -> Count | None:
  """How many tests a suite that exited zero passed and skipped, from its own summary;
  `None` when the output carries no summary to read.

  One rule for every language (S13): zero passed is not a pass, whatever the exit code.
  Every runner exits zero on a suite that skipped everything (`truewire.testing` skips each
  replay whose method is not generated yet), and `cargo test` and `go test` on none at all.
  Colour codes are dropped first: a package's own config can force them (`--color=yes`).
  """
  output = _COLOUR.sub('', output)
  if language == 'python':
    summaries = _PYTEST_SUMMARY.findall(output)
    return Count(_count('passed', summaries[-1]), _count('skipped', summaries[-1])) if summaries else None
  if language == 'typescript':
    summaries = _VITEST_SUMMARY.findall(output)
    return Count(_count('passed', summaries[-1]), _count('skipped', summaries[-1])) if summaries else None
  if language == 'rust':
    results = _RUST_RESULT.findall(output)
    return Count(sum(int(passed) for passed, _ in results), sum(int(ignored) for _, ignored in results))
  # Leaves only: a parent prints `--- PASS:` even when every subtest skipped. Unanchored:
  # a test that prints without a newline puts its `--- PASS:` mid-line.
  ends = _GO_TEST_END.findall(output)
  parents = {name.rsplit('/', depth)[0] for _, name in ends for depth in range(1, name.count('/') + 1)}
  leaves = [result for result, name in ends if name not in parents]
  return Count(leaves.count('PASS'), leaves.count('SKIP'))


def run(suite: Suite, *, echo: Callable[[str], None] | None = None) -> Outcome:
  """Run `suite`, passing what it prints to `echo` as it arrives (not per line: pytest's
  progress dots would wait for the line to end); return its outcome."""
  if shutil.which(suite.command[0]) is None:
    return Outcome(suite.language, 127, f'{suite.command[0]} not found on PATH')
  chunks: list[str] = []
  decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
  with subprocess.Popen(
    suite.command, cwd=suite.directory, env={**os.environ, **suite.env},
    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
  ) as process:
    assert process.stdout is not None
    while data := os.read(process.stdout.fileno(), 65536):
      if text := decoder.decode(data):
        chunks.append(text)
        if echo is not None:
          echo(text)
  if tail := decoder.decode(b'', final=True):
    chunks.append(tail)
  output = ''.join(chunks)
  code = process.returncode
  if code == 0:
    count = count_tests(suite.language, output)
    if count is None:
      return Outcome(suite.language, 1, f'{suite.shown} printed no test summary to count', output)
    if count.passed == 0:
      skipped = f' ({count.skipped} skipped)' if count.skipped else ''
      return Outcome(suite.language, 1, f'{suite.shown} ran no tests{skipped}', output)
    return Outcome(suite.language, 0, output=output)
  if suite.language == 'python' and 'No module named pytest' in output:
    return Outcome(suite.language, code, f'pytest is not installed in {suite.command[0]}; install it there', output)
  if suite.language == 'python' and code == NO_TESTS_COLLECTED:
    return Outcome(suite.language, code, f'{suite.shown} collected no tests', output)
  return Outcome(suite.language, code, f'{suite.shown} exited {code}', output)


def run_language(project: Project, language: str, *, echo: Callable[[str], None] | None = None) -> Outcome:
  """Run `language`'s suite; a suite that cannot run is a failed outcome saying why.

  With `echo`, a `== <language>: <command> (in <directory>)` header goes first, then the
  suite's output as it arrives.
  """
  try:
    found = suite(project, language)
  except NoSuite as exc:
    if echo is not None:
      echo(f'== {language}: {exc}\n')
    return Outcome(language, 1, str(exc))
  if echo is not None:
    echo(f'== {language}: {found.shown} (in {found.directory})\n')
  return run(found, echo=echo)


def worst(outcomes: list[Outcome]) -> int:
  """T4's exit code: 1 when any language failed, else 0."""
  return 0 if all(outcome.passed for outcome in outcomes) else 1
