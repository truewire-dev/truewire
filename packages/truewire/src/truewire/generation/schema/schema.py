from typing_extensions import Mapping
from pydantic import Field, ConfigDict
from .openapi_schema_pydantic import Schema

Schemas = Mapping[str, Schema]

class JSONSchema(Schema):
  defs: Schemas = Field(default={}, validation_alias='$defs', serialization_alias='$defs')
  model_config = ConfigDict(validate_by_alias=True)