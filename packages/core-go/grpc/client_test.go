package grpc_test

import (
	"context"
	"errors"
	"net"
	"strings"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/wrapperspb"

	truewire "truewire.dev/core"
	twgrpc "truewire.dev/core/grpc"
)

// echoServer answers `/echo.Echo/Say` with a StringValue: the request's text, or the
// status its text names ("invalid", "auth", ...).
func echoServer(t *testing.T) string {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(grpc.UnknownServiceHandler(func(_ any, stream grpc.ServerStream) error {
		method, _ := grpc.MethodFromServerStream(stream)
		if method != "/echo.Echo/Say" {
			return status.Errorf(codes.Unimplemented, "no method %s", method)
		}
		request := new(wrapperspb.StringValue)
		if err := stream.RecvMsg(request); err != nil {
			return err
		}
		md, _ := metadata.FromIncomingContext(stream.Context())
		switch request.Value {
		case "invalid":
			return status.Error(codes.InvalidArgument, "no such text")
		case "auth":
			return status.Error(codes.PermissionDenied, "not yours")
		case "slow":
			return status.Error(codes.ResourceExhausted, "slow down")
		case "down":
			return status.Error(codes.Unavailable, "maintenance")
		case "boom":
			return status.Error(codes.Internal, "internal")
		case "sleep":
			time.Sleep(500 * time.Millisecond)
		}
		return stream.SendMsg(wrapperspb.String(request.Value + "|" + strings.Join(md.Get("x-client"), ",")))
	}))
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	return listener.Addr().String()
}

func say(t *testing.T, client *twgrpc.Client, text string, opts ...truewire.CallOption) (*wrapperspb.StringValue, error) {
	t.Helper()
	response := new(wrapperspb.StringValue)
	err := client.Invoke(context.Background(), twgrpc.Call{
		Method: "/echo.Echo/Say", Request: wrapperspb.String(text), Response: response,
		Meta: map[string]bool{"public": true}, Options: truewire.Options(opts...),
	})
	return response, err
}

func TestInvokeDecodesTheResponse(t *testing.T) {
	client, err := twgrpc.New(twgrpc.Options{Target: echoServer(t), Insecure: true, Metadata: map[string]string{"x-client": "truewire"}})
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	var core twgrpc.Endpoint = client
	_ = core
	response, err := say(t, client, "hello")
	if err != nil {
		t.Fatal(err)
	}
	if response.Value != "hello|truewire" {
		t.Fatalf("got %q", response.Value)
	}
}

func TestStatusesMapIntoTheErrorTaxonomy(t *testing.T) {
	client, err := twgrpc.New(twgrpc.Options{Target: echoServer(t), Insecure: true})
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	cases := []struct {
		text     string
		sentinel error
		name     string
		code     int
	}{
		{"invalid", truewire.ErrBadRequest, "invalid_argument", 3},
		{"auth", truewire.ErrAuth, "permission_denied", 7},
		{"slow", truewire.ErrRateLimited, "resource_exhausted", 8},
		{"boom", truewire.ErrAPI, "internal", 13},
	}
	for _, c := range cases {
		_, err := say(t, client, c.text)
		if !errors.Is(err, c.sentinel) || !errors.Is(err, truewire.ErrAPI) {
			t.Fatalf("%s: %v is not %v", c.text, err, c.sentinel)
		}
		var e *truewire.Error
		if !errors.As(err, &e) {
			t.Fatalf("%s: not a truewire error", c.text)
		}
		body, ok := e.Body.(twgrpc.Status)
		if !ok || body.Code != c.code || body.Name != c.name {
			t.Fatalf("%s: body %#v", c.text, e.Body)
		}
	}
}

func TestTransportFailuresAreNetworkErrors(t *testing.T) {
	client, err := twgrpc.New(twgrpc.Options{Target: echoServer(t), Insecure: true})
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	if _, err := say(t, client, "down"); !errors.Is(err, truewire.ErrNetwork) {
		t.Fatalf("UNAVAILABLE: %v", err)
	}
	if _, err := say(t, client, "sleep", truewire.WithTimeout(50*time.Millisecond)); !errors.Is(err, truewire.ErrNetwork) {
		t.Fatalf("deadline: %v", err)
	}
	listener, _ := net.Listen("tcp", "127.0.0.1:0")
	addr := listener.Addr().String()
	_ = listener.Close()
	unreachable, err := twgrpc.New(twgrpc.Options{Target: addr, Insecure: true, Timeout: 2 * time.Second})
	if err != nil {
		t.Fatal(err)
	}
	defer unreachable.Close()
	if _, err := say(t, unreachable, "x"); !errors.Is(err, truewire.ErrNetwork) {
		t.Fatalf("unreachable: %v", err)
	}
	if twgrpc.MapError(nil) != nil {
		t.Fatal("nil must stay nil")
	}
	if err := twgrpc.MapError(context.Canceled); !errors.Is(err, context.Canceled) {
		t.Fatalf("cancel: %v", err)
	}
}
