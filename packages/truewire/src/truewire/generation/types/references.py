from typing_extensions import Mapping
from dataclasses import dataclass

from truewire.generation.schema import Schema, Reference
from .maps import MapReduce

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


def topo_sort(g: Mapping[str, set[str]]) -> list[str]:
  """Topologically sort a directed graph."""
  from toposort import toposort_flatten
  return toposort_flatten(g)

def generation_order(schemas: Mapping[str, Schema]) -> list[str]:
  return topo_sort(dependencies.nested(schemas))

def external_references(schemas: Mapping[str, Schema]) -> set[str]:
  out = set[str]()
  for deps in dependencies.nested(schemas).values():
    for k in deps:
      if k not in schemas:
        out.add(k)
  return out