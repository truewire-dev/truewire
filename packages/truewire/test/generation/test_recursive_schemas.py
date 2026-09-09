"""Reproduction and regression tests for self-referential (recursive) schemas.

A recursive record is an ordinary API shape -- a comment thread, a file tree, a nested
JSON value -- and Python expresses it with a forward reference (`child: NotRequired
['Node']`), which a `TypedDict` accepts. Four shapes are pinned here:

1. a record referencing itself directly (`Node.child: Node`);
2. a record referencing itself through an array (`Node.children: Node[]`);
3. two records referencing each other (`Node.branch: Branch`, `Branch.node: Node`);
4. a cycle among schemas that render *inline* rather than as classes
   (`Tree = string | Tree[]`), which has no Python rendering at all.

1 and 2 already worked. 3 and 4 are the reproduction: with `origin/main` at 4d97683,

    $ truewire check                       # Result: OK
    $ truewire generate python
    CircularDependencyError: Circular dependencies exist among these items:
    {Node:{'Branch'}, Branch:{'Node'}}

for 3, raised from `toposort_flatten` under
`truewire.generation.types.references.generation_order`, and

    $ truewire check                       # Result: OK
    $ truewire generate python
    RecursionError: maximum recursion depth exceeded in __instancecheck__
      truewire/generation/schema/resolve.py:36

for 4, whose stack is `InlineSchemas.map` -> `path_map` -> `InlineSchemas.map` (nine
frames of `truewire/generation/types/maps.py` per hop) bottoming out in whichever call
happens to exhaust the stack -- here `LocalResolver.__call__`'s own `rec`. Both passed
`truewire check` first and died in generation.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from pydantic import TypeAdapter
from typer.testing import CliRunner

from truewire.cli import app
from truewire.generation.python.types import TypeGenerator
from truewire.generation.python.types.code import quote_self_reference
from truewire.generation.schema import Schema, SchemaCycleError
from truewire.spec.authoring import check_schema_cycles


def generate(schemas: dict[str, dict], *, inline: bool = True):
  """Run the full Python type pipeline over raw JSON Schema fragments."""
  return TypeGenerator()({k: Schema.model_validate(v) for k, v in schemas.items()}, inline=inline)


NODE_SELF = {
  'Node': {
    'title': 'Node',
    'type': 'object',
    'description': 'A node in a tree.',
    'required': ['id'],
    'properties': {
      'id': {'type': 'string', 'description': 'Node id.'},
      'child': {'$ref': 'Node', 'description': 'The single child node, when present.'},
    },
  },
}
"""Rule 1: a record whose own property is a reference back to itself."""

NODE_ARRAY = {
  'Node': {
    'title': 'Node',
    'type': 'object',
    'description': 'A node in a tree.',
    'required': ['id'],
    'properties': {
      'id': {'type': 'string', 'description': 'Node id.'},
      'children': {
        'type': 'array',
        'description': 'Child nodes.',
        'items': {'$ref': 'Node'},
      },
    },
  },
}
"""Rule 2: a record that reaches itself through an array's `items`."""

MUTUAL = {
  'Node': {
    'title': 'Node',
    'type': 'object',
    'description': 'A node in a tree.',
    'required': ['id'],
    'properties': {
      'id': {'type': 'string', 'description': 'Node id.'},
      'branch': {'$ref': 'Branch', 'description': 'The branch below this node.'},
    },
  },
  'Branch': {
    'title': 'Branch',
    'type': 'object',
    'description': 'A branch holding one node.',
    'required': ['label'],
    'properties': {
      'label': {'type': 'string', 'description': 'Branch label.'},
      'node': {'$ref': 'Node', 'description': 'The node this branch holds.'},
    },
  },
}
"""Rule 3: two records that reference each other."""

INLINE_CYCLE = {
  'Tree': {
    'title': 'Tree',
    'description': 'A leaf or a list of trees.',
    'anyOf': [
      {'type': 'string', 'description': 'A leaf.'},
      {'type': 'array', 'description': 'A subtree.', 'items': {'$ref': 'Tree'}},
    ],
  },
}
"""Rule 4: a cycle among schemas that render inline, which cannot be expressed at all."""

