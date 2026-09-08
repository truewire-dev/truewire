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
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from truewire.cli import app
from truewire.generation.python.types import TypeGenerator
from truewire.generation.schema import Schema, SchemaCycleError


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

  @pytest.mark.xfail(strict=True, reason='reproduction: `truewire check` passes a spec that cannot render')
  def test_inline_cycle_fails_check(self, tmp_path: Path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    project = write_project(tmp_path, runner, INLINE_CYCLE)
    result = runner.invoke(app, ['check', '--project', str(project)])
    assert result.exit_code != 0, result.output
