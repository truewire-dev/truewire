"""`truewire.toml` as a pydantic model: the source of `truewire.toml.json` (W13).

`truewire.project` still parses the file, with messages written for it; this model describes
the same file for an editor. `test_schemas.py` holds the loader, the model and the published
schema to one verdict on every `truewire.toml` in the repository and on a list of edge cases.

Docstrings here are published as editor hovers, so they are written for the file's author.
"""
from collections import Counter
from datetime import date, datetime
from typing_extensions import Annotated, Any, Literal, Self, TypeVar, get_args

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from truewire.spec.codegen_toml import CodegenConfig

Language = Literal['python', 'typescript', 'rust', 'go']
"""The languages a project may declare a `[<language>]` section for."""

LANGUAGES: tuple[str, ...] = get_args(Language)

T = TypeVar('T')


def _unique(items: list[T]) -> list[T]:
  """`items`, refused when any appears twice, as `uniqueItems` refuses it in the schema."""
  repeated = sorted(str(item) for item, count in Counter(items).items() if count > 1)
  if repeated:
    raise ValueError(f'lists {", ".join(repeated)} more than once')
  return items


def _no_time(value: Any) -> Any:
  """`value`, refused when it is a date with a time, which the loader refuses too."""
  if isinstance(value, datetime):
    raise ValueError('must be a date, YYYY-MM-DD, without a time')
  return value


EnvName = Annotated[str, Field(pattern=r'^[A-Za-z_][A-Za-z0-9_]*$')]
"""An environment variable's name: never its value, never a path."""

Unique = Annotated[list[T], AfterValidator(_unique), Field(json_schema_extra={'uniqueItems': True})]
"""A list in which nothing appears twice."""


class ProjectTable(BaseModel):
  """`[project]`: what the project is called."""
  model_config = ConfigDict(extra='forbid', use_attribute_docstrings=True)

  name: str = Field(min_length=1)
  """The project's name; the package names default to it."""


class SpecTable(BaseModel):
  """`[spec]`: where the spec lives."""
  model_config = ConfigDict(extra='forbid', use_attribute_docstrings=True)

  dir: str = Field(default='spec', min_length=1)
  """The spec directory, relative to `truewire.toml`; it holds `endpoints/` and `schemas.json`."""


class SecretsTable(BaseModel):
  """`[secrets]`: the environment variables a caller sets, by name only. Their values go in
  `.env`, which is git-ignored, and nowhere else."""
  model_config = ConfigDict(extra='forbid', use_attribute_docstrings=True)

  required: Unique[EnvName] = Field(default_factory=list)
  """Variables the client cannot run without."""
  optional: Unique[EnvName] = Field(default_factory=list)
  """Variables that unlock more of the API when set."""

  @model_validator(mode='after')
  def _not_both(self) -> Self:
    """Refuse a variable listed as both required and optional."""
    both = sorted(set(self.required) & set(self.optional))
    if both:
      raise ValueError(f'lists {", ".join(both)} as both required and optional')
    return self


class PolicyTable(BaseModel):
  """`[policy]`: what the client may do on its own behalf."""
  model_config = ConfigDict(extra='forbid', use_attribute_docstrings=True)

  rate: Annotated[float, Field(gt=0, strict=True, allow_inf_nan=False)] | None = None
  """Requests per second the client paces itself to; unpaced when absent."""
  retry: bool = Field(default=False, strict=True)
  """Whether the client retries a failed request on its own."""
  refuse: Unique[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)
  """Endpoint ids the client refuses to call, e.g. `account.withdraw`."""


class ScoreTable(BaseModel):
  """`[score]`: facts about the project that no command can measure."""
  model_config = ConfigDict(extra='forbid', use_attribute_docstrings=True)

  stranger: Annotated[date, BeforeValidator(_no_time)] | None = None
  """The day a newcomer last completed the quickstart from the published package and docs
  alone."""


class TruewireToml(CodegenConfig):
  """`truewire.toml`, the one project file of a Truewire project."""
  model_config = ConfigDict(extra='forbid', title='truewire.toml', use_attribute_docstrings=True)

  project: ProjectTable | None = None
  """What the project is called; a file without it is named after its directory."""
  spec: SpecTable | None = None
  """Where the spec lives."""
  secrets: SecretsTable | None = None
  """The environment variables a caller sets, by name only."""
  policy: PolicyTable | None = None
  """What the client may do on its own behalf: pacing, retries, refusals."""
  score: ScoreTable | None = None
  """Facts about the project that no command can measure."""
