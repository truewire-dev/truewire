"""
Shared result type for a `docs/production_standards.md` check hosted under
`truewire.standards` -- one per rule that is client-wide or cross-file rather than
per-operation, so it doesn't fit `truewire.spec.authoring.Violation`'s per-endpoint shape.
"""
from typing_extensions import Literal, TypedDict

Severity = Literal['error', 'warning']
"""Whether a finding fails `truewire standards`' gate (`error`) or only flags it (`warning`)."""

class Finding(TypedDict):
  """One breach of a `docs/production_standards.md` rule."""
  rule: str
  """Rule id from `docs/production_standards.md`, e.g. `'S1'`, `'S16'`."""
  location: str
  """Where the finding is: a file path relative to the project root, optionally suffixed
  with the offending field or line (`'docs/index.md:42'`, `'spec/endpoints/.../endpoint.json: client_id'`)."""
  message: str
  severity: Severity
