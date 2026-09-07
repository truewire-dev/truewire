"""
Exercise `truewire.spec.authoring.check_mixed_leaf_router` (design §4, `docs/spec/
authoring.md` rule 16, `docs/production_standards.md` S30, Task 24c) against synthetic
fixtures, never `clients/` -- mirrors `test_spec_authoring_schemas_shadowing.py`'s own
isolation style for its neighboring `check_schemas_no_shadowing`.

Only directory shape matters to this check, not endpoint content, so fixtures write the
smallest `endpoint.json` that makes a directory resolve as a leaf.
"""
from pathlib import Path

from truewire.spec.authoring import check_mixed_leaf_router

ENDPOINT = '{"meta": {"public": true}, "spec": {"kind": "rpc", "transports": ["http"], "path": "/x", "method": "GET"}}'


def write_leaf(root: Path, *segments: str) -> None:
  """Write a minimal `endpoint.json` at `spec/endpoints/<segments>`."""
  directory = root / 'spec' / 'endpoints'
  for segment in segments:
    directory = directory / segment
  directory.mkdir(parents=True, exist_ok=True)
  (directory / 'endpoint.json').write_text(ENDPOINT)


def test_no_endpoints_root_produces_no_findings(tmp_path: Path):
  """A client with no `endpoints` at all has nothing to walk."""
  assert check_mixed_leaf_router(tmp_path) == []


def test_pure_aggregate_directory_is_clean(tmp_path: Path):
  """A directory holding only leaf children (no directory both a leaf and a router) is the
  ordinary aggregate shape and reports nothing."""
  write_leaf(tmp_path, 'market', 'order')
  write_leaf(tmp_path, 'market', 'orderbook')
  assert check_mixed_leaf_router(tmp_path) == []


def test_pure_composite_directory_is_clean(tmp_path: Path):
  """A directory holding only subdirectories, none of which is itself both a leaf and a
  router, is the ordinary composite shape and reports nothing."""
  write_leaf(tmp_path, 'account', 'deposits', 'history')
  write_leaf(tmp_path, 'account', 'withdrawals', 'history')
  assert check_mixed_leaf_router(tmp_path) == []


def test_directory_with_own_endpoint_and_descendant_leaf_is_flagged(tmp_path: Path):
  """bit2me's real shape: `v1/account/endpoint.json` sitting alongside
  `v1/account/subaccount/endpoint.json` -- `v1/account` is both a leaf endpoint and a
  router grouping, refused by rule 16."""
  write_leaf(tmp_path, 'v1', 'account')
  write_leaf(tmp_path, 'v1', 'account', 'subaccount')

  violations = check_mixed_leaf_router(tmp_path)

  assert len(violations) == 1
  assert violations[0]['rule'] == 'mixed-leaf-router'
  location = str(Path('spec', 'endpoints', 'v1', 'account'))
  assert violations[0]['location'] == location
  assert location in violations[0]['message']


def test_one_violation_per_offending_directory_not_per_descendant_endpoint(tmp_path: Path):
  """Two descendant leaves under the same offending directory still report as one
  violation -- the count states directories to restructure, not endpoints affected."""
  write_leaf(tmp_path, 'v1', 'account')
  write_leaf(tmp_path, 'v1', 'account', 'subaccount')
  write_leaf(tmp_path, 'v1', 'account', 'verify')

  violations = check_mixed_leaf_router(tmp_path)

  assert len(violations) == 1


def test_multiple_offending_directories_each_reported(tmp_path: Path):
  """Two independent mixed directories (bit2me's real fleet-wide shape, 8 of them) each
  produce their own finding."""
  write_leaf(tmp_path, 'v1', 'account')
  write_leaf(tmp_path, 'v1', 'account', 'subaccount')
  write_leaf(tmp_path, 'v1', 'wallet')
  write_leaf(tmp_path, 'v1', 'wallet', 'balance')

  violations = check_mixed_leaf_router(tmp_path)

  assert len(violations) == 2
  locations = {v['location'] for v in violations}
  assert locations == {
    str(Path('spec', 'endpoints', 'v1', 'account')),
    str(Path('spec', 'endpoints', 'v1', 'wallet')),
  }


def test_nested_mixed_directory_deeper_than_root_is_flagged(tmp_path: Path):
  """Rule 16 applies at every depth, not just directly under `spec/endpoints/` -- a leaf
  two levels down still can't also carry an endpoint-bearing descendant."""
  write_leaf(tmp_path, 'v1', 'account', 'sub')
  write_leaf(tmp_path, 'v1', 'account', 'sub', 'nested')

  violations = check_mixed_leaf_router(tmp_path)

  assert len(violations) == 1
  assert violations[0]['location'] == str(Path('spec', 'endpoints', 'v1', 'account', 'sub'))
