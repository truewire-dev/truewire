"""`docs/docs.yml` as a pydantic model: the source of `docs.yml.json` (W10).

The file carries the nav, the quickstart with one block per declared language, and the
metadata the site needs. The model says what the file may hold; whether each page it names
exists and each declared language has a block is `truewire.docs.docs_yml`'s to say, because
that needs the project around it.
"""
from typing_extensions import Annotated, Union

from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag

from truewire.schemas.config import Language

Page = Annotated[str, Field(pattern=r'^[^/].*\.md$')]
"""A page, as its path under `docs/`: `index.md`, `how-to/index.md`."""


def _nav_kind(value: object) -> str:
  """Which kind of nav entry `value` is: a mapping is a section, anything else a page."""
  return 'section' if isinstance(value, dict | NavSection) else 'page'


class NavSection(BaseModel):
  """A titled group of nav entries."""
  model_config = ConfigDict(extra='forbid', title='nav section', use_attribute_docstrings=True)

  title: str = Field(min_length=1)
  """The section's heading in the nav."""
  pages: list['NavEntry'] = Field(min_length=1)
  """The section's pages and subsections, in reading order."""


NavEntry = Annotated[
  Union[Annotated[Page, Tag('page')], Annotated[NavSection, Tag('section')]],
  Discriminator(_nav_kind),
]
"""One entry of the nav: a page, or a section of entries."""

NavSection.model_rebuild()


class QuickstartBlock(BaseModel):
  """The quickstart in one language."""
  model_config = ConfigDict(extra='forbid', title='quickstart block', use_attribute_docstrings=True)

  install: str | None = None
  """The shell command that installs the package, e.g. `pip install petstore`."""
  code: str = Field(min_length=1)
  """The first call, as the reader copies it."""


class DocsYml(BaseModel):
  """`docs/docs.yml`: the nav, the quickstart, and the metadata of a project's docs."""
  model_config = ConfigDict(extra='forbid', title='docs.yml', use_attribute_docstrings=True)

  schema_: str | None = Field(default=None, alias='$schema')
  """The published schema of this file, for editors."""
  title: str = Field(min_length=1)
  """The docs site's title, usually the client's name."""
  description: str | None = None
  """One sentence on what the API is, for the site's metadata."""
  nav: list[NavEntry] = Field(min_length=1)
  """The pages in reading order: each a path under `docs/`, or a section `{title, pages}`."""
  quickstart: dict[Language, QuickstartBlock] = Field(default_factory=dict)
  """One block per language `truewire.toml` declares, keyed by the language."""
