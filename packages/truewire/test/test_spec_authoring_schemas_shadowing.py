"""
Exercise `truewire.spec.authoring.check_schemas_no_shadowing` (design §5b, Task 24b)
against synthetic fixtures, never `clients/` -- mirrors `test_standards_router_coverage.py`'s
own isolation style for its neighboring `check_router_core`.

Only id sets matter to this check, not schema content, so fixtures write the smallest
schemas.json shape that carries the ids under test.
"""
import json
from pathlib import Path

from truewire.spec.authoring import check_schemas_no_shadowing


def write_schemas(root: Path, *segments: str, ids: list[str]) -> None:
  """Write a `schemas.json` declaring one dummy schema per id in `ids`.

  `segments` is empty for the client root's own `spec/schemas.json`; otherwise a path
  under `spec/endpoints/` (`'futures'`, or `'futures', 'usdt'` for a nested scope).
  """
  directory = root / 'spec' if not segments else root / 'spec' / 'endpoints'
  for segment in segments:
    directory = directory / segment
  directory.mkdir(parents=True, exist_ok=True)
  data = {id: {'title': id, 'type': 'string'} for id in ids}
  (directory / 'schemas.json').write_text(json.dumps(data))


def test_single_schemas_file_produces_no_findings(tmp_path: Path):
  """A client with only a root `spec/schemas.json` (or none at all) has nothing that
  could possibly shadow anything else."""
  assert check_schemas_no_shadowing(tmp_path) == []
  write_schemas(tmp_path, ids=['OrderSide'])
  assert check_schemas_no_shadowing(tmp_path) == []


def test_disjoint_ids_across_root_and_nested_scope_is_clean(tmp_path: Path):
  """The client root's own `spec/schemas.json` and a nested `futures/schemas.json`
  declaring disjoint ids is exactly design §5b's intended shape -- a leaf under `futures/`
  inherits both, with nothing to collide."""
  write_schemas(tmp_path, ids=['OrderSide'])
  write_schemas(tmp_path, 'futures', ids=['FuturesSide'])
  assert check_schemas_no_shadowing(tmp_path) == []


def test_same_id_at_root_and_nested_scope_is_flagged(tmp_path: Path):
  """The client root's own `spec/schemas.json` sits on every path to root (design §5b's
  "final fallback... the walk always reaches it last"), so a nested scope repeating one of
  its ids is a real collision, not two unrelated declarations."""
  write_schemas(tmp_path, ids=['OrderSide'])
  write_schemas(tmp_path, 'futures', ids=['OrderSide'])

  violations = check_schemas_no_shadowing(tmp_path)

  assert len(violations) == 1
  assert violations[0]['rule'] == 'schemas-shadowing'
  assert 'OrderSide' in violations[0]['message']


def test_same_id_at_two_unrelated_nested_scopes_is_clean(tmp_path: Path):
  """Two sibling subtrees (`spot/`, `futures/`) declaring the same id is not a collision
  at all -- no single endpoint's ancestor walk ever sees both scopes, per design §5b."""
  write_schemas(tmp_path, 'spot', ids=['Side'])
  write_schemas(tmp_path, 'futures', ids=['Side'])
  assert check_schemas_no_shadowing(tmp_path) == []


def test_same_id_at_two_nested_ancestor_descendant_scopes_is_flagged(tmp_path: Path):
  """A nested scope and one further nested beneath it (`futures/`, `futures/usdt/`) are on
  the same path to root exactly like the client-root case -- a leaf under `futures/usdt/`
  would inherit both, so a shared id collides the identical way."""
  write_schemas(tmp_path, 'futures', ids=['FuturesSide'])
  write_schemas(tmp_path, 'futures', 'usdt', ids=['FuturesSide'])

  violations = check_schemas_no_shadowing(tmp_path)

  assert len(violations) == 1
  assert violations[0]['rule'] == 'schemas-shadowing'


def test_distinct_ids_report_no_findings(tmp_path: Path):
  """Two scopes on the same path to root, each declaring only its own distinct ids, is
  the ordinary case and reports nothing."""
  write_schemas(tmp_path, ids=['OrderSide'])
  write_schemas(tmp_path, 'futures', ids=['FuturesSide', 'FuturesMode'])
  assert check_schemas_no_shadowing(tmp_path) == []
