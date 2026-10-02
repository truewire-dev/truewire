//! Protobuf messages as JSON values: a `.proto` compiled in-process, round trips with
//! proto field names and 64-bit integers as strings, a wrapper's `oneof` narrowed, and the
//! failure paths.

use std::path::PathBuf;

use serde_json::json;
use truewire_core::proto::{narrow, Protos};

pub fn protos() -> Protos {
    // One directory per call: tests run in parallel and must not write each other's files.
    static NEXT: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
    let n = NEXT.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!("truewire-proto-{}-{n}", std::process::id()));
    std::fs::create_dir_all(dir.join("demo")).unwrap();
    std::fs::write(
        dir.join("demo/push.proto"),
        r#"syntax = "proto3";
package demo.v1;
message Deal { string price = 1; uint64 trade_time = 2; }
message Deals { repeated Deal deals = 1; string event_type = 2; }
message Wrapper {
  string channel = 1;
  oneof body {
    Deals public_deals = 301;
    Deals private_deals = 306;
  }
}
"#,
    )
    .unwrap();
    Protos::compile(&[PathBuf::from("demo/push.proto")], &[dir]).unwrap()
}

#[test]
fn a_message_round_trips_through_its_bytes_with_proto_field_names() {
    let protos = protos();
    let value = json!({"channel": "spot@public.deals", "public_deals": {"deals": [{"price": "1.5", "trade_time": "1700000000000"}], "event_type": "deals"}});
    let bytes = protos.encode("demo.v1.Wrapper", &value).unwrap();
    assert_eq!(protos.decode("demo.v1.Wrapper", &bytes).unwrap(), value);
    // Decoding accepts the JSON-name spelling too.
    let camel = json!({"channel": "c", "publicDeals": {"eventType": "x"}});
    let bytes = protos.encode("demo.v1.Wrapper", &camel).unwrap();
    assert_eq!(
        protos.decode("demo.v1.Wrapper", &bytes).unwrap()["public_deals"]["event_type"],
        "x"
    );
}

#[test]
fn narrow_reads_the_branch_a_push_carries() {
    let protos = protos();
    let bytes = protos
        .encode(
            "demo.v1.Wrapper",
            &json!({"channel": "c", "private_deals": {"event_type": "mine"}}),
        )
        .unwrap();
    let wrapper = protos.decode("demo.v1.Wrapper", &bytes).unwrap();
    assert_eq!(narrow(&wrapper, "private_deals").unwrap()["event_type"], "mine");
    assert!(narrow(&wrapper, "public_deals").is_none());
}

#[test]
fn unknown_messages_bad_bytes_and_bad_json_fail_as_they_should() {
    let protos = protos();
    assert!(protos.decode("demo.v1.Nope", b"").unwrap_err().is_logic());
    assert!(protos
        .decode("demo.v1.Wrapper", &[0xff, 0xff, 0xff])
        .unwrap_err()
        .is_validation());
    assert!(protos
        .encode("demo.v1.Deal", &json!({"trade_time": "soon"}))
        .unwrap_err()
        .is_validation());
    assert!(Protos::from_descriptor_set(b"\xff\xff").unwrap_err().is_logic());
    assert!(Protos::compile(&["missing.proto"], &[std::env::temp_dir()])
        .unwrap_err()
        .is_logic());
}

#[test]
fn sources_in_memory_compile_with_their_imports() {
    let sources = [
        (
            "deals/deal.proto",
            "syntax = \"proto3\";\npackage demo.v1;\nmessage Deal { string price = 1; int64 time = 2; }\n",
        ),
        (
            "wrapper.proto",
            "syntax = \"proto3\";\npackage demo.v1;\nimport \"deals/deal.proto\";\nimport \"google/protobuf/empty.proto\";\nmessage Wrapper { string channel = 1; oneof body { Deal deal = 301; google.protobuf.Empty none = 302; } }\n",
        ),
    ];
    let protos = Protos::from_sources(&sources).unwrap();
    let value = json!({"channel": "c", "deal": {"price": "1", "time": "1700000000000"}});
    let bytes = protos.encode("demo.v1.Wrapper", &value).unwrap();
    assert_eq!(protos.decode("demo.v1.Wrapper", &bytes).unwrap(), value);
    let missing = Protos::from_sources(&[("a.proto", "syntax = \"proto3\";\nimport \"b.proto\";\n")]);
    assert!(missing.unwrap_err().to_string().contains("b.proto"));
}
