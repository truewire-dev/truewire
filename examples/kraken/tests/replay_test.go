// Generic replay coverage: every recorded HTTP example, through the real Go client, signed
// with the fake key pair (the mock ignores the redacted nonce and matches the rest of the
// form body). An endpoint whose `surface` is hand-written is skipped.
package tests

import (
	"testing"

	"truewire.dev/core/twtest"

	"truewire.dev/examples/kraken/src/kraken/replay"
)

func TestRecordedHTTPExamplesReplayThroughTheGeneratedClient(t *testing.T) {
	client, _ := mockClient(t)
	twtest.ReplayHTTP(t, projectRoot, replay.Table(client))
}
