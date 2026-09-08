"""The codegen half of `truewire.toml` (`[cores.*]` and `[python]`): not purely "the
per-target-language escape hatch" -- it's the part declaring per-`core`-name facts, some
of which are language-neutral (`[cores.<name>]`'s own `meta` JSON Schema -- an upstream-API
fact, exactly as meaningful to a hypothetical Rust/TypeScript backend as to this one) and
some of which are per-language bindings for that same name (`[python]`'s root class name --
unmechanizable, real API names like `KuCoin` defeat PascalCase-of-slug -- and its
`core`-resolvable class references).

`load_codegen_toml` reads these sections from the project's `truewire.toml`
(`truewire.project`)."""
import tomllib
from pathlib import Path
from typing_extensions import Any

from pydantic import BaseModel, ConfigDict, Field


class CoreConfig(BaseModel):
  """One top-level `[cores.<name>]` entry: this symbolic core name's
  `meta` shape, as a real JSON Schema -- an API fact, not a Python fact, so it lives
  outside `[python]` and is meaningful to any future non-Python backend too. Distinct
  from, and never a replacement for, `[python.cores.<name>]`'s own `PythonCoreConfig`
  (the hand-written base class this same symbolic name resolves to) -- one core name
  can, and typically does, appear in both tables, each describing a different kind of
  fact about it."""
  model_config = ConfigDict(extra='forbid')

  meta: dict[str, Any] | None = None
  """JSON Schema describing this core's `meta` shape, or `None` (the common case, most
  cores need no `meta` schema at all) when this core has no per-endpoint quirks to
  describe -- every endpoint resolving to a core with no schema here must then declare
  its own `meta: {}`."""


class NewParam(BaseModel):
  """The table form of one `[python.cores.<name>].params` value: its type plus whether a
  caller must pass it. The string form (`network = "pkg.core:Network"`) is a required
  parameter; this form exists to declare an optional one."""
  model_config = ConfigDict(extra='forbid')

  type: str = Field(min_length=1)
  """`module.path:Name` (imported by the composing module) or a bare builtin name
  (`str`, `int`, `bool`, `float`), rendered as the parameter's annotation."""
  required: bool = True
  """`False` renders `name: Type | None = None` and passes `None` through to `new()`."""


class PythonCoreConfig(BaseModel):
  """One `[python.cores.<name>]` entry: the hand-written base class this symbolic core
  name resolves to, how a composite whose base it is hands each child its transport
  (`children`), and -- when the base is built through `new(client, *, ...)` rather than
  its dataclass constructor -- which keywords that `new()` takes (`forward`, `params`).
  Every entry is this identical shape, root position included: there is no separate
  bare-string form, and no separate `client_base` field -- whatever resolves at the root
  position is just another entry here. The generator reads only this table to compose a
  router; it never imports the base (ADR 0011)."""
  model_config = ConfigDict(extra='forbid')

  base: str = Field(min_length=1)
  """`module.path:ClassName` this symbolic core name resolves to."""
  children: dict[str, str] | None = None
  """Child attribute name -> field name on `self` to forward when composing it,
  e.g. `{"rest": "rest_client", "streams": "streams_client"}`. `None` (the common
  case for most projects) means this base composes no distinctly-based child -- every
  subdirectory it composes forwards the single implicit `self.client`."""
  forward: list[str] | None = None
  """`new()` keywords the composing class passes from its own same-named fields
  (`market_client=self.market_client`). Declaring this (even as `[]`) or `params` means
  the base is constructed through `new(client, *, ...)`; a child under a core declaring
  neither is constructed as `Child(client=self.<field>)`."""
  params: dict[str, str | NewParam] | None = None
  """`new()` keywords a caller supplies, name -> type (`"pkg.core:Network"`, a bare
  builtin, or the `{type, required}` table). The composing class exposes each as a
  keyword-only parameter of the child's accessor method, unless its own resolved core is
  this same core, in which case the class already carries the field and forwards
  `self.<name>` instead."""

  @property
  def composes_via_new(self) -> bool:
    """Whether a child under this core is built through `new(client, *, ...)`."""
    return self.forward is not None or self.params is not None

  @property
  def new_params(self) -> dict[str, NewParam]:
    """`params` with every string-form entry expanded to its `NewParam` table."""
    return {
      name: value if isinstance(value, NewParam) else NewParam(type=value)
      for name, value in (self.params or {}).items()
    }


