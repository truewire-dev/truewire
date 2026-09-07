from typing_extensions import Callable, Mapping, Protocol, TypedDict, Collection
from dataclasses import dataclass, field

from truewire.generation.schema import (
  Resolver, ResolutionError, EmptyResolver,
  Schema, Reference, Schemas
)
from truewire.generation.types import (
  Translate, external_references, merge_imports,
  TypeRenderer, RenderedTypes
)

class Disambiguator(Protocol):
  def __call__(
    self, schemas: Mapping[str, Schema | Reference], /, *,
    forbidden: Collection[str], fixed: Mapping[str, str],
  ) -> Mapping[str, str]:
    ...

class ExternalReference(TypedDict):
  name: str
  package: str

@dataclass(kw_only=True)
class TypeGenerator:
  resolve: Resolver = field(default_factory=EmptyResolver)
  normalize: Callable[[Schemas], Schemas]
  disambiguate: Disambiguator
  external_references: Mapping[str, ExternalReference] = field(default_factory=dict)
  forbidden: Collection[str] = ()
  """Names reserved by the surrounding module, which no schema may take."""
  render: TypeRenderer

  @property
  def external_translations(self) -> dict[str, str]:
    return {k: v['name'] for k, v in self.external_references.items()}

  def __call__(
    self, all_schemas: Mapping[str, Schema | Reference],
    /, *, inline: bool = False
  ) -> RenderedTypes:
    """Render the types for the given schemas.
    
    - `all_schemas`: The schemas to render.
    - `inline`: Whether to export definitions for inlineable schemas
    """
    references = {k: v.ref for k, v in all_schemas.items() if isinstance(v, Reference)}
    schemas = {k: v for k, v in all_schemas.items() if not isinstance(v, Reference)}
    
    normalized_schemas = self.normalize(schemas)
    translations = self.disambiguate(
      normalized_schemas, forbidden=self.forbidden, fixed=self.external_translations,
    )
    translate = Translate.of(translations)
    translated_schemas = translate.all(normalized_schemas)
    render = self.render(translated_schemas, inline=inline)
    
    rev_translations = {v: k for k, v in translations.items()}
    render.identifiers = {rev_translations.get(k, k): v for k, v in render.identifiers.items()}
    render.definitions = {rev_translations.get(k, k): v for k, v in render.definitions.items()}
    render.generation_order = [rev_translations.get(k, k) for k in render.generation_order]

    all_imports = [render.imports]
    for id in external_references(normalized_schemas):
      if (ref := self.external_references.get(id)) is None:
        raise ResolutionError(id)
      else:
        render.identifiers[id] = ref['name']
        all_imports.append({ref['package']: {ref['name']}})

    for id, k in references.items():
      render.identifiers[id] = translations[k]
      ref = self.external_references[k]
      all_imports.append({ref['package']: {ref['name']}})

    render.imports = merge_imports(all_imports)
    return render
