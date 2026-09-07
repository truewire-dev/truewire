"""
`docs/production_standards.md` S1: every upstream documentation URL, and every external
link in a project's own `README.md`/`docs/`, resolves to the exact page cited -- not a
stale redirect or a domain-level landing page.

S1 was `manual` -- no checker existed. This one is deliberately `warning`-only and opt-in:
`truewire.cli.standards` never runs it as part of the default pass, since a genuinely
rate-limited or CAPTCHA'd upstream doc host makes live-network checking inherently flaky,
and no policy for running this unattended in CI has been settled. Rather than solve that,
this check is meant to be run deliberately by a human (`truewire standards --only links`),
where flakiness is tolerable.
"""
import re
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from truewire.project import Project, root_of, spec_dir

from .finding import Finding

URL_PATTERN = re.compile(r'https?://[^\s<>"\'\]]+')
"""`]` is excluded (on top of the obvious whitespace/quote/angle-bracket delimiters) so a
markdown link whose *text* is itself a bare URL -- `[https://a](https://b)` -- splits into
two correct matches at the `]` boundary, instead of one match that swallows `](` and glues
both URLs into one garbled string. `)` stays in the class (some real URLs carry one, e.g. a
Wikipedia disambiguation link) and is handled by `TRAILING_PUNCTUATION` stripping instead."""
TRAILING_PUNCTUATION = '.,;:)]}>*'
REQUEST_TIMEOUT = 10.0
USER_AGENT = (
  'Mozilla/5.0 (compatible; truewire-link-checker/1.0; '
  '+https://github.com/truewire-dev/truewire)'
)
"""Some doc hosts 403 the default `urllib` user agent outright."""


def extract_urls(text: str) -> set[str]:
  """
  Every distinct http(s) URL referenced in a markdown file, wire templates excluded.

  One regex catches a bare URL and the target half of a `[text](url)`/`<a href="url">`
  link alike -- the match doesn't care which syntax it sits in, only trailing punctuation
  the surrounding syntax leaves behind (a closing `)`, a trailing `">`, a sentence's `.`)
  needs stripping off afterward. A URL containing `{`/`}` is a wire template -- an
  `upstream.md` `Base URL` line, e.g. `https://{network}.example.com/v2/{apiKey}` -- not
  a fetchable documentation page, and is excluded.

  Args:
    text: Raw file content to scan.
  """
  urls: set[str] = set()
  for match in URL_PATTERN.findall(text):
    url = match.rstrip(TRAILING_PUNCTUATION)
    if '{' in url or '}' in url:
      continue
    urls.add(url)
  return urls


def collect_source_files(client_root: Path | Project) -> list[Path]:
  """
  Every file in scope for S1: `upstream.md`, `router.json`, `README.md`, and
  `docs/**/*.md`.

  `router.json`'s `upstream` field is a plain URL in JSON text, and
  `extract_urls`' regex already only matches an actual `http(s)://...` substring
  regardless of the surrounding file's syntax, so no JSON-specific parsing is needed here
  -- same treatment as every other file in this list.

  Args:
    client_root: Project (or project root).
  """
  endpoints_dir = spec_dir(client_root) / 'endpoints'
  client_root = root_of(client_root)
  files = sorted(endpoints_dir.rglob('upstream.md')) + sorted(endpoints_dir.rglob('router.json'))
  readme = client_root / 'README.md'
  if readme.is_file():
    files.append(readme)
  docs_dir = client_root / 'docs'
  if docs_dir.is_dir():
    files.extend(sorted(docs_dir.rglob('*.md')))
  return files


def fetch_status(url: str) -> tuple[int, str] | str:
  """
  Resolve one URL, returning `(status, final_url)` or an error message on failure.

  Tries `HEAD` first; falls back to `GET` only on a `405` (some hosts reject `HEAD`
  outright). Every network failure -- timeout, DNS, TLS, connection reset -- is caught and
  returned as a message rather than raised: a network hiccup is data this check reports,
  not a bug in the check itself.

  Args:
    url: URL to resolve.
  """
  for method in ('HEAD', 'GET'):
    request = urllib.request.Request(url, method=method, headers={'User-Agent': USER_AGENT})
    try:
      with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return response.status, response.geturl()
    except urllib.error.HTTPError as exc:
      if exc.code == 405 and method == 'HEAD':
        continue
      return exc.code, exc.geturl() or url
    except Exception as exc:
      return f'{type(exc).__name__}: {exc}'
  return 'HEAD and GET both rejected with 405'  # pragma: no cover - defensive


def is_landing_page_redirect(original: str, final: str) -> bool:
  """
  Whether a redirect landed on a domain-level page instead of the cited endpoint/topic page.

  A heuristic, not proof (S1's own `Enforcement` line says so): if the cited URL names a
  real path -- more than one path segment -- and the final resolved URL's path is empty or
  `/`, the redirect most likely dropped the caller onto a landing page rather than the
  section actually cited.

  Args:
    original: URL as written in the source file.
    final: URL the request actually resolved to, after redirects.
  """
  original_path = urlparse(original).path.strip('/')
  final_path = urlparse(final).path.strip('/')
  return len(original_path.split('/')) >= 2 and final_path == ''


def check_links(client_root: Path | Project) -> list[Finding]:
  """
  S1: flag a broken or landing-page-redirected URL in a project's upstream docs / README / docs.

  Every finding is `rule='S1'`, `severity='warning'` -- this check is opt-in only (see the
  module docstring), never part of `truewire standards`' default pass.

  One finding is reported per `(url, file)` pair, since the same broken link cited from two
  files is two separate facts an author has to fix -- but each distinct URL is fetched only
  once, regardless of how many files cite it.

  Args:
    client_root: Project (or project root).
  """
  client_root = root_of(client_root)
  by_url: dict[str, list[Path]] = {}
  for path in collect_source_files(client_root):
    for url in extract_urls(path.read_text()):
      by_url.setdefault(url, []).append(path)

  out: list[Finding] = []
  for url, files in sorted(by_url.items()):
    result = fetch_status(url)
    if isinstance(result, str):
      message = f'could not resolve `{url}`: {result}'
    else:
      status, final_url = result
      if not (200 <= status < 300):
        message = f'`{url}` returned HTTP {status}'
      elif is_landing_page_redirect(url, final_url):
        message = (
          f'`{url}` redirected to `{final_url}`, which looks like a domain-level landing '
          f'page rather than the cited page'
        )
      else:
        continue
    for path in files:
      out.append(Finding(
        rule='S1',
        location=f'{path.relative_to(client_root)}: {url}',
        message=message,
        severity='warning',
      ))
  return out
