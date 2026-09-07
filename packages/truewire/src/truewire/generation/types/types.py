from typing_extensions import Mapping, Collection, Iterable, Protocol, Sequence
from dataclasses import dataclass, field

from truewire.generation.schema import Schema

Imports = Mapping[str, Collection[str]]
"""pkg -> set of imported names"""

def add_imports(imports: Imports, other: Imports) -> Imports:
  from collections import defaultdict
  out = defaultdict[str, set[str]](set)
  for pkg, names in imports.items():
    out[pkg].update(names)
  for pkg, names in other.items():
    out[pkg].update(names)
  return dict(out)

def merge_imports(imports: Iterable[Imports]) -> Imports:
  from collections import defaultdict
  out = defaultdict(set)
  for other in imports:
    for pkg, names in other.items():
      out[pkg].update(names)
  return dict(out)

@dataclass(kw_only=True)
class RenderedTypes:
  definitions: Mapping[str, str] = field(default_factory=dict)
  identifiers: Mapping[str, str] = field(default_factory=dict)
  imports: Imports = field(default_factory=dict)
  generation_order: Sequence[str]

class TypeRenderer(Protocol):
  def __call__(self, schemas: Mapping[str, Schema], /, *, inline: bool = False) -> RenderedTypes:
    ...