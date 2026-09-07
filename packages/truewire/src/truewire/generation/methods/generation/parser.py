from typing_extensions import Protocol, Mapping, Sequence
from dataclasses import dataclass

from truewire.generation.schema import (
  Operation, Schema, Reference,
  response_json_schema, body_json_schema
)
from truewire.generation.types import merge_imports, RenderedTypes

from .schema import Module, Parameter, TAuth

Imports = dict[str, set[str]]

class TypeGenerator(Protocol):

  def __call__(self, schema: Reference|Schema|None, id: str | None = None) -> tuple[str, Imports]:
    ...

  def all(self, schemas: Mapping[str, Schema]) -> tuple[Mapping[str, str], Imports]:
    ...

def default_value(s: Reference|Schema) -> str | None:
  if not isinstance(s, Reference):
    if (d := s.default) is not None:
      if s.type == 'string':
        return f"'{d}'"
      elif s.type == 'boolean':
        return 'True' if d else 'False'
      else:
        return str(d)

@dataclass
class Parser:
  def __call__(
    self, op: Operation, types: RenderedTypes, *,
    method: str, path: str, function_name: str, class_name: str,
    auth: TAuth, body_name: str | None = None,
  ) -> Module[TAuth]:

    path_params: list[Parameter] = []
    query_params: list[Parameter] = []

    for p in op.parameters or []:
      assert not isinstance(p, Reference)
      type, imps = self.type_generator(p.schema_)
      imports.append(imps)
      param: Parameter = {
        'name': p.name,
        'type': type,
        'default': p.schema_ and default_value(p.schema_),
        'required': p.required,
        'docstring': p.description,
      }
      if p.in_ == 'path':
        path_params.append(param)
      elif p.in_ == 'query':
        query_params.append(param)
      else:
        raise NotImplementedError(f'Parameter in {p.in_} not supported')

    responses: dict[str, str] = {}
    for code, r in op.responses.items():
      assert not isinstance(r, Reference)
      if (s := response_json_schema(r)) is not None:
        type, imps = self.type_generator(s)
        imports.append(imps)
        responses[code] = type
      else:
        imports.append({'typing_extensions': {'Any'}})
        responses[code] = 'Any'

    body_param: Parameter | None = None
    if (body := op.request_body) is not None:
      assert not isinstance(body, Reference)
      if (s := body_json_schema(body)) is not None:
        type, imps = self.type_generator(s)
        imports.append(imps)
        body_param = {
          'name': body_name or getattr(s, 'title', None) or getattr(s, 'ref', None) or 'body',
          'default': None if isinstance(s, Reference) else s.default,
          'type': type,
          'required': body.required,
          'docstring': body.description,
        }

    return {
      'method': method,
      'path': path,
      'function_name': function_name,
      'class_name': class_name,
      'description': op.description or op.summary,
      'docs_url': op.externalDocs and op.externalDocs.url or None,
      'auth': auth,
      'path_params': path_params,
      'query_params': query_params,
      'body': body_param,
      'responses': responses,
      'type_defs': type_defs,
      'imports': merge_imports(imports),
    }

