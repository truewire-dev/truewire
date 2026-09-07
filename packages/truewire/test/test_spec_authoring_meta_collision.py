"""
Exercise `truewire.spec.authoring.check_meta` (design §2/§6) against synthetic
fixtures, never `clients/` -- mirrors `test_spec_authoring_schemas_shadowing.py`'s own
isolation style for its neighboring `check_schemas_no_shadowing`.

`check_meta` reports two independent rules: `meta-schema` (an endpoint's own `meta` dict
fails to validate against its resolved core's declared `meta` schema) and
`meta-collision` (a property name declared by that schema also appears in the endpoint's
own `request`/`parameters` schema -- a real collision risk, the same shape S8 flags for
`validate`). Both need `codegen/config.toml`'s new top-level `[cores.<name>]` table (a
venue fact, sibling to `[python]`) resolved against `router.json`'s own `core` field, exactly
the ancestor walk `_resolve_core`/`_meta_core_name` perform.
"""
import json
from pathlib import Path
from typing_extensions import Any

from truewire.spec.authoring import check_meta


def toml_inline(value: Any) -> str:
  """Render a plain Python value (dict/list/str/bool/number) as a TOML inline-table
  literal -- `tomllib` (stdlib) only reads TOML, and no TOML writer is a dependency
  here, so fixture schemas below are built as real Python dicts and rendered through
  this rather than hand-typing nested `{ key = value }` TOML by hand for every case."""
  if isinstance(value, dict):
    return '{ ' + ', '.join(f'{k} = {toml_inline(v)}' for k, v in value.items()) + ' }'
  if isinstance(value, list):
    return '[' + ', '.join(toml_inline(v) for v in value) + ']'
  if isinstance(value, bool):
    return 'true' if value else 'false'
  if isinstance(value, str):
    return json.dumps(value)
  if isinstance(value, (int, float)):
    return str(value)
  raise TypeError(f'unsupported TOML fixture value: {value!r}')


def write_router(root: Path, *segments: str, core: str) -> None:
  """Write a `router.json` declaring `core` at `spec/endpoints/<segments>`.

  `segments` is empty for the client's own root `router.json`.
  """
  directory = root / 'spec' / 'endpoints'
  for segment in segments:
    directory = directory / segment
  directory.mkdir(parents=True, exist_ok=True)
  (directory / 'router.json').write_text(json.dumps({
    'description': 'Test router.', 'upstream': 'https://example.com/docs', 'core': core,
  }))


def write_codegen_toml(root: Path, *, cores: dict[str, dict | None]) -> None:
  """Write a minimal `codegen/config.toml`: a `[python]` section resolving every name in
  `cores` to a dummy base class, plus a top-level `[cores.<name>]` entry for each one --
  present (even with no `meta` line under it, matching the design doc's own "no meta --
  most cores need none" worked example) so a test can distinguish "this core's own
  `[cores.<name>]` entry declares no schema" from "this client's `codegen/config.toml`
  has no top-level `[cores]` table at all"."""
  lines = ['[python]', 'name = "X"', '']
  for name in cores:
    lines.append(f'[python.cores.{name}]')
    lines.append(f'base = "x.core:{name.capitalize()}Endpoint"')
    lines.append('')
  config_path = root / 'truewire.toml'
  config_path.write_text('\n'.join(lines))
  # A separate write, appended, since `codegen/config.toml`'s top-level `[cores.*]`
  # table (§6) is language-neutral and sits above `[python]` in the file the design
  # doc's own worked example shows -- order doesn't matter to the TOML parser, but this
  # keeps the fixture readable in the same shape.
  cores_lines = []
  for name, meta_schema in cores.items():
    cores_lines.append(f'[cores.{name}]')
    if meta_schema is not None:
      cores_lines.append(f'meta = {toml_inline(meta_schema)}')
    cores_lines.append('')
  existing = config_path.read_text()
  config_path.write_text('\n'.join(cores_lines) + '\n' + existing)


def write_endpoint(
  root: Path, *segments: str, meta: dict, request_properties: dict[str, dict] = {},
) -> None:
  """Write a minimal new-shape (`request`/`response`) `endpoint.json` at
  `spec/endpoints/<segments>/endpoint.json`."""
  directory = root / 'spec' / 'endpoints'
  for segment in segments:
    directory = directory / segment
  directory.mkdir(parents=True, exist_ok=True)
  (directory / 'endpoint.json').write_text(json.dumps({
    'meta': meta,
    'spec': {
      'kind': 'rpc', 'transports': ['http'], 'path': '/x', 'method': 'GET',
      'request': {
        'title': 'Request', 'type': 'object', 'properties': request_properties,
      },
      'response': {'title': 'Response', 'type': 'object', 'properties': {}},
    },
  }))


def test_no_codegen_toml_is_unchecked(tmp_path: Path):
  """A legacy, not-yet-migrated client has no `codegen/config.toml` at all -- `meta`
  stays fully unchecked, exactly as `auth` always was."""
  write_endpoint(tmp_path, 'x', meta={'anything': True})
  assert check_meta(tmp_path) == []


def test_core_with_no_declared_schema_is_unchecked(tmp_path: Path):
  """Most cores need no `meta` schema at all (design §6) -- an endpoint resolving to
  one is not checked, and its non-empty `meta` is not flagged here (the generation-time
  `ValueError` in `rpc_endpoint`/`stream_endpoint` is the backstop for that case, not
  this authoring-time check)."""
  write_router(tmp_path, core='default')
  write_codegen_toml(tmp_path, cores={'default': None})
  write_endpoint(tmp_path, 'x', meta={'public': True})
  assert check_meta(tmp_path) == []


