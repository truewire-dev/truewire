module truewire.dev/examples/kraken

go 1.23

require truewire.dev/core v0.0.0

require github.com/coder/websocket v1.8.15 // indirect

// The runtime this repository builds; a project outside it requires a released version instead.
replace truewire.dev/core => ../../packages/core-go
