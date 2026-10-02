//! W15: `truewire.toml` refuses `spot.funding.withdraw`, so the generated method fails with
//! `RefusedByPolicy` before any request. A loopback server counts every request that
//! reaches it: none for the refused endpoint, one for a sibling that is not refused.

mod common;

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use kraken::core::{SocketCore, SpotCore, Transports};
use kraken::policy::RefusedByPolicy;
use kraken::spot::funding::{withdraw, withdraw_methods};
use kraken::CallOptions;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use truewire_core::{serde_json, Error};

/// A server answering every request with a Kraken error envelope; returns its URL and the
/// number of requests it has read.
async fn counting_server() -> (String, Arc<AtomicUsize>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let requests = Arc::new(AtomicUsize::new(0));
    let counted = requests.clone();
    tokio::spawn(async move {
        loop {
            let (mut socket, _) = listener.accept().await.unwrap();
            let mut buffer = [0u8; 4096];
            if socket.read(&mut buffer).await.unwrap_or(0) == 0 {
                continue;
            }
            counted.fetch_add(1, Ordering::SeqCst);
            let body = r#"{"error": ["EGeneral:Permission denied"]}"#;
            let head = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                body.len()
            );
            let _ = socket.write_all(head.as_bytes()).await;
            let _ = socket.write_all(body.as_bytes()).await;
            let _ = socket.shutdown().await;
        }
    });
    (url, requests)
}

fn client(url: String) -> kraken::Kraken {
    kraken::Kraken::from_transports(&Transports {
        spot_client: Arc::new(SpotCore::new(
            Some(url),
            Some(common::fake_credentials()),
            None,
        )),
        market_client: Arc::new(SocketCore::new("ws://127.0.0.1:9", None)),
        private_client: Arc::new(SocketCore::new("ws://127.0.0.1:9", None)),
    })
}

fn assert_refused(error: Error) {
    let refused = RefusedByPolicy::of(&error).expect("the error is the refusal");
    assert_eq!(refused.endpoint, "spot.funding.withdraw");
    assert!(matches!(error, Error::Logic(_)), "{error:?}");
}

#[tokio::test]
async fn a_refused_endpoint_fails_before_any_request() {
    let (url, requests) = counting_server().await;
    let client = client(url);
    let request = withdraw::Request {
        asset: "XBT".to_string(),
        key: "cold-storage".to_string(),
        amount: "0.5".to_string(),
        ..Default::default()
    };

    let typed = client
        .spot
        .funding
        .withdraw(request.clone(), CallOptions::default())
        .await;
    assert_refused(typed.unwrap_err());
    let raw = client
        .spot
        .funding
        .withdraw_raw(request, CallOptions::default())
        .await;
    assert_refused(raw.unwrap_err());
    let dispatched = client
        .call(
            "spot.funding.withdraw",
            serde_json::json!({"asset": "XBT", "key": "cold-storage", "amount": "0.5"}),
            CallOptions::default(),
        )
        .await;
    assert_refused(dispatched.unwrap_err());
    assert_eq!(requests.load(Ordering::SeqCst), 0);

    // The server does see what the client sends: a sibling that is not refused reaches it.
    let allowed = client
        .spot
        .funding
        .withdraw_methods(withdraw_methods::Request::default(), CallOptions::default())
        .await;
    assert!(RefusedByPolicy::of(&allowed.unwrap_err()).is_none());
    assert_eq!(requests.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn retry_signs_a_fresh_nonce() {
    use truewire_core::HttpClient;
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let server = tokio::spawn(async move {
        for status in [503, 200] {
            let (mut socket, _) = listener.accept().await.unwrap();
            let mut raw = Vec::new();
            let mut buffer = [0; 4096];
            loop {
                let n = socket.read(&mut buffer).await.unwrap();
                assert!(n > 0);
                raw.extend_from_slice(&buffer[..n]);
                let text = String::from_utf8_lossy(&raw);
                if let Some((head, body)) = text.split_once("\r\n\r\n") {
                    let length: usize = head
                        .lines()
                        .find_map(|line| {
                            let (key, value) = line.split_once(':')?;
                            key.eq_ignore_ascii_case("content-length")
                                .then(|| value.trim().parse().unwrap())
                        })
                        .unwrap_or(0);
                    if body.len() >= length {
                        break;
                    }
                }
            }
            let body = r#"{"error":[],"result":{"token":"synthetic-token","expires":900}}"#;
            let reply = format!("HTTP/1.1 {status} X\r\nContent-Length: {}\r\nRetry-After: 0\r\nConnection: close\r\n\r\n{body}", body.len());
            socket.write_all(reply.as_bytes()).await.unwrap();
        }
    });
    let http = HttpClient::default().with_retry(true);
    let recording = http.recording();
    let credentials = common::fake_credentials();
    let core = SpotCore::new(Some(url), Some(credentials.clone()), Some(http));
    core.ws_token().await.unwrap();
    server.await.unwrap();
    let exchanges = recording.exchanges();
    assert_eq!(exchanges.len(), 2);
    let mut nonces = Vec::new();
    for exchange in exchanges {
        let body = String::from_utf8(exchange.request.body.unwrap().to_vec()).unwrap();
        let nonce: u64 = body.strip_prefix("nonce=").unwrap().parse().unwrap();
        nonces.push(nonce);
        assert_eq!(
            exchange.request.headers["API-Sign"],
            kraken::core::sign(
                "/0/private/GetWebSocketsToken",
                nonce,
                &body,
                &credentials.private_key,
            )
            .unwrap()
        );
    }
    assert!(nonces[1] > nonces[0]);
}
