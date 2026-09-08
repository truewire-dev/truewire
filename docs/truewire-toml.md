# `truewire.toml` reference

A Truewire project is a directory holding a `truewire.toml`. Every command finds the
nearest one above the working directory, or takes `--project PATH`. `truewire init` writes
the file with the `[cores.*]` and `[python.cores.*]` tables its `--template` needs; see
[docs/cores.md](cores.md) for what each template wires.

```toml
[project]
name = "petstore"            # required. Also the default Python package name.

[spec]
dir = "spec"                 # default "spec": holds endpoints/ and schemas.json

[secrets]                    # optional: credential variable *names*, never values.
required = ["PETSTORE_API_KEY"]

[cores.default]              # one table per symbolic core name a router.json can declare.
meta = { type = "object", properties = { public = { type = "boolean" } }, additionalProperties = false }

[python]
package = "petstore"         # default: [project].name
src = "src"                  # default "src": the package lives at <src>/<package>
name = "Petstore"            # root class name; default: PascalCase of [project].name
backend = "backend.py"       # optional: a module exporting `generator`, a Generator subclass
ruff = "ruff.toml"           # optional: formatter config; default is the one shipped with truewire
pyright = false              # run pyright after generate; also implied by a pyrightconfig.json

[python.cores.root]          # the core the root router.json names (usually "root")
base = "petstore.core:ClientBase"

[python.cores.default]       # every other symbolic core: base class + how children get the transport
base = "petstore.core:Endpoint"

[python.cores.streams]       # a base built through `new(client, *, ...)` declares its keywords
base = "petstore.core:StreamsBase"
forward = ["market_client"]  # passed from the composing class's own same-named field
params = { network = "petstore.core:Network" }   # exposed to the caller, typed by import
children = { market_data = "market_client", private = "private_client" }

[typescript]
package = "petstore"         # default: [project].name; the package lives at <src>/<package>
src = "src"                  # default "src"
name = "Petstore"            # root class name; default: [python].name, else PascalCase of [project].name
```

## Sections

- **`[project]`**: `name` is required. It names the project in output and is the default
  for `[python].package`.
- **`[spec]`**: `dir` relocates the spec tree. `truewire check`, `examples`, `surface`, `mock`
  and `generate` all read `<dir>/endpoints/**/endpoint.json`, `<dir>/**/router.json` and
  `<dir>/schemas.json`.
- **`[secrets]`**: variable names only. `truewire standards` uses them to flag a recorded
  example that leaked a real credential-shaped value.
- **`[cores.<name>]`**: the JSON Schema every endpoint's `meta` must satisfy when its nearest
  `router.json` resolves to `<name>`. Omit `meta` for a core that reads nothing per call.
- **`[python]`**: where the generated Python package goes and how it is finished.
- **`[python.cores.<name>]`**: `base` is `module.path:Class`, the hand-written class every
  generated endpoint under that core subclasses. Nothing is imported during generation
  (ADR 0011); the three optional keys below say how a composite built on this base is
  constructed.
  - `children` maps a composed child attribute to the field of `self` it receives as its
    transport, for a base whose children need different connections
    (`{ spot = "spot_client", streams = "private_client" }`). Unlisted children get
    `self.client`.
  - `forward` lists the keywords of this base's `new(client, *, ...)` that the composing
    class passes from its own same-named fields: `forward = ["market_client"]` renders
    `Streams.new(self.private_client, market_client=self.market_client)`.
  - `params` maps the keywords of `new()` a caller supplies to their types. A string is
    `module.path:Name` (imported by the composing module) or a bare builtin (`str`); the
    table form `{ type = "...", required = false }` makes it optional. The composing class
    exposes each as a keyword-only parameter (`def token(self, *, network: Network)`),
    unless its own core is this same core, in which case it already carries the field and
    forwards `self.<name>`.

  Declaring `forward` (even as `[]`) or `params` means the base is built through
  `new(client, *, ...)`; declaring neither builds it as `Child(client=self.<field>)`. The
  protocols a base satisfies are in `truewire_core.contract`.
- **`[[python.extras."<router node>"]]`**: hand-written classes folded into a generated
  router node (`file`, `class`, optional `replaces`).
- **`[typescript]`**: where `truewire generate typescript` writes the TypeScript package
  (`<src>/<package>/`) and what its root class is called. There is no per-core table: a
  generated class takes its core as a constructor argument typed by the `@truewire/core`
  contract (`HttpEndpoint<Meta>`, `CommandEndpoint<Meta>`, `StreamEndpoint<Meta>`), and
  the hand-written `<package>/core/index.ts` satisfies it by shape, so nothing is imported
  or resolved at generation. The composition keys are read from `[python.cores.<name>]`,
  the one declaration serving both languages: a core declaring `children` or `forward`
  makes its router take a *fields object* -- one property per field the declarations
  name, exported as the `<Class>Core` interface -- and hand each child the field its
  `children` entry maps it to (`client` when unmapped), or the whole object to a child
  that is itself composite. `params` needs nothing: the hand-written core takes its
  parameters when it is built. See [docs/typescript.md](typescript.md#composite-cores).

## Generated state

`generate` writes `<src>/<package>/meta.py`, one `TypedDict` per `[cores.<name>]` that
declares a `meta` schema (`SpotMeta` for `spot`); the hand-written core annotates its
`meta` parameter with it. `truewire init` writes the first one so the core template can
import it before the first `generate`.

`generate typescript` writes `<src>/<package>/meta.ts`, one interface per core with a `meta`
schema (`DefaultMeta` for `default`), which the hand-written core names as the `Meta`
parameter of `HttpEndpoint`.

`generate` writes `.truewire/<language>-files.json` (`python-files.json`,
`typescript-files.json`), the manifest of files it owns. Files not in the manifest are
never deleted; `generate --check` compares the plan to it and each owned file's content
to what the plan renders (formatted the way `generate` writes it), and `generate --delete`
removes only what it owns. Add `.truewire/` to `.gitignore` (`truewire init` does): the
manifest is local state, not source. On a checkout without it, `generate --check` takes
the plan as the owned file list -- it names every file `generate` would write -- and
checks existence and content the same way; the one thing it cannot see without a manifest
is a file an earlier plan owned that this one does not, which the next `generate` deletes.
So CI runs `truewire generate python --check` on a fresh clone with nothing committed
under `.truewire/`.
