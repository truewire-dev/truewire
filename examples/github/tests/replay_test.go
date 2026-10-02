// Generic replay coverage: every recorded HTTP example, through the real Go client.
//
// Each `spec/endpoints/**/examples/<id>.request.json` is replayed against `truewire mock`
// through the generated method its function path names (the generated replay table): the
// typed call proves the types accept the recorded response, and the `Raw` twin proves the
// typed value dumps back to exactly the body the wire sent.
package tests

import (
	"testing"

	"truewire.dev/core/twtest"

	"truewire.dev/examples/github/src/github"
	"truewire.dev/examples/github/src/github/core"
	"truewire.dev/examples/github/src/github/replay"
)

const projectRoot = ".."

func client(t *testing.T) *github.GitHub {
	mock := twtest.StartMock(t, projectRoot)
	return github.New(core.Options{BaseURL: mock.HTTPBaseURL})
}

func TestRecordedHTTPExamplesReplayThroughTheGeneratedClient(t *testing.T) {
	twtest.ReplayHTTP(t, projectRoot, replay.Table(client(t)))
}