class ExtraEntry(BaseModel):
  """One hand-written Python class `router()`'s aggregate composition folds in as a base
  alongside its own generated leaves -- the Python-target-specific analog of a
  `[python.cores]` entry, for a sibling that isn't itself a resolved core (say
  `GetCandlesPaged(GetCandles)`, a hand-written convenience wrapper `router()` had no
  way to substitute for its own generated `GetCandles` base, or `Broadcast`, a
  hand-written class with no corresponding `spec/endpoints/` leaf at all -- wallet
  signing is out of the declarative spec's scope entirely)."""
  model_config = ConfigDict(extra='forbid')

  file: str = Field(min_length=1)
  """Module name (no `.py`), a sibling of the router's own `__init__.py` in the same
  `spec/endpoints/`-mirrored directory -- `"get_candles_paged"` for
  `.../get_candles_paged.py`."""
  class_: str = Field(
    min_length=1, validation_alias='class', serialization_alias='class',
  )
  """The hand-written class `file` defines."""
  replaces: str | None = None
  """The generated leaf's own attribute name this substitutes for, when the hand-written
  class subclasses (and so already provides) a real generated leaf's own method --
  `router()` lists it in that leaf's place rather than composing both. `None` (the
  common case for a class with no spec leaf of its own at all, like `Broadcast`) adds it
  as one more base with no substitution; its own attribute name is then `file`'s own
  basename."""


class PythonCodegenConfig(BaseModel):
  """The `[python]` section: everything the Python backend can't derive mechanically."""
  model_config = ConfigDict(extra='forbid')

  name: str | None = Field(default=None, min_length=1)
  """Generated root class name -- real API names (`KuCoin`, `dYdX`) aren't PascalCase-of-slug."""
  package: str | None = Field(default=None, min_length=1)
  """Import name of the generated package (`petstore` for `src/petstore/`). Defaults to
  `[project].name`."""
  src: str = 'src'
  """Directory the package lives under, relative to the project root."""
  ruff: str | None = None
  """Ruff config `truewire generate` formats with, relative to the project root. `None`
  uses the config shipped with truewire (`truewire/resources/ruff.toml`)."""
  pyright: bool = False
  """Run pyright over the package after generating. Also implied by a `pyrightconfig.json`
  at the project root."""
  backend: str | None = None
  """A Python module (relative to the project root) exporting a `generator` -- a
  `truewire.codegen.python.Generator` subclass customizing generation. `None` (the common
  case) generates with the universal `Generator` and no overrides."""
  cores: dict[str, PythonCoreConfig] = Field(min_length=1)
  """Symbolic core name -> its resolved base, selected by `router.json`'s
  `core` field. At least one entry -- every project needs at least its root's own base,
  even a single-core project."""
  extras: dict[str, list[ExtraEntry]] | None = None
  """Router node (dotted, e.g. `"indexer.data"` -- the function-tree node `router()`
  composes, never including `output_base`) -> hand-written classes to fold into that
  node's composition, beyond what its own `spec/endpoints/` leaves generate. `None` (the
  overwhelming common case) -- most projects need no escape hatch here at all. Retires
  `Endpoint.extra`/`ExtraMethod`, which declared the identical fact per-endpoint but was
  never actually read by any codegen backend."""


class CodegenConfig(BaseModel):
  """The codegen sections of `truewire.toml` -- `[python]` is sectioned by target language so a future
  non-Python backend gets its own section without touching this one; `cores`
  is language-neutral and sits alongside it, keyed by the identical symbolic core name
  `[python.cores]`/`router.json`'s own `core` field already resolve."""
  model_config = ConfigDict(extra='forbid')

  python: PythonCodegenConfig | None = None
  cores: dict[str, CoreConfig] | None = None
  """Symbolic core name -> its declared `meta` shape. Optional per name --
  most cores need no entry at all -- and, unlike `[python.cores]`, never required to
  exist for a name that has no quirks to describe."""


def load_codegen_config(data: dict[str, Any]) -> CodegenConfig:
  """
  Validate the codegen sections (`cores`, `python`) of an already-decoded `truewire.toml`.

  Args:
    data: A mapping holding at most the `cores` and `python` keys.

  Raises:
    pydantic.ValidationError: Invalid configuration.
  """
  return CodegenConfig.model_validate(data)


def load_codegen_toml(root: Path) -> CodegenConfig:
  """
  Load and validate the codegen sections of the `truewire.toml` at a project root.

  Args:
    root: The project directory -- `truewire.toml` sits directly under this.

  Raises:
    FileNotFoundError: No `truewire.toml` at `root`.
    ValueError: Invalid configuration, including missing `cores` table in `[python]` section.
  """
  path = root / 'truewire.toml'
  if not path.is_file():
    raise FileNotFoundError(f'{root}: expected truewire.toml')
  with path.open('rb') as handle:
    data = tomllib.load(handle)
  return load_codegen_config({key: data[key] for key in ('cores', 'python') if key in data})
