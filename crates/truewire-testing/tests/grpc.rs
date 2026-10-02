//! The fake gRPC server against a project written on the fly: a recorded request (in any
//! field order, defaults spelled or not) gets its response, anything else `NOT_FOUND`, a
//! method with no recording `UNIMPLEMENTED`; `compare` catches a reply that differs.

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use serde_json::json;
use truewire_core::grpc::GrpcClient;
use truewire_core::proto::Protos;
use truewire_core::{ApiKind, CallOptions, GrpcCall, GrpcEndpoint};
use truewire_testing::{grpc_examples, replay_grpc, GrpcMock, Replayed};

const PROTO: &str = r#"syntax = "proto3";
package demo.bank.v1;
import "google/protobuf/any.proto";
message PageRequest { bytes key = 1; uint64 limit = 2; }
message BalanceRequest { string address = 1; string denom = 2; PageRequest pagination = 3; }
message BalanceResponse { string amount = 1; uint64 height = 2; }
message Account { string address = 1; google.protobuf.Any pub_key = 2; }
message AccountRequest { string address = 1; }
message AccountResponse { repeated google.protobuf.Any accounts = 1; }
service Query {
  rpc Balance(BalanceRequest) returns (BalanceResponse);
  rpc Other(BalanceRequest) returns (BalanceResponse);
  rpc Accounts(AccountRequest) returns (AccountResponse);
}
"#;

fn project(name: &str) -> (PathBuf, Arc<Protos>) {
    let root = std::env::temp_dir().join(format!("truewire-testing-grpc-{}-{name}", std::process::id()));
    let _ = fs::remove_dir_all(&root);
    let endpoint = root.join("spec/endpoints/bank/balance");
    fs::create_dir_all(endpoint.join("examples")).unwrap();
    fs::create_dir_all(root.join("spec/proto")).unwrap();
    fs::write(root.join("truewire.toml"), "[project]\nname = \"demo\"\n").unwrap();
    fs::write(root.join("spec/proto/query.proto"), PROTO).unwrap();
    write(
        &endpoint.join("endpoint.json"),
        json!({"spec": {"kind": "grpc", "service": "demo.bank.v1.Query", "rpc": "Balance",
            "request": "demo.bank.v1.BalanceRequest", "response": "demo.bank.v1.BalanceResponse",
            "proto": "query.proto"}}),
    );
    write(
        &endpoint.join("examples/01.request.json"),
        json!({"address": "demo1", "denom": "udemo", "pagination": {"limit": "2"}}),
    );
    write(
        &endpoint.join("examples/01.response.json"),
        json!({"amount": "10", "height": "18446744073709551615"}),
    );
    write(&endpoint.join("examples/02.request.json"), json!({"address": "demo2"}));
    write(&endpoint.join("examples/02.response.json"), json!({"amount": "0"}));
    let accounts = root.join("spec/endpoints/auth/accounts");
    fs::create_dir_all(accounts.join("examples")).unwrap();
    write(
        &accounts.join("endpoint.json"),
        json!({"spec": {"kind": "grpc", "service": "demo.bank.v1.Query", "rpc": "Accounts",
            "request": "demo.bank.v1.AccountRequest", "response": "demo.bank.v1.AccountResponse",
            "proto": "query.proto"}}),
    );
    // Both spellings of an Any: betterproto's `type_url`/`value`, and proto JSON's `@type`,
    // here nesting the other spelling inside it.
    write(&accounts.join("examples/01.request.json"), json!({"address": "demo1"}));
    write(
        &accounts.join("examples/01.response.json"),
        json!({"accounts": [
            {"type_url": "/demo.bank.v1.Account", "value": "CgVkZW1vMQ=="},
            {"@type": "/demo.bank.v1.Account", "address": "demo2",
             "pub_key": {"type_url": "/crypto.PubKey", "value": "AQI="}}
        ]}),
    );
    // An `@type` the descriptors do not declare: left out of the mock, skipped by the replay.
    write(&accounts.join("examples/02.request.json"), json!({"address": "demo3"}));
    write(
        &accounts.join("examples/02.response.json"),
        json!({"accounts": [{"@type": "/cosmos.crypto.ed25519.PubKey", "key": "AQI="}]}),
    );
    let protos = Protos::compile(&[PathBuf::from("query.proto")], &[root.join("spec/proto")]).unwrap();
    (root, Arc::new(protos))
}

