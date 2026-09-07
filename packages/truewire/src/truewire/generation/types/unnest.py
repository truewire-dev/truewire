from typing_extensions import Mapping
from abc import abstractmethod

from truewire.generation.schema import Schema, Reference
from .maps import MapReduce

class Unnest(MapReduce[dict[str, Schema]]):
  
  @abstractmethod
  def unnest(self, schema: Schema) -> bool:
    ...

  @staticmethod
  def records():
    class UnnestRecords(Unnest):
      def unnest(self, schema: Schema) -> bool:
        """Report whether a nested schema is the kind that renders as its own class.

        `properties` alone is not the test, because the parser dispatches on `type` first:
        a schema declaring `type: 'array'` renders as a `list[...]` however many
        `properties` it also carries. Unnesting such a schema replaces it with a reference
        to a name that renders inline and so defines nothing, and the module ends up
        annotating a field with an undefined name — which no other check catches, since the
        name never reaches `generation_order`. One real subscription has exactly one such
        schema, an array of aliases carrying an empty `properties`.

        Args:
          schema: A schema found nested inside another one.
        """
        return schema.properties is not None and schema.type in (None, 'object')
    return UnnestRecords()

  @staticmethod
  def always():
    class UnnestAlways(Unnest):
      def unnest(self, schema: Schema) -> bool:
        return True
    return UnnestAlways()

  def map(self, schema: Reference|Schema, path: tuple[str, ...]) -> tuple[Reference|Schema, dict[str, Schema]]:
    if isinstance(schema, Reference) or not self.unnest(schema):
      return schema, {}
    else:
      ref = '/'.join(path)
      return Reference(ref=ref), {ref: schema} # type: ignore

  def reduce(self, xs: list[dict[str, Schema]]) -> dict[str, Schema]:
    out: dict[str, Schema] = {}
    for obj in xs:
      out.update(obj)
    return out

  def zero(self) -> dict[str, Schema]:
    return {}

  @classmethod
  def one(cls, schema: Reference|Schema, id: str):
    """Unnest a single schema.
    
    - Internal schemas where `self.unnest(schema)` are replaced by references.
    - Returns `schema, defs`, where `schema` references schemas in `defs`
    """
    return cls()(schema, (id,))

  @classmethod
  def all(cls, schemas: Mapping[str, Schema]) -> dict[str, Schema]:
    """Unnest all schemas. 
    
    - For each schema, internal schemas where `self.unnest(schema)` are replaced by references.
    - Extracted references are added to the output.
    """
    unnest = cls()
    out: dict[str, Schema] = {}
    for id, schema in schemas.items():
      new_schema, defs = unnest(schema, path=(id,))
      out[id] = new_schema
      out.update(defs)
    return out
