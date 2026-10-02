// Package grpctest is the gRPC half of `truewire.dev/core/twtest` (ADR 0017): an in-process
// gRPC server answering every recorded gRPC example of a project, and the replay of each
// through a generated client.
//
//	mock := grpctest.StartMock(t, projectRoot)
//	conn, _ := twgrpc.New(twgrpc.Options{Target: mock.Addr, Insecure: true})
//	grpctest.Replay(t, projectRoot, replay.GrpcTable(dydx.FromCores(..., conn, ...)))
package grpctest

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	ggrpc "google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
	"google.golang.org/protobuf/reflect/protoregistry"

	"truewire.dev/core/twtest"
)

// Example is one recorded unary gRPC exchange: `<id>.request.json` and `<id>.response.json`,
// each the message in its proto JSON form.
type Example struct {
	Function string
	ID       string
	Dir      string
	// Method is the call's HTTP/2 path, "/<package>.<Service>/<Rpc>".
	Method string
	// RequestType and ResponseType are the messages' fully-qualified proto names.
	RequestType  string
	ResponseType string
	Request      json.RawMessage
	Response     json.RawMessage
	Handwritten  bool
}

type endpointFile struct {
	Function string `json:"function"`
	Surface  struct {
		Kind string `json:"kind"`
	} `json:"surface"`
	Spec struct {
		Kind    string `json:"kind"`
		Service string `json:"service"`
		Rpc     string `json:"rpc"`
		// Request and Response are message names on a `grpc` endpoint, and JSON Schemas on
		// the `rpc` and `stream` endpoints the same tree holds, so they are read only once
		// the kind is known.
		Request  json.RawMessage `json:"request"`
		Response json.RawMessage `json:"response"`
	} `json:"spec"`
}

// messageName is a gRPC endpoint's `request`/`response` message name.
func messageName(path, key string, raw json.RawMessage) (string, error) {
	var name string
	if err := json.Unmarshal(raw, &name); err != nil {
		return "", fmt.Errorf("%s: spec.%s of a grpc endpoint is not a message name: %w", path, key, err)
	}
	return name, nil
}

// Examples is every recorded gRPC example of the project, sorted by function path then id.
func Examples(root string) ([]Example, error) {
	base := filepath.Join(root, "spec", "endpoints")
	var out []Example
	err := filepath.WalkDir(base, func(path string, d os.DirEntry, err error) error {
		if err != nil || d.IsDir() || d.Name() != "endpoint.json" {
			return err
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		var endpoint endpointFile
		if err := json.Unmarshal(data, &endpoint); err != nil {
			return fmt.Errorf("%s: %w", path, err)
		}
		if endpoint.Spec.Kind != "grpc" || endpoint.Surface.Kind == "absent" {
			return nil
		}
		requestType, err := messageName(path, "request", endpoint.Spec.Request)
		if err != nil {
			return err
		}
		responseType, err := messageName(path, "response", endpoint.Spec.Response)
		if err != nil {
			return err
		}
		dir := filepath.Dir(path)
		function := endpoint.Function
		if function == "" {
			rel, _ := filepath.Rel(base, dir)
			function = strings.ReplaceAll(filepath.ToSlash(rel), "/", ".")
		}
		matches, _ := filepath.Glob(filepath.Join(dir, "examples", "*.request.json"))
		sort.Strings(matches)
		for _, requestPath := range matches {
			id := strings.TrimSuffix(filepath.Base(requestPath), ".request.json")
			request, err := os.ReadFile(requestPath)
			if err != nil {
				continue
			}
			response, err := os.ReadFile(filepath.Join(dir, "examples", id+".response.json"))
			if err != nil {
				continue
			}
			out = append(out, Example{
				Function: function, ID: id, Dir: dir,
				Method:      "/" + endpoint.Spec.Service + "/" + endpoint.Spec.Rpc,
				RequestType: requestType, ResponseType: responseType,
				Request: request, Response: response, Handwritten: endpoint.Surface.Kind == "handwritten",
			})
		}
		return nil
	})
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].Function != out[j].Function {
			return out[i].Function < out[j].Function
		}
		return out[i].ID < out[j].ID
	})
	return out, err
}

var decode = protojson.UnmarshalOptions{DiscardUnknown: true}

func messageType(name string) (protoreflect.MessageType, error) {
	mt, err := protoregistry.GlobalTypes.FindMessageByName(protoreflect.FullName(name))
	if err != nil {
		return nil, fmt.Errorf("message %s is not registered: import the generated protos (the client package does)", name)
	}
	return mt, nil
}

// Mock is an in-process gRPC server answering every recorded gRPC example of a project: a
// call whose request equals a recorded one (compared as protobuf messages) gets that
// example's response, any other call NOT_FOUND. Message types come from the protobuf-go
// registry, so the test must import the generated client.
type Mock struct {
	// Addr is the server's "host:port".
	Addr string

	mu     sync.Mutex
	calls  map[string]int
	server *ggrpc.Server
}

