"""
`docs/production_standards.md` S26 -- every grouping directory under `spec/endpoints/`
declares its own `router.json` (`docs/spec/authoring.md` rule 14), not just the ones that
happen to have an obviously distinct upstream page.

A grouping whose endpoints genuinely span more than one upstream page, or that exists
purely for Python-side organization, does not get to skip `router.json` -- it declares one
anyway, citing the page that covers the most of it or reusing a parent/sibling's
already-established link ("closest thing possible, not invented" -- S26's own bar, distinct
from the exact-match precision every other `router.json` aims for).

`error`-severity: a project missing coverage genuinely fails `truewire standards`, not just
gets flagged.
"""
from pathlib import Path

from truewire.project import Project, resolve

from .finding import Finding


def _is_grouping(directory: Path) -> bool:
  """Whether `directory` is a `router.json`-eligible grouping: no `endpoint.json` of its
  own (that would make it a leaf endpoint, not a grouping), but at least one descendant
  that has one (an empty directory, or one holding only non-endpoint scratch files, is
  neither a leaf nor a grouping and is exempt)."""
  if (directory / 'endpoint.json').is_file():
    return False
  return next(directory.rglob('endpoint.json'), None) is not None


def check_router_coverage(client_root: Path | Project) -> list[Finding]:
  """
  S26: flag a grouping directory under `spec/endpoints/` with no `router.json` of its own.

  Args:
    client_root: Project (or project root).
  """
  project = resolve(client_root)
  client_root = project.root
  endpoints_root = project.endpoints_dir
  if not endpoints_root.is_dir():
    return []
  out: list[Finding] = []
  for directory in sorted(endpoints_root.rglob('*')):
    if not directory.is_dir() or directory.name == 'examples':
      continue
    if not _is_grouping(directory):
      continue
    if (directory / 'router.json').is_file():
      continue
    out.append(Finding(
      rule='S26',
      location=f'{directory.relative_to(client_root)}',
      message=(
        'grouping directory has no `router.json` -- every hover point beneath it needs a '
        'real upstream link (docs/spec/authoring.md rule 14); declare one, reusing the '
        'closest available page when no exact single page exists (production_standards.md '
        'S26)'
      ),
      severity='error',
    ))
  return out
