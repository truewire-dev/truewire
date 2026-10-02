# ADR 0017: gRPC endpoints are planned from `spec/proto`, rendered over each language's standard protobuf stubs, and tested against an in-process fake server

- Status: accepted
- Date: 2026-09-15

## Context

dYdX's chain surface is 50 unary gRPC calls (`kind: grpc`, 17 of them paginated), and the
decision on dYdX (full parity in every language) needs them in TypeScript, Go and later
Rust. Only Python rendered them: its generator imported betterproto2 classes and read
their dataclass fields. `truewire plan` left gRPC endpoints out altogether (G9), so no
other backend had anything to render from, and `truewire mock` serves no gRPC.

The forces:

- **The spec already declares the wire.** `GrpcEndpointSpec` names the service, rpc and
  fully-qualified messages, and `spec/proto/` holds the `.proto` sources. A pagination
  names dotted paths into those messages (`pagination.key`, `pagination.next_key`).
- **A gRPC method is a typed API.** A caller passes a request message and reads a response
  message, including nested ones (`Coin`, `PageRequest`). ADR 0016 decodes WebSocket frames
  into untyped ProtoJSON at run time, which fits a push a hand-written core narrows. It
  does not fit a generated method, whose signature is the message types.
- **Real trees do not compile as shipped.** Cosmos and dYdX protos import option-only
  files the spec does not vendor (`gogoproto`, `cosmos_proto`, `amino`, `google/api`) and,
  in dYdX's case, messages from files that were never copied. Every compiler rejects that.
- **Mock tests must not depend on a network or a running chain.**

## Decision

**Plan.** `truewire plan` lists every gRPC endpoint as `kind: grpc`: `wire.path` is the
HTTP/2 path (`/<service>/<rpc>`), `request.shape` is `message`, and a `grpc` block carries
the service and its file, the rpc, the request and response types (`{kind, name, file,
package}`), the request fields, and `paging`: each declared pagination path resolved hop
by hop through the messages, so a backend knows the cursor is `bytes` and the page index
`uint64` without reading a `.proto`. `pagination.walker` follows the same rules as for
`rpc` endpoints. A dependency-free reader (`truewire.grpc.proto`) resolves the names; no
compiler runs to plan.

**Stubs.** `truewire protos <language>` builds the language's standard stubs from
`spec/proto/`: protobuf-es (`protoc-gen-es`) for TypeScript, protobuf-go (`protoc-gen-go`,
buf managed mode with `go_package_prefix`) for Go, and prost (`protoc-gen-prost`, flat
output, plus the tree's `FileDescriptorSet` from `buf build` and a generated `mod.rs`
nesting a module per package segment) for Rust. The Rust stubs are checked in beside the
generated modules rather than built by a `build.rs`: a `build.rs` would need the stripped
tree, which only `truewire` produces, on every consumer's machine. It
strips what the tree cannot resolve first: imports of absent files, custom options (which
never change the encoding), and declarations naming absent types, dropping a `oneof` that
would be left empty. It refuses when a stripped declaration is reachable from an
endpoint's messages, naming it. Stubs land in `<package>/protos/`; the command owns the
stub files there (`--check` compares them).

**Rendering.** An endpoint takes one gRPC core by contract (ADR 0011) and calls one verb
with the method, the request message, the response to fill (Go) and `meta`:
`GrpcEndpoint<Meta>.unary({method, request, meta})` in TypeScript,
`twgrpc.Endpoint.Invoke(ctx, twgrpc.Call{Method, Request, Response, Meta})` in Go,
`GrpcEndpoint<Meta>::invoke(GrpcCall { method, request, meta, options })` in Rust. Rust's
call carries the request encoded and returns the reply encoded (`Vec<u8>`), since a trait
object cannot be generic in the message types; the generated method encodes its `prost`
message and decodes the reply. The request is the stub's message (TypeScript accepts its `MessageInitShape`), not flattened
parameters. There is no `validate: false`/`Raw` form, since a protobuf reply is decoded by
its schema. Token and page walks clone the request, set the driver path (creating nested
messages) and read rows, cursor and total through the stub fields.

