from typing_extensions import Collection, Mapping
from collections import Counter

from truewire.generation.schema import Schema, Reference
from truewire.generation.util import pascal_case

def type_name(part: str) -> str:
  return pascal_case(part.replace('$', '')) # for $parameter and $response

def path_name(path: str, schema: Schema | Reference, n: int) -> str | None:
  parts = path.split('/')[-n:] if n > 0 else []
  segment_name = ''.join(type_name(p) for p in parts)
  # `schema.title` is already correctly PascalCase per spec-authoring rule 1 and must be
  # used verbatim: running it through `type_name`/`pascal_case` re-cases an already-cased
  # title, which corrupts a digit-then-letter boundary (e.g. `Erc20Transfer` -> `Erc20transfer`)
  # that `caseconverter.pascalcase` doesn't treat as a word boundary.
  if isinstance(schema, Schema) and schema.title:
    return schema.title + segment_name
  return segment_name or None

def unique_mapping(schemas: Mapping[str, Schema | Reference], n: int, *, forbidden: Collection[str] = set()) -> dict[str, str]:
  map = {k: p for k, v in schemas.items() if (p := path_name(k, v, n)) is not None}
  counts = Counter(map.values())
  counts.update(forbidden)
  return {k: v for k, v in map.items() if counts[v] == 1}

def disambiguate(schemas: Mapping[str, Schema | Reference], *, forbidden: Collection[str] = set(), fixed: Mapping[str, str] = {}) -> dict[str, str]:
  """Creates a unique mapping of type names without collisions.
  
  - `schemas`: The schemas to disambiguate, formatted as paths (e.g. `name/of/type`)
  - `forbidden`: A set of schemas that should not be used in the mapping
  """
  unassigned = dict(schemas)
  translations: dict[str, str] = dict(fixed)
  i = 0
  while unassigned:
    map = unique_mapping(
      unassigned, i,
      forbidden=set(translations.values()).union(forbidden).union(fixed.values())
    )
    translations.update(map)
    unassigned = {k: v for k, v in unassigned.items() if k not in map}
    i += 1
  return translations
    