"""The Rust backend's gRPC endpoints (ADR 0017).

Rendered from the `grpc_client` fixture: each endpoint module aliases the `prost` messages
`truewire protos rust` builds, calls `GrpcEndpoint::invoke` with the method's HTTP/2 path
and the encoded request, and walks a token or page pagination through the `prost` fields;
`dispatch.rs` reaches every gRPC method by function path with encoded messages. With cargo,
`buf` and `protoc-gen-prost` installed, the stubs are built, the crate is generated, and it
must pass `cargo fmt --check`, `cargo clippy -D warnings` and a test that replays every
example and walks both paginations against `truewire-testing`'s fake gRPC server.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from truewire.cli import app
from truewire.codegen.rust import render_package
from truewire.codegen.rust.prost_names import heck_words, to_snake, to_upper_camel
from truewire.grpc.stubs import StubError, find_tool, rust_mod
from truewire.plan.build import build_plan
from truewire.project import load_project

FIXTURE = Path(__file__).parent / 'fixtures' / 'grpc_client'
CRATES = Path(__file__).parents[3] / 'crates'

CARGO_TOML = '''[package]
name = "grpc_demo"
version = "0.1.0"
edition = "2021"
publish = false

[lib]
path = "src/grpc_demo/lib.rs"

[dependencies]
prost = "0.14"
prost-types = "0.14"
truewire-core = {{ path = "{crates}/truewire-core" }}

[dev-dependencies]
tokio = {{ version = "1", features = ["macros", "rt-multi-thread"] }}
truewire-core = {{ path = "{crates}/truewire-core", features = ["grpc"] }}
truewire-testing = {{ path = "{crates}/truewire-testing", features = ["grpc"] }}
'''

RUST_TEST = '''use std::sync::Arc;

use grpc_demo::bank::{all_balances, search};
use grpc_demo::protos::demo::base::v1::PageRequest;
use grpc_demo::{CallOptions, GrpcDemo};
use truewire_core::grpc::GrpcClient;
use truewire_core::proto::Protos;
use truewire_core::ApiKind;
use truewire_testing::{grpc_examples, replay_grpc, GrpcMock};

const ROOT: &str = env!("CARGO_MANIFEST_DIR");

async fn client() -> (GrpcDemo, GrpcMock, Arc<Protos>) {
    let protos = Arc::new(Protos::from_descriptor_set(grpc_demo::protos::FILE_DESCRIPTOR_SET).unwrap());
    let mock = GrpcMock::start(ROOT, protos.clone()).await.unwrap();
    let client = GrpcDemo::from_core(GrpcClient::new(mock.url()).unwrap());
    (client, mock, protos)
}

#[tokio::test]
async fn every_example_replays_through_the_generated_client() {
    let (client, _mock, protos) = client().await;
    let examples = grpc_examples(ROOT);
    assert_eq!(examples.len(), 4);
    let client = &client;
    let report = replay_grpc(&examples, &protos, |function, request| async move {
        client.call_grpc(&function, &request, CallOptions::default()).await
    })
    .await;
    report.assert_passed();
    assert_eq!(report.passed.len(), 4);
}

#[tokio::test]
async fn a_token_walk_follows_next_key_without_touching_the_request() {
    let (client, mock, _) = client().await;
    let request = all_balances::Request {
        address: "demo1qqqsyqcyq5rqwzqfpg9scrgwpugpzysn".into(),
        pagination: Some(PageRequest { limit: 2, ..Default::default() }),
        ..Default::default()
    };
    let rows = client.bank.all_balances_paged(request.clone(), CallOptions::default()).await.unwrap();
    let denoms: Vec<&str> = rows.iter().map(|coin| coin.denom.as_str()).collect();
    assert_eq!(denoms, ["uatom", "udemo", "uusdc"]);
    assert_eq!(mock.answered("bank.all_balances"), 2);
    assert!(request.pagination.unwrap().key.is_empty());
}

#[tokio::test]
async fn a_page_walk_stops_at_the_total_and_an_unrecorded_call_is_a_bad_request() {
    let (client, _mock, _) = client().await;
    let request = search::Request { query: "denom='udemo'".into(), order_by: 2, limit: 2, ..Default::default() };
    let rows = client.bank.search_paged(request, CallOptions::default()).await.unwrap();
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].score, 9);
    let error = client.bank.balance(Default::default(), CallOptions::default()).await.unwrap_err();
    assert!(error.is_api(ApiKind::BadRequest), "{error}");
}
'''


@pytest.fixture(scope='module')
def rendered():
  project = load_project(FIXTURE)
  return render_package(build_plan(project), project)


def test_prost_names_follow_heck():
  assert heck_words('HTTPServer') == ['HTTP', 'Server']
  assert heck_words('v1beta1') == ['v1beta1']
  assert heck_words('QueryBTCPriceRequest') == ['Query', 'BTC', 'Price', 'Request']
  assert to_upper_camel('QueryBTCPriceRequest') == 'QueryBtcPriceRequest'
  assert to_upper_camel('MsgPlaceOrder') == 'MsgPlaceOrder'
  assert to_snake('SearchResponse') == 'search_response'
  assert to_snake('type') == 'r#type'
  assert to_snake('self') == 'self_'
  assert to_snake('dydxprotocol') == 'dydxprotocol'


def test_protos_mod_nests_a_module_per_package_segment():
  source = rust_mod(['demo.bank.v1', 'demo.base.v1'])
  assert '#![allow(clippy::all, clippy::pedantic, missing_docs, rustdoc::all)]' in source
  assert 'pub mod demo {\n    pub mod bank {\n        pub mod v1 {\n            include!("demo.bank.v1.rs");' in source
  assert 'pub const FILE_DESCRIPTOR_SET: &[u8] = include_bytes!("descriptors.binpb");' in source


def test_endpoint_calls_the_grpc_core(rendered):
  source = rendered.files['bank/balance.rs']
  assert 'pub const METHOD: &str = "/demo.bank.v1.Query/Balance";' in source
  assert 'pub type Request = crate::protos::demo::bank::v1::QueryBalanceRequest;' in source
  assert 'core: Arc<dyn GrpcEndpoint<GrpcMeta>>,' in source
  assert 'pub async fn balance(&self, request: Request, options: CallOptions) -> Result<Response> {' in source
  assert 'let reply = self.core.invoke(call).await?;' in source
  assert 'request: request.encode_to_vec(),' in source
  assert '        decode_message(&reply)\n' in source
  assert 'use crate::grpc_codec::decode_message;' in source
  assert rendered.skipped == []


def test_walkers_read_the_prost_fields(rendered):
  token = rendered.files['bank/all_balances.rs']
  assert 'pub type Row = crate::protos::demo::base::v1::Coin;' in token
  assert 'PaginatedResponse<Row, Vec<u8>>' in token
  assert 'let parent = request.pagination.get_or_insert_with(Default::default);\n                parent.key = state;' in token
  assert '.map(|value| value.next_key.clone())' in token
  page = rendered.files['bank/search.rs']
  assert 'pub type Row = crate::protos::demo::bank::v1::search_response::Hit;' in page
  assert 'PaginatedResponse<Row, u64>' in page
  assert 'let reached = total_reached(false, page, 1, size, rows.len(), total);' in page
  assert 'PaginatedResponse::new(1, next)' in page


def test_router_dispatch_and_lib(rendered):
  router = rendered.files['bank/mod.rs']
  assert 'pub fn new(core: Arc<dyn GrpcEndpoint<GrpcMeta>>) -> Self {' in router
  root = rendered.files['client.rs']
  assert 'pub fn from_core(core: impl GrpcEndpoint<GrpcMeta> + \'static) -> Self {' in root
  dispatch = rendered.files['dispatch.rs']
  assert '/// Call the gRPC endpoint `function` names (`bank.' in dispatch
  assert '#[doc(hidden)]\n    pub async fn call_grpc(' in dispatch
  assert 'mod dispatch;' in rendered.files['lib.rs'] and 'pub mod dispatch;' not in rendered.files['lib.rs']
  assert '"bank.search" => {' in dispatch
  assert '.all_balances(decode_message(request)?, options)' in dispatch
  assert 'serde_json' not in dispatch
  assert 'pub mod protos;' in rendered.files['lib.rs'] and 'pub mod grpc_codec;' in rendered.files['lib.rs']
  assert 'pub fn decode_message<M: Message + Default>(bytes: &[u8]) -> Result<M> {' in rendered.files['grpc_codec.rs']


def test_a_grpc_project_gets_no_proto_sources_module(rendered):
  """The fixture has `spec/proto/`, but its `protos` module is `protos/mod.rs` from `truewire
  protos rust` (ADR 0017): a `protos.rs` of `SOURCES` (ADR 0016) beside it would be a second
  file for one module."""
  assert (FIXTURE / 'spec' / 'proto').is_dir()
  assert 'protos.rs' not in rendered.files
  assert rendered.files['lib.rs'].count('pub mod protos;') == 1


def _tools() -> str | None:
  cargo = shutil.which('cargo') or str(Path.home() / '.cargo' / 'bin' / 'cargo')
  if not Path(cargo).is_file():
    return None
  try:
    find_tool(load_project(FIXTURE), 'buf')
    find_tool(load_project(FIXTURE), 'protoc-gen-prost')
  except StubError:
    return None
  return cargo


def test_generated_crate_formats_lints_and_replays_against_the_grpc_mock(tmp_path: Path):
  cargo = _tools()
  if cargo is None:
    pytest.skip('needs cargo, buf and protoc-gen-prost')
  root = tmp_path / 'client'
  shutil.copytree(FIXTURE, root)
  runner = CliRunner()
  for command in (['protos', 'rust'], ['generate', 'rust']):
    result = runner.invoke(app, [*command, '--project', str(root)])
    assert result.exit_code == 0, result.output
  check = runner.invoke(app, ['protos', 'rust', '--check', '--project', str(root)])
  assert check.exit_code == 0, check.output
  (root / 'Cargo.toml').write_text(CARGO_TOML.format(crates=CRATES))
  (root / 'src' / 'grpc_demo' / 'core.rs').write_text('//! No hand-written core: `GrpcClient` is the gRPC core.\n')
  (root / 'tests').mkdir()
  (root / 'tests' / 'grpc.rs').write_text(RUST_TEST)
  env = {**os.environ, 'CARGO_TARGET_DIR': os.environ.get('TRUEWIRE_CARGO_TARGET_DIR', str(tmp_path / 'target'))}
  # The hand-written test is not held to the generator's formatting; the generated sources are.
  subprocess.run([cargo, 'fmt', '--', str(root / 'tests' / 'grpc.rs')], cwd=root, env=env, check=True)
  formatted = subprocess.run([cargo, 'fmt', '--check'], cwd=root, capture_output=True, text=True, env=env)
  assert formatted.returncode == 0, formatted.stdout + formatted.stderr
  linted = subprocess.run(
    [cargo, 'clippy', '--all-targets', '--', '-D', 'warnings'], cwd=root, capture_output=True, text=True, env=env, timeout=1800,
  )
  assert linted.returncode == 0, linted.stderr[-6000:]
  tested = subprocess.run([cargo, 'test'], cwd=root, capture_output=True, text=True, env=env, timeout=1800)
  assert tested.returncode == 0, tested.stdout[-6000:] + tested.stderr[-6000:]
