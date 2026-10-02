//! Discovery against the repository's own example projects, the JSON diff, the replay
//! report, and the mock binary lookup.

use std::path::PathBuf;

use serde_json::json;
use truewire_testing::{first_difference, http_examples, replay, spec_dir, ws_examples, Mock, Replayed, WsKind};

fn example(name: &str) -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../examples")
        .join(name)
}

#[test]
fn http_examples_are_found_with_their_function_paths_and_requests() {
    let examples = http_examples(example("github"));
    assert!(!examples.is_empty());
    let get = examples
        .iter()
        .find(|e| e.function == "repos.get")
        .expect("repos.get is recorded");
    assert_eq!(get.request, json!({"owner": "truewire-dev", "repo": "truewire"}));
    assert!(examples.windows(2).all(|pair| pair[0].function <= pair[1].function));
    assert!(ws_examples(example("github")).is_empty());
}

#[test]
fn ws_examples_carry_kind_parameters_reply_and_messages() {
    let examples = ws_examples(example("kraken"));
    let ticker = examples
        .iter()
        .find(|e| e.function == "streams.market_data.ticker")
        .expect("ticker");
    assert_eq!(ticker.kind, WsKind::Stream);
    assert_eq!(ticker.parameters, json!({"symbol": ["BTC/USD"]}));
    assert!(ticker.reply.is_some() && !ticker.messages.is_empty());
    let add_order = examples
        .iter()
        .find(|e| e.function == "trading_ws.add_order")
        .expect("add_order");
    assert_eq!(add_order.kind, WsKind::Rpc);
    assert!(add_order.messages.is_empty());
    let ping = examples
        .iter()
        .find(|e| e.function == "streams.market_data.ping")
        .expect("ping");
    assert_eq!(ping.parameters, json!({}));
    // `retrieve_export` declares a hand-written surface: nothing generated serves it.
    assert!(http_examples(example("kraken"))
        .iter()
        .all(|e| e.function != "spot.account.retrieve_export"));
}

#[test]
fn the_spec_dir_defaults_to_spec() {
    assert_eq!(spec_dir(example("github")), example("github").join("spec"));
}

#[test]
fn first_difference_names_the_pointer() {
    assert_eq!(first_difference(&json!({"a": [1, 2]}), &json!({"a": [1, 2]})), None);
    assert_eq!(
        first_difference(&json!({"b": 1, "a": 2}), &json!({"a": 2, "b": 1})),
        None
    );
    assert_eq!(
        first_difference(&json!({"a": [1, 2]}), &json!({"a": [1, 3]})).unwrap(),
        "/a/1: 2 vs 3"
    );
    assert_eq!(
        first_difference(&json!({"a": 1}), &json!({})).unwrap(),
        "/a: only on the left"
    );
    assert_eq!(
        first_difference(&json!([1]), &json!([1, 2])).unwrap(),
        "/: 1 vs 2 items"
    );
    // JSON has one number type: a float and an integer of the same value are equal.
    assert_eq!(
        first_difference(&json!({"fee": 0.0, "n": 10000.0}), &json!({"fee": 0, "n": 10000})),
        None
    );
    assert_eq!(first_difference(&json!(1.5), &json!(1)).unwrap(), "/: 1.5 vs 1");
    // RFC 3339 date-times compare by instant; other strings by text.
    assert_eq!(
        first_difference(&json!("2026-08-13T11:58:00Z"), &json!("2026-08-13T11:58:00.000000000Z")),
        None
    );
    assert_eq!(
        first_difference(&json!("2026-08-13T13:58:00+02:00"), &json!("2026-08-13T11:58:00Z")),
        None
    );
    assert!(first_difference(&json!("2026-08-13T11:58:00Z"), &json!("2026-08-13T11:58:01Z")).is_some());
    assert!(first_difference(&json!("11:58"), &json!("11:58:00")).is_some());
    assert_eq!(first_difference(&json!("1"), &json!(1)).unwrap(), "/: \"1\" vs 1");
}

#[test]
fn replay_collects_every_outcome_and_fails_once_naming_them_all() {
    let examples = http_examples(example("github"));
    let report = futures::executor::block_on(replay(&examples, |example| {
        let outcome = match example.function.as_str() {
            "repos.get" => Ok(Replayed::compare(json!({"x": 1}), json!({"x": 2}))),
            "repos.get_commit" => Err("the call failed"),
            "issues.list" => Ok(Replayed::Skipped("not exact".to_string())),
            _ => Ok(Replayed::Ok),
        };
        async move { outcome }
    }));
    assert!(!report.passed.is_empty());
    assert_eq!(report.failed.len(), 2);
    assert!(report
        .failed
        .iter()
        .any(|(label, why)| label.starts_with("repos.get (") && why.contains("/x: 1 vs 2")));
    let panic = std::panic::catch_unwind(|| report.assert_passed()).unwrap_err();
    let message = panic.downcast_ref::<String>().unwrap();
    assert!(
        message.contains("2 of") && message.contains("the call failed"),
        "{message}"
    );
}

#[test]
fn mock_binary_prefers_truewire_bin() {
    // Only this test touches the variable.
    std::env::set_var("TRUEWIRE_BIN", "/opt/truewire");
    assert_eq!(Mock::binary(example("github")), PathBuf::from("/opt/truewire"));
    std::env::remove_var("TRUEWIRE_BIN");
    let found = Mock::binary(example("github"));
    assert!(found.ends_with(".venv/bin/truewire") || found.as_os_str() == "truewire");
}

#[test]
fn a_mock_that_cannot_run_is_an_error_not_a_hang() {
    let error = Mock::start("/nonexistent-project").map(|_| ()).err();
    // Either the binary is missing, or it exits at once on a project that does not exist.
    assert!(error.is_some());
}

#[test]
fn a_date_time_without_an_offset_compares_as_utc() {
    use serde_json::json;
    use truewire_testing::first_difference;
    let naive = json!({"at": "1970-01-01T00:00:00", "nanos": "2026-02-20T19:01:52.647760965"});
    let dumped = json!({"at": "1970-01-01T00:00:00Z", "nanos": "2026-02-20T19:01:52.647760965Z"});
    assert_eq!(first_difference(&dumped, &naive), None);
    assert!(first_difference(&json!("1970-01-01T00:00:01Z"), &json!("1970-01-01T00:00:00")).is_some());
    assert!(first_difference(&json!("1970-01-01"), &json!("1970-01-01T00:00:00Z")).is_some());
}
