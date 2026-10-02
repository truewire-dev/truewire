module truewire.dev/examples/github

go 1.23

require truewire.dev/core v0.0.0

// The runtime this repository builds; a project outside it requires a released version instead.
replace truewire.dev/core => ../../packages/core-go
