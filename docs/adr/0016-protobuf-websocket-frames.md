# ADR 0016: Protobuf WebSocket frames are decoded at run time from `spec/proto` sources, as ProtoJSON

- Status: accepted
- Date: 2026-09-15

## Context

mexc's spot WebSocket pushes are Protocol Buffers: every frame is a `PushDataV3ApiWrapper`
whose `body` oneof holds the one message a channel carries. The spec already records the
facts a client needs:

- `<id>.messages.protobuf.json`: the binary frames, base64, which `truewire mock` sends in
  place of `<id>.messages.json` and `truewire check` validates as base64.
- `<id>.messages.json`: a decoded, human-readable sidecar.
- `meta.proto_field` on each stream endpoint, declared by the project's
  `[cores.<name>].meta` schema: which `body` member the channel narrows to.

What the spec did not hold was the schema itself. Python's mexc core carries
`betterproto2`-generated classes in the package and narrows with
`getattr(wrapper, meta['proto_field'])`; the nine leaves stay `surface: handwritten`. The
TypeScript and Go runtimes had no way to decode a binary frame at all (G10), and Rust will
need the same thing next.

The options were:

1. A protoc/buf step per language (`protoc-gen-es`, `protoc-gen-go`, `prost-build`). Typed
   classes, but every generating machine needs protoc and a plugin per language, the
   generated names differ per plugin, and Python's betterproto2 output already differs from
   all three.
2. Compile the `.proto` sources at generation time into a `FileDescriptorSet` and embed the
   bytes. Needs protoc (or grpc_tools) in the generator.
3. Embed the `.proto` sources verbatim in the generated package and compile them at run time
   with a pure-library compiler, reading the result as ProtoJSON.

## Decision

Option 3.

**Spec.** A project keeps its `.proto` files under `spec/proto/` (any depth; imports resolve
by path relative to that directory). They are spec, like `schemas.json`: copied from
upstream, never generated. gRPC (G9) reads the same directory.

**Generator.** Each non-Python backend writes the sources into the package verbatim, keyed by
their path under `spec/proto/`, and nothing else:

| Language | File | Symbol |
|---|---|---|
| TypeScript | `<package>/proto.ts` | `PROTO_SOURCES` |
| Go | `<package>/protos/protos.go` (`protos` is a reserved root directory) | `protos.Sources` |
| Rust | `<package>/protos.rs` | `SOURCES` |

A project with no `spec/proto/` gets no such file. No protoc runs anywhere.

**Runtime.** A core compiles the sources once and decodes each binary frame against its
envelope message:

| Language | Module | Library |
|---|---|---|
| TypeScript | `@truewire/core/protobuf`: `ProtoFrames.compile(sources, message)`, `frames.decode(data)` | `protobufjs` (optional peer dependency) |
| Go | `truewire.dev/core/protoframes` (its own module, so `truewire.dev/core` has no protobuf dependency): `Compile`/`MustCompile`, `(*Frames).Decode` | `github.com/bufbuild/protocompile`, `google.golang.org/protobuf` (`dynamicpb`, `protojson`) |
| Rust | `truewire_core::proto` (feature `proto`): `Protos::from_sources(SOURCES)`, `decode`, `narrow` | `protox` (compile) + `prost-reflect` (decode, serde ProtoJSON) |

A decoded `Frame` has the same small surface in every language: `has(field)`,
`field(field)` (the value as ProtoJSON, absent when unset), `string(field)`,
`oneofCase(oneof)` and `json()` (the whole frame). Fields are named by their `.proto` name,
the name `meta.proto_field` carries.

**Value rendering is canonical ProtoJSON**: lowerCamelCase `json_name` keys, 64-bit
integers as strings, bytes as base64, enums by name, implicit-presence fields at their
default left out. It is the one rendering every protobuf library implements the same way,
so one frame decodes to one JSON value in every language. `packages/testing-ts/test/fixture-protobuf/frames.golden.json`
holds every recorded mexc frame with its decode by betterproto2 (Python); the TypeScript and
Go tests assert byte-for-byte agreement with it, and the Rust tests will read the same file.

**Where narrowing lives.** Decoding the envelope and narrowing it by `meta.proto_field` is
a core's job, as in Python: the core's `parseMsg`/`Parse` decodes binary frames and routes
them by their `channel` field, and the endpoint core filters on `has(proto_field)` and maps
to `field(proto_field)`. The runtime gives the mechanism; the venue dialect (JSON
subscribe/ack beside binary pushes) stays in the hand-written core.

**Testing.** `truewire mock` already replays binary frames. `@truewire/testing`'s
`WsExample.frames` and Go `twtest.Example.Frames` now carry the decoded sidecar bytes, so a
project test can decode every recorded frame through its own core. Both runtimes test a
protobuf-framed streams core against `truewire mock` over the fixture project.

## Consequences

- mexc's spot stream leaves become implementable in TypeScript and Go with no build step:
  a core holds `ProtoFrames.compile(PROTO_SOURCES, 'PushDataV3ApiWrapper')`. The spec
  still needs `spec/proto/*.proto` (upstream `mexcdevelop/websocket-proto`); a scratch copy
  reconstructed from the Python classes decodes all nine recorded frames to their declared
  `proto_field` in both languages.
- The pushed value is untyped ProtoJSON (`unknown` / `json.RawMessage`), not a generated
  class. A generated method's message codec can still validate it, but only when the
  endpoint's `payload` schema mirrors ProtoJSON names. mexc's docs-derived schemas do not
  (`publicdeals`, `dealsList`, `sendtime` beside `sendTime`), which is why its leaves stay
  hand-written. Aligning those schemas to ProtoJSON would let the generated stream methods
  serve them. That is spec work, left open.
- Run-time compilation costs a few milliseconds once per process, and `protobufjs`,
  `protocompile` and `protox` weigh more than generated code would. The weight only reaches
  a project that imports the protobuf module: it is an optional peer dependency in
  TypeScript and a separate package in Go.
- Well-known types (`google/protobuf/*.proto`) resolve in Go (protocompile's standard
  imports) but must be vendored under `spec/proto/` for TypeScript. No mexc message uses them.
- Python is unchanged: its cores keep betterproto2 classes. A Python `truewire_core`
  equivalent can come later if a second protobuf venue appears.
