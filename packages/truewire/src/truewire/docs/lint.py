"""Check that a project's published docs address a reader, not the pipeline that built them.

Three rules, deliberately separate.

`internal-reference` is a denylist over prose: it encodes a judgement about voice, and a
project may argue with a specific pattern.

`verbose-page` is a word budget. It is the crudest of the three and the only one that
catches the most common failure — a page that is correct, well-organised, and four times
longer than anyone will read.

`broken-doc-link` is a deterministic check over relative links. Unlike the live S1
checker, it needs no network and therefore belongs in the normal docs gate.

Prose only. Fenced code blocks are skipped and do not count against the budget — an example
may legitimately name a path, a variable called `coverage` is not a leak, and a page should
never be penalised for showing more code.
"""

from typing_extensions import Iterator
from dataclasses import dataclass
from pathlib import Path
import re
from urllib.parse import unquote, urlparse

FENCE = re.compile(r'^\s*(```|~~~)')
HEADING = re.compile(r'^\s*#+\s')
"""A markdown ATX heading. Structure, not prose — excluded from the word budget."""

INTERNAL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
  ('internal path', re.compile(r'\bspec/[A-Za-z_][\w./-]*')),
  ('internal path', re.compile(r'\b(AGENTS|CLAUDE)\.md\b')),
  ('internal path', re.compile(r'\btruewire\.toml\b')),
  ('internal path', re.compile(r'\bcodegen\b')),
  ('internal path', re.compile(r'\bsource repository\b|\bthe monorepo\b', re.I)),
  ('pipeline voice', re.compile(r'\bscoped out\b|\bout of scope\b', re.I)),
  ('pipeline voice', re.compile(r'\bdeliberately (deferred|out of scope|scoped out)\b', re.I)),
  ('pipeline voice', re.compile(r'\bdoc-derived\b|\blive-(confirmed|verified|captured)\b', re.I)),
  ('pipeline voice', re.compile(r'\b(un)?confirmed live\b', re.I)),
  ('pipeline voice', re.compile(r'\bprovenance\b', re.I)),
  ('pipeline voice', re.compile(r'\bthese docs (run|are executed|are run) against\b', re.I)),
  ('pipeline voice', re.compile(r'\bnot exercised\b', re.I)),
  ('pipeline voice', re.compile(r'\bthis (build|run|session|rebuild)\b', re.I)),
  ('pipeline voice', re.compile(r'\b(this client|what) was built\b|\bclient was built against\b', re.I)),
  ('pipeline voice', re.compile(r'\bper-endpoint record\b|\bdoc/live conflicts\b', re.I)),
  ('pipeline voice', re.compile(r'\bunverified\b', re.I)),
  ('pipeline voice', re.compile(r'\bwe (were unable|could not|did not manage)\b', re.I)),
  ('pipeline voice', re.compile(r'\bgate \d\b', re.I)),
]
"""Patterns that mark prose as written about our process rather than for a reader.

Measured against all 230 published markdown files across the original corpus:
**36 true hits, 0 false positives.** Every pattern earns its place on that evidence.

Four candidates were tested and rejected, and must not be added back without new evidence:

- `\bcoverage\b` — 0 true, 10 false. Every hit is an `## API Coverage` heading.
- `\bnot (yet )?implemented\b` — 0 true, 9 false. It is the correct user-facing way to state
  a scope limit, and several projects' docs use it well.
- `\bgated?\b` — 5 true, 2 false, and the 5 are "gated behind a Business Development
  whitelist", which is a fact about the upstream API and belongs in the docs.
- `\bwhitelist\b` — 0 true, 11 false. All upstream-API facts.

The bare word `deferred` is also excluded; only `deliberately deferred` is flagged, because
"deferred settlement" is a plausible domain term.
"""

GITHUB_BLOB = re.compile(r'https://github\.com/[\w-]+/[\w-]+/blob/[\w.-]+/(\S+?)[)\s"\'>]')
MARKDOWN_LINK = re.compile(r'!?\[[^\]]*\]\(([^)\s]+)(?:\s+["\'][^)]*)?\)')
HTML_LINK = re.compile(r'\b(?:href|src)=["\']([^"\']+)["\']', re.I)

WORD_BUDGET = {
  'api-keys': 400,
  'index': 500,
  'how-to': 700,
}
"""Prose-word ceilings per page family. Code blocks do not count against them.

A budget rather than a review note, because "too long" loses every argument it has with a
page that is already written. One docs rebuild shipped 34,184 words across 44 pages —
`api-keys.md` alone spent 728 words to say that the API wants three values instead of two,
padded with a router table, a lecture on what HMAC covers, and advice not to commit secrets
to git. The reader is a developer who came to make one API call.

Reference pages are uncapped: `docs/reference/api/**` is generated `:::` mkdocstrings output,
and `error-handling` and `websocket` are lookup tables nobody reads end to end.
"""


def _budget_for(path: Path, root: Path) -> int | None:
  """Return the prose-word ceiling for one page, or `None` when it is uncapped."""
  rel = path.relative_to(root).as_posix()
  if rel.startswith('docs/reference/') or rel == 'README.md':
    return None
  if rel == 'docs/api-keys.md':
    return WORD_BUDGET['api-keys']
  if rel.startswith('docs/how-to/'):
    return WORD_BUDGET['how-to']
  if rel == 'docs/index.md':
    return WORD_BUDGET['index']
  return None


