"""
Exercise `truewire.standards.router_coverage.check_router_coverage` (S26) against
synthetic fixtures, never `clients/` -- per the isolation contract
`common/lib/test/test_mock.py`'s module docstring records, these must pass in a checkout
with zero clients.

The check only cares whether `endpoint.json`/`router.json` *exist* at a given path, not
their content, so fixtures here are empty files -- no need for a real spec payload the way
`test_standards_duplicate_schemas.py`'s does.
"""
import json
from pathlib import Path

from truewire.spec.authoring import check_router_core
from truewire.standards.router_coverage import check_router_coverage


def touch_endpoint(root: Path, *segments: str) -> None:
  """Create an empty `endpoint.json` at `spec/endpoints/<segments>/endpoint.json`."""
  directory = root / 'spec' / 'endpoints'
  for segment in segments:
    directory = directory / segment
  directory.mkdir(parents=True, exist_ok=True)
  (directory / 'endpoint.json').write_text('{}')


def write_router(root: Path, *segments: str, core: str | None = None) -> None:
  """Create a real `router.json` at `spec/endpoints/<segments>/router.json`, optionally
  declaring `core` (design §5)."""
  directory = root / 'spec' / 'endpoints'
  for segment in segments:
    directory = directory / segment
  directory.mkdir(parents=True, exist_ok=True)
  doc = {'description': 'A grouping.', 'upstream': 'https://example.com/docs'}
  if core is not None:
    doc['core'] = core
  (directory / 'router.json').write_text(json.dumps(doc))


def write_codegen_toml(root: Path) -> None:
  """Mark `root` as migrated to the request/response codegen shape by touching an empty
  `codegen/config.toml` -- `check_router_core` only checks for its presence, not its
  content."""
  (root / 'truewire.toml').write_text(
    '[python]\nname = "X"\n[python.cores.default]\nbase = "x.core:Endpoint"\n'
  )


def test_grouping_with_no_router_json_is_flagged(tmp_path):
  """A directory with no `endpoint.json` of its own, but a descendant that has one, is a
  router.json-eligible grouping -- missing one is exactly S26's case."""
  touch_endpoint(tmp_path, 'wallet', 'status')

  findings = check_router_coverage(tmp_path)

  assert len(findings) == 1
  assert findings[0]['rule'] == 'S26'
  assert findings[0]['severity'] == 'error'
  assert findings[0]['location'] == 'spec/endpoints/wallet'


def test_grouping_with_router_json_produces_no_finding(tmp_path):
  """Declaring the file closes the gap, whether or not the link is an exact match --
  S26 only checks presence, not precision (see docs/spec/authoring.md rule 14)."""
  touch_endpoint(tmp_path, 'wallet', 'status')
  write_router(tmp_path, 'wallet')

  assert check_router_coverage(tmp_path) == []


def test_leaf_endpoint_directory_is_never_flagged(tmp_path):
  """A directory holding `endpoint.json` directly is a leaf, not a grouping -- `router.json`
  is never expected there, with or without a parent grouping above it."""
  touch_endpoint(tmp_path, 'wallet', 'status')
  write_router(tmp_path, 'wallet')

  findings = check_router_coverage(tmp_path)

  assert all('status' not in f['location'] for f in findings)


def test_every_missing_level_of_a_nested_gap_is_reported(tmp_path):
  """Two nested groupings, neither declared, are two separate facts to fix -- one finding
  per directory, not just the deepest or the shallowest."""
  touch_endpoint(tmp_path, 'wallet', 'asset', 'transfer')

  findings = check_router_coverage(tmp_path)
  locations = {f['location'] for f in findings}

  assert locations == {'spec/endpoints/wallet', 'spec/endpoints/wallet/asset'}


def test_examples_directory_is_never_treated_as_a_grouping(tmp_path):
  """An endpoint's own `examples/` directory holds request/response fixtures, not further
  endpoints -- it must never be mistaken for a missing grouping."""
  touch_endpoint(tmp_path, 'wallet', 'status')
  write_router(tmp_path, 'wallet')
  examples_dir = tmp_path / 'spec' / 'endpoints' / 'wallet' / 'status' / 'examples'
  examples_dir.mkdir(parents=True)
  (examples_dir / '01.request.json').write_text('{}')

  findings = check_router_coverage(tmp_path)

  assert all('examples' not in f['location'] for f in findings)


def test_empty_or_missing_endpoints_directory_produces_no_findings(tmp_path):
  """A client with no `spec/endpoints/` at all (or an empty one) has nothing to flag."""
  assert check_router_coverage(tmp_path) == []

  (tmp_path / 'spec' / 'endpoints').mkdir(parents=True)
  assert check_router_coverage(tmp_path) == []


def test_root_router_json_without_core_is_flagged(tmp_path: Path):
  """A migrated client (`codegen/config.toml` present) whose root `router.json` declares no `core`
  is exactly design §5's "no implicit fallback" gap -- every client's resolution walk needs
  a real ancestor to terminate at."""
  write_codegen_toml(tmp_path)
  write_router(tmp_path)

  violations = check_router_core(tmp_path)

  assert any(v['rule'] == 'router-core-missing' for v in violations)


def test_root_router_json_with_core_is_clean(tmp_path: Path):
  """Declaring `core` on the root `router.json` closes the gap."""
  write_codegen_toml(tmp_path)
  write_router(tmp_path, core='default')

  assert check_router_core(tmp_path) == []


def test_root_router_json_missing_entirely_is_flagged(tmp_path: Path):
  """A migrated client with no root `router.json` at all is the same gap as one that
  declares no `core` -- there's no `router.json` to resolve a `core` from either way."""
  write_codegen_toml(tmp_path)

  violations = check_router_core(tmp_path)

  assert any(v['rule'] == 'router-core-missing' for v in violations)


def test_unmigrated_client_with_no_codegen_toml_is_skipped(tmp_path: Path):
  """A client with no `codegen/config.toml` hasn't migrated to the request/response
  shape at all, so it has no `core`-resolvable classes yet -- nothing to check."""
  write_router(tmp_path)

  assert check_router_core(tmp_path) == []
