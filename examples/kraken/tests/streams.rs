//! The WebSocket surface against `truewire mock`: channel subscriptions over each socket,
//! the trading methods, and the two whole-frame commands (`ping`, `batch_cancel`) whose
//! reply is not nested under `result`. The same walks as `test/streams.test.ts`.
//!
//! A subscription is read, never unsubscribed: the mock has no recorded unsubscribe ack
//! for these channels and synthesizes one without a `req_id`, which a `req_id`-correlated
//! connection cannot match.

mod common;

use futures::StreamExt;
use kraken::streams::market_data::ticker;
use kraken::trading_ws::{add_order, batch_cancel};
use kraken::CallOptions;
use truewire_core::decode;
use truewire_core::serde_json::json;
use truewire_testing::{ws_examples, WsKind};

#[tokio::test]
async fn a_public_channel_replies_and_pushes_over_the_market_socket() {
    let mock = common::mock();
    let transports = common::transports(&mock);
    let client = common::client(&transports);
    let parameters: ticker::Parameters = decode(json!({"symbol": ["BTC/USD"]})).unwrap();
    let mut stream = client
        .streams
        .market_data
        .ticker(parameters, CallOptions::default())
        .await
        .unwrap();
    assert_eq!(stream.reply.as_ref().unwrap()["method"], "subscribe");
    let message = stream.next().await.expect("a push").expect("a valid push");
    assert_eq!(truewire_core::dump(&message).unwrap()["channel"], "ticker");
    assert!(transports.market_client.socket().is_open().await);
    assert!(!transports.private_client.socket().is_open().await);
    transports.close().await;
}

#[tokio::test]
async fn a_private_channel_goes_over_the_private_socket() {
    let mock = common::mock();
    let transports = common::transports(&mock);
    let client = common::client(&transports);
    let mut stream = client
        .streams
        .private
        .balances(decode(json!({})).unwrap(), CallOptions::default())
        .await
        .unwrap();
    let message = truewire_core::dump(&stream.next().await.unwrap().unwrap()).unwrap();
    assert_eq!(message["channel"], "balances");
    assert!(!message["data"].as_array().unwrap().is_empty());
    assert!(transports.private_client.socket().is_open().await);
    assert!(!transports.market_client.socket().is_open().await);
    transports.close().await;
}

/// Every recorded subscription, typed and raw: the first push, dumped back, equals the
/// recording. A subscription with no recorded acknowledgement (`executions` records pushes
/// only) is skipped: a `req_id`-correlated client waits for an ack the mock cannot send.
/// Each read has a timeout, so a gap in the recordings fails instead of hanging.
#[tokio::test]
async fn every_recorded_subscription_pushes_its_first_message_typed_and_raw() {
    let mock = common::mock();
    let limit = std::time::Duration::from_secs(10);
    let mut checked = 0;
    for example in ws_examples(common::root())
        .into_iter()
        .filter(|e| e.kind == WsKind::Stream)
    {
        let (Some(recorded), Some(_)) = (example.messages.first(), example.reply.as_ref()) else {
            continue;
        };
        // One subscription per channel per connection: fresh transports for each of the two reads.
        for raw in [false, true] {
            let label = format!("{} ({}, raw: {raw})", example.function, example.id);
            let transports = common::transports(&mock);
            let client = common::client(&transports);
            let parameters = example.parameters.clone();
            let first = tokio::time::timeout(limit, async {
                let mut stream = if raw {
                    client
                        .subscribe_raw(&example.function, parameters, CallOptions::default())
                        .await?
                } else {
                    client
                        .subscribe(&example.function, parameters, CallOptions::default())
                        .await?
                };
                stream.next().await.expect("a push")
            })
            .await
            .unwrap_or_else(|_| panic!("{label}: no acknowledgement and push within {limit:?}"))
            .unwrap_or_else(|e| panic!("{label}: {e}"));
            if let Some(difference) = truewire_testing::first_difference(&first, recorded) {
                panic!("{label}: the first push differs from the recording at {difference}");
            }
            transports.close().await;
        }
        checked += 1;
    }
    assert!(
        checked >= 6,
        "only {checked} recorded subscriptions checked"
    );
}

/// `add_order` declares `correlate`, so the mock answers under whatever `req_id` it was
/// sent; `ping` and `batch_cancel` do not, and their recorded reply carries `req_id` 0,
/// which only the first request on a fresh connection sends. Each of those two therefore
/// goes over fresh transports, and every call has a timeout so a reply that cannot
/// correlate fails instead of hanging.
#[tokio::test]
async fn trading_methods_and_whole_frame_commands() {
    let mock = common::mock();
    let options = || CallOptions::default().timeout(std::time::Duration::from_secs(10));

    let transports = common::transports(&mock);
    let client = common::client(&transports);
    let request: add_order::Request = decode(
        json!({"symbol": "XBT/USDC", "side": "buy", "order_type": "limit", "order_qty": 0.0001, "limit_price": 10000.0}),
    )
    .unwrap();
    let result = client
        .trading_ws
        .add_order(request, options())
        .await
        .unwrap();
    assert!(result.order_id.is_some_and(|id| !id.is_empty()));
    transports.close().await;

    let transports = common::transports(&mock);
    let client = common::client(&transports);
    let pong = client.streams.market_data.ping(options()).await.unwrap();
    assert!(pong.time_in.timestamp() > 0 && pong.time_out >= pong.time_in);
    transports.close().await;

    let transports = common::transports(&mock);
    let client = common::client(&transports);
    let request: batch_cancel::Request =
        decode(json!({"orders": ["OOWVMC-7HDFH-B7UPWM", "O7L6T4-QEGI4-7M4PUY"]})).unwrap();
    let reply = client
        .trading_ws
        .batch_cancel(request, options())
        .await
        .unwrap();
    assert_eq!(truewire_core::dump(&reply).unwrap()["orders_cancelled"], 2);
    transports.close().await;
}
