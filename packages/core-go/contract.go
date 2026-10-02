package core

import (
	"context"
	"encoding/json"
	"time"
)

// The contract between a hand-written core and the code `truewire generate go` emits: the
// Go half of `truewire_core.contract`, `contract.ts` and `contract.rs`.
//
// A generated endpoint holds its core behind one of the interfaces below and calls exactly
// one verb on it; a generated router hands the same core to every child. The generator
// never imports the project's own core package (ADR 0011): the core satisfies the
// interfaces, and the compiler checks it where the client is built.
//
// The verbs have distinct names (Request, Command, Subscribe) so one core value can serve
// every transport a subtree uses. `Meta` is the endpoint's declared `meta`, as the struct
// `<package>/meta` renders for its core (`meta.DefaultMeta{...}`), or nil for a core that
// declares no schema; a core type-asserts it.
//
// What the plan carries, and where it goes:
//
//   - wire.method/wire.path, wire.path (a WebSocket command's method name) or wire.channel:
//     on the call verbatim, placeholders unfilled.
//   - The request: dumped to its wire JSON before the call, so the core sees wire names
//     and wire forms and fills `{name}` placeholders from it (Object, FillPath). nil when
//     the endpoint declares no request.
//   - The reply: the core returns the wire body, envelope unwrapped and errors mapped.
//     Generated code then decodes it into the response type, or for `<Method>Raw` hands it
//     back as it came. A core never validates.

// CallOptions are the per-call options every generated method accepts.
type CallOptions struct {
	// Timeout abandons the call with a network error; the core's default when 0.
	Timeout time.Duration
	// Span overrides a `seek` walk's declared span; the declared default when 0.
	Span int64
	// Transport picks the wire of an `rpc` endpoint that declares both `http` and `ws`:
	// the first declared transport when "". An endpoint with one transport ignores it.
	Transport Transport
}

// Transport names one wire an `rpc` endpoint declaring both transports can be sent over.
type Transport string

const (
	// TransportHTTP sends the call as an HttpCall to the core's Request.
	TransportHTTP Transport = "http"
	// TransportWS sends the call as a CommandCall to the core's Command.
	TransportWS Transport = "ws"
)

// WithTransport sets CallOptions.Transport.
func WithTransport(t Transport) CallOption { return func(o *CallOptions) { o.Transport = t } }

// CallOption sets one field of CallOptions.
type CallOption func(*CallOptions)

// WithTimeout sets CallOptions.Timeout.
func WithTimeout(d time.Duration) CallOption { return func(o *CallOptions) { o.Timeout = d } }

// Options applies opts to a zero CallOptions.
func Options(opts ...CallOption) CallOptions {
	var out CallOptions
	for _, opt := range opts {
		opt(&out)
	}
	return out
}

// HttpCall is one HTTP call. `{name}` placeholders in Path are filled from Request.
type HttpCall struct {
	// Method is the wire HTTP method; "" when the spec leaves it to the core.
	Method  string
	Path    string
	Request json.RawMessage
	Meta    any
	Options CallOptions
}

// CommandCall is one WebSocket command; Path is the wire method name.
type CommandCall struct {
	Path    string
	Request json.RawMessage
	Meta    any
	Options CallOptions
}

// SubscribeCall is one channel subscription; `{name}` placeholders in Channel are filled
// from Parameters.
type SubscribeCall struct {
	Channel    string
	Parameters json.RawMessage
	Meta       any
	Options    CallOptions
}

// HttpEndpoint is the core of a generated `rpc` endpoint reached over HTTP.
type HttpEndpoint interface {
	Request(ctx context.Context, call HttpCall) (json.RawMessage, error)
}

// CommandEndpoint is the core of a generated `rpc` endpoint reached over a WebSocket.
type CommandEndpoint interface {
	Command(ctx context.Context, call CommandCall) (json.RawMessage, error)
}

// RpcEndpoint is the core of a generated `rpc` endpoint declaring both `http` and `ws`: one
// core value serves both halves, and CallOptions.Transport picks the verb per call.
type RpcEndpoint interface {
	HttpEndpoint
	CommandEndpoint
}

// StreamEndpoint is the core of a generated `stream` endpoint.
type StreamEndpoint interface {
	Subscribe(ctx context.Context, call SubscribeCall) (*Stream[json.RawMessage], error)
}
