"""The Go backend's gRPC endpoints (ADR 0017).

Rendered from the `grpc_client` fixture: each endpoint package aliases its protobuf-go
messages, calls `twgrpc.Endpoint.Invoke` (`truewire.dev/core/grpc`) with the method's HTTP/2 path, and walks a
token or page pagination through the stubs' nil-safe getters; the replay table lists every
gRPC method. With a Go toolchain, `buf` and `protoc-gen-go` installed, the stubs are built,
the package is generated and a Go test replays every example and walks both paginations
against `grpctest.StartMock`.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from truewire.cli import app
from truewire.codegen.go import render_package
from truewire.codegen.go.grpc import go_camel_case
from truewire.grpc.stubs import StubError, find_tool
from truewire.plan.build import build_plan
from truewire.project import load_project

FIXTURE = Path(__file__).parent / 'fixtures' / 'grpc_client'
CORE_GO = Path(__file__).parents[3] / 'packages' / 'core-go'

GO_TEST = '''package tests

import (
	"context"
	"errors"
	"testing"

	truewire "truewire.dev/core"
	twgrpc "truewire.dev/core/grpc"
	"truewire.dev/core/grpc/grpctest"
	grpcdemo "truewire.dev/fixtures/grpcdemo/src/grpcdemo"
	"truewire.dev/fixtures/grpcdemo/src/grpcdemo/bank/allbalances"
	"truewire.dev/fixtures/grpcdemo/src/grpcdemo/bank/search"
	demobasev1 "truewire.dev/fixtures/grpcdemo/src/grpcdemo/protos/demo/base/v1"
	"truewire.dev/fixtures/grpcdemo/src/grpcdemo/replay"
)

func client(t *testing.T) (*grpcdemo.GrpcDemo, *grpctest.Mock) {
	mock := grpctest.StartMock(t, "..")
	conn, err := twgrpc.New(twgrpc.Options{Target: mock.Addr, Insecure: true})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	return grpcdemo.FromCore(conn), mock
}

func TestReplay(t *testing.T) {
	c, _ := client(t)
	grpctest.Replay(t, "..", replay.GrpcTable(c))
}

func TestTokenWalk(t *testing.T) {
	c, mock := client(t)
	req := &allbalances.Request{Address: "demo1qqqsyqcyq5rqwzqfpg9scrgwpugpzysn", Pagination: &demobasev1.PageRequest{Limit: 2}}
	rows, err := c.Bank.AllBalancesPaged(req).All(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 3 || rows[2].GetDenom() != "uusdc" || mock.Answered("bank.all_balances") != 2 {
		t.Fatalf("rows %v calls %d", rows, mock.Answered("bank.all_balances"))
	}
	if req.Pagination.Key != nil {
		t.Fatal("the walk mutated the caller's request")
	}
}

func TestPageWalkAndUnmatchedCall(t *testing.T) {
	c, _ := client(t)
	req := &search.Request{Query: "denom='udemo'", OrderBy: 2, Limit: 2}
	rows, err := c.Bank.SearchPaged(req).All(context.Background())
	if err != nil || len(rows) != 1 || rows[0].GetScore() != 9 {
		t.Fatalf("rows %v err %v", rows, err)
	}
	if _, err := c.Bank.Balance(context.Background(), nil); !errors.Is(err, truewire.ErrBadRequest) {
		t.Fatalf("unmatched: %v", err)
	}
}
'''


@pytest.fixture(scope='module')
def rendered():
  project = load_project(FIXTURE)
  return render_package(build_plan(project), project)


def test_go_camel_case_matches_protoc_gen_go():
  assert go_camel_case('next_key') == 'NextKey'
  assert go_camel_case('SearchResponse.Hit') == 'SearchResponse_Hit'
  assert go_camel_case('tx_responses') == 'TxResponses'
  assert go_camel_case('_private') == 'XPrivate'
  assert go_camel_case('height2_value') == 'Height2Value'


def test_endpoint_calls_the_grpc_core(rendered):
  source = rendered.files['bank/balance/balance.go']
  assert 'demobankv1 "truewire.dev/fixtures/grpcdemo/src/grpcdemo/protos/demo/bank/v1"' in source
  assert 'type Request = demobankv1.QueryBalanceRequest' in source
  assert 'twgrpc "truewire.dev/core/grpc"' in source
  assert 'func New(core twgrpc.Endpoint) *Endpoint {' in source
  assert 'func (e *Endpoint) Balance(ctx context.Context, request *Request, opts ...truewire.CallOption) (*Response, error) {' in source
  assert 'twgrpc.Call{Method: "/demo.bank.v1.Query/Balance", Request: request, Response: response, Meta: meta.GrpcMeta{Public: truewire.Ptr(true)}' in source
  assert rendered.skipped == []


def test_walkers_read_through_getters(rendered):
  token = rendered.files['bank/allbalances/allbalances.go']
  assert '*truewire.PaginatedResponse[*Row, []byte]' in token
  assert 'at.Pagination = &demobasev1.PageRequest{}' in token
  assert 'following := response.GetPagination().GetNextKey()' in token
  page = rendered.files['bank/search/search.go']
  assert 'type Row = demobankv1.SearchResponse_Hit' in page
  assert 'truewire.TotalReached(false, int64(state), 1, size, len(rows), total)' in page
  assert 'return truewire.NewPaginatedResponse(uint64(1), next)' in page


def test_router_and_replay_table(rendered):
  router = rendered.files['bank/bank.go']
  assert 'func New(core twgrpc.Endpoint) *Bank {' in router
  assert 'func (r *Bank) AllBalancesPaged(request *allbalances.Request, opts ...truewire.CallOption) *truewire.PaginatedResponse[*allbalances.Row, []byte] {' in router
  replay = rendered.files['replay/replay.go']
  assert 'func GrpcTable(client *grpcdemo.GrpcDemo) map[string]grpctest.Call {' in replay
  assert 'table["bank.search"] = grpctest.ReplayOf(client.Bank.Search)' in replay
  assert 'func Table(' not in replay and 'core/twtest"' not in replay


def _go() -> str | None:
  for candidate in (shutil.which('go'), str(Path.home() / '.local' / 'go' / 'bin' / 'go')):
    if candidate and Path(candidate).is_file():
      return candidate
  return None


def test_generated_client_replays_against_the_grpc_mock(tmp_path: Path):
  go = _go()
  if go is None:
    pytest.skip('no Go toolchain')
  try:
    find_tool(load_project(FIXTURE), 'buf')
    find_tool(load_project(FIXTURE), 'protoc-gen-go')
  except StubError as exc:
    pytest.skip(str(exc))
  root = tmp_path / 'client'
  shutil.copytree(FIXTURE, root)
  runner = CliRunner()
  for command in (['protos', 'go'], ['generate', 'go']):
    result = runner.invoke(app, [*command, '--project', str(root)])
    assert result.exit_code == 0, result.output
  (root / 'go.mod').write_text(
    'module truewire.dev/fixtures/grpcdemo\n\ngo 1.23.0\n\n'
    'require (\n\ttruewire.dev/core v0.0.0\n\ttruewire.dev/core/grpc v0.0.0\n)\n\n'
    f'replace truewire.dev/core => {CORE_GO}\n\nreplace truewire.dev/core/grpc => {CORE_GO / "grpc"}\n'
  )
  (root / 'tests').mkdir()
  (root / 'tests' / 'grpc_test.go').write_text(GO_TEST)
  env = {**os.environ, 'GOFLAGS': '-mod=mod'}
  subprocess.run([go, 'mod', 'tidy'], cwd=root, capture_output=True, text=True, env=env)
  gofmt = Path(go).with_name('gofmt')
  formatted = subprocess.run([str(gofmt), '-l', 'src/grpcdemo/bank', 'src/grpcdemo/replay', 'tests'], cwd=root, capture_output=True, text=True)
  assert formatted.stdout == '', formatted.stdout
  tested = subprocess.run([go, 'test', './...'], cwd=root, capture_output=True, text=True, env=env, timeout=600)
  assert tested.returncode == 0, tested.stdout + tested.stderr
