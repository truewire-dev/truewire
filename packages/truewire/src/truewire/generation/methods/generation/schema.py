from typing_extensions import (
  Mapping, Literal, Collection, Sequence,
  TypedDict, NotRequired,
  TypeVar, Generic,
)

TAuth = TypeVar('TAuth', default=bool)

class Parameter(TypedDict):
  name: str
  type: str
  required: bool
  default: NotRequired[str|None]
  docstring: NotRequired[str|None]

class Module(TypedDict, Generic[TAuth]):
  method: str
  path: str
  function_name: str
  class_name: str
  description: NotRequired[str|None]
  auth: TAuth
  path_params: Collection[Parameter]
  query_params: Collection[Parameter]
  body: NotRequired[Parameter|None]
  responses: Mapping[str, str]
  type_defs: Mapping[str, str]
  imports: Mapping[str, Collection[str]]
  docs_url: NotRequired[str|None]
