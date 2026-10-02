# Go

`truewire generate go` renders a project's plan (`docs/plan.md`) as Go packages with the
same guarantees as the other backends: typed requests and responses, validation with a raw
escape hatch, and resumable pagination walkers. The runtime it depends on is
`truewire.dev/core` (`packages/core-go`). The decisions that differ from the other backends
are recorded in ADR 0015.

Status: the runtime and the generator exist and are tested (`runtime-go` and `examples-go`
in CI). Every endpoint kind the plan holds is rendered: HTTP and WebSocket `rpc`, `stream`
(with a typed subscribe `reply`, ADR 0014), composite cores, and `page`, `token`, `offset` and
`seek` walkers. `examples/github` (HTTP, paging) and `examples/kraken` (signed REST, WebSocket
commands and channels, a composite root) are generated, vetted and replayed against
`truewire mock`. An `rpc` endpoint declaring both transports is rendered over both (see
"Both transports"). Left out, and reported as skipped: walks with no resumable state and
cursors with no Go reading. Unary gRPC endpoints are rendered over protobuf-go stubs
(`truewire protos go`), called through `truewire.dev/core/grpc` and replayed with
`grpctest.StartMock`/`Replay` (ADR 0017); streaming gRPC is not done yet. Protobuf
WebSocket frames are decoded by a hand-written core with `truewire.dev/core/protoframes`
over the generated `protos.Sources` (ADR 0016). The module is not
published; a project consumes it with a `replace` directive or a `go.work`.

## Using it

```toml
# truewire.toml
[go]
package = "github"                        # the root package lives at src/github/
src = "src"
name = "GitHub"                           # the root struct
module = "truewire.dev/examples/github"   # the module path go.mod declares
root = "."                                # where go.mod is, relative to the project root
```

```sh
truewire generate go            # writes src/github/**/*.go and .truewire/codegen/go.json
truewire generate go --check    # CI: every owned file exists and is what the plan renders
```

Import paths are `module` joined with the package directory relative to `root`
(`truewire.dev/examples/github/src/github/repos/get`). Then write `go.mod`, the hand-written
core package, and a `New` beside the generated root:

```go
// src/github/new.go (hand-written; the generated root package leaves New to you)
func New(options core.Options) *GitHub { return FromCore(core.New(options)) }
```

```go
client := github.New(core.Options{Token: os.Getenv("GITHUB_TOKEN")})
repo, err := client.Repos.Get(ctx, get.Request{Owner: "truewire-dev", Repo: "truewire"})
raw, err := client.Repos.GetRaw(ctx, get.Request{Owner: "truewire-dev", Repo: "truewire"})
commits, err := client.Repos.ListCommitsPaged(listcommits.PagedRequest{Owner: "truewire-dev", Repo: "truewire"}).All(ctx)
```

`examples/github` is the reference: `truewire.toml`, `go.mod`, `src/github/core/core.go`,
`src/github/new.go` and `tests/`.

## What is generated

| file | contents |
| --- | --- |
| `client.go` | the root struct and `FromCore(core)` (or `FromCores(...)` for a composite root) |
| `<router>/<router>.go` | a router struct delegating to its endpoints, child routers as exported fields |
| `<router>/<endpoint>/<endpoint>.go` | the endpoint's types, `Endpoint`, `New`, the method, its `Raw` twin and the `Paged` walker |
| `types/types.go`, `types/<scope>/types.go` | the shared `schemas.json` types |
| `meta/meta.go` | one struct per `[cores.<name>]` with a `meta` schema (`DefaultMeta`) |
| `replay/replay.go` | every HTTP method with a typed and a raw form, by function path, for `twtest.ReplayHTTP` |

Every method takes `ctx context.Context` first and `opts ...truewire.CallOption` last
(`truewire.WithTimeout`, `truewire.WithSpan`). Names are exported `PascalCase` with Go's
initialisms (`html_url` is `HTMLURL`); a package is the segment's words run together
(`list_commits` is `listcommits`). Output is printed in `gofmt`'s form, so `gofmt -l` is
empty on it.

