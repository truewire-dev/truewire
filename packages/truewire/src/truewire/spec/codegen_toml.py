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
  model_config = ConfigDict(extra='forbid', json_schema_extra={'description': 'One `[cores.<name>]` entry: facts about a symbolic core name that hold in every language.'})

  meta: dict[str, Any] | None = Field(default=None, description=(
    "A JSON Schema for the `meta` of every endpoint on this core. Without one, each such "
    "endpoint declares its own `meta: {}`."
  ))
  """JSON Schema describing this core's `meta` shape, or `None` (the common case, most
  cores need no `meta` schema at all) when this core has no per-endpoint quirks to
  describe -- every endpoint resolving to a core with no schema here must then declare
  its own `meta: {}`."""


class NewParam(BaseModel):
  """The table form of one `[python.cores.<name>].params` value: its type plus whether a
  caller must pass it. The string form (`network = "pkg.core:Network"`) is a required
  parameter; this form exists to declare an optional one."""
  model_config = ConfigDict(extra='forbid', json_schema_extra={'description': 'A `new()` keyword as a table: its type, and whether a caller must pass it.'})

  type: str = Field(min_length=1, description='`module.path:Name`, or a builtin: `str`, `int`, `bool`, `float`.')
  """`module.path:Name` (imported by the composing module) or a bare builtin name
  (`str`, `int`, `bool`, `float`), rendered as the parameter's annotation."""
  required: bool = Field(default=True, description='Whether a caller must pass it; `false` makes it optional, `None` by default.')
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
  model_config = ConfigDict(extra='forbid', json_schema_extra={'description': 'One `[python.cores.<name>]` entry: the hand-written Python class a core name resolves to, and how it is built.'})

  base: str = Field(min_length=1, description='The class this core name resolves to, as `module.path:ClassName`.')
  """`module.path:ClassName` this symbolic core name resolves to."""
  children: dict[str, str] | None = Field(default=None, description=(
    'Child attribute -> the field of this class forwarded to it, e.g. `{rest = "rest_client"}`. '
    'Without it, every child gets the one client.'
  ))
  """Child attribute name -> field name on `self` to forward when composing it,
  e.g. `{"rest": "rest_client", "streams": "streams_client"}`. `None` (the common
  case for most projects) means this base composes no distinctly-based child -- every
  subdirectory it composes forwards the single implicit `self.client`."""
  forward: list[str] | None = Field(default=None, description=(
    '`new()` keywords passed on from the composing class\'s same-named fields. Declaring it, '
    'even empty, builds the class through `new(client, *, ...)`.'
  ))
  """`new()` keywords the composing class passes from its own same-named fields
  (`market_client=self.market_client`). Declaring this (even as `[]`) or `params` means
  the base is constructed through `new(client, *, ...)`; a child under a core declaring
  neither is constructed as `Child(client=self.<field>)`."""
  params: dict[str, str | NewParam] | None = Field(default=None, description=(
    '`new()` keywords a caller supplies: name -> type (`"pkg.core:Network"`, a builtin), or a '
    '`{type, required}` table for an optional one.'
  ))
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
  model_config = ConfigDict(extra='forbid', json_schema_extra={'description': 'A hand-written Python class a generated router folds in beside its generated endpoints.'})

  file: str = Field(min_length=1, description='The module defining the class, without `.py`, beside the router\'s `__init__.py`.')
  """Module name (no `.py`), a sibling of the router's own `__init__.py` in the same
  `spec/endpoints/`-mirrored directory -- `"get_candles_paged"` for
  `.../get_candles_paged.py`."""
  class_: str = Field(
    min_length=1, validation_alias='class', serialization_alias='class',
    description='The class `file` defines.',
  )
  """The hand-written class `file` defines."""
  replaces: str | None = Field(default=None, description=(
    'The generated endpoint this class stands in for, by its attribute name. Without it, the '
    'class is added beside the generated ones.'
  ))
  """The generated leaf's own attribute name this substitutes for, when the hand-written
  class subclasses (and so already provides) a real generated leaf's own method --
  `router()` lists it in that leaf's place rather than composing both. `None` (the
  common case for a class with no spec leaf of its own at all, like `Broadcast`) adds it
  as one more base with no substitution; its own attribute name is then `file`'s own
  basename."""


