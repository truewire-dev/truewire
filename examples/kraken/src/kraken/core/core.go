// Package core is the hand-written core of the kraken client: the three transports the
// generated Kraken is built from.
//
// SpotClient is the REST transport (signing, envelope, errors); MarketClient and
// PrivateClient are the two WebSocket v2 connections, the private one sending the token it
// fetches through the REST transport. `streams.private` and `trading_ws` share the private
// one. Adapt this package to the API; the generated code never changes when you do.
package core

import (
	"context"
	"time"

	truewire "truewire.dev/core"
	"truewire.dev/core/ws"
)

// Options is what New takes.
type Options struct {
	// BaseURL defaults to SpotAPIURL.
	BaseURL string
	// WSURL defaults to SpotWSURL.
	WSURL string
	// WSAuthURL defaults to SpotWSAuthURL.
	WSAuthURL string
	// Credentials is the key pair; without one only public endpoints and channels work.
	Credentials *Credentials
	// HTTP is the client to send through; one is made when nil.
	HTTP *truewire.HttpClient
	// Dial replaces the WebSocket dialer.
	Dial ws.Dialer
}

// Core is the three transports, built together.
type Core struct {
	SpotClient    *Spot
	MarketClient  *Socket
	PrivateClient *Socket
}

// New returns the transports from options.
func New(options Options) *Core {
	spot := NewSpot(SpotOptions{BaseURL: options.BaseURL, Credentials: options.Credentials, HTTP: options.HTTP})
	market := options.WSURL
	if market == "" {
		market = SpotWSURL
	}
	private := options.WSAuthURL
	if private == "" {
		private = SpotWSAuthURL
	}
	var token func(ctx context.Context) (string, error)
	if options.Credentials != nil {
		cache := &TokenCache{Fetch: func(ctx context.Context) (string, time.Duration, error) { return spot.WsToken(ctx) }}
		token = cache.Get
	}
	return &Core{
		SpotClient:    spot,
		MarketClient:  NewSocket(SocketOptions{URL: market, Dial: options.Dial}),
		PrivateClient: NewSocket(SocketOptions{URL: private, Token: token, Dial: options.Dial}),
	}
}

// Close closes both WebSocket connections; the next use opens them again.
func (c *Core) Close() error {
	_ = c.MarketClient.Close()
	return c.PrivateClient.Close()
}