// StartMock starts a Mock for the project at root on a free port and stops it when the
// test ends.
func StartMock(t testing.TB, root string) *Mock {
	t.Helper()
	examples, err := Examples(root)
	if err != nil {
		t.Fatal(err)
	}
	byMethod := map[string][]Example{}
	for _, ex := range examples {
		byMethod[ex.Method] = append(byMethod[ex.Method], ex)
	}
	mock := &Mock{calls: map[string]int{}}
	mock.server = ggrpc.NewServer(ggrpc.UnknownServiceHandler(func(_ any, stream ggrpc.ServerStream) error {
		method, _ := ggrpc.MethodFromServerStream(stream)
		recorded := byMethod[method]
		if len(recorded) == 0 {
			return status.Errorf(codes.Unimplemented, "grpctest: no recorded example for %s", method)
		}
		requestType, err := messageType(recorded[0].RequestType)
		if err != nil {
			return status.Error(codes.Internal, err.Error())
		}
		responseType, err := messageType(recorded[0].ResponseType)
		if err != nil {
			return status.Error(codes.Internal, err.Error())
		}
		request := requestType.New().Interface()
		if err := stream.RecvMsg(request); err != nil {
			return err
		}
		for _, ex := range recorded {
			want := requestType.New().Interface()
			if err := decode.Unmarshal(ex.Request, want); err != nil {
				return status.Errorf(codes.Internal, "grpctest: %s (%s): the recorded request does not decode: %v", ex.Function, ex.ID, err)
			}
			if !proto.Equal(want, request) {
				continue
			}
			response := responseType.New().Interface()
			if err := decode.Unmarshal(ex.Response, response); err != nil {
				return status.Errorf(codes.Internal, "grpctest: %s (%s): the recorded response does not decode: %v", ex.Function, ex.ID, err)
			}
			mock.mu.Lock()
			mock.calls[ex.Function]++
			mock.mu.Unlock()
			return stream.SendMsg(response)
		}
		return status.Errorf(codes.NotFound, "grpctest: %s: no recorded example matches the request %s", recorded[0].Function, protojson.Format(request))
	}))
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	mock.Addr = listener.Addr().String()
	go func() { _ = mock.server.Serve(listener) }()
	t.Cleanup(mock.server.Stop)
	return mock
}

// Answered is how many calls of function the mock has answered.
func (m *Mock) Answered(function string) int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.calls[function]
}

// Call replays one recorded request through a generated gRPC method: the response it
// returned and the recorded one, both in proto JSON (proto field names).
type Call func(ctx context.Context, request, response json.RawMessage) (got, want json.RawMessage, err error)

var encode = protojson.MarshalOptions{UseProtoNames: true}

// ReplayOf is the Call of a generated gRPC method (`client.Bank.AllBalances`).
func ReplayOf[Req, Res proto.Message, O any](method func(context.Context, Req, ...O) (Res, error)) Call {
	return func(ctx context.Context, request, response json.RawMessage) (json.RawMessage, json.RawMessage, error) {
		var zeroRequest Req
		typed := zeroRequest.ProtoReflect().Type().New().Interface().(Req)
		if err := decode.Unmarshal(request, typed); err != nil {
			return nil, nil, fmt.Errorf("the recorded request does not decode: %w", err)
		}
		var zeroResponse Res
		want := zeroResponse.ProtoReflect().Type().New().Interface()
		if err := decode.Unmarshal(response, want); err != nil {
			return nil, nil, fmt.Errorf("the recorded response does not decode: %w", err)
		}
		got, err := method(ctx, typed)
		if err != nil {
			return nil, nil, err
		}
		gotJSON, err := encode.Marshal(got)
		if err != nil {
			return nil, nil, err
		}
		wantJSON, err := encode.Marshal(want)
		return gotJSON, wantJSON, err
	}
}

// Replay replays every recorded gRPC example of the project through table (function path
// -> Call), each as a subtest against a client pointed at a StartMock, and fails when a
// call errors or its response differs from the recorded one. An example whose function has
// no entry fails; a hand-written surface is skipped.
func Replay(t *testing.T, root string, table map[string]Call) {
	t.Helper()
	examples, err := Examples(root)
	if err != nil {
		t.Fatal(err)
	}
	if len(examples) == 0 {
		t.Fatal("no recorded gRPC examples found")
	}
	for _, ex := range examples {
		t.Run(ex.Function+"/"+ex.ID, func(t *testing.T) {
			if ex.Handwritten {
				t.Skipf("%s: a hand-written surface, replayed by its own tests", ex.Function)
			}
			call, ok := table[ex.Function]
			if !ok {
				t.Fatalf("%s: no generated method to replay it through", ex.Function)
			}
			ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
			defer cancel()
			got, want, err := call(ctx, ex.Request, ex.Response)
			if err != nil {
				t.Fatalf("%s (%s): %v", ex.Function, ex.ID, err)
			}
			if diff := twtest.FirstDifference(got, want); diff != "" {
				t.Fatalf("%s (%s): the response differs from the recording at %s", ex.Function, ex.ID, diff)
			}
		})
	}
}