class PythonCodegenConfig(BaseModel):
  """The `[python]` section: everything the Python backend can't derive mechanically."""
  model_config = ConfigDict(extra='forbid', json_schema_extra={'description': '`[python]`: the generated Python package.'})

  name: str | None = Field(default=None, min_length=1, description='Generated root class name, e.g. `KuCoin`; PascalCase of `[project].name` by default.')
  """Generated root class name -- real API names (`KuCoin`, `dYdX`) aren't PascalCase-of-slug."""
  package: str | None = Field(default=None, min_length=1, description='Import name of the generated package (`petstore` for `src/petstore/`); `[project].name` by default.')
  """Import name of the generated package (`petstore` for `src/petstore/`). Defaults to
  `[project].name`."""
  src: str = Field(default='src', description='Directory the package lives under, relative to the project root.')
  """Directory the package lives under, relative to the project root."""
  ruff: str | None = Field(default=None, description='Ruff config `truewire generate` formats with, relative to the project root; the one shipped with truewire by default.')
  """Ruff config `truewire generate` formats with, relative to the project root. `None`
  uses the config shipped with truewire (`truewire/resources/ruff.toml`)."""
  pyright: bool = Field(default=False, description='Run pyright over the package after generating; also on when a `pyrightconfig.json` is at the root.')
  """Run pyright over the package after generating. Also implied by a `pyrightconfig.json`
  at the project root."""
  backend: str | None = Field(default=None, description='A Python module, relative to the project root, exporting a `generator` that customizes generation.')
  """A Python module (relative to the project root) exporting a `generator` -- a
  `truewire.codegen.python.Generator` subclass customizing generation. `None` (the common
  case) generates with the universal `Generator` and no overrides."""
  cores: dict[str, PythonCoreConfig] = Field(min_length=1, description='Core name -> the hand-written class it resolves to; `router.json`\'s `core` picks one. At least the root\'s.')
  """Symbolic core name -> its resolved base, selected by `router.json`'s
  `core` field. At least one entry -- every project needs at least its root's own base,
  even a single-core project."""
  extras: dict[str, list[ExtraEntry]] | None = Field(default=None, description='Router node (dotted, `"indexer.data"`) -> hand-written classes folded into it.')
  """Router node (dotted, e.g. `"indexer.data"` -- the function-tree node `router()`
  composes, never including `output_base`) -> hand-written classes to fold into that
  node's composition, beyond what its own `spec/endpoints/` leaves generate. `None` (the
  overwhelming common case) -- most projects need no escape hatch here at all. Retires
  `Endpoint.extra`/`ExtraMethod`, which declared the identical fact per-endpoint but was
  never actually read by any codegen backend."""


class TypescriptExtraEntry(BaseModel):
  """One hand-written TypeScript class a generated router folds in: the TypeScript analog
  of a `[python.extras]` entry. The router imports `class` from `file`, builds it from the
  core the router already holds (the whole core, or `field` under a composite), and exposes
  each of `methods` as a delegating property typed by the class's own method, so its
  overloads survive."""
  model_config = ConfigDict(extra='forbid', json_schema_extra={'description': 'A hand-written TypeScript class a generated router folds in beside its generated endpoints.'})

  file: str = Field(min_length=1, pattern=r'^[A-Za-z_][A-Za-z0-9_]*$', description='The module exporting the class, without `.ts`, beside the router\'s `index.ts`.')
  """Module name (no `.ts`), a sibling of the router's `index.ts`: `retrieve_export` for
  `<package>/spot/account/retrieve_export.ts`."""
  class_: str = Field(min_length=1, validation_alias='class', serialization_alias='class', description='The class `file` exports.')
  """The class `file` exports."""
  methods: list[str] = Field(default_factory=list, description='The methods of the class the router exposes, by their TypeScript names.')
  """The class's methods the router exposes, by their TypeScript names."""
  replaces: str | None = Field(default=None, description=(
    'The generated child endpoint this class stands in for, by its spec segment. Without it, '
    'the class is added beside the generated ones.'
  ))
  """A generated child endpoint (by its spec segment, `get_candles`) this class stands in
  for: it subclasses the generated class, so the router keeps delegating the generated
  method to it. `None` adds the class beside the generated children."""
  field: str | None = Field(default=None, description='Under a composite router, the field of its core the class is built from; `client` by default.')
  """Under a composite router, the field of its core the class is built from; `client`
  by default. Ignored by a plain router, which hands every child the same core."""


class TypescriptCodegenConfig(BaseModel):
  """The `[typescript]` section: where the generated TypeScript package goes and what its
  root class is called. The TypeScript backend reads no per-core binding: a generated
  class takes its core as a constructor argument typed by the `@truewire/core` contract
  (`HttpEndpoint<Meta>`), and the hand-written `core/index.ts` satisfies it
  structurally (ADR 0011)."""
  model_config = ConfigDict(extra='forbid', json_schema_extra={'description': '`[typescript]`: the generated TypeScript package.'})

  name: str | None = Field(default=None, min_length=1, description='Generated root class name, e.g. `KuCoin`; `[python].name` by default.')
  """Generated root class name; defaults to `[python].name`, else PascalCase of `[project].name`."""
  package: str | None = Field(default=None, min_length=1, description='Directory of the generated package under `src`; `[project].name` by default.')
  """Directory of the generated package under `src` (`github` for `src/github/`). Defaults
  to `[project].name`."""
  src: str = Field(default='src', description='Directory the package lives under, relative to the project root.')
  """Directory the package lives under, relative to the project root."""
  extras: dict[str, list[TypescriptExtraEntry]] | None = Field(default=None, description=(
    'Router node (dotted, `"spot.account"`; `""` for the root) -> hand-written classes folded into it.'
  ))
  """Router node (dotted, `"spot.account"`; `""` for the root) -> hand-written classes
  folded into that router beside its generated children. What serves an endpoint whose
  spec declares `surface: handwritten`, which the TypeScript backend does not render."""


