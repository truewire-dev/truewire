from typing_extensions import Iterable, Mapping, Callable, Protocol, Collection
from dataclasses import dataclass, field

from truewire.generation.schema import (
  Resolver, OpenApiResolver, EmptyResolver,
  Parameter, Schema, Reference, Operation,
  Response, response_json_schema, set_response_json_schema,
  RequestBody, body_json_schema, set_body_json_schema,
)
from truewire.generation import types

def is_inline(schema: Reference|Schema) -> bool:
  return isinstance(schema, Reference) or schema.properties is None

def default_parameter_key(parameter: Parameter) -> str:
  return f'$parameter/{parameter.name}'

def default_response_key(code: str, response: Response) -> str:
  return f'$response/response{code}'

def default_body_key(body: RequestBody|None) -> str:
  return '$request/body'

class Disambiguator(Protocol):
  def __call__(self, schemas: Mapping[str, Schema], /, *, forbidden: Collection[str]) -> Mapping[str, str]:
    ...

@dataclass
class References:
  local_schemas: dict[str, Schema] = field(default_factory=dict)
  external_references: set[str] = field(default_factory=set)

@dataclass
class Normalizer:
  resolve: Resolver = field(default_factory=EmptyResolver, kw_only=True)
  disambiguate: Disambiguator = field(kw_only=True)
  normalize: Callable[[Mapping[str, Schema]], Mapping[str, Schema]] = field(kw_only=True)
  inline: Callable[[Reference|Schema], bool] = field(default=is_inline, kw_only=True)
  parameter_key: Callable[[Parameter], str] = field(default=default_parameter_key, kw_only=True)
  response_key: Callable[[str, Response], str] = field(default=default_response_key, kw_only=True)
  body_key: Callable[[RequestBody|None], str] = field(default=default_body_key, kw_only=True)

  def parameter_references(
    self, parameters: Iterable[Parameter],
  ) -> References:
    refs = References()
    for p in parameters:
      if (s := p.schema_) is not None:
        if isinstance(s, Reference):
          refs.external_references.add(s.ref)
        else:
          refs.local_schemas[self.parameter_key(p)] = s
    return refs

  def response_references(
    self, responses: Mapping[str, Response],
  ) -> References:
    refs = References()
    for code, r in responses.items():
      if (s := response_json_schema(r)) is not None:
        if isinstance(s, Reference):
          refs.external_references.add(s.ref)
        else:
          refs.local_schemas[self.response_key(code, r)] = s
    return refs

  @classmethod
  def of(
    cls, openapi_schema: Mapping, *,
    disambiguate: Disambiguator,
    normalize: Callable[[Mapping[str, Schema]], Mapping[str, Schema]],
    inline: Callable[[Reference|Schema], bool] = is_inline,
    parameter_key: Callable[[Parameter], str] = default_parameter_key,
    response_key: Callable[[str, Response], str] = default_response_key,
    body_key: Callable[[RequestBody|None], str] = default_body_key,
  ):
    resolve = OpenApiResolver(openapi_schema)
    return cls(
      resolve=resolve,
      disambiguate=disambiguate, normalize=normalize, inline=inline,
      parameter_key=parameter_key, response_key=response_key, body_key=body_key,
    )


  @dataclass
  class Result:
    operation: Operation
    local_schemas: dict[str, Schema]
    external_references: set[str]

  def __call__(self, op: Operation, *, forbidden_identifiers: Collection[str] = set()) -> Result:
    op = op.model_copy(deep=True)
    
    params = [self.resolve.parameter(p) for p in op.parameters or []]
    responses = {code: self.resolve.response(r) for code, r in op.responses.items()}

    param_refs = self.parameter_references(params)
    response_refs = self.response_references(responses)
    local_schemas = param_refs.local_schemas | response_refs.local_schemas
    external_references = param_refs.external_references | response_refs.external_references
    
    if op.request_body is not None:
      body = self.resolve.body(op.request_body)
      body_key = self.body_key(body)
      if (s := body_json_schema(body)) is not None:
        if isinstance(s, Reference):
          external_references.add(s.ref)
        else:
          local_schemas[body_key] = s
    else:
      body = None
      body_key = self.body_key(None)

    type_norm = types.IterativeNormalizer(normalize=self.normalize)
    local_schemas = type_norm(local_schemas)
    external_references.update(types.external_references(local_schemas))

    for p in params:
      key = self.parameter_key(p)
      if (s := local_schemas.get(key)) is not None:
        if self.inline(s):
          del local_schemas[key]
        else:
          s = Reference(ref=key) # type: ignore
        p.schema_ = s

    for code, r in responses.items():
      key = self.response_key(code, r)
      if (s := local_schemas.get(key)) is not None:
        if self.inline(s):
          del local_schemas[key]
        else:
          s = Reference(ref=key) # type: ignore
        set_response_json_schema(r, s)

    if body is not None and (s := local_schemas.get(body_key)) is not None:
      if self.inline(s):
        del local_schemas[body_key]
      else:
        s = Reference(ref=body_key) # type: ignore
      set_body_json_schema(body, s)

    local_schemas = {k: v for k, v in local_schemas.items() if not isinstance(v, Reference)}
    translate = self.disambiguate(local_schemas, forbidden=external_references.union(forbidden_identifiers))
    translate_refs = types.Translate(lambda x: translate.get(x, x))
    local_schemas = { translate[k]: translate_refs(v) for k, v in local_schemas.items() }

    for p in params:
      if p.schema_:
        p.schema_ = translate_refs(p.schema_)

    for code, r in responses.items():
      if (s := response_json_schema(r)) is not None:
        set_response_json_schema(r, translate_refs(s))

    if (body is not None and (s := body_json_schema(body)) is not None):
      set_body_json_schema(body, translate_refs(s))

    op.request_body = body # type: ignore
    op.parameters = params # type: ignore
    op.responses = responses # type: ignore
    return self.Result(op, local_schemas, external_references)
