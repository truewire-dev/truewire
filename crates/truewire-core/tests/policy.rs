//! Pins `HttpClient::with_rate` and `with_retry`: the runtime side of `[policy].rate` and
//! `[policy].retry` (workspace clause W15).
//!
//! Every test talks to a real loopback HTTP/1.1 server that answers from a script, so
//! pacing is measured on requests that crossed a socket and a retry is a second request the
//! server saw.

use std::collections::VecDeque;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde_json::json;
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::net::{TcpListener, TcpStream};
use truewire_core::http::{HttpClient, RequestOptions, RETRY_ATTEMPTS};
use truewire_core::Error;

type Step = (u16, Vec<(&'static str, String)>);

/// Answers each request with the next step of its script, then 200s.
struct Server {
    url: String,
    /// When each request arrived, in order.
    starts: Arc<Mutex<Vec<Instant>>>,
}

impl Server {
    async fn start(script: Vec<Step>) -> Self {
        Self::on(TcpListener::bind("127.0.0.1:0").await.expect("bind"), script)
    }

    fn on(listener: TcpListener, script: Vec<Step>) -> Self {
        let url = format!("http://{}/", listener.local_addr().expect("addr"));
        let starts = Arc::new(Mutex::new(Vec::new()));
        let script = Arc::new(Mutex::new(VecDeque::from(script)));
        let (seen, steps) = (starts.clone(), script.clone());
        tokio::spawn(async move {
            loop {
                let Ok((stream, _)) = listener.accept().await else {
                    return;
                };
                tokio::spawn(serve(stream, seen.clone(), steps.clone()));
            }
        });
        Self { url, starts }
    }

    fn starts(&self) -> Vec<Instant> {
        self.starts.lock().unwrap().clone()
    }
}

async fn serve(stream: TcpStream, starts: Arc<Mutex<Vec<Instant>>>, script: Arc<Mutex<VecDeque<Step>>>) {
    let mut reader = BufReader::new(stream);
    loop {
        let mut length = 0usize;
        let mut line = String::new();
        if reader.read_line(&mut line).await.unwrap_or(0) == 0 {
            return;
        }
        loop {
            line.clear();
            if reader.read_line(&mut line).await.unwrap_or(0) == 0 {
                return;
            }
            if line == "\r\n" {
                break;
            }
            if let Some((name, value)) = line.split_once(':') {
                if name.eq_ignore_ascii_case("content-length") {
                    length = value.trim().parse().unwrap_or(0);
                }
            }
        }
        let mut body = vec![0; length];
        if reader.read_exact(&mut body).await.is_err() {
            return;
        }
        starts.lock().unwrap().push(Instant::now());
        let (status, headers) = script.lock().unwrap().pop_front().unwrap_or((200, vec![]));
        let mut reply = format!("HTTP/1.1 {status} X\r\nContent-Length: 2\r\n");
        for (name, value) in headers {
            reply.push_str(&format!("{name}: {value}\r\n"));
        }
        reply.push_str("\r\nok");
        if reader.get_mut().write_all(reply.as_bytes()).await.is_err() {
            return;
        }
    }
}

fn retry_after(value: &str) -> Vec<(&'static str, String)> {
    vec![("Retry-After", value.to_string())]
}

async fn get(http: &HttpClient, url: &str) -> u16 {
    http.request("GET", url, RequestOptions::new())
        .await
        .expect("reply")
        .status
}

#[tokio::test]
async fn ten_calls_at_rate_5_take_at_least_1_8_s() {
    let server = Server::start(vec![]).await;
    let http = HttpClient::default().with_rate(Some(5.0));
    let began = Instant::now();
    for _ in 0..10 {
        assert_eq!(get(&http, &server.url).await, 200);
    }
    let elapsed = began.elapsed();
    assert!(elapsed >= Duration::from_millis(1800), "{elapsed:?}");
    assert_eq!(server.starts().len(), 10);
}

#[tokio::test]
async fn concurrent_callers_and_clones_share_the_pace() {
    let server = Server::start(vec![]).await;
    let http = HttpClient::default().with_rate(Some(5.0));
    let began = Instant::now();
    let calls = (0..10).map(|_| {
        let (http, url) = (http.clone(), server.url.clone());
        tokio::spawn(async move { get(&http, &url).await })
    });
    for call in calls {
        assert_eq!(call.await.unwrap(), 200);
    }
    assert!(began.elapsed() >= Duration::from_millis(1800));
}

#[tokio::test]
async fn a_fractional_rate_spaces_by_its_inverse() {
    let server = Server::start(vec![]).await;
    let http = HttpClient::default().with_rate(Some(2.5));
    let began = Instant::now();
    for _ in 0..3 {
        get(&http, &server.url).await;
    }
    assert!(began.elapsed() >= Duration::from_millis(800));
}

#[tokio::test]
async fn no_rate_sends_at_once() {
    let server = Server::start(vec![]).await;
    let http = HttpClient::default().with_rate(None);
    let began = Instant::now();
    for _ in 0..10 {
        get(&http, &server.url).await;
    }
    assert!(began.elapsed() < Duration::from_secs(1));
}

#[test]
fn a_rate_that_is_not_positive_and_finite_panics() {
    for rate in [0.0, -1.0, f64::INFINITY, f64::NAN] {
        let refused = std::panic::catch_unwind(|| HttpClient::default().with_rate(Some(rate)));
        assert!(refused.is_err(), "rate {rate} was accepted");
    }
}

#[tokio::test]
async fn status_503_then_200_succeeds_with_retry() {
    let server = Server::start(vec![(503, vec![]), (200, vec![])]).await;
    let http = HttpClient::default().with_retry(true);
    assert_eq!(get(&http, &server.url).await, 200);
    assert_eq!(server.starts().len(), 2);
}

#[tokio::test]
async fn status_503_then_200_fails_without_retry() {
    let server = Server::start(vec![(503, vec![]), (200, vec![])]).await;
    let http = HttpClient::default().with_retry(false);
    assert_eq!(get(&http, &server.url).await, 503);
    assert_eq!(server.starts().len(), 1);
}

#[tokio::test]
async fn status_429_is_retried_after_its_retry_after_seconds_body_and_all() {
    let server = Server::start(vec![(429, retry_after("1"))]).await;
    let http = HttpClient::default().with_retry(true);
    let reply = http
        .request("POST", &server.url, RequestOptions::new().json(json!({"a": 1})))
        .await
        .unwrap();
    assert_eq!(reply.status, 200);
    let starts = server.starts();
    assert!(starts[1] - starts[0] >= Duration::from_millis(950), "{starts:?}");
}

#[tokio::test]
async fn retry_after_as_an_http_date() {
    let when = (chrono::Utc::now() + chrono::Duration::seconds(2))
        .format("%a, %d %b %Y %H:%M:%S GMT")
        .to_string();
    let remaining = chrono::DateTime::parse_from_rfc2822(&when)
        .unwrap()
        .with_timezone(&chrono::Utc)
        - chrono::Utc::now();
    let deadline = Instant::now() + remaining.to_std().unwrap_or(Duration::ZERO);
    let server = Server::start(vec![(503, retry_after(&when))]).await;
    let http = HttpClient::default().with_retry(true);
    assert_eq!(get(&http, &server.url).await, 200);
    // Client/TLS setup can consume part of the wait. The server's absolute deadline
    // is the contract, rather than a fixed interval measured after setup.
    let starts = server.starts();
    assert!(starts[1] + Duration::from_millis(50) >= deadline, "{starts:?}");
}

#[tokio::test]
async fn a_retry_after_over_the_cap_returns_the_reply() {
    let server = Server::start(vec![(503, retry_after("3600"))]).await;
    let http = HttpClient::default().with_retry(true);
    assert_eq!(get(&http, &server.url).await, 503);
    assert_eq!(server.starts().len(), 1);
}

#[tokio::test]
async fn attempts_are_bounded() {
    let script = (0..=RETRY_ATTEMPTS).map(|_| (503, retry_after("0"))).collect();
    let server = Server::start(script).await;
    let http = HttpClient::default().with_retry(true);
    assert_eq!(get(&http, &server.url).await, 503);
    assert_eq!(server.starts().len(), RETRY_ATTEMPTS as usize);
}

#[tokio::test]
async fn other_statuses_are_not_retried() {
    for status in [500, 502, 504, 401, 404] {
        let server = Server::start(vec![(status, retry_after("0"))]).await;
        let http = HttpClient::default().with_retry(true);
        assert_eq!(get(&http, &server.url).await, status);
        assert_eq!(server.starts().len(), 1, "{status}");
    }
}

#[tokio::test]
async fn every_attempt_is_recorded() {
    let server = Server::start(vec![(503, retry_after("0"))]).await;
    let http = HttpClient::default().with_retry(true);
    let recording = http.recording();
    get(&http, &server.url).await;
    let statuses: Vec<u16> = recording.exchanges().iter().map(|x| x.response.status).collect();
    assert_eq!(statuses, [503, 200]);
}

#[tokio::test]
async fn retries_are_paced() {
    let server = Server::start(vec![(503, retry_after("0"))]).await;
    let http = HttpClient::default().with_rate(Some(2.0)).with_retry(true);
    let began = Instant::now();
    get(&http, &server.url).await;
    // Timed at the caller: pacing spaces the sends, and a late first arrival shortens the
    // gap the server sees.
    assert!(began.elapsed() >= Duration::from_millis(490));
    assert_eq!(server.starts().len(), 2);
}

async fn free_port() -> std::net::SocketAddr {
    TcpListener::bind("127.0.0.1:0").await.unwrap().local_addr().unwrap()
}

#[tokio::test]
async fn a_refused_connection_is_retried() {
    let address = free_port().await;
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(200)).await;
        let server = Server::on(TcpListener::bind(address).await.expect("rebind"), vec![]);
        tokio::time::sleep(Duration::from_secs(5)).await;
        drop(server);
    });
    let http = HttpClient::default().with_retry(true);
    assert_eq!(get(&http, &format!("http://{address}/")).await, 200);
}

