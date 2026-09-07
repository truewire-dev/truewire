"""
`docs/production_standards.md` S16: flag a recorded example whose credential-shaped field
doesn't look like an obviously-fake placeholder.

Genuinely fuzzy -- a response body is never compared or redacted by the mock server (unlike
a request-side credential, which `redacted`/ADR 0007 actively strips), so this is a name-
and-value heuristic over captured examples, not a proof. Every finding is `warning`-severity
for that reason; see `docs/spec/authoring.md` rule 6.
"""
import json
import re
from pathlib import Path
from typing_extensions import Any, Iterator

from truewire.project import Project, resolve
from truewire.standards.finding import Finding

CREDENTIAL_VOCAB = frozenset({
  'client', 'secret', 'key', 'apikey', 'privatekey', 'passphrase', 'password', 'token',
  'accesstoken', 'access', 'private', 'auth', 'credential', 'wallet', 'signature', 'seed',
  'mnemonic',
})
"""Words that make a field name credential-shaped. Deliberately excludes generic words a
project's env-var name also carries (`id`, `test`, `live`, the API's own name) -- a derived
token is only kept when it's also a member of this set, so `TEST_PETSTORE_CLIENT_ID` yields
`client` (kept) but not `id` or `petstore` (dropped)."""

_DENYLIST_WORDS = (
  'apiKey', 'api_key', 'secret', 'privateKey', 'private_key', 'clientSecret',
  'client_secret', 'accessToken', 'access_token', 'passphrase', 'password',
)
"""Built-in credential-shaped field-name denylist, pre-normalization."""

BUILTIN_DENYLIST = frozenset(re.sub(r'[^a-z0-9]', '', word.lower()) for word in _DENYLIST_WORDS)
"""`_DENYLIST_WORDS`, normalized the same way `normalize_name` folds a captured field name
before comparing."""

FAKE_MARKERS = ('redacted', 'fake', 'xxx', 'test', 'example', 'placeholder', 'dummy')
"""Case-insensitive substrings that make a captured value read as an obviously-fake
placeholder rather than a real secret."""

MIN_VALUE_LENGTH = 6
"""A string shorter than this is unlikely to be a real credential -- more likely a flag,
code, or short id -- so it's skipped rather than flagged, to cut down on pure noise."""

EXAMPLE_GLOBS = (
  '*.response.json', '*.reply.json', '*.messages.json', '*.message.json',
  '*.messages.protobuf.json',
)
"""Recorded-example filename patterns under an endpoint's `examples/` directory, matching
`truewire.cli.test`'s own `http_examples`/`ws_examples` globs."""


def normalize_name(name: str) -> str:
  """
  Fold a field name to lowercase alphanumerics, matching
  `truewire.spec.authoring.normalize`'s own rule so a captured `client_secret` and a
  declared `clientSecret` compare equal.

  Args:
    name: JSON object key as captured in an example.
  """
  return re.sub(r'[^a-z0-9]', '', name.lower())


def credential_tokens(client_root: Path | Project) -> frozenset[str]:
  """
  Credential-shaped name tokens for one project: the built-in denylist, plus any word of a
  declared `[secrets]` env-var name (`truewire.toml`'s `[secrets].required`/`optional`)
  that is itself a member of `CREDENTIAL_VOCAB`.

  A project with no `truewire.toml`, or none carrying a `[secrets]` table, contributes
  nothing beyond the built-in denylist -- a public-only project is not an error here.

  Args:
    client_root: Project (or project root).
  """
  tokens = set(BUILTIN_DENYLIST)
  try:
    secrets = resolve(client_root).secrets
  except Exception:
    return frozenset(tokens)
  names = [*secrets.get('required', []), *secrets.get('optional', [])]
  for name in names:
    if not isinstance(name, str):
      continue
    for word in re.split(r'[^a-zA-Z0-9]+', name.lower()):
      if word in CREDENTIAL_VOCAB:
        tokens.add(word)
  return frozenset(tokens)


def is_credential_shaped(key: str, tokens: frozenset[str]) -> str | None:
  """
  Return the matched token if a JSON key looks credential-shaped, else `None`.

  Matching is substring-containment on the normalized key against each token, not equality
  -- so `primary_client_secret` still matches `clientsecret` -- which is deliberately loose;
  S16 is a heuristic that asks a human to check, never one that fails a build on its own.

  Args:
    key: JSON object key as captured in an example.
    tokens: Credential-shaped tokens from `credential_tokens`.
  """
  normalized = normalize_name(key)
  for token in tokens:
    if token in normalized:
      return token
  return None


def looks_fake(value: str) -> bool:
  """
  Return whether a captured value carries an obviously-fake placeholder marker.

  Args:
    value: The credential-shaped field's captured string value.
  """
  lowered = value.lower()
  return any(marker in lowered for marker in FAKE_MARKERS)


def walk_fields(value: Any, path: str = '') -> Iterator[tuple[str, str, Any]]:
  """
  Every `(path, key, value)` triple for a dict key anywhere in a decoded JSON tree.

  Args:
    value: Decoded JSON value.
    path: Dotted/bracketed path of `value` inside the example.
  """
  if isinstance(value, dict):
    for key, child in value.items():
      child_path = f'{path}.{key}' if path else key
      yield child_path, key, child
      yield from walk_fields(child, child_path)
  elif isinstance(value, list):
    for index, child in enumerate(value):
      yield from walk_fields(child, f'{path}[{index}]')


def check_secret_placeholders(client_root: Path | Project) -> list[Finding]:
  """
  S16: flag a recorded example field that looks credential-shaped and whose value carries
  no obviously-fake placeholder marker.

  Args:
    client_root: Project (or project root).
  """
  tokens = credential_tokens(client_root)
  project = resolve(client_root)
  client_root = project.root
  out: list[Finding] = []
  endpoints_root = project.endpoints_dir
  if not endpoints_root.is_dir():
    return out
  for pattern in EXAMPLE_GLOBS:
    for example_path in sorted(endpoints_root.glob(f'**/examples/{pattern}')):
      try:
        payload = json.loads(example_path.read_text())
      except (json.JSONDecodeError, OSError):
        continue
      relative = example_path.relative_to(client_root)
      for path, key, field_value in walk_fields(payload):
        if not isinstance(field_value, str) or len(field_value) < MIN_VALUE_LENGTH:
          continue
        token = is_credential_shaped(key, tokens)
        if token is None or looks_fake(field_value):
          continue
        out.append(Finding(
          rule='S16',
          location=f'{relative}: {path}',
          message=(
            f'`{key}` looks credential-shaped (matches `{token}`) and its captured value '
            f'carries no obviously-fake marker (REDACTED/FAKE/XXX/TEST/EXAMPLE/'
            f'PLACEHOLDER/DUMMY); confirm it is not a real secret before committing'
          ),
          severity='warning',
        ))
  return out
