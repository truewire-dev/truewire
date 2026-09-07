"""Shared request-schema utilities used by both codegen and the mock server --
the one canonical {placeholder}-matching rule, replacing three near-duplicate regexes."""
import re
from dataclasses import dataclass
from typing_extensions import Any

PLACEHOLDER = re.compile(r'\{([^{}]+)\}')
"""A `{name}` template slot inside an operation identifier (`path` or `channel`). Matches
the parameter name verbatim, whatever characters it uses -- codegen's own substitution does
`'{' + param.name + '}'` against the raw declared name, hyphens and all (an
`order-id` path segment, say), not a Python-identifier-restricted one."""


@dataclass(frozen=True)
class RequestLocations:
  """A `request` schema's properties, split into path-templated and everything else."""
  path_params: frozenset[str]
  rest: frozenset[str]


def split_request_by_location(
  template: str, request_schema: dict[str, Any] | None,
) -> RequestLocations:
  """
  Split a `request` schema's properties by whether they appear as a `{placeholder}` in
  `template` (a `path` or `channel` string) -- there is no location marker:
  a property matching a template placeholder is a path parameter, derived from the
  template itself, never a `path: true` flag.

  Args:
    template: The endpoint's `path` or `channel` string.
    request_schema: The endpoint's `request` JSON Schema, or `None`.
  """
  placeholders = frozenset(PLACEHOLDER.findall(template))
  properties = (request_schema or {}).get('properties') or {}
  names = frozenset(properties)
  return RequestLocations(path_params=names & placeholders, rest=names - placeholders)