#[tokio::test]
async fn a_refused_connection_is_a_network_error_without_retry() {
    let address = free_port().await;
    let error = HttpClient::default()
        .request("GET", &format!("http://{address}/"), RequestOptions::new())
        .await
        .unwrap_err();
    assert!(matches!(error, Error::Network(_)), "{error:?}");
}

#[tokio::test]
async fn prepares_a_fresh_signed_body_after_every_pace_and_retry() {
    let server = Server::start(vec![(503, retry_after("1"))]).await;
    let http = HttpClient::default().with_rate(Some(5.0)).with_retry(true);
    let recording = http.recording();
    let mut stamps = Vec::new();
    let response = http
        .request_prepared("POST", &server.url, || {
            stamps.push(Instant::now());
            Ok(RequestOptions::new()
                .header("X-Nonce", stamps.len().to_string())
                .body(format!("nonce={}", stamps.len())))
        })
        .await
        .unwrap();
    assert_eq!(response.status, 200);
    assert_eq!(stamps.len(), 2);
    assert!(stamps[1].duration_since(stamps[0]) >= Duration::from_secs(1));
    for (start, stamp) in server.starts().iter().zip(stamps.iter()) {
        assert!(start.duration_since(*stamp) < Duration::from_millis(200));
    }
    for (i, exchange) in recording.exchanges().iter().enumerate() {
        assert_eq!(exchange.request.headers["X-Nonce"], (i + 1).to_string());
        assert_eq!(
            exchange.request.body.as_ref().unwrap().as_ref(),
            format!("nonce={}", i + 1).as_bytes()
        );
    }
}

