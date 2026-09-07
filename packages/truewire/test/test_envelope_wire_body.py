"""ADR 0010: the response schema describes the wire body, and `envelope.payload` selects the
value the generated method returns.

`truewire check` validates a recording whole; the audit's `envelope` rule requires the path
to name a property of the schema; pagination paths resolve inside the selected value; the
generator types the return value from the selected node and never renders the wrapper; and
the core contract is untouched -- a core that unwraps still unwraps, and the method still
hands back the unwrapped value.
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from truewire.cli import app
from truewire.project import resolve
from truewire.spec import Endpoint, select_schema
from truewire.spec.authoring import audit

WIRE = 'Wire envelope field; see the core.'

PET = {
  'title': 'Pet', 'type': 'object', 'description': 'One pet.', 'required': ['id'],
  'properties': {
    'id': {'type': 'integer', 'description': 'Pet id.'},
    'name': {'type': 'string', 'description': 'Pet name.'},
  },
}

PET_FRAME = {
  'title': 'PetFrame', 'type': 'object', 'description': 'Wire frame.',
  'required': ['error', 'result'],
  'properties': {
    'error': {'type': 'array', 'items': {'type': 'string'}, 'description': WIRE},
    'result': PET,
  },
}

PAGE = {
  'title': 'PetPage', 'type': 'object', 'description': 'One page of pets.',
  'required': ['items'],
  'properties': {
    'items': {'type': 'array', 'description': 'Pets on this page.', 'items': PET},
    'count': {'type': 'integer', 'description': 'Pets in total.'},
  },
}

PAGE_FRAME = {
  'title': 'PetPageFrame', 'type': 'object', 'description': 'Wire frame.',
  'required': ['error', 'result'],
  'properties': {
    'error': {'type': 'array', 'items': {'type': 'string'}, 'description': WIRE},
    'result': PAGE,
  },
}


def endpoint_json(response: dict, *, pagination: dict | None = None, request: dict | None = None) -> dict:
  data = {
    'docs': 'https://example.com/docs/pets',
    'meta': {'public': True},
    'spec': {
      'kind': 'rpc', 'transports': ['http'], 'path': '/pets', 'method': 'GET',
      'description': 'List pets.',
      'request': request or {
        'title': 'ListPetsRequest', 'type': 'object',
        'properties': {'kind': {'type': 'string', 'description': 'Kind of pet.'}},
      },
      'response': response,
    },
    'envelope': {'payload': 'result'},
  }
  if pagination is not None:
    data['pagination'] = pagination
  return data


def load(data: dict) -> Endpoint:
  return Endpoint.model_validate(data)


# --- select_schema -----------------------------------------------------------------------

def test_select_schema_walks_properties():
  assert select_schema(PET_FRAME, 'result') is PET
  assert select_schema(PET_FRAME, 'result.name') == {'type': 'string', 'description': 'Pet name.'}
  assert select_schema(PET_FRAME, '') is PET_FRAME


def test_select_schema_walks_a_ref_through_shared_schemas():
  frame = {'title': 'Frame', 'type': 'object', 'properties': {'data': {'$ref': 'Envelope', 'description': 'x'}}}
  shared = {'Envelope': {'title': 'Envelope', 'type': 'object', 'properties': {'rows': {'type': 'array'}}}}
  assert select_schema(frame, 'data.rows', shared=shared) == {'type': 'array'}
  # A `$ref` node itself is returned as written, so the caller renders the reference.
  assert select_schema(frame, 'data', shared=shared) == {'$ref': 'Envelope', 'description': 'x'}
  with pytest.raises(LookupError, match='cannot resolve'):
    select_schema(frame, 'data.rows')


def test_select_schema_walks_positional_rows():
  frame = {'type': 'array', 'prefixItems': [{'type': 'integer'}, {'title': 'Row', 'type': 'object', 'properties': {'a': {'type': 'string'}}}]}
  assert select_schema(frame, '[1].a') == {'type': 'string'}
  assert select_schema({'type': 'array', 'items': {'type': 'string'}}, '[0]') == {'type': 'string'}


def test_select_schema_names_the_missing_segment():
  with pytest.raises(LookupError, match='`payload` is not a property of `<root>`, which declares `error`, `result`'):
    select_schema(PET_FRAME, 'payload')
  with pytest.raises(LookupError, match='anyOf'):
    select_schema({'anyOf': [PET_FRAME, {'type': 'null'}]}, 'result')


# --- audit -------------------------------------------------------------------------------

def test_audit_flags_a_payload_path_the_schema_does_not_carry():
  violations = [v for v in audit(load(endpoint_json(PET))) if v['rule'] == 'envelope']
  assert len(violations) == 1
  assert violations[0]['location'] == 'envelope.payload'
  assert '`result` names no property of the response schema' in violations[0]['message']
  assert 'truewire migrate' in violations[0]['message']


def test_audit_accepts_a_payload_path_the_schema_carries():
  assert [v for v in audit(load(endpoint_json(PET_FRAME))) if v['rule'] == 'envelope'] == []


def test_audit_leaves_a_ref_response_undecided():
  data = endpoint_json({'$ref': 'Frame', 'description': 'Frame.'})
  assert [v for v in audit(load(data)) if v['rule'] == 'envelope'] == []


def test_audit_flags_a_scalar_response_that_cannot_carry_the_path():
  data = endpoint_json({'type': 'string', 'description': 'A bare string.'})
  assert [v['rule'] for v in audit(load(data)) if v['rule'] == 'envelope'] == ['envelope']


def test_pagination_paths_are_relative_to_the_selected_value():
  request = {
    'title': 'ListPetsRequest', 'type': 'object',
    'properties': {
      'page': {'type': 'integer', 'description': 'Page index.'},
      'per_page': {'type': 'integer', 'description': 'Page size.'},
    },
  }
  relative = {
    'strategy': 'page', 'index': {'parameter': 'page', 'start': 1},
    'size': {'parameter': 'per_page'},
    'done': {'kind': 'total', 'path': 'count', 'counts': 'items', 'rows': 'items'},
  }
  clean = audit(load(endpoint_json(PAGE_FRAME, pagination=relative, request=request)))
  assert [v for v in clean if v['rule'] == 'pagination'] == []

  rooted = {**relative, 'done': {'kind': 'total', 'path': 'result.count', 'counts': 'items', 'rows': 'result.items'}}
  found = audit(load(endpoint_json(PAGE_FRAME, pagination=rooted, request=request)))
  assert sorted(v['location'] for v in found if v['rule'] == 'pagination') == [
    'pagination.done.path', 'pagination.done.rows',
  ]


# --- truewire check ----------------------------------------------------------------------

def write_project(tmp_path: Path, monkeypatch, *, response: dict, frame: dict, pagination: dict | None = None, request: dict | None = None) -> Path:
  runner = CliRunner()
  monkeypatch.chdir(tmp_path)
  assert runner.invoke(app, ['init', 'demo']).exit_code == 0
  project = tmp_path / 'demo'
  group = project / 'spec' / 'endpoints' / 'pets'
  (group / 'list' / 'examples').mkdir(parents=True)
  (group / 'router.json').write_text(json.dumps({
    'description': 'Pets.', 'upstream': 'https://example.com/docs', 'core': 'default',
  }))
  (group / 'list' / 'endpoint.json').write_text(json.dumps(endpoint_json(response, pagination=pagination, request=request)))
  (group / 'list' / 'examples' / 'default.request.json').write_text(json.dumps({'request': {}}))
  (group / 'list' / 'examples' / 'default.response.json').write_text(json.dumps({'status': 200, 'payload': frame}))
  return project


def test_check_validates_the_recorded_frame_against_the_whole_schema(tmp_path: Path, monkeypatch):
  project = write_project(tmp_path, monkeypatch, response=PET_FRAME, frame={'error': [], 'result': {'id': 1, 'name': 'Rex'}})
  result = CliRunner().invoke(app, ['check', '--project', str(project)])
  assert result.exit_code == 0, result.output
  assert 'files      1\n  errors     0' in result.output


def test_check_fails_a_schema_written_for_the_unwrapped_value(tmp_path: Path, monkeypatch):
  project = write_project(tmp_path, monkeypatch, response=PET, frame={'error': [], 'result': {'id': 1, 'name': 'Rex'}})
  result = CliRunner().invoke(app, ['check', '--project', str(project)])
  assert result.exit_code != 0
  assert "<root>: 'id' is a required property" in result.output
  assert '`result` names no property of the response schema' in result.output


def test_check_fails_a_frame_the_schema_does_not_describe(tmp_path: Path, monkeypatch):
  project = write_project(tmp_path, monkeypatch, response=PET_FRAME, frame={'result': {'id': 1}})
  result = CliRunner().invoke(app, ['check', '--project', str(project)])
  assert result.exit_code != 0
  assert "<root>: 'error' is a required property" in result.output


# --- generate ----------------------------------------------------------------------------

UNWRAP_OLD = '''    if check:
      return validator(cast(type, response_type)).json(raw)
    import json
    return json.loads(raw)
'''
UNWRAP_NEW = '''    import json
    value = json.loads(raw)['result']
    if check:
      return validator(cast(type, response_type)).python(value)
    return value
'''


def test_generated_method_returns_the_selected_value(tmp_path: Path, monkeypatch):
  """The generator types the return value from the node `envelope.payload` selects, never
  renders the frame, and the method returns what the (unwrapping) core hands back."""
  request = {
    'title': 'ListPetsRequest', 'type': 'object',
    'properties': {
      'page': {'type': 'integer', 'description': 'Page index.'},
      'per_page': {'type': 'integer', 'description': 'Page size.'},
    },
  }
  pagination = {
    'strategy': 'page', 'index': {'parameter': 'page', 'start': 1},
    'size': {'parameter': 'per_page'}, 'done': {'kind': 'short_page', 'rows': 'items'},
  }
  frame = {'error': [], 'result': {'items': [{'id': 1, 'name': 'Rex'}], 'count': 1}}
  project = write_project(tmp_path, monkeypatch, response=PAGE_FRAME, frame=frame, pagination=pagination, request=request)
  (project / 'spec' / 'endpoints' / 'pets' / 'list' / 'examples' / 'default.request.json').write_text(
    json.dumps({'request': {'page': 1, 'per_page': 2}})
  )
  core = project / 'src' / 'demo' / 'core' / '__init__.py'
  assert UNWRAP_OLD in core.read_text()
  core.write_text(core.read_text().replace(UNWRAP_OLD, UNWRAP_NEW))

  runner = CliRunner()
  assert runner.invoke(app, ['check', '--project', str(project)]).exit_code == 0
  generated = runner.invoke(app, ['generate', 'python', '--project', str(project)])
  assert generated.exit_code == 0, generated.output
  source = (project / 'src' / 'demo' / 'pets' / 'list.py').read_text()
  assert '-> PetPage:' in source
  assert 'response_type=PetPage' in source
  assert 'class Pet(TypedDict):' in source
  assert 'PetPageFrame' not in source
  assert 'error' not in source.split('class PetPage(TypedDict):')[1].split('class ')[0]
  assert 'PaginatedResponse[Pet' in source

  from truewire.mock import running_mock_servers

  sys.path.insert(0, str(project / 'src'))
  try:
    for name in [m for m in sys.modules if m == 'demo' or m.startswith('demo.')]:
      del sys.modules[name]
    from demo import Demo  # type: ignore[import-not-found]

    async def call(base_url: str):
      async with Demo.new(base_url=base_url) as client:
        page = await client.pets.list(page=1, per_page=2)
        rows = await client.pets.list_paged(per_page=2)
        return page, rows

    with running_mock_servers(resolve(project)) as servers:
      page, rows = asyncio.run(call(servers.http_base_url))
    assert page == {'items': [{'id': 1, 'name': 'Rex'}], 'count': 1}
    assert rows == [{'id': 1, 'name': 'Rex'}]
  finally:
    sys.path.remove(str(project / 'src'))
    for name in [m for m in sys.modules if m == 'demo' or m.startswith('demo.')]:
      del sys.modules[name]