A record is a struct whose `UnmarshalJSON`/`MarshalJSON` name every key as the wire spells
it and check it: a required key must be present, a non-nullable one must not be null, and an
undocumented key is kept in `Extra`. How a key is held:

| key | Go field |
| --- | --- |
| required | `T` |
| required, nullable | `*T` (or the slice, map or `any` itself) |
| optional | `*T` (or the slice, map or `any` itself), nil when absent |
| optional and nullable | `truewire.Optional[*T]`: `Set` false when absent, `Value` nil when null |

A string `literal` is a named string type with one constant per value
(`list.IssueStateOpen`); a union is a struct with one pointer per variant, the first variant
the value decodes as set; a tuple is a struct of `V0`, `V1`, ...; formats are
`truewire.Decimal`, `truewire.IntegerString`, `truewire.BooleanString`,
`truewire.TimestampMillis` (and the other epochs, embedding `time.Time`),
`truewire.TimestampIso` and `truewire.DateIso`. `truewire.Decimal` and
`truewire.IntegerString` hold exactly the digits the API sent, validated on decode and
encode, so no value loses precision: an `integer-string` has no size limit (a wei amount, an
ERC-1155 token id), like Python's `int`, and reads as a number through `Int64` (false when
out of range), `BigInt` or `Compare`. A failure is a `*truewire.Error` of kind
`validation` whose `Issues` locate every failed check by JSON pointer.

## The core contract

```go
type HttpEndpoint interface {
	Request(ctx context.Context, call truewire.HttpCall) (json.RawMessage, error)
}
type CommandEndpoint interface {
	Command(ctx context.Context, call truewire.CommandCall) (json.RawMessage, error)
}
type StreamEndpoint interface {
	Subscribe(ctx context.Context, call truewire.SubscribeCall) (*truewire.Stream[json.RawMessage], error)
}
```

The call carries the wire method and path (or channel) with placeholders unfilled, the
dumped request (`truewire.Object` and `truewire.FillPath` read it), `Meta` (the generated
`meta.XMeta` value, which the core type-asserts) and the options. The core returns the wire
body with its envelope unwrapped and its errors mapped; it never validates. A stream core
sets the stream's `Reply` to the subscribe acknowledgement as `json.RawMessage`.

## Both transports

An `rpc` endpoint declaring `"transports": ["http", "ws"]` (deribit, hyperliquid) holds a
`truewire.RpcEndpoint`, which embeds `HttpEndpoint` and `CommandEndpoint`. A call goes over
the first declared transport; `truewire.WithTransport` picks the other one:

```go
trades, err := client.Trading.GetUserTradesByInstrument(ctx, request)                                     // HTTP
trades, err = client.Trading.GetUserTradesByInstrument(ctx, request, truewire.WithTransport(truewire.TransportWS)) // WebSocket
```

An endpoint with one transport ignores the option. The replay table replays an endpoint
whose first transport is HTTP.

## Paging

Every walker is a `*truewire.PaginatedResponse[Row, State]`: `page` walks carry the page
number, `offset` walks the row offset (starting at 0 and stepping by the rows each page
held), `token` walks the cursor and `seek` walks a `truewire.SeekState`. A page is short, or
a `seek` page full, measured against the caller's page size clamped to the size schema's
`maximum` (`min(limit, 1000)`), since the API serves no more than that. A `seek` walk also
sends that clamped size, floored at 2 (`min(max(limit, 2), 1000)`): a page must hold one new
row beside the one it re-reads. A required size left at zero is unset: it goes out as 0,
unclamped, and the cap falls back to the size's default.

## Hand-written methods

An endpoint the backend should not generate (a binary download, say) declares a `surface`
in its spec, as for every backend. `handwritten` is skipped by `truewire generate go` and
written beside the generated router, in the router's own package: every router struct keeps
the core it was built from in an unexported field, so the method reaches the same transport.

```go
// src/kraken/spot/account/retrieveexport.go (hand-written)
func (r *Account) RetrieveExport(ctx context.Context, id string) ([]byte, error) {
	downloader, ok := r.core.(interface {
		RetrieveExport(ctx context.Context, id string) ([]byte, error)
	})
	if !ok {
		return nil, truewire.LogicError("the spot core %T cannot download export archives", r.core)
	}
	return downloader.RetrieveExport(ctx, id)
}
```

A router whose endpoints are all hand-written (mexc's protobuf-framed spot streams) is still
rendered: its struct holds the contract a generated endpoint would (`truewire.StreamEndpoint`
for a stream), and its parent hands it that core, so the methods written beside it have a
package and a transport.

`truewire surface --language go` then checks every spec against the Go package: generated
(its `Endpoint` method is on disk), hand-written (a `.go` file in the parent router's
package declares a method named `PascalCase` of the `surface.symbol`'s method part), or
absent. Anything else is a gap and fails the check.

## Errors

```go
if errors.Is(err, truewire.ErrRateLimited) { ... }   // also matches ErrAPI and ErrTruewire
var e *truewire.Error
if errors.As(err, &e) { log.Print(e.Status, e.Body, e.Issues) }
```

## gRPC endpoints

A `kind: grpc` endpoint (ADR 0017) is rendered over protobuf-go stubs built from
`spec/proto/` into `<package>/protos/<proto dir>`:

```sh
truewire protos go        # the stubs (--check compares them)
truewire generate go      # the endpoint packages, which import them
```

Each endpoint package aliases `Request`/`Response` (and a walker's `Row`) to the stub
messages and calls `twgrpc.Endpoint.Invoke`; `twgrpc.Client` is the core over grpc-go.
`grpctest.StartMock` serves the recordings from an in-process server and
`grpctest.Replay(t, root, replay.GrpcTable(client))` replays them.

The gRPC runtime is its own module, `truewire.dev/core/grpc` (`packages/core-go/grpc`, test
helpers in `grpctest`), and so is `truewire.dev/core/protoframes` (ADR 0016):
`truewire.dev/core` itself depends on nothing but `coder/websocket`, so a client without
gRPC or protobuf frames never pulls grpc-go or protobuf into its `go.mod`. A client that has
them requires and replaces the extra module beside the core:

```
require (
	truewire.dev/core v0.0.0
	truewire.dev/core/grpc v0.0.0
)

replace truewire.dev/core => ../../packages/core-go

replace truewire.dev/core/grpc => ../../packages/core-go/grpc
```

Building the stubs needs `buf` and the language's protoc plugin. Nothing else in Truewire
needs them: `truewire plan`, `check` and the generated code's callers do not. Install them
user-locally (the directory must be on `PATH`; `truewire protos` also finds them in a
`node_modules/.bin` above the project, or through `TRUEWIRE_BUF`, `TRUEWIRE_PROTOC_GEN_ES`
and `TRUEWIRE_PROTOC_GEN_GO`):

```sh
npm install -g --prefix ~/.local @bufbuild/buf@1.73.0 @bufbuild/protoc-gen-es@2.15.0
GOBIN=~/.local/bin go install google.golang.org/protobuf/cmd/protoc-gen-go@v1.36.12
```

The toolchain tests that build stubs (`test_grpc_proto.py`, `test_codegen_go_grpc.py`) skip
when a tool is missing, so run them with the tools installed before trusting a green run.

## Testing

`truewire.dev/core/twtest` spawns `truewire mock` for a project (`StartMock`, which finds
`.venv/bin/truewire` in an ancestor directory or reads `$TRUEWIRE_BIN`) and replays every
recorded HTTP example through the generated replay table:

```go
mock := twtest.StartMock(t, "..")
twtest.ReplayHTTP(t, "..", replay.Table(github.New(core.Options{BaseURL: mock.HTTPBaseURL})))
```

Each example's typed result must dump back to exactly the body the wire sent. An endpoint
whose `surface` is hand-written is skipped.

## Local consumption

`packages/core-go` is not published. A project in this repository uses
`replace truewire.dev/core => ../../packages/core-go` in its `go.mod`; a project elsewhere
uses the same `replace` with its own relative path, or a `go.work` listing both modules.
