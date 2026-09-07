"""`router.json`: an optional, declared description and upstream link for one router
grouping under `spec/endpoints/` -- see `docs/spec/authoring.md` rule 14."""
from pathlib import Path

from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from typing_extensions import Annotated

from .repo import load_json


def _description(value: str) -> str:
  """Reject a description containing a literal `\"\"\"`, which would break the generated
  docstring it renders into."""
  if '"""' in value:
    raise ValueError(
      'description must not contain a literal `"""` -- it would break the generated docstring'
    )
  return value


def _upstream(value: str) -> str:
  """Reject an upstream link that isn't a fetchable http(s) URL, or one containing a
  literal `\"\"\"` (same reasoning as `_description`)."""
  if not (value.startswith('http://') or value.startswith('https://')):
    raise ValueError(
      f'upstream {value!r} must be a http(s):// URL -- never invented or omitted, per docs/spec/authoring.md rule 14'
    )
  if '"""' in value:
    raise ValueError(
      'upstream must not contain a literal `"""` -- it would break the generated docstring'
    )
  return value


class RouterDoc(BaseModel):
  """Declared description and upstream link for one router grouping's `router.json`."""
  model_config = ConfigDict(extra='forbid')

  description: Annotated[str, Field(min_length=1), AfterValidator(_description)]
  """Prose describing what this grouping actually is, not a restatement of its path."""
  upstream: Annotated[str, Field(min_length=1), AfterValidator(_upstream)]
  """Canonical URL to the upstream API's own documentation page for this grouping."""
  core: Annotated[str, Field(min_length=1)] | None = None
  """Symbolic name of this subtree's `core` class, resolved against
  `truewire.toml`'s `[python.cores]` table by the nearest ancestor declaring one. `None`
  means this router grouping inherits its ancestor's declaration -- there is no implicit
  project-wide default; the project's own root `router.json` must declare one."""


def load_router(directory: Path) -> RouterDoc | None:
  """
  Load `router.json` from `directory`, or `None` if it doesn't declare one.

  Args:
    directory: Directory under `spec/endpoints/` a router grouping corresponds to.
  """
  path = directory / 'router.json'
  if not path.is_file():
    return None
  return RouterDoc.model_validate(load_json(path))
