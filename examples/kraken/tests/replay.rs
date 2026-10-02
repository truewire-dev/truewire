//! Generic replay coverage: every recorded HTTP example and every recorded WebSocket
//! command, through the real client by function path (`dispatch.rs`). The typed call
//! proves the generated types accept the recorded reply; its dump must equal the `_raw`
//! twin's value exactly, so nothing was lost or reshaped on the way through.

mod common;

use std::time::Duration;

use kraken::CallOptions;
use truewire_testing::{http_examples, replay, ws_examples, Replayed, WsKind};

#[tokio::test]
async fn recorded_http_examples_replay_through_the_generated_client() {
    let mock = common::mock();
    let transports = common::transports(&mock);
    let client = common::client(&transports);
    // `retrieve_export` is hand-written (its reply is a zip archive, the recording only a
    // description of it), so `dispatch.rs` does not carry it: `tests/export.rs` covers it.
    let examples: Vec<_> = http_examples(common::root())
        .into_iter()
        .filter(|e| e.function != "spot.account.retrieve_export")
        .collect();
    assert!(
        examples.len() > 40,
        "only {} HTTP examples found",
        examples.len()
    );
    replay(&examples, |example| {
        let client = &client;
        async move {
            let typed = client
                .call(
                    &example.function,
                    example.request.clone(),
                    CallOptions::default(),
                )
                .await?;
            let raw = client
                .call_raw(
                    &example.function,
                    example.request.clone(),
                    CallOptions::default(),
                )
                .await?;
            Ok::<_, truewire_core::Error>(Replayed::compare(typed, raw))
        }
    })
    .await
    .assert_passed();
}

/// A command recorded without a declared `correlate` (`ping`, `batch_cancel`) replays its
/// reply under the `req_id` it was captured with, which a fresh connection's first request
/// matches. So each command goes over fresh sockets, with a timeout, so a reply the mock
/// cannot correlate fails the example instead of hanging the test.
#[tokio::test]
async fn recorded_websocket_commands_replay_through_the_generated_client() {
    let mock = common::mock();
    let commands: Vec<_> = ws_examples(common::root())
        .into_iter()
        .filter(|e| e.kind == WsKind::Rpc)
        .collect();
    assert!(
        commands.len() >= 9,
        "only {} WebSocket commands found",
        commands.len()
    );
    let options = || CallOptions::default().timeout(Duration::from_secs(10));
    let report = replay(&commands, |example| {
        let mock = &mock;
        async move {
            let transports = common::transports(mock);
            let client = common::client(&transports);
            let typed = client
                .call(&example.function, example.parameters.clone(), options())
                .await;
            transports.close().await;
            let transports = common::transports(mock);
            let client = common::client(&transports);
            let raw = client
                .call_raw(&example.function, example.parameters, options())
                .await;
            transports.close().await;
            Ok::<_, truewire_core::Error>(Replayed::compare(typed?, raw?))
        }
    })
    .await;
    report.assert_passed();
}
