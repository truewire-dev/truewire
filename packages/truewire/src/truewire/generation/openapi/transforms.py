from truewire.generation.schema import (
  Resolver, EmptyResolver,
  Parameter, Schema, Reference, Operation,
)
from truewire.generation.schema import ensure_nonref

def resolve_references(op: Operation, /, *, resolve: Resolver = EmptyResolver()) -> Operation:
  op = op.model_copy(deep=True)
  op.parameters = [resolve.parameter(p) for p in op.parameters or []]
  op.responses = {code: resolve.response(r) for code, r in op.responses.items()}
  if op.request_body is not None:
    op.request_body = resolve.body(op.request_body)
  return op

def PARAMETER_KEY(parameter: Parameter) -> str:
  return f'$parameter/{parameter.name}'

def RESPONSE_KEY(code: str) -> str:
  return f'$response/response{code}'

BODY_KEY = '$request/body'

def normalize_schemas(op: Operation, /) -> tuple[Operation, dict[str, Schema | Reference]]:
  """Ensure all schemas are references, and return all referenced schemas."""

  op = op.model_copy(deep=True)

  params = [ensure_nonref(p) for p in op.parameters or []]
  responses = {code: ensure_nonref(r) for code, r in op.responses.items()}
  body = op.request_body and ensure_nonref(op.request_body)

  schemas: dict[str, Schema | Reference] = {}

  for p in params:
    p = ensure_nonref(p)
    if p.schema_ is None:
      p.schema_ = Schema()
    key = PARAMETER_KEY(p)
    schemas[key] = p.schema_
    p.schema_ = Reference(ref=key)

  for code, r in responses.items():
    if r.content:
      if len(r.content) > 1:
        raise ValueError(f'Multiple content types in response [status={code}]: {r.content}')
      _, media_type = next(iter(r.content.items()))
      if media_type.schema_ is None:
        media_type.schema_ = Schema()
      key = RESPONSE_KEY(code)
      schemas[key] = media_type.schema_
      media_type.schema_ = Reference(ref=key)

  if body is not None:
    if body.content:
      if len(body.content) > 1:
        raise ValueError(f'Multiple content types in request body: {body.content}')
      _, media_type = next(iter(body.content.items()))
      if media_type.schema_ is None:
        media_type.schema_ = Schema()
      key = BODY_KEY
      schemas[key] = media_type.schema_
      media_type.schema_ = Reference(ref=key)

  return op, schemas