**Runtime.** `@truewire/core/grpc` (`GrpcClient` over `@connectrpc/connect-node`, optional
peer dependencies) and `truewire.dev/core/grpc` (grpc-go; a module of its own, so
`truewire.dev/core` carries no gRPC or protobuf dependency and only a client with gRPC
endpoints requires it), and `truewire_core::grpc::GrpcClient` (`tonic`, feature `grpc`,
implementing `GrpcEndpoint` for every `Meta`). Statuses map into the
shared taxonomy: `UNAVAILABLE` and `DEADLINE_EXCEEDED` are network errors;
`INVALID_ARGUMENT`, `FAILED_PRECONDITION`, `OUT_OF_RANGE` and `NOT_FOUND` are bad requests;
`UNAUTHENTICATED` and `PERMISSION_DENIED` are auth errors; `RESOURCE_EXHAUSTED` is rate
limited; anything else is an API error carrying `{code, name, message}`.

**Testing.** Recordings stay `<id>.request.json` / `<id>.response.json` in proto JSON.
Each language serves them from an in-process fake gRPC server that matches a request by
message equality and answers `NOT_FOUND` otherwise: `@truewire/testing/grpc`
(`startGrpcMock`, `describeGrpcReplay`) and `truewire.dev/core/grpc/grpctest` (`StartMock`, `Replay`, fed by
the generated `replay.GrpcTable`), and `truewire-testing` (feature `grpc`: `GrpcMock`,
`grpc_examples`, fed by the generated `call_grpc` and reading recordings with the stubs'
`FILE_DESCRIPTOR_SET`). `truewire mock` stays HTTP/WebSocket.

## Consequences

- Validated against a copy of `clients/dydx/spec`. The plan lists all 50 gRPC endpoints.
  After adding one missing file, the stubs build (49 files per language) and the generated
  TypeScript and Go packages type-check and vet with every gRPC method rendered. The
  missing file is `cosmos/bank/v1beta1/bank.proto`, which the dYdX spec lacks: `Params`
  and `Metadata` break four bank endpoints, and `truewire protos` names them. Vendoring
  that file is spec work.
- Rust, validated against a copy of the same spec with `cosmos/bank/v1beta1/bank.proto`
  added from Cosmos SDK v0.54.3 (the version typed-dev's dYdX stubs were built from): the
  stubs build (26 package files, the descriptor set and `mod.rs`), all 50 gRPC methods and
  all 18 walkers render, the crate passes `cargo fmt --check` and `cargo clippy -D
  warnings`, and replaying the recordings through `call_grpc` against `GrpcMock` passes 49
  and skips one: `chain.staking.validators (01)` records an `Any` whose `@type`
  (`/cosmos.crypto.ed25519.PubKey`) the tree does not declare, so it cannot be encoded.
  Recorded `Any`s come in two spellings, proto JSON's `@type` and betterproto's
  `type_url`/`value` (78 of them in dYdX); the Rust harness reads both.
- Generating a client with gRPC endpoints needs `buf` and a plugin (npm for TypeScript,
  `go install` for Go, `cargo install protoc-gen-prost` for Rust). Planning, `check` and the generated code's consumers do not. Tests
  that need the tools skip without them.
- One `.proto` tree serves both ADRs: ADR 0016 compiles it at run time for frames, this
  one compiles it at build time for typed methods. `protos/protos.go` (ADR 0016) and the Go
  stub packages share `protos/` without either tool deleting the other's files.
- Stub naming is the plugins', mirrored by the backends: nested messages joined by `_`,
  protoc-gen-go's `GoCamelCase`, protobuf-es's `protoCamelCase`, prost-build's `heck`
  casing and parent-named modules. A message whose stub name
  the plugin had to disambiguate (a message named like its file's service) is not handled
  and would fail to compile, not silently misbind.
- Left open: streaming rpcs (the plan carries `streaming`; no backend renders non-unary),
  a walk driven through a `oneof` member, and Python moving onto the plan's `grpc` block
  (it keeps introspecting betterproto2).