fn write(path: &Path, value: serde_json::Value) {
    fs::write(path, serde_json::to_string_pretty(&value).unwrap()).unwrap();
}

#[test]
fn examples_are_discovered_with_their_method_and_messages() {
    let (root, _) = project("discover");
    let examples = grpc_examples(&root);
    assert_eq!(examples.len(), 4);
    assert_eq!(examples[0].function, "auth.accounts");
    let examples: Vec<_> = examples
        .into_iter()
        .filter(|example| example.function == "bank.balance")
        .collect();
    assert_eq!(examples[0].method, "/demo.bank.v1.Query/Balance");
    assert_eq!(examples[0].request_type, "demo.bank.v1.BalanceRequest");
    assert!(truewire_testing::http_examples(&root)
        .iter()
        .all(|example| example.function != "bank.balance"));
}

#[tokio::test]
async fn a_recorded_request_is_answered_and_anything_else_is_not_found() {
    let (root, protos) = project("answers");
    let mock = GrpcMock::start(&root, protos.clone()).await.unwrap();
    let client = GrpcClient::new(mock.url()).unwrap().with_protos(protos.clone());

    // Field order, a default spelled out and the JSON name all compare as the same message.
    let request = json!({"pagination": {"limit": "2", "key": ""}, "denom": "udemo", "address": "demo1"});
    let reply = client
        .unary("demo.bank.v1.Query", "Balance", &request, None)
        .await
        .unwrap();
    assert_eq!(reply, json!({"amount": "10", "height": "18446744073709551615"}));
    assert_eq!(mock.answered("bank.balance"), 1);

    let unmatched = client
        .unary("demo.bank.v1.Query", "Balance", &json!({"address": "nobody"}), None)
        .await
        .unwrap_err();
    assert!(unmatched.is_api(ApiKind::BadRequest), "{unmatched}");
    assert!(
        unmatched.message().contains("no recorded example matches"),
        "{unmatched}"
    );

    let unrecorded = client
        .unary("demo.bank.v1.Query", "Other", &json!({}), None)
        .await
        .unwrap_err();
    assert!(unrecorded.is_api(ApiKind::Api), "{unrecorded}");
    assert_eq!(mock.answered("bank.balance"), 1);
}

#[tokio::test]
async fn replay_through_a_core_compares_the_reply_with_the_recording() {
    let (root, protos) = project("replay");
    let mock = GrpcMock::start(&root, protos.clone()).await.unwrap();
    assert_eq!(mock.left_out().len(), 1);
    assert_eq!(mock.left_out()[0].1, "/cosmos.crypto.ed25519.PubKey");
    let core = GrpcClient::new(mock.url()).unwrap();
    let examples = grpc_examples(&root);
    let methods: std::collections::HashMap<String, String> = examples
        .iter()
        .map(|example| (example.function.clone(), example.method.clone()))
        .collect();
    let (core, methods) = (&core, &methods);
    let report = replay_grpc(&examples, &protos, |function, request| async move {
        let call = GrpcCall {
            method: &methods[&function],
            request,
            meta: &(),
            options: CallOptions::default(),
        };
        GrpcEndpoint::<()>::invoke(core, call).await
    })
    .await;
    report.assert_passed();
    assert_eq!(report.passed.len(), 3);
    assert_eq!(report.skipped.len(), 1);
    assert!(report.skipped[0].1.contains("cosmos.crypto.ed25519.PubKey"));
    assert_eq!(mock.answered("bank.balance"), 2);
    assert_eq!(mock.answered("auth.accounts"), 1);

    let balance = examples
        .iter()
        .find(|example| example.id == "02" && example.function == "bank.balance")
        .unwrap();
    let other = protos
        .encode("demo.bank.v1.BalanceResponse", &json!({"amount": "11"}))
        .unwrap();
    assert!(matches!(balance.compare(&protos, &other), Replayed::Failed(_)));
}

#[tokio::test]
async fn a_recording_the_descriptors_cannot_read_stops_the_mock() {
    let (root, protos) = project("broken");
    write(
        &root.join("spec/endpoints/bank/balance/examples/03.request.json"),
        json!({"pagination": {"limit": "tall"}}),
    );
    write(
        &root.join("spec/endpoints/bank/balance/examples/03.response.json"),
        json!({}),
    );
    let error = GrpcMock::start(&root, protos).await.err().expect("the mock refused");
    assert!(error.to_string().contains("bank.balance (03)"), "{error}");
}
