module truewire.dev/core/protoframes

go 1.23.0

require (
	github.com/bufbuild/protocompile v0.14.1
	google.golang.org/protobuf v1.36.12
	truewire.dev/core v0.0.0
)

require (
	github.com/coder/websocket v1.8.15 // indirect
	golang.org/x/sync v0.8.0 // indirect
)

// The runtime beside it; a project outside this repository requires released versions instead.
replace truewire.dev/core => ../
