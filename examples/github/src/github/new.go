package github

import "truewire.dev/examples/github/src/github/core"

// New returns a client over the hand-written core.
//
// `truewire generate go` names the generated constructor FromCore and leaves New for a
// project to define beside it, in the root package: the Go answer to the base class
// `[python.cores.root]` names in the Python backend.
func New(options core.Options) *GitHub {
	return FromCore(core.New(options))
}
