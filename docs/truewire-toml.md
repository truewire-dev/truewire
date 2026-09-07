# `truewire.toml` reference

A Truewire project is a directory holding a `truewire.toml`. Every command finds the
nearest one above the working directory, or takes `--project PATH`.

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
- **`[python]`**: where the generated package goes and how it is finished. Only `python` is a
  target today.
- **`[python.cores.<name>]`**: `base` is `module.path:Class`, the hand-written class every
  generated endpoint under that core subclasses; the module is imported during generation to
  introspect its `new()` signature. `children` maps a composed child attribute to the field
  it receives the transport from, for cores whose children need a different connection.
- **`[[python.extras."<router node>"]]`**: hand-written classes folded into a generated
  router node (`file`, `class`, optional `replaces`).

## Generated state

`generate` writes `.truewire/python-files.json`, the manifest of files it owns. Files not in
the manifest are never deleted; `generate --check` compares the plan to it, and
`generate --delete` removes only what it owns. Add `.truewire/` to `.gitignore`.