@dataclass(frozen=True)
class Finding:
  """One rule violation, at one line of one file."""
  path: Path
  line: int
  text: str
  rule: str
  detail: str


@dataclass(frozen=True)
class Block:
  """One fenced code block of a markdown file."""
  info: str
  """The opening fence's info string: `python`, `bash`, or `''` when unlabelled."""
  line: int
  """1-indexed line of the block's first line of code — the one after the opening fence."""
  code: str


def code_blocks(text: str) -> Iterator[Block]:
  """Yield every fenced code block of a markdown source, in order.

  The single fence scanner in this package: the audience rules read the prose it leaves
  behind, and [`check`][truewire.docs.check] type-checks the python it collects. A fence
  left unclosed at end of file still yields its block, so that a stray fence hides the
  rest of the page from the prose rules rather than exposing the rest of the page to them.
  """
  info: str | None = None
  start = 0
  lines: list[str] = []
  for number, line in enumerate(text.splitlines(), start=1):
    if (hit := FENCE.match(line)) is not None:
      if info is None:
        info, start, lines = line.strip().removeprefix(hit.group(1)).strip(), number + 1, []
      else:
        yield Block(info=info, line=start, code=''.join(f'{row}\n' for row in lines))
        info = None
      continue
    if info is not None:
      lines.append(line)
  if info is not None:
    yield Block(info=info, line=start, code=''.join(f'{row}\n' for row in lines))


def _prose_lines(path: Path) -> Iterator[tuple[int, str]]:
  """Yield the 1-indexed prose lines of a markdown file, skipping fenced code."""
  text = path.read_text()
  fenced = {
    number
    for block in code_blocks(text)
    for number in range(block.line - 1, block.line + len(block.code.splitlines()) + 1)
  }
  for number, line in enumerate(text.splitlines(), start=1):
    if number not in fenced:
      yield number, line


def doc_files(root: Path) -> list[Path]:
  """Every published markdown file of one client: its README and its docs tree."""
  files = [root / 'README.md'] if (root / 'README.md').is_file() else []
  files.extend(sorted((root / 'docs').rglob('*.md')))
  return files


def link_files(root: Path) -> list[Path]:
  """Every published source that may contain reader-facing Markdown links."""
  files = doc_files(root)
  quickstart = root / 'docs' / 'quickstart.yaml'
  if quickstart.is_file():
    files.append(quickstart)
  return files


def _link_targets(line: str) -> Iterator[str]:
  """Yield Markdown and HTML link targets from one source line."""
  yield from (match.group(1) for match in MARKDOWN_LINK.finditer(line))
  yield from (match.group(1) for match in HTML_LINK.finditer(line))


def _local_target(root: Path, source: Path, target: str) -> Path | None:
  """Resolve a locally-verifiable link target, or return `None` for an external link."""
  parsed = urlparse(target)
  if parsed.scheme or target.startswith('//') or target.startswith('#'):
    return None
  if parsed.path.startswith('/') or not parsed.path:
    return None
  path = (source.parent / unquote(parsed.path)).resolve()
  if path.is_dir():
    return path / 'index.md'
  return path


def _broken_link_findings(root: Path) -> Iterator[Finding]:
  """Report internal documentation links whose source target does not exist."""
  for path in link_files(root):
    for number, line in enumerate(path.read_text().splitlines(), start=1):
      for target in _link_targets(line):
        resolved = _local_target(root, path, target)
        if resolved is None or resolved.is_file():
          continue
        try:
          relative = resolved.relative_to(root).as_posix()
        except ValueError:
          relative = resolved.as_posix()
        yield Finding(
          path=path, line=number, text=line.strip(), rule='broken-doc-link',
          detail=f'{target!r} resolves to missing {relative}',
        )


def lint_docs(root: Path) -> list[Finding]:
  """Report every audience violation in one project's published docs."""
  findings: list[Finding] = []
  for path in doc_files(root):
    words = sum(
      len(line.split()) for _, line in _prose_lines(path)
      if not HEADING.match(line)
    )
    budget = _budget_for(path, root)
    if budget is not None and words > budget:
      findings.append(Finding(
        path=path, line=1, text=f'{words} prose words',
        rule='verbose-page',
        detail=f'{words} prose words against a budget of {budget}; cut, do not reformat',
      ))
    for number, line in _prose_lines(path):
      masked = line
      for hit in GITHUB_BLOB.finditer(line + ' '):
        start, end = hit.span()
        end = min(end, len(line))
        if end > start:
          masked = masked[:start] + ' ' * (end - start) + masked[end:]
      for label, pattern in INTERNAL_PATTERNS:
        if (hit := pattern.search(masked)) is not None:
          findings.append(Finding(
            path=path, line=number, text=line.strip(),
            rule='internal-reference',
            detail=f'{label}: {hit.group(0)!r}',
          ))
          break
  findings.extend(_broken_link_findings(root))
  return findings
