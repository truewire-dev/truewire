//! The hand-written `spot.account.retrieve_export`, which the generic replay skips: the
//! reply is a zip archive (the recorded example holds only a description of it), so a
//! loopback server serves real bytes, and a JSON error envelope.

mod common;

use std::sync::Arc;

use kraken::core::{SocketCore, SpotCore, Transports};
use kraken::CallOptions;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use truewire_core::ApiKind;

/// Serve one request: a 418 unless it is the signed RetrieveExport POST for `UYAJ`, else
/// `body` with `content_type`.
async fn serve_once(body: Vec<u8>, content_type: &'static str) -> String {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        let mut request = Vec::new();
        let mut buffer = [0u8; 4096];
        loop {
            let read = socket.read(&mut buffer).await.unwrap();
            request.extend_from_slice(&buffer[..read]);
            let text = String::from_utf8_lossy(&request).to_string();
            if let Some(end) = text.find("\r\n\r\n") {
                let length = text
                    .lines()
                    .find_map(|line| {
                        let (name, value) = line.split_once(':')?;
                        name.eq_ignore_ascii_case("content-length")
                            .then(|| value.trim().parse::<usize>().ok())?
                    })
                    .unwrap_or(0);
                if request.len() >= end + 4 + length {
                    break;
                }
            }
            if read == 0 {
                break;
            }
        }
        let text = String::from_utf8_lossy(&request).to_lowercase();
        let (head, form) = text.split_once("\r\n\r\n").unwrap_or((&text, ""));
        let ok = head.starts_with("post /0/private/retrieveexport ")
            && head.contains("api-key: mock-api-key")
            && head.contains("api-sign: ")
            && form.split('&').any(|pair| pair == "id=uyaj")
            && form.split('&').any(|pair| pair.starts_with("nonce="));
        let (status, body) = if ok {
            ("200 OK", body)
        } else {
            ("418 I'm a teapot", Vec::new())
        };
        let head = format!(
            "HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
            body.len()
        );
        socket.write_all(head.as_bytes()).await.unwrap();
        socket.write_all(&body).await.unwrap();
        socket.shutdown().await.unwrap();
    });
    url
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

#[tokio::test]
async fn retrieve_export_returns_the_archive_bytes() {
    let archive = b"PK\x03\x04 not really a zip \xff\x00".to_vec();
    let url = serve_once(archive.clone(), "application/zip").await;
    let got = client(url)
        .spot
        .account
        .retrieve_export("UYAJ", CallOptions::default())
        .await
        .unwrap();
    assert_eq!(got, archive);
}

#[tokio::test]
async fn retrieve_export_raises_a_json_error_envelope() {
    let url = serve_once(
        br#"{"error": ["EGeneral:Invalid arguments"]}"#.to_vec(),
        "application/json",
    )
    .await;
    let error = client(url)
        .spot
        .account
        .retrieve_export("UYAJ", CallOptions::default())
        .await
        .unwrap_err();
    assert!(error.is_api(ApiKind::BadRequest), "{error}");
}

#[tokio::test]
async fn retrieve_export_needs_credentials() {
    let client = kraken::Kraken::new(kraken::core::CoreOptions::default());
    let error = client
        .spot
        .account
        .retrieve_export("UYAJ", CallOptions::default())
        .await
        .unwrap_err();
    assert!(error.is_api(ApiKind::Auth), "{error}");
}
