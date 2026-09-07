from typing_extensions import Callable
from dataclasses import dataclass, field
from truewire.generation.schema import Schemas
from truewire.generation.types import (
  TypeGenerator as _TypeGenerator, TypeRenderer, Disambiguator,
)
from . import disambiguate, Renderer, Normalizer

@dataclass(kw_only=True)
class TypeGenerator(_TypeGenerator):
  disambiguate: Disambiguator = disambiguate
  normalize: Callable[[Schemas], Schemas] = field(default_factory=Normalizer.iterative)
  render: TypeRenderer = field(default_factory=Renderer)