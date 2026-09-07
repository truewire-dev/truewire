"""
`docs/production_standards.md` S6 -- the "unreduced duplicate of a shared shape" half.

`truewire.spec.authoring.check_titles` (rule 1) only checks that an inline schema
carries a *title*; it can't tell that two differently-titled inline schemas describe the
same shape and should have been one `$ref`'d entry in `schemas.json` instead. The real
case this was built for: eight separate inline `Money`-shaped schemas, each individually
titled, colliding by property set once codegen's own disambiguator starts appending path
segments to tell them apart (`LimitLimitGtc0OrderConfigurationAnyOf3LimitLimitGtc`). Seeing
it requires comparing schemas *across* a project's whole spec tree rather than within one
operation, which is why this lives here rather than as another `truewire.spec.authoring`
per-operation check.
"""
from pathlib import Path
from typing_extensions import Any

from truewire.project import Project
from truewire.spec import endpoint_records
from truewire.spec.authoring import is_ref, nodes, operation_json

from .finding import Finding

MIN_SIGNATURE_SIZE = 2
"""Minimum property count an inline schema needs before its signature is compared at all.

A single-property schema (`{'id': ...}`) carries almost no structural signature -- lots of
genuinely unrelated shapes share one property name, so comparing on it would mostly flag
coincidence, not a shape that should have been `$ref`'d. Two or more properties is still a
low bar (it will still flag a small, legitimately-repeated envelope shape sometimes), which
is exactly why this check reports `warning`, not `error`: it needs to prove itself against
real projects before either the threshold or the severity is worth revisiting.
"""

MAX_SAMPLE = 5
"""Occurrences shown per duplicated signature before the rest collapse into a count, so a
shape copy-pasted across dozens of endpoints reports as one readable finding."""


def _signature(properties: dict[str, Any]) -> tuple[str, ...]:
  """
  Structural signature of an inline object schema: its own direct property names, sorted.

  Deliberately shallow -- the property *names* alone, not each property's nested type -- to
  match `docs/production_standards.md` S6's own wording, "an identical property-set
  signature." A deep structural comparison would also catch two schemas that happen to
  share names but differ in type, which is a different (and much noisier) claim than "this
  should have been one `$ref`'d shape."

  Args:
    properties: An inline schema's own `properties` map.
  """
  return tuple(sorted(properties.keys()))


def check_duplicate_schemas(client_root: Path | Project) -> list[Finding]:
  """
  Flag inline object schemas that share a property-set signature without a common `$ref`.

  Walks every endpoint's `spec.openapi` operation under `client_root`, collecting every
  *inline* (non-`$ref`) object schema with at least `MIN_SIGNATURE_SIZE` properties, and
  groups them by `_signature`. A signature shared by two or more distinct occurrences is
  reported once, naming a bounded sample of where it was found -- never once per
  occurrence, which would make a widely copy-pasted shape drown out everything else.

  Two schemas that both `$ref` the same `schemas.json` entry are never flagged: `is_ref`
  excludes a `$ref` node from the pool entirely, so only genuinely re-typed-out inline
  copies are compared. A gRPC endpoint (no `spec.openapi` operation) is skipped, the same
  way `truewire.spec.authoring.audit` skips it.

  Args:
    client_root: Project (or project root).

  Returns:
    One `Finding` per duplicated signature, `rule='S6'`, `severity='warning'`.
  """
  occurrences: dict[tuple[str, ...], list[str]] = {}
  for record in endpoint_records(client_root):
    operation = operation_json(record.endpoint)
    if operation is None:
      continue
    for path, _name, node in nodes(operation):
      if is_ref(node):
        continue
      properties = node.get('properties')
      if not isinstance(properties, dict) or len(properties) < MIN_SIGNATURE_SIZE:
        continue
      signature = _signature(properties)
      occurrences.setdefault(signature, []).append(f'{record.endpoint.function}.{path}')

  out: list[Finding] = []
  for signature, locations in occurrences.items():
    if len(locations) < 2:
      continue
    sample = locations[:MAX_SAMPLE]
    remaining = len(locations) - len(sample)
    sample_text = ', '.join(sample)
    if remaining:
      sample_text += f', +{remaining} more'
    out.append(Finding(
      rule='S6',
      location=f'spec/endpoints/ ({len(locations)} occurrences): {sample_text}',
      message=(
        f'{len(locations)} inline schemas share the property set '
        f'{list(signature)!r} with no common `$ref` -- likely the same shape copy-pasted '
        f'instead of defined once in `schemas.json` and referenced'
      ),
      severity='warning',
    ))
  return out