class RustCodegenConfig(BaseModel):
  """The `[rust]` section: where the generated Rust modules go and what the root struct is
  called. Like the TypeScript backend, the Rust one reads no per-core binding: a generated
  struct holds its core as an `Arc<dyn HttpEndpoint<Meta>>` from `truewire-core`, and the
  hand-written `core` module the crate root declares implements the trait (ADR 0011)."""
  model_config = ConfigDict(extra='forbid', json_schema_extra={'description': '`[rust]`: the generated Rust crate.'})

  name: str | None = Field(default=None, min_length=1, description='Generated root struct name, e.g. `KuCoin`; `[python].name` by default.')
  """Generated root struct name; defaults to `[python].name`, else PascalCase of `[project].name`."""
  package: str | None = Field(default=None, min_length=1, description='Directory of the generated modules under `src`; `[project].name` by default.')
  """Directory of the generated modules under `src` (`github` for `src/github/`, whose
  `lib.rs` the project's `Cargo.toml` names as the library path). Defaults to
  `[project].name`."""
  src: str = Field(default='src', description='Directory the package lives under, relative to the project root.')
  """Directory the package lives under, relative to the project root."""


class GoCodegenConfig(BaseModel):
  """The `[go]` section: where the generated Go packages go, what the root struct is called,
  and the import path they are reached by. Like the TypeScript and Rust backends, the Go one
  reads no per-core binding: a generated struct holds its core behind a `truewire.dev/core`
  contract interface (`HttpEndpoint`), and the hand-written `core` package satisfies it
  (ADR 0011)."""
  model_config = ConfigDict(extra='forbid', json_schema_extra={'description': '`[go]`: the generated Go packages.'})

  name: str | None = Field(default=None, min_length=1, description='Generated root struct name, e.g. `KuCoin`; `[python].name` by default.')
  """Generated root struct name; defaults to `[python].name`, else PascalCase of `[project].name`."""
  package: str | None = Field(default=None, min_length=1, description='Directory of the generated root package under `src`; `[project].name` by default.')
  """Directory of the generated root package under `src` (`github` for `src/github/`).
  Defaults to `[project].name`."""
  src: str = Field(default='src', description='Directory the package lives under, relative to the project root.')
  """Directory the package lives under, relative to the project root."""
  module: str = Field(min_length=1, description='The module path `go.mod` declares, e.g. `example.com/kraken`.')
  """The module path `go.mod` declares (`typed.local/kraken`)."""
  root: str = Field(default='.', description='Directory holding `go.mod`, relative to the project root.')
  """Directory holding `go.mod`, relative to the project root. Every generated import path
  is `module` joined with the package directory's path relative to it."""


class CodegenConfig(BaseModel):
  """The codegen sections of `truewire.toml` -- `[python]`, `[typescript]` and `[rust]` are sectioned by
  target language so each backend has its own section without touching the other; `cores`
  is language-neutral and sits alongside them, keyed by the identical symbolic core name
  `[python.cores]`/`router.json`'s own `core` field already resolve."""
  model_config = ConfigDict(extra='forbid')

  python: PythonCodegenConfig | None = Field(default=None, description='The generated Python package.')
  typescript: TypescriptCodegenConfig | None = Field(default=None, description='The generated TypeScript package.')
  rust: RustCodegenConfig | None = Field(default=None, description='The generated Rust crate.')
  go: GoCodegenConfig | None = Field(default=None, description='The generated Go packages.')
  cores: dict[str, CoreConfig] | None = Field(default=None, description='Core name -> facts about it that hold in every language; most cores need no entry.')
  """Symbolic core name -> its declared `meta` shape. Optional per name --
  most cores need no entry at all -- and, unlike `[python.cores]`, never required to
  exist for a name that has no quirks to describe."""


def load_codegen_config(data: dict[str, Any]) -> CodegenConfig:
  """
  Validate the codegen sections (`cores`, `python`, `typescript`, `rust`) of an already-decoded
  `truewire.toml`.

  Args:
    data: A mapping holding at most the `cores`, `python`, `typescript` and `rust` keys.

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
  return load_codegen_config(
    {key: data[key] for key in ('cores', 'python', 'typescript', 'rust', 'go') if key in data}
  )
