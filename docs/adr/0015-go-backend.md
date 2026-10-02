# ADR 0015: The Go backend renders one package per endpoint, verbs with distinct names, and `meta` as a value the core type-asserts

- Status: accepted
- Date: 2026-09-15

## Context

`truewire generate go` is the fourth backend over the plan (`docs/plan.md`). Go has no
overloads, no sum types, no generic methods, and it exports by capitalisation. Three of the
decisions the Python, TypeScript and Rust backends made cannot be carried over as they are:

- **Name collisions inside a module.** Every endpoint of a router shares one Python module,
  one TypeScript file and one Rust module, so the plan's `Request`/`Response` names are
  disambiguated by file. Go scopes names by package, which is a directory.
- **A core serving several contracts.** Rust's `HttpEndpoint<SpotMeta> + HttpEndpoint<FuturesMeta>`
  and TypeScript's structural intersection let one core value serve two `meta` shapes and
  two transports under one method name (`request`). A Go type has one method per name.
- **Validation and the raw path.** Go's `encoding/json` does not check required keys, does
  not keep undocumented ones, and cannot tell an absent key from a null one.

## Decision

- **One package per endpoint** (`repos/get`, `repos/listcommits`), holding its types under
  the plan's own names and an `Endpoint` struct; one package per router holding the router
  struct; the root package holds the root struct, `meta/`, `types/` and `replay/`. Imports
  are exact because `[go]` declares `module` and `root` (the directory of `go.mod`).
- **Distinct verbs**: `HttpEndpoint.Request`, `CommandEndpoint.Command`,
  `StreamEndpoint.Subscribe`. A router whose subtree uses several declares a `Core`
  interface embedding them, so one core value still serves a whole client.
- **`meta` is a value, not a type parameter**: the call carries `Meta any` holding the
  generated `meta.XMeta{...}`, and the core type-asserts it. The contract interfaces are not
  generic, so a core serving endpoints with different `meta` shapes is still one type.
  What Rust's compiler checks at construction, a Go core checks once per call.
- **Records carry their own validation**: each generated struct has `UnmarshalJSON`/
  `MarshalJSON` that call `truewire.DecodeObject`/`EncodeObject` with one descriptor per key
  (`Required`, `RequiredNullable`, `OptionalField`, `OptionalNullable`). A key both optional
  and nullable is `truewire.Optional[T]`; an optional or nullable key is otherwise the
  nil-able form (`*T`, a slice, a map, `any`). Undocumented keys land in `Extra`. Unions are
  structs of variant pointers tried in order, tuples structs of positions.
- **`validate: false` is the `Raw` twin**, as in Rust: `Get` returns the typed value,
  `GetRaw` the `json.RawMessage` the core returned.
- **Walkers return `*truewire.PaginatedResponse[Row, State]`** with `Pages`/`Rows` as
  `iter.Seq2`; a `seek` walk (ADR 0013) is rendered over `truewire.Seek`, whose state is
  `SeekState{Pos, Carried}`. A declared stream `reply` (ADR 0014) makes the typed method
  return `*truewire.Subscription[Message, Reply]`.
- **An `rpc` endpoint declaring both transports** holds a `truewire.RpcEndpoint`
  (`HttpEndpoint` and `CommandEndpoint` embedded) and chooses the verb per call from
  `CallOptions.Transport` (`truewire.WithTransport`), the first declared transport by
  default: the Go form of Python's `transport=` keyword, with no second method per wire.
  A router's twin (`Raw`, `Paged`) that collides with a sibling endpoint's own method is
  renamed with a numeric suffix; the endpoint's own method keeps its name.
- **The printer reproduces `gofmt`** (tabs, struct field columns broken by comments and
  blank lines, sorted import groups) and emits nothing `gofmt` would align otherwise, so
  `--check` needs no Go toolchain and `gofmt -l` stays empty in CI.

## Consequences

- A hand-written Go core is one type per transport with `Request`/`Command`/`Subscribe`
  methods; the constructor a caller uses (`github.New`) is a hand-written file beside the
  generated root, since generated code never imports the core (ADR 0011).
- A `meta` mismatch between the generated literal and the core is a runtime type assertion,
  not a compile error.
- Package names are the segment's words run together (`list_commits` -> `listcommits`), so
  a spec whose two sibling segments differ only by separators is refused by rule 18 as in
  every other backend.
