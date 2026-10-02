"""Render the JSON Schemas the models in `truewire.schemas` are the source of (T7).

`python -m truewire.schemas` writes them into `published/`; `--check` exits 1 when a
committed file differs from its model. The site serves the same files at `SCHEMA_BASE`.
"""
import json
from pathlib import Path
from typing_extensions import Any

from pydantic import BaseModel
from pydantic.json_schema import GenerateJsonSchema, JsonSchemaValue
from pydantic_core import core_schema

from .config import TruewireToml
from .docs import DocsYml

SCHEMA_BASE = 'https://truewire.dev/schemas/'
"""Where the site serves every published schema, by file name."""

DIALECT = 'https://json-schema.org/draft/2020-12/schema'
"""The JSON Schema dialect pydantic writes."""



class NoNullSchema(GenerateJsonSchema):
  """Writes an optional field as its value's schema alone, without the `null` branch.

  TOML has no null: an absent key is the only way to leave a field unset. A `{"anyOf":
  [{...}, {"type": "null"}]}` there only costs the editor its message, because taplo then
  reports a typo inside a table as matching no branch of the `anyOf`, without naming it.
  """

  def nullable_schema(self, schema: core_schema.NullableSchema) -> JsonSchemaValue:
    return self.generate_inner(schema['schema'])

  def default_schema(self, schema: core_schema.WithDefaultSchema) -> JsonSchemaValue:
    json_schema = super().default_schema(schema)
    if json_schema.get('default', ...) is None:
      del json_schema['default']
    return json_schema


PUBLISHED: dict[str, tuple[type[BaseModel], type[GenerateJsonSchema]]] = {
  'docs.yml.json': (DocsYml, GenerateJsonSchema),
  'truewire.toml.json': (TruewireToml, NoNullSchema),
}
"""Each published schema's file name, to the model it is generated from and how: YAML has a
null, so `docs.yml` keeps pydantic's own rendering."""


def published_dir() -> Path:
  """The committed copies of the published schemas, in the package."""
  return Path(__file__).parent / 'published'


def render(name: str) -> str:
  """The JSON Schema file `name` of `PUBLISHED`, as committed and served."""
  model, generator = PUBLISHED[name]
  schema = model.model_json_schema(by_alias=True, mode='validation', schema_generator=generator)
  document: dict[str, Any] = {'$schema': DIALECT, '$id': SCHEMA_BASE + name, **schema}
  return json.dumps(document, indent=2, ensure_ascii=False) + '\n'
