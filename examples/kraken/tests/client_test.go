package tests

import (
	"testing"

	"truewire.dev/core/twtest"

	"truewire.dev/examples/kraken/src/kraken"
	"truewire.dev/examples/kraken/src/kraken/core"
)

const projectRoot = ".."

// fakeCredentials are never real: the private key is base64 of thirty-two zero bytes. The
// mock checks a request's shape, never its `API-Sign`.
var fakeCredentials = &core.Credentials{APIKey: "mock-api-key", PrivateKey: "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="}

// mockClient is a Kraken client whose transports all point at the local mock: REST with the
// fake key pair, both sockets on the mock's one URL with no token source (the recorded
// frames carry none).
func mockClient(t *testing.T) (*kraken.Kraken, *core.Core) {
	mock := twtest.StartMock(t, projectRoot)
	c := &core.Core{
		SpotClient:    core.NewSpot(core.SpotOptions{BaseURL: mock.HTTPBaseURL, Credentials: fakeCredentials}),
		MarketClient:  core.NewSocket(core.SocketOptions{URL: mock.WSURL}),
		PrivateClient: core.NewSocket(core.SocketOptions{URL: mock.WSURL}),
	}
	t.Cleanup(func() { _ = c.Close() })
	return kraken.New(c), c
}