#[tokio::test]
async fn post_is_not_repeated_after_a_redirect_connection_failure() {
    for status in [303, 307] {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let target = format!("http://{}/", listener.local_addr().unwrap());
        drop(listener);
        let server = Server::start(vec![(status, vec![("Location", target)])]).await;
        let result = HttpClient::default()
            .with_retry(true)
            .request("POST", &server.url, RequestOptions::new().body("order"))
            .await;
        assert!(result.is_err());
        assert_eq!(server.starts().len(), 1);
    }
}

#[tokio::test]
async fn post_is_not_repeated_after_a_redirect_to_503() {
    let target = Server::start(vec![(503, retry_after("0"))]).await;
    let origin = Server::start(vec![(303, vec![("Location", target.url.clone())])]).await;
    let reply = HttpClient::default()
        .with_retry(true)
        .request("POST", &origin.url, RequestOptions::new().body("order"))
        .await
        .unwrap();
    assert_eq!(reply.status, 503);
    assert_eq!(origin.starts().len(), 1);
}

#[tokio::test]
async fn queued_requests_are_prepared_only_at_their_pacing_turn() {
    let server = Server::start(vec![]).await;
    let http = HttpClient::default().with_rate(Some(5.0));
    let stamps = Arc::new(Mutex::new(Vec::new()));
    let mut tasks = Vec::new();
    for _ in 0..3 {
        let http = http.clone();
        let url = server.url.clone();
        let stamps = stamps.clone();
        tasks.push(tokio::spawn(async move {
            http.request_prepared("POST", &url, || {
                stamps.lock().unwrap().push(Instant::now());
                Ok(RequestOptions::new())
            })
            .await
            .unwrap();
        }));
    }
    for task in tasks {
        task.await.unwrap();
    }
    let stamps = stamps.lock().unwrap();
    assert_eq!(stamps.len(), 3);
    for pair in stamps.windows(2) {
        assert!(pair[1] - pair[0] >= Duration::from_millis(190));
    }
}

/// A call the runtime refuses before sending (a malformed URL) should not wait in the
/// pacer, nor push back the next caller.
#[tokio::test]
async fn a_request_refused_before_sending_does_not_spend_a_pace_slot() {
    let server = Server::start(vec![]).await;
    let url = server.url;
    let http = HttpClient::default().with_rate(Some(1.0));
    http.request("GET", &url, RequestOptions::new()).await.unwrap();
    let began = Instant::now();
    let error = http
        .request("GET", "not a url", RequestOptions::new())
        .await
        .unwrap_err();
    let refused_after = began.elapsed();
    http.request("GET", &url, RequestOptions::new()).await.unwrap();
    let next_after = began.elapsed();
    println!("refused after {refused_after:?} ({error}); next sent after {next_after:?}");
    assert!(
        refused_after < Duration::from_millis(100),
        "refusal waited {refused_after:?}"
    );
    assert!(
        next_after < Duration::from_millis(1200),
        "next call waited {next_after:?}"
    );
}

/// Same for an unknown method.
#[tokio::test]
async fn a_bad_method_does_not_wait_in_the_pacer() {
    let server = Server::start(vec![]).await;
    let url = server.url;
    let http = HttpClient::default().with_rate(Some(1.0));
    http.request("GET", &url, RequestOptions::new()).await.unwrap();
    let began = Instant::now();
    let _ = http.request("GE T", &url, RequestOptions::new()).await.unwrap_err();
    let waited = began.elapsed();
    println!("bad method refused after {waited:?}");
    assert!(waited < Duration::from_millis(100), "refusal waited {waited:?}");
}
