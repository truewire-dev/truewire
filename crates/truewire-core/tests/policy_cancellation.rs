//! A cancelled pacing waiter releases its slot (TRU-229).

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use truewire_core::http::{HttpClient, RequestOptions};

/// Answers every request with `status` and `extra` headers; counts requests.
async fn server(status: u16, extra: &'static str) -> (String, Arc<AtomicUsize>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}/", listener.local_addr().unwrap());
    let count = Arc::new(AtomicUsize::new(0));
    let seen = count.clone();
    tokio::spawn(async move {
        loop {
            let (mut stream, _) = listener.accept().await.unwrap();
            let seen = seen.clone();
            tokio::spawn(async move {
                let mut buf = [0u8; 4096];
                loop {
                    match stream.read(&mut buf).await {
                        Ok(0) | Err(_) => return,
                        Ok(_) => {}
                    }
                    seen.fetch_add(1, Ordering::SeqCst);
                    let reply = format!(
                        "HTTP/1.1 {status} X\r\ncontent-type: application/json\r\ncontent-length: 2\r\n{extra}\r\n{{}}"
                    );
                    if stream.write_all(reply.as_bytes()).await.is_err() {
                        return;
                    }
                }
            });
        }
    });
    (url, count)
}

/// A request cancelled while it waits for its pace slot never started, so it should not
/// push back the next one. At rate 1, four cancelled waiters plus a 1.2 s gap should leave
/// the next request free to go at once.
#[tokio::test]
async fn cancelled_waiters_do_not_hold_slots() {
    let (url, count) = server(200, "").await;
    let http = HttpClient::default().with_rate(Some(1.0));
    http.request("GET", &url, RequestOptions::default()).await.unwrap();
    for _ in 0..4 {
        let _ = tokio::time::timeout(
            Duration::from_millis(20),
            http.request("GET", &url, RequestOptions::default()),
        )
        .await;
    }
    assert_eq!(
        count.load(Ordering::SeqCst),
        1,
        "cancelled requests never reached the server"
    );
    tokio::time::sleep(Duration::from_millis(1200)).await;
    let began = Instant::now();
    http.request("GET", &url, RequestOptions::default()).await.unwrap();
    let waited = began.elapsed();
    assert!(
        waited < Duration::from_millis(300),
        "waited {waited:?} for slots nobody used"
    );
}
