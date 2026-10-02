"""Validating a recorded or live wire body against an endpoint's response schema.

One implementation shared by `truewire check`, which validates what was recorded, and
`truewire conform`, which validates what the API answers tonight. The two must agree: a
body `check` accepts in a recording is a body `conform` accepts live.
"""

from copy import deepcopy
from typing_extensions import Any

from jsonschema import Draft202012Validator

SCHEMA_DIALECT = 'https://json-schema.org/draft/2020-12/schema'


def rewrite_refs(obj: Any) -> Any:
  """Point every bare-name `$ref` (`"$ref": "Pet"`, a shared schema) at `#/$defs/Pet`.

  A `$ref` already written as a local pointer (`#/...`) is left alone.
  """
  if isinstance(obj, dict):
    out: dict[str, Any] = {}
    for key, value in obj.items():
      if key == '$ref' and isinstance(value, str) and not value.startswith('#/'):
        out[key] = f'#/$defs/{value}'
      else:
        out[key] = rewrite_refs(value)
    return out
  if isinstance(obj, list):
    return [rewrite_refs(value) for value in obj]
  return obj


def schema_document(schema: dict[str, Any], shared_schemas: dict[str, Any]) -> dict[str, Any]:
  """One self-contained schema document: `schema` at the root, every shared schema under
  `$defs` (a slashed id `a/b` nested as `$defs.a.b`), every bare-name `$ref` rewritten
  to point there.

  Args:
    schema: The endpoint's response schema, as the spec writes it.
    shared_schemas: The project's merged `schemas.json` scopes (`load_shared_schemas`).
  """
  defs: dict[str, Any] = {}
  for key, value in shared_schemas.items():
    parts = key.split('/')
    node = defs
    for part in parts[:-1]:
      node = node.setdefault(part, {})
    node[parts[-1]] = rewrite_refs(value)
  root = deepcopy(rewrite_refs(schema))
  root.setdefault('$schema', SCHEMA_DIALECT)
  root['$defs'] = defs
  return root


def validator_for(schema: dict[str, Any], shared_schemas: dict[str, Any]) -> Draft202012Validator:
  """A draft 2020-12 validator for `schema`, shared schemas resolvable by bare name."""
  return Draft202012Validator(schema_document(schema, shared_schemas))


def response_json_schema(response: dict[str, Any]) -> dict[str, Any] | None:
  """The `application/json` schema of one OpenAPI response object, or `None`."""
  content = response.get('content')
  if not isinstance(content, dict):
    return None
  media = content.get('application/json')
  if not isinstance(media, dict):
    return None
  schema = media.get('schema')
  if not isinstance(schema, dict):
    return None
  return schema


def http_response_schema(spec: dict[str, Any], status: int) -> tuple[dict[str, Any] | None, str | None]:
  """The schema an rpc-over-HTTP endpoint declares for a response with `status`.

  A migrated endpoint declares one `response` schema for its whole 2xx reply; a legacy
  one keeps an OpenAPI `responses` map keyed by status code.

  Args:
    spec: The endpoint's raw `spec` object, as read from `endpoint.json`.
    status: The HTTP status of the response being validated.

  Returns:
    The schema and `None`, or `None` and the reason there is no schema to validate against.
  """
  if spec.get('request') is not None or spec.get('response') is not None:
    schema = spec.get('response')
    if schema is None:
      return None, 'endpoint declares no `response` schema'
    return schema, None
  responses = (spec.get('openapi') or {}).get('responses', {})
  if str(status) not in responses:
    return None, f'status {status} not present in endpoint responses'
  schema = response_json_schema(responses[str(status)])
  if schema is None:
    return None, f'response {status} has no application/json schema'
  return schema, None
