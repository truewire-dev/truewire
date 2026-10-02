package grpctest

import (
	"os"
	"path/filepath"
	"testing"
)

func writeFile(t *testing.T, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

// A project mixing gRPC with `rpc` endpoints, whose spec `request`/`response` are JSON
// Schemas rather than message names, still lists its gRPC examples (dYdX's Comet and
// Indexer endpoints beside its chain queries).
func TestExamplesSkipsRpcEndpointsWithSchemaRequests(t *testing.T) {
	root := t.TempDir()
	writeFile(t, filepath.Join(root, "spec/endpoints/comet/block/endpoint.json"),
		`{"spec": {"kind": "rpc", "path": "/block", "request": {"type": "object"}, "response": {"type": "object"}}}`)
	writeFile(t, filepath.Join(root, "spec/endpoints/comet/block/examples/one.request.json"), `{"request": {}}`)
	writeFile(t, filepath.Join(root, "spec/endpoints/comet/block/examples/one.response.json"), `{"status": 200, "payload": {}}`)
	writeFile(t, filepath.Join(root, "spec/endpoints/bank/balance/endpoint.json"),
		`{"spec": {"kind": "grpc", "service": "cosmos.bank.v1beta1.Query", "rpc": "Balance", "request": "cosmos.bank.v1beta1.QueryBalanceRequest", "response": "cosmos.bank.v1beta1.QueryBalanceResponse"}}`)
	writeFile(t, filepath.Join(root, "spec/endpoints/bank/balance/examples/one.request.json"), `{"address": "a"}`)
	writeFile(t, filepath.Join(root, "spec/endpoints/bank/balance/examples/one.response.json"), `{}`)

	examples, err := Examples(root)
	if err != nil {
		t.Fatal(err)
	}
	if len(examples) != 1 {
		t.Fatalf("%d examples, want the one gRPC example", len(examples))
	}
	ex := examples[0]
	if ex.Function != "bank.balance" || ex.Method != "/cosmos.bank.v1beta1.Query/Balance" ||
		ex.RequestType != "cosmos.bank.v1beta1.QueryBalanceRequest" || ex.ResponseType != "cosmos.bank.v1beta1.QueryBalanceResponse" {
		t.Fatalf("unexpected example %+v", ex)
	}
}

func TestExamplesRejectsAGrpcEndpointWithoutMessageNames(t *testing.T) {
	root := t.TempDir()
	writeFile(t, filepath.Join(root, "spec/endpoints/bank/balance/endpoint.json"),
		`{"spec": {"kind": "grpc", "service": "s", "rpc": "r", "request": {"type": "object"}, "response": "x"}}`)
	if _, err := Examples(root); err == nil {
		t.Fatal("want an error for a grpc endpoint whose request is not a message name")
	}
}
