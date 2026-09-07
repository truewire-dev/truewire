from typing_extensions import Mapping
from truewire.generation.schema import LocalResolver, Schema
from truewire.generation.types import RemoveOneOf, FlattenAllOf, MergeAnyOf, InlineSchemas, Unnest

class Normalizer:
  """Normalizes schemas for Python generation."""

  def maps(self, schemas: Mapping[str, Schema]):
    resolver = LocalResolver(schemas)
    return (
      RemoveOneOf(),
      FlattenAllOf(resolver),
      MergeAnyOf(resolver),
      InlineSchemas(resolver),
    )
  
  def __call__(self, schemas: Mapping[str, Schema]) -> Mapping[str, Schema]:
    unnest = Unnest.records()
    for f in self.maps(schemas):
      schemas = {k: f(v) for k, v in schemas.items()}
    return unnest.all(schemas)


  @classmethod
  def iterative(cls):
    from truewire.generation.types import IterativeNormalizer
    return IterativeNormalizer(normalize=cls())