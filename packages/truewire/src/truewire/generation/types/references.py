from typing_extensions import Iterator, Mapping
from dataclasses import dataclass

from truewire.generation.schema import (
  LocalResolver, ResolutionError, Schema, Reference,
)
from .maps import (
  FlattenAllOf, MapReduce, MergeAnyOf, RemoveOneOf, renders_as_record,
)

@dataclass
class dependencies(MapReduce[set[str]]):
  def zero(self) -> set[str]:
    return set()

  def reduce(self, xs: list[set[str]]) -> set[str]:
    return set().union(*xs)

  def map(self, schema: Reference|Schema, path: tuple[str, ...]) -> tuple[Reference|Schema, set[str]]:
    if isinstance(schema, Reference):
      return schema, {schema.ref}
    else:
      return schema, set()

  @classmethod
  def one(cls, schema: Reference|Schema, id: str) -> set[str]:
    """Direct dependencies of a schema."""
    return cls()(schema, (id,))[1]

  @classmethod
  def all(cls, schemas: Mapping[str, Schema]) -> dict[str, set[str]]:
    """Direct dependencies of the given schemas."""
    return {
      id: cls.one(s, id) if (s := schemas.get(id)) is not None else set()
      for id in schemas
    }

  @classmethod
  def nested(cls, schemas: Mapping[str, Schema]) -> dict[str, set[str]]:
    """Direct and indirect dependencies of the given schemas."""
    visited = set[str]()
    out: dict[str, set[str]] = {}

    def rec(id: str):
      if id not in visited:
        visited.add(id)
        if (schema := schemas.get(id)) is not None:
          refs = cls.one(schema, id)
          out[id] = refs
          for ref in refs:
            rec(ref)

    for id in schemas:
      rec(id)
    return dict(out)


def cycles(g: Mapping[str, set[str]]) -> list[list[str]]:
  """Every reference cycle in `g`, as its strongly connected component.

  Each component holds two or more nodes that all reach each other, name-sorted; a node
  that merely references itself is not one -- one name defined once is not a cycle any
  ordering has to break. Tarjan's algorithm, run iteratively so a deep reference chain
  cannot exhaust the interpreter stack (the failure this whole module exists to stop).

  Args:
    g: Node to the set of nodes it depends on. Nodes appearing only as a dependency
      (a reference out of the graph) are treated as having none of their own.
  """
  nodes = list(g) + sorted({d for deps in g.values() for d in deps} - set(g))
  index: dict[str, int] = {}
  low: dict[str, int] = {}
  on_stack: set[str] = set()
  stack: list[str] = []
  out: list[list[str]] = []
  counter = 0

  for root in nodes:
    if root in index:
      continue
    # (node, iterator over its dependencies); the explicit frame stack is what keeps
    # this iterative.
    work: list[tuple[str, Iterator[str]]] = [(root, iter(sorted(g.get(root, ()))))]
    index[root] = low[root] = counter
    counter += 1
    stack.append(root)
    on_stack.add(root)
    while work:
      node, deps = work[-1]
      for dep in deps:
        if dep not in index:
          index[dep] = low[dep] = counter
          counter += 1
          stack.append(dep)
          on_stack.add(dep)
          work.append((dep, iter(sorted(g.get(dep, ())))))
          break
        if dep in on_stack:
          low[node] = min(low[node], index[dep])
      else:
        work.pop()
        if work:
          low[work[-1][0]] = min(low[work[-1][0]], low[node])
        if low[node] == index[node]:
          component: list[str] = []
          while True:
            member = stack.pop()
            on_stack.discard(member)
            component.append(member)
            if member == node:
              break
          if len(component) > 1:
            out.append(sorted(component))
  return out

def topo_sort(g: Mapping[str, set[str]]) -> list[str]:
  """Topologically sort a directed graph, tolerating reference cycles.

  `toposort_flatten` raises `CircularDependencyError` on a cycle, but a cycle between
  records is a legal shape -- a comment thread, a file tree -- that Python renders with a
  forward reference. So each cycle is collapsed to its name-first member, the
  condensation (always acyclic) is sorted, and each collapsed node expands back to its
  members in name order. A graph with no cycle has nothing to collapse and comes back
  exactly as `toposort_flatten` ordered it.

  A cycle among schemas that render *inline* never reaches here: `InlineSchemas` raises
  `SchemaCycleError` while normalizing, long before an order is asked for.
  """
  from toposort import toposort_flatten
  groups = cycles(g)
  if not groups:
    return toposort_flatten(g)
  representative = {member: group[0] for group in groups for member in group}
  members = {group[0]: group for group in groups}
  collapsed: dict[str, set[str]] = {}
  for node, deps in g.items():
    rep = representative.get(node, node)
    mapped = {representative.get(dep, dep) for dep in deps}
    collapsed.setdefault(rep, set()).update(mapped - {rep})
  return [name for rep in toposort_flatten(collapsed) for name in members.get(rep, [rep])]

def generation_order(schemas: Mapping[str, Schema]) -> list[str]:
  return topo_sort(dependencies.nested(schemas))

def external_references(schemas: Mapping[str, Schema]) -> set[str]:
  out = set[str]()
  for deps in dependencies.nested(schemas).values():
    for k in deps:
      if k not in schemas:
        out.add(k)
  return out


def unrenderable_cycles(schemas: Mapping[str, Schema]) -> list[list[str]]:
  """Every reference cycle in `schemas` that no backend can render, name-sorted.

  A cycle is renderable when at least one schema on it renders as a record: a record has
  a name in the generated module, and a reference back to that name is a forward
  reference every target language can state. A cycle where every schema renders as an
  expression instead -- `Tree` = a string or an array of `Tree`, or `Node` =
  `dict[str, Node]` -- has nothing to close on. Expanding one either expands the next
  forever, or emits an alias whose right-hand side names itself, which is a `NameError`
  the moment the module is imported.

  A self-reference counts as a cycle here, unlike in `cycles`: `Node = dict[str, Node]`
  is one name, and one name is enough to be unrenderable even though it is not enough to
  need an ordering broken.

  The graph is read after the normalizing rewrites that change what a schema *is*
  (`oneOf` -> `anyOf`, `allOf` flattened into properties, `anyOf` merged), because those
  both create records and delete the references between them: two schemas that only
  `allOf` each other end up as two empty records with no cycle left. Reading raw JSON
  would report that as unrenderable.

  A spec that is broken some other way -- a `$ref` to nothing, an external reference
  inside an `allOf` -- reports nothing: those have their own checks, and guessing past
  them would report a cycle that is really a typo.

  Args:
    schemas: Every shared schema in one project, id to schema.
  """
  prepared = dict(schemas)
  resolver = LocalResolver(prepared)
  try:
    for step in (RemoveOneOf(), FlattenAllOf(resolver), MergeAnyOf(resolver)):
      prepared = {id: step(schema) for id, schema in prepared.items()}
  except (ValueError, ResolutionError):
    return []
  graph = {id: deps & set(prepared) for id, deps in dependencies.all(prepared).items()}
  groups = cycles(graph) + [[id] for id, deps in graph.items() if id in deps]
  return sorted(
    (group for group in groups if not any(renders_as_record(prepared[id]) for id in group)),
    key=lambda group: group[0],
  )
