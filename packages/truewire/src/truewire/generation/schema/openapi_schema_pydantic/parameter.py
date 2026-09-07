from typing_extensions import Any

from pydantic import BaseModel, ConfigDict, Field

from ..parameter_location import ParameterLocation
from .example import Example
from .media_type import MediaType
from .reference import ReferenceOr
from .schema import Schema


class Parameter(BaseModel):
    """
    Describes a single operation parameter.

    A unique parameter is defined by a combination of a [name](#parameterName) and [location](#parameterIn).

    References:
        - https://swagger.io/docs/specification/describing-parameters/
        - https://swagger.io/docs/specification/serialization/
        - https://github.com/OAI/OpenAPI-Specification/blob/main/versions/3.0.3.md#parameterObject
    """

    name: str
    in_: ParameterLocation = Field(validation_alias='in', serialization_alias='in')
    description: str | None = None
    required: bool = False
    deprecated: bool = False
    allowEmptyValue: bool = False
    style: str | None = None
    explode: bool = False
    allowReserved: bool = False
    schema_: ReferenceOr[Schema] | None = Field(default=None, validation_alias='schema', serialization_alias='schema')
    example: Any | None = None
    examples: dict[str, ReferenceOr[Example]] | None = None
    content: dict[str, MediaType] | None = None
    model_config = ConfigDict(
        # `MediaType` is not build yet, will rebuild in `__init__.py`:
        defer_build=True,
        extra='allow',
        validate_by_name=True,
        serialize_by_alias=True,
        json_schema_extra={
            'examples': [
                {
                    'name': 'token',
                    'in': 'header',
                    'description': 'token to be passed as a header',
                    'required': True,
                    'schema': {'type': 'array', 'items': {'type': 'integer', 'format': 'int64'}},
                    'style': 'simple',
                },
                {
                    'name': 'username',
                    'in': 'path',
                    'description': 'username to fetch',
                    'required': True,
                    'schema': {'type': 'string'},
                },
                {
                    'name': 'id',
                    'in': 'query',
                    'description': 'ID of the object to fetch',
                    'required': False,
                    'schema': {'type': 'array', 'items': {'type': 'string'}},
                    'style': 'form',
                    'explode': True,
                },
                {
                    'in': 'query',
                    'name': 'freeForm',
                    'schema': {'type': 'object', 'additionalProperties': {'type': 'integer'}},
                    'style': 'form',
                },
                {
                    'in': 'query',
                    'name': 'coordinates',
                    'content': {
                        'application/json': {
                            'schema': {
                                'type': 'object',
                                'required': ['lat', 'long'],
                                'properties': {'lat': {'type': 'number'}, 'long': {'type': 'number'}},
                            }
                        }
                    },
                },
            ]
        },
    )
