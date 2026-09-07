from typing_extensions import TypeVar
from . import Response, Reference, Schema, MediaType, RequestBody

T = TypeVar('T')

def ensure_ref(obj) -> Reference:
  if isinstance(obj, Reference):
    return obj
  else:
    raise ValueError(f'Unexpected non-reference: {obj}')

def ensure_nonref(obj: T | Reference) -> T:
  if isinstance(obj, Reference):
    raise ValueError(f'Unexpected reference: {obj.ref}')
  return obj

def body_json_schema(b: RequestBody) -> Reference|Schema|None:
  if 'application/json' in b.content:
    return b.content['application/json'].schema_

def set_body_json_schema(b: RequestBody, s: Reference|Schema):
  b.content = b.content or {}
  b.content['application/json'] = b.content.get('application/json') or MediaType()
  b.content['application/json'].schema_ = s

def response_json_schema(r: Response) -> Reference|Schema|None:
  if r.content and 'application/json' in r.content:
    return r.content['application/json'].schema_

def set_response_json_schema(r: Response, s: Reference|Schema):
  r.content = r.content or {}
  r.content['application/json'] = r.content.get('application/json') or MediaType()
  r.content['application/json'].schema_ = s