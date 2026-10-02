#![cfg(not(any(feature = "rustls-tls", feature = "native-tls")))]
//! HTTPS must never be forwarded in cleartext, even when reqwest has no TLS support.
//! Kept in its own test binary because the environment-proxy case changes process state.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use truewire_core::http::RequestOptions;
use truewire_core::{Error, HttpClient, HttpClientOptions};

#[tokio::test]
async fn https_is_rejected_before_sending_to_any_proxy_without_tls() {
    for name in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"] {
        std::env::remove_var(name);
        std::env::remove_var(name.to_ascii_lowercase());
    }
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let proxy = format!("http://{}", listener.local_addr().unwrap());
    let seen = Arc::new(Mutex::new(Vec::new()));
    let record = seen.clone();
    let server = tokio::spawn(async move {
        while let Ok((mut stream, _)) = listener.accept().await {
            let mut head = Vec::new();
            let mut byte = [0];
            while !head.ends_with(b"\r\n\r\n") {
                if stream.read(&mut byte).await.unwrap_or(0) == 0 {
                    break;
                }
                head.push(byte[0]);
            }
            record.lock().unwrap().push(String::from_utf8_lossy(&head).into_owned());
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
                .await
                .unwrap();
        }
    });

    let explicit = HttpClient::new(HttpClientOptions::default().with_proxy(&proxy).unwrap());
    let direct = HttpClient::new(HttpClientOptions {
        client: Some(reqwest::Client::builder().no_proxy().build().unwrap()),
        ..Default::default()
    });
    std::env::set_var("HTTPS_PROXY", &proxy);
    std::env::set_var("HTTP_PROXY", &proxy);
    let environment = HttpClient::default();

    for client in [&explicit, &direct, &environment] {
        for target in [
            "https://api.example.invalid/private",
            "HTTPS://api.example.invalid/private",
        ] {
            let error = client
                .request(
                    "GET",
                    target,
                    RequestOptions::new()
                        .header("Authorization", "Bearer synthetic-test-value")
                        .timeout(Duration::from_secs(1)),
                )
                .await
                .unwrap_err();
            assert!(matches!(error, Error::Logic(_)), "{error:?}");
            assert!(error.to_string().contains("needs TLS"), "{error:?}");
        }
    }
    assert!(seen.lock().unwrap().is_empty(), "HTTPS reached the proxy");

    // Positive controls: the listener is reachable and both proxy configurations work
    // for plain HTTP, so an empty recording above cannot hide a broken test proxy.
    for client in [&explicit, &environment] {
        let response = client
            .request(
                "GET",
                "http://api.example.invalid/public",
                RequestOptions::new().timeout(Duration::from_secs(1)),
            )
            .await
            .unwrap();
        assert_eq!(response.status, 200);
    }
    let requests = seen.lock().unwrap();
    assert_eq!(requests.len(), 2);
    assert!(requests
        .iter()
        .all(|head| head.starts_with("GET http://api.example.invalid/public ")));
    assert!(requests.iter().all(|head| !head.contains("synthetic-test-value")));
    server.abort();
    std::env::remove_var("HTTPS_PROXY");
    std::env::remove_var("HTTP_PROXY");
}
