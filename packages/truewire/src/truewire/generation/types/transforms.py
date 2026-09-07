from typing_extensions import Callable, Generic, Mapping, TypeVar
from dataclasses import dataclass

from truewire.generation.schema import Reference, Schema

S = TypeVar('S', bound=Reference|Schema)
S2 = TypeVar('S2', bound=Reference|Schema)

@dataclass(kw_only=True)
class IterativeNormalizer(Generic[S]):
  """Iteratively normalize schemas until they converge."""
  normalize: Callable[[Mapping[str, S]], Mapping[str, S]]

  def __call__(self, schemas: Mapping[str, S]) -> dict[str, S]:
    last: dict[str, S] = {}

    while set(schemas) != set(last):
      last = dict(schemas)
      schemas = self.normalize(schemas)

    return dict(schemas)