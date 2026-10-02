package kraken

import "truewire.dev/examples/kraken/src/kraken/core"

// New returns a client over the three transports of the hand-written core.
func New(c *core.Core) *Kraken {
	return FromCores(c.MarketClient, c.PrivateClient, c.SpotClient)
}