def test_valid_meta_against_declared_schema_is_clean(tmp_path: Path):
  """A `meta` dict that validates cleanly against its resolved core's declared schema
  reports nothing."""
  write_router(tmp_path, core='exchange')
  write_codegen_toml(tmp_path, cores={'exchange': {
    'type': 'object',
    'properties': {
      'scheme': {'type': 'string', 'enum': ['l1', 'user_signed']},
      'action': {'type': 'string', 'enum': ['ordinary', 'batched']},
    },
    'required': ['scheme'],
  }})
  write_endpoint(tmp_path, 'order', meta={'scheme': 'l1', 'action': 'batched'})
  assert check_meta(tmp_path) == []


def test_meta_missing_a_required_field_is_flagged(tmp_path: Path):
  """A `meta` dict missing the schema's own required field fails validation."""
  write_router(tmp_path, core='exchange')
  write_codegen_toml(tmp_path, cores={'exchange': {
    'type': 'object',
    'properties': {'scheme': {'type': 'string', 'enum': ['l1', 'user_signed']}},
    'required': ['scheme'],
  }})
  write_endpoint(tmp_path, 'order', meta={})

  violations = check_meta(tmp_path)

  assert len(violations) == 1
  assert violations[0]['rule'] == 'meta-schema'
  assert 'scheme' in violations[0]['message']


def test_meta_with_wrong_type_is_flagged(tmp_path: Path):
  """A `meta` field whose value doesn't match its declared schema type fails validation."""
  write_router(tmp_path, core='exchange')
  write_codegen_toml(tmp_path, cores={'exchange': {
    'type': 'object',
    'properties': {
      'scheme': {'type': 'string', 'enum': ['l1', 'user_signed']},
      'vault_scoped': {'type': 'boolean'},
    },
    'required': ['scheme'],
  }})
  write_endpoint(tmp_path, 'order', meta={'scheme': 'l1', 'vault_scoped': 'yes'})

  violations = check_meta(tmp_path)

  assert len(violations) == 1
  assert violations[0]['rule'] == 'meta-schema'


def test_meta_with_unknown_property_under_additional_properties_false_is_flagged(
  tmp_path: Path,
):
  """`additionalProperties: false` closes the schema -- a stray key the schema doesn't
  declare fails validation, the same way a wire request typo would."""
  write_router(tmp_path, core='exchange')
  write_codegen_toml(tmp_path, cores={'exchange': {
    'type': 'object',
    'properties': {'scheme': {'type': 'string', 'enum': ['l1', 'user_signed']}},
    'required': ['scheme'],
    'additionalProperties': False,
  }})
  write_endpoint(tmp_path, 'order', meta={'scheme': 'l1', 'schema': 'l1'})

  violations = check_meta(tmp_path)

  assert len(violations) == 1
  assert violations[0]['rule'] == 'meta-schema'


def test_meta_schema_property_colliding_with_request_property_is_flagged(tmp_path: Path):
  """A property name the resolved core's `meta` schema declares (`scheme`) also
  appearing in the endpoint's own `request` schema is a real collision risk -- flagged
  even though nothing about *this* endpoint's own recorded `meta` dict is invalid."""
  write_router(tmp_path, core='exchange')
  write_codegen_toml(tmp_path, cores={'exchange': {
    'type': 'object',
    'properties': {'scheme': {'type': 'string', 'enum': ['l1', 'user_signed']}},
    'required': ['scheme'],
  }})
  write_endpoint(
    tmp_path, 'order', meta={'scheme': 'l1'},
    request_properties={'scheme': {'type': 'string'}},
  )

  violations = check_meta(tmp_path)

  assert len(violations) == 1
  assert violations[0]['rule'] == 'meta-collision'
  assert 'scheme' in violations[0]['message']


def test_no_collision_when_property_names_are_disjoint(tmp_path: Path):
  """The ordinary case: a resolved core's `meta` schema and the endpoint's own request
  schema declare disjoint property names."""
  write_router(tmp_path, core='exchange')
  write_codegen_toml(tmp_path, cores={'exchange': {
    'type': 'object',
    'properties': {'scheme': {'type': 'string', 'enum': ['l1', 'user_signed']}},
    'required': ['scheme'],
  }})
  write_endpoint(
    tmp_path, 'order', meta={'scheme': 'l1'},
    request_properties={'symbol': {'type': 'string'}},
  )
  assert check_meta(tmp_path) == []


def test_nested_endpoint_resolves_core_via_nearest_ancestor_router(tmp_path: Path):
  """The identical nearest-ancestor `router.json` walk `_resolve_core` itself uses --
  a leaf two levels under the declaring `router.json` still resolves the same core."""
  write_router(tmp_path, core='root')
  write_router(tmp_path, 'exchange', core='exchange')
  write_codegen_toml(tmp_path, cores={'root': None, 'exchange': {
    'type': 'object',
    'properties': {'scheme': {'type': 'string', 'enum': ['l1', 'user_signed']}},
    'required': ['scheme'],
  }})
  write_endpoint(tmp_path, 'exchange', 'orders', 'place', meta={})

  violations = check_meta(tmp_path)

  assert len(violations) == 1
  assert violations[0]['rule'] == 'meta-schema'
  assert 'exchange/orders/place' in violations[0]['location']