DICT_CYCLE = {
  'Node': {
    'title': 'Node',
    'type': 'object',
    'description': 'A tree of trees.',
    'additionalProperties': {'$ref': 'Node'},
  },
}
"""A record-shaped cycle that still renders as an expression: `properties` is absent, so
`Node` is a `dict[str, Node]` alias, not a class, and the alias names itself."""


class TestSupportedRecursion:
  """A cycle that passes through a record renders, with the forward reference quoted."""

  def test_direct_self_reference(self):
    rendered = generate(NODE_SELF)
    assert "child: NotRequired['Node']" in rendered.definitions['Node']

  def test_self_reference_through_array(self):
    rendered = generate(NODE_ARRAY)
    assert "children: NotRequired[list['Node']]" in rendered.definitions['Node']

  def test_mutual_recursion(self):
    rendered = generate(MUTUAL)
    assert set(rendered.generation_order) == {'Node', 'Branch'}

  def test_mutual_recursion_quotes_the_reference_that_points_forward(self):
    """Whichever record is emitted first names the other before it is bound."""
    rendered = generate(MUTUAL)
    first, second = rendered.generation_order
    assert f"NotRequired['{second}']" in rendered.definitions[first]
    assert f'NotRequired[{first}]' in rendered.definitions[second]

  def test_generation_order_is_unchanged_without_a_cycle(self):
    """Collapsing cycles must not reorder a module that has none."""
    acyclic = {
      'Branch': MUTUAL['Branch'],
      'Node': {**MUTUAL['Node'], 'properties': {'id': {'type': 'string'}}},
    }
    assert generate(acyclic).generation_order == ['Node', 'Branch']


class TestUnsupportedRecursion:
  """A cycle among inline-rendered schemas is refused, not crashed on."""

  def test_inline_cycle_raises_a_named_error(self):
    with pytest.raises(SchemaCycleError) as raised:
      generate(INLINE_CYCLE)
    assert raised.value.cycle == ('Tree',)

  def test_alias_cycle_raises_rather_than_emitting_an_unbound_name(self):
    """`Node = dict[str, Node]` would raise `NameError` the moment it is imported."""
    with pytest.raises(SchemaCycleError) as raised:
      generate(DICT_CYCLE)
    assert raised.value.cycle == ('Node',)

  def test_indirect_inline_cycle_names_every_schema_on_it(self):
    with pytest.raises(SchemaCycleError) as raised:
      generate({
        'Leaves': {'type': 'array', 'items': {'$ref': 'Tree'}},
        'Tree': {'anyOf': [{'type': 'string'}, {'$ref': 'Leaves'}]},
      })
    assert set(raised.value.cycle) == {'Leaves', 'Tree'}


def write_project(tmp_path: Path, runner: CliRunner, schemas: dict[str, dict]) -> Path:
  """A minimal one-endpoint project whose response is a `$ref` into `schemas`."""
  assert runner.invoke(app, ['init', 'demo']).exit_code == 0
  project = tmp_path / 'demo'
  (project / 'spec' / 'schemas.json').write_text(json.dumps(schemas, indent=2) + '\n')
  group = project / 'spec' / 'endpoints' / 'nodes'
  (group / 'get').mkdir(parents=True)
  (group / 'router.json').write_text(json.dumps({
    'description': 'Nodes: a recursive tree.',
    'upstream': 'https://api.example.com/nodes',
    'core': 'default',
  }))
  (group / 'get' / 'endpoint.json').write_text(json.dumps({
    'docs': 'https://api.example.com/docs/nodes',
    'meta': {'public': True},
    'spec': {
      'kind': 'rpc',
      'transports': ['http'],
      'path': '/nodes/{id}',
      'method': 'GET',
      'description': 'Get one node and its subtree.',
      'request': {
        'title': 'GetNodeRequest',
        'type': 'object',
        'description': 'Which node.',
        'required': ['id'],
        'properties': {'id': {'type': 'string', 'description': 'Node id.'}},
      },
      'response': {'$ref': next(iter(schemas)), 'description': 'The node.'},
    },
  }))
  return project



