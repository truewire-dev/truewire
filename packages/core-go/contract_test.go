package core

import (
	"testing"
	"time"
)

func TestWithTransportPicksTheWireAndDefaultsToNone(t *testing.T) {
	if got := Options(WithTimeout(time.Second)).Transport; got != "" {
		t.Fatalf("default transport %q", got)
	}
	if got := Options(WithTransport(TransportWS)).Transport; got != TransportWS {
		t.Fatalf("transport %q", got)
	}
	if got := Options(WithTransport(TransportWS), WithTransport(TransportHTTP)).Transport; got != TransportHTTP {
		t.Fatalf("last option wins, got %q", got)
	}
}
