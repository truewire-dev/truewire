"""S10: check the exact manifest version with one short registry request."""
import json
import tomllib
from urllib.parse import quote

import httpx
from httpx import Client

from truewire.project import Project
from truewire.test import MANIFESTS, NoSuite, package_directory

from .card import Cell

REGISTRIES = {'python': 'PyPI', 'typescript': 'npm', 'rust': 'crates.io', 'go': 'Go proxy'}
TIMEOUT = 5.0
USER_AGENT = 'truewire-score (https://truewire.dev)'


def lookup(name: str, version: str, language: str, *, transport: httpx.BaseTransport | None = None) -> Cell:
  """Only HTTP 200 proves publication; 404 proves absence. All other outcomes are unchecked."""
  registry = REGISTRIES[language]
  escaped_name, escaped_version = quote(name, safe=''), quote(version, safe='')
  if language == 'python':
    url = f'https://pypi.org/pypi/{escaped_name}/{escaped_version}/json'
  elif language == 'typescript':
    url = f'https://registry.npmjs.org/{escaped_name}/{escaped_version}'
  elif language == 'rust':
    url = f'https://crates.io/api/v1/crates/{escaped_name}/{escaped_version}'
  else:
    module = quote(_go_escape(name), safe='/!')
    tag = quote(_go_escape('v' + version.removeprefix('v')), safe='!')
    url = f'https://proxy.golang.org/{module}/@v/{tag}.info'
  try:
    with Client(transport=transport, timeout=TIMEOUT, headers={'User-Agent': USER_AGENT}, follow_redirects=False) as client:
      response = client.get(url)
  except httpx.RequestError:
    return Cell('unchecked', f'{registry} unreachable')
  if response.status_code == 200:
    return Cell('pass', f'{name} {version}')
  if response.status_code == 404:
    return Cell('fail', f'{name} {version} is not on {registry}')
  if response.status_code >= 500:
    return Cell('unchecked', f'{registry} unreachable')
  return Cell('unchecked', f'{registry} returned HTTP {response.status_code}')


def _go_escape(value: str) -> str:
  """Go proxy paths encode each ASCII uppercase letter as ! followed by lowercase."""
  return ''.join('!' + char.lower() if 'A' <= char <= 'Z' else char for char in value)


def check_package(project: Project, language: str, *, transport: httpx.BaseTransport | None = None) -> Cell:
  """Read package metadata from its own manifest, never the generated import name."""
  if language == 'go':
    return Cell('unchecked', 'no declared version for Go package')
  try:
    manifest = package_directory(project, language) / MANIFESTS[language]
    text = manifest.read_text()
    if language == 'typescript':
      package = json.loads(text)
    else:
      package = tomllib.loads(text).get('project' if language == 'python' else 'package')
  except (NoSuite, OSError, ValueError) as exc:
    return Cell('unchecked', f'cannot read {MANIFESTS[language]}: {exc}')
  if not isinstance(package, dict):
    return Cell('unchecked', f'{manifest.name}: no package metadata')
  name, version = package.get('name'), package.get('version')
  if not isinstance(name, str) or not name.strip():
    return Cell('unchecked', f'{manifest.name}: no declared package name')
  if not isinstance(version, str) or not version.strip():
    return Cell('unchecked', f'{manifest.name}: no declared version for {name}')
  return lookup(name, version, language, transport=transport)