def import_generated(path: Path):
  """Import one generated module by path, so its annotations are evaluated for real."""
  spec = importlib.util.spec_from_file_location(f'generated_{path.stem}_{id(path)}', path)
  assert spec is not None and spec.loader is not None
  module = importlib.util.module_from_spec(spec)
  sys.modules[spec.name] = module
  spec.loader.exec_module(module)
  return module

class TestEndToEnd:
  """`truewire check` must never pass a spec `truewire generate python` cannot render."""

  def test_self_reference_generates(self, tmp_path: Path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    project = write_project(tmp_path, runner, NODE_SELF)
    assert runner.invoke(app, ['check', '--project', str(project)]).exit_code == 0
    result = runner.invoke(app, ['generate', 'python', '--project', str(project)])
    assert result.exit_code == 0, result.output
    source = (project / 'src' / 'demo' / 'schemas.py').read_text()
    assert "child: NotRequired['Node']" in source

  def test_mutual_recursion_generates(self, tmp_path: Path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    project = write_project(tmp_path, runner, MUTUAL)
    assert runner.invoke(app, ['check', '--project', str(project)]).exit_code == 0
    result = runner.invoke(app, ['generate', 'python', '--project', str(project)])
    assert result.exit_code == 0, result.output
    source = (project / 'src' / 'demo' / 'schemas.py').read_text()
    assert source.index('class Branch') < source.index('class Node')
    assert "node: NotRequired['Node']" in source

  def test_generated_recursion_validates_at_runtime(self, tmp_path: Path, monkeypatch):
    """A quoted forward reference has to resolve when the client actually validates a
    response, not just satisfy a type checker reading the source."""
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    project = write_project(tmp_path, runner, MUTUAL)
    assert runner.invoke(app, ['generate', 'python', '--project', str(project)]).exit_code == 0
    module = import_generated(project / 'src' / 'demo' / 'schemas.py')
    payload = {'id': 'a', 'branch': {'label': 'left', 'node': {'id': 'b'}}}
    assert TypeAdapter(module.Node).validate_python(payload) == payload

  def test_inline_cycle_fails_check(self, tmp_path: Path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    project = write_project(tmp_path, runner, INLINE_CYCLE)
    result = runner.invoke(app, ['check', '--project', str(project)])
    assert result.exit_code != 0, result.output
    assert 'Tree references itself' in result.output
    assert 'rule 17' in result.output

  def test_inline_cycle_refused_by_generation_too(self, tmp_path: Path, monkeypatch):
    """The gate and the generator must agree about what is renderable."""
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    project = write_project(tmp_path, runner, INLINE_CYCLE)
    result = runner.invoke(app, ['generate', 'python', '--project', str(project)])
    assert result.exit_code != 0
    assert not isinstance(result.exception, RecursionError)

  def test_array_self_reference_generates(self, tmp_path: Path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    project = write_project(tmp_path, runner, NODE_ARRAY)
    assert runner.invoke(app, ['check', '--project', str(project)]).exit_code == 0
    result = runner.invoke(app, ['generate', 'python', '--project', str(project)])
    assert result.exit_code == 0, result.output
    source = (project / 'src' / 'demo' / 'schemas.py').read_text()
    assert "children: NotRequired[list['Node']]" in source


ENUM_NAMED_AFTER_ITSELF = {
  'Alert': {
    'title': 'Alert',
    'type': 'object',
    'description': 'One alert.',
    'required': ['id', 'messageType'],
    'properties': {
      'id': {'type': 'string', 'description': 'Alert id.'},
      'messageType': {
        'type': 'string',
        'enum': ['Alert', 'Update', 'Cancel'],
        'description': 'Whether this issues, updates or cancels.',
      },
      'supersedes': {'$ref': 'Alert', 'description': 'The alert this one replaces.'},
    },
  },
}
"""A record whose own enum carries a member spelled exactly like the record.

`api.weather.gov` really does this: a CAP alert's `messageType` is one of
`Alert`/`Update`/`Cancel`, inside the record named `Alert`.
"""


class TestSelfReferenceQuotingSkipsStringLiterals:
  """`quote_self_reference` quotes bare type names, never text inside a string literal.

  The substitution is textual, over an already-rendered type expression, so it has to know
  what in that expression is a name and what is a value. It did not: `\\bAlert\\b` matched the
  member of `Literal['Alert', 'Update', 'Cancel']` inside the record named `Alert` and
  rewrote it to `''Alert''` -- a syntax error, from a value that was never a reference.
  """

  def test_a_bare_reference_is_quoted(self):
    assert quote_self_reference('list[Alert]', 'Alert') == "list['Alert']"

  def test_an_enum_member_spelled_like_the_record_is_left_alone(self):
    expr = "Literal['Alert', 'Update', 'Cancel']"
    assert quote_self_reference(expr, 'Alert') == expr

  def test_an_already_quoted_reference_is_not_quoted_twice(self):
    assert quote_self_reference("list['Alert']", 'Alert') == "list['Alert']"

  def test_a_bare_reference_beside_an_enum_member_is_still_quoted(self):
    assert (
      quote_self_reference("Literal['Alert'] | list[Alert]", 'Alert')
      == "Literal['Alert'] | list['Alert']"
    )

  def test_a_longer_name_containing_the_record_is_untouched(self):
    assert quote_self_reference('AlertReference', 'Alert') == 'AlertReference'

  def test_the_generated_module_parses_and_validates(self, tmp_path: Path, monkeypatch):
    """The end of the bug: the module was a syntax error, so nothing downstream ran."""
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    project = write_project(tmp_path, runner, ENUM_NAMED_AFTER_ITSELF)
    assert runner.invoke(app, ['check', '--project', str(project)]).exit_code == 0
    result = runner.invoke(app, ['generate', 'python', '--project', str(project)])
    assert result.exit_code == 0, result.output
    source = (project / 'src' / 'demo' / 'schemas.py').read_text()
    assert "Literal['Alert', 'Update', 'Cancel']" in source
    assert "''Alert''" not in source
    module = import_generated(project / 'src' / 'demo' / 'schemas.py')
    payload = {'id': 'a', 'messageType': 'Alert', 'supersedes': {'id': 'b', 'messageType': 'Cancel'}}
    assert TypeAdapter(module.Alert).validate_python(payload) == payload


class TestCycleAudit:
  """`check_schema_cycles` reports only the cycles nothing can render."""

  def test_a_record_cycle_is_not_a_violation(self, tmp_path: Path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    project = write_project(tmp_path, runner, MUTUAL)
    assert check_schema_cycles(project) == []

  def test_an_inline_cycle_is_one_violation_naming_every_schema(self, tmp_path: Path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    project = write_project(tmp_path, runner, {
      'Leaves': {'type': 'array', 'description': 'Trees.', 'items': {'$ref': 'Tree'}},
      'Tree': {
        'title': 'Tree',
        'description': 'A leaf or some trees.',
        'anyOf': [{'type': 'string'}, {'$ref': 'Leaves'}],
      },
    })
    violations = check_schema_cycles(project)
    assert len(violations) == 1
    assert violations[0]['rule'] == 'schema-cycle'
    assert violations[0]['location'] == 'Leaves -> Tree -> Leaves'

  def test_an_alias_cycle_is_a_violation(self, tmp_path: Path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    project = write_project(tmp_path, runner, DICT_CYCLE)
    assert [v['location'] for v in check_schema_cycles(project)] == ['Node -> Node']
    assert runner.invoke(app, ['check', '--project', str(project)]).exit_code != 0

  def test_an_all_of_pair_is_not_a_cycle_once_flattened(self, tmp_path: Path, monkeypatch):
    """`allOf` is flattened into properties, which deletes the references it read."""
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    project = write_project(tmp_path, runner, {
      'A': {'title': 'A', 'description': 'A.', 'allOf': [{'$ref': 'B'}]},
      'B': {'title': 'B', 'description': 'B.', 'allOf': [{'$ref': 'A'}]},
    })
    assert check_schema_cycles(project) == []

  def test_a_project_with_no_shared_schemas_reports_nothing(self, tmp_path: Path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ['init', 'bare']).exit_code == 0
    assert check_schema_cycles(tmp_path / 'bare') == []
