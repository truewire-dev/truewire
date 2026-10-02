// Package grpc is the gRPC runtime of a truewire-generated Go client (ADR 0017): the
// contract a generated gRPC endpoint calls, and Client, the core that satisfies it over
// grpc-go.
//
// It is its own module (`truewire.dev/core/grpc`) so that `truewire.dev/core` carries no
// gRPC or protobuf dependency: only a client with gRPC endpoints requires and replaces it.
// Generated code imports it as `twgrpc "truewire.dev/core/grpc"`.
//
//	conn, err := twgrpc.New(twgrpc.Options{Target: "grpc.example.com:443"})
//	client := dydx.FromCores(..., conn, ...)
//	defer conn.Close()
package grpc

import (
	"context"
	"crypto/tls"
	"errors"
	"strings"
	"time"

	ggrpc "google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"

	truewire "truewire.dev/core"
)

// Call is one unary gRPC call. The generated endpoint allocates Response and the core
// decodes the reply into it; Request and Response are the protobuf-go messages `truewire
// protos go` builds from the project's `spec/proto/` tree.
type Call struct {
	// Method is the call's HTTP/2 path, "/<package>.<Service>/<Rpc>".
	Method   string
	Request  proto.Message
	Response proto.Message
	// Meta is the endpoint's declared `meta`, as for the other verbs.
	Meta    any
	Options truewire.CallOptions
}

// Endpoint is the core of a generated gRPC endpoint. Invoke sends one unary call, decodes
// the reply into call.Response, and returns a *truewire.Error: KindNetwork for a failure
// reaching the server (UNAVAILABLE, DEADLINE_EXCEEDED), an API kind for a status the server
// returned. Client is the implementation over grpc-go.
type Endpoint interface {
	Invoke(ctx context.Context, call Call) error
}

// Status is the Body of an API error a gRPC call returned.
type Status struct {
	// Code is the numeric gRPC status code (3 for INVALID_ARGUMENT).
	Code int `json:"code"`
	// Name is the code's name, "invalid_argument".
	Name    string `json:"name"`
	Message string `json:"message"`
}

// Options configures a Client.
type Options struct {
	// Target is the server, "host:port" (any grpc-go target).
	Target string
	// Insecure dials without TLS, for a plaintext server such as a local mock.
	Insecure bool
	// Timeout is the default per-call timeout; none when 0.
	Timeout time.Duration
	// Metadata is sent with every call.
	Metadata map[string]string
	// DialOptions are passed to grpc.NewClient after the credentials.
	DialOptions []ggrpc.DialOption
}

// Client is a gRPC core over one connection. It serves every gRPC endpoint of a generated
// tree, whatever their `meta`.
type Client struct {
	conn    *ggrpc.ClientConn
	options Options
}

var _ Endpoint = (*Client)(nil)

// New opens a Client; grpc-go connects lazily, on the first call.
func New(options Options) (*Client, error) {
	creds := credentials.NewTLS(&tls.Config{MinVersion: tls.VersionTLS12})
	if options.Insecure {
		creds = insecure.NewCredentials()
	}
	dial := append([]ggrpc.DialOption{ggrpc.WithTransportCredentials(creds)}, options.DialOptions...)
	conn, err := ggrpc.NewClient(options.Target, dial...)
	if err != nil {
		return nil, truewire.NetworkError("grpc: "+err.Error(), err)
	}
	return &Client{conn: conn, options: options}, nil
}

// Conn is the underlying connection.
func (c *Client) Conn() *ggrpc.ClientConn { return c.conn }

// Invoke sends one unary call and decodes the reply into call.Response.
func (c *Client) Invoke(ctx context.Context, call Call) error {
	timeout := call.Options.Timeout
	if timeout == 0 {
		timeout = c.options.Timeout
	}
	if timeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, timeout)
		defer cancel()
	}
	if len(c.options.Metadata) > 0 {
		pairs := make([]string, 0, 2*len(c.options.Metadata))
		for k, v := range c.options.Metadata {
			pairs = append(pairs, k, v)
		}
		ctx = metadata.AppendToOutgoingContext(ctx, pairs...)
	}
	return MapError(c.conn.Invoke(ctx, call.Method, call.Request, call.Response))
}

// Close closes the connection.
func (c *Client) Close() error { return c.conn.Close() }

// MapError is a grpc-go error as a *truewire.Error: UNAVAILABLE and DEADLINE_EXCEEDED are
// network errors, a status the server returned an API error (bad-request, auth or
// rate-limited where one fits) whose Body is a Status. nil stays nil, and a context
// cancellation is returned unchanged.
func MapError(err error) error {
	if err == nil {
		return nil
	}
	if errors.Is(err, context.Canceled) {
		return err
	}
	st, ok := status.FromError(err)
	if !ok {
		return truewire.NetworkError("grpc: "+err.Error(), err)
	}
	body := Status{Code: int(st.Code()), Name: codeName(st.Code()), Message: st.Message()}
	var kind truewire.Kind
	switch st.Code() {
	case codes.Unavailable, codes.DeadlineExceeded:
		return truewire.NetworkError("grpc "+body.Name+": "+st.Message(), err)
	case codes.Canceled:
		return err
	case codes.Unauthenticated, codes.PermissionDenied:
		kind = truewire.KindAuth
	case codes.ResourceExhausted:
		kind = truewire.KindRateLimited
	case codes.InvalidArgument, codes.FailedPrecondition, codes.OutOfRange, codes.NotFound:
		kind = truewire.KindBadRequest
	default:
		kind = truewire.KindAPI
	}
	return truewire.APIError(kind, "grpc "+body.Name+": "+st.Message()).WithBody(body).WithCause(err)
}

func codeName(code codes.Code) string {
	name := code.String()
	var b strings.Builder
	for i, r := range name {
		if i > 0 && r >= 'A' && r <= 'Z' {
			b.WriteByte('_')
		}
		b.WriteRune(r)
	}
	return strings.ToLower(b.String())
}
