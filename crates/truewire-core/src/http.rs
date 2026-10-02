//! HTTP transport over `reqwest`.
//!
//! One [`HttpClient`] per generated client. It does two things `reqwest` alone does not:
//! every failure to reach the server becomes an [`Error::Network`], and every exchange can
//! be observed at the wire level (the request and the response, before the client core
//! unwraps an envelope or maps an error), which is what `truewire capture` needs to record
//! examples. The reply is returned whatever its status; mapping a non-2xx reply to an
//! [`ApiError`](crate::errors::ApiError) is the core's business.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::sync::Mutex as AsyncMutex;
use tokio::time::Instant;

use bytes::Bytes;
use reqwest::header::{HeaderMap, HeaderName, HeaderValue, CONTENT_TYPE};
use reqwest::Method;
use serde_json::Value;
use url::Url;

use crate::errors::{Error, Result};

/// A request exactly as it went on the wire.
#[derive(Debug, Clone)]
pub struct Request {
    pub method: Method,
    /// The full URL, query string included.
    pub url: Url,
    pub headers: HeaderMap,
    /// The body sent, if any.
    pub body: Option<Bytes>,
}

/// A reply exactly as it came off the wire, body fully read.
#[derive(Debug, Clone)]
pub struct Response {
    pub status: u16,
    pub headers: HeaderMap,
    pub body: Bytes,
    /// The URL the reply came from (after redirects).
    pub url: Url,
}

impl Response {
    /// Whether the status is 2xx.
    pub fn is_success(&self) -> bool {
        (200..300).contains(&self.status)
    }

    /// The body as text (lossily, when it is not UTF-8).
    pub fn text(&self) -> String {
        String::from_utf8_lossy(&self.body).into_owned()
    }

    /// The body decoded as JSON; a syntax error is a `ValidationError`.
    pub fn json(&self) -> Result<Value> {
        crate::validation::parse_slice(&self.body)
    }

    /// One header's value as text, if present.
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers.get(name).and_then(|v| v.to_str().ok())
    }
}

/// One request and the response it got, exactly as they crossed the wire.
#[derive(Debug, Clone)]
pub struct Exchange {
    pub request: Request,
    pub response: Response,
}

/// A query parameter value: `None` entries are skipped.
pub type Query = Vec<(String, Option<String>)>;

/// One query value in [`RequestOptions::query`]: what a dumped request field holds.
pub fn query_value(value: &Value) -> Option<String> {
    match value {
        Value::Null => None,
        Value::String(s) => Some(s.clone()),
        Value::Bool(b) => Some(b.to_string()),
        Value::Number(n) => Some(n.to_string()),
        // A nested value travels as JSON text, as the example cores send it.
        other => Some(other.to_string()),
    }
}

/// A query from a dumped request object: every scalar field rendered, `null` skipped.
pub fn query_from(fields: impl IntoIterator<Item = (String, Value)>) -> Query {
    fields
        .into_iter()
        .map(|(name, value)| (name, query_value(&value)))
        .collect()
}

/// What one request carries beside its method and URL.
#[derive(Debug, Clone, Default)]
pub struct RequestOptions {
    /// Query parameters appended to the URL; `None` values are skipped.
    pub query: Query,
    pub headers: Vec<(String, String)>,
    /// Raw body, sent as-is.
    pub body: Option<Bytes>,
    /// JSON body: serialized and sent as `application/json`. Takes precedence over `body`.
    pub json: Option<Value>,
    /// Time before the request is abandoned with a `NetworkError`; overrides the client default.
    pub timeout: Option<Duration>,
}

impl RequestOptions {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn query(mut self, query: Query) -> Self {
        self.query = query;
        self
    }

    pub fn header(mut self, name: impl Into<String>, value: impl Into<String>) -> Self {
        self.headers.push((name.into(), value.into()));
        self
    }

    pub fn headers(mut self, headers: impl IntoIterator<Item = (String, String)>) -> Self {
        self.headers.extend(headers);
        self
    }

    pub fn body(mut self, body: impl Into<Bytes>) -> Self {
        self.body = Some(body.into());
        self
    }

    pub fn json(mut self, json: Value) -> Self {
        self.json = Some(json);
        self
    }

    pub fn timeout(mut self, timeout: Duration) -> Self {
        self.timeout = Some(timeout);
        self
    }
}

/// A permanent exchange hook; see [`HttpClient::recording`] for a scoped one.
pub type ExchangeHook = Arc<dyn Fn(&Exchange) + Send + Sync>;

/// How an [`HttpClient`] is built.
#[derive(Clone, Default)]
pub struct HttpClientOptions {
    /// The `reqwest` client to send through (an agent, a proxy configuration, a test
    /// double's base); one with `reqwest`'s defaults when omitted.
    pub client: Option<reqwest::Client>,
    /// Default per-request timeout; none when omitted.
    pub timeout: Option<Duration>,
    /// Permanent exchange hook.
    pub on_exchange: Option<ExchangeHook>,
}

impl HttpClientOptions {
    /// Send every request through the HTTP(S) proxy at `url` (`http://host:3128`,
    /// credentials in the userinfo): reqwest tunnels `https://` with `CONNECT` and forwards
    /// `http://` as an absolute-form request. An explicit proxy ignores the environment,
    /// `NO_PROXY` included; `""` leaves the options as they are, and without a proxy
    /// reqwest reads `HTTPS_PROXY`/`HTTP_PROXY`/`ALL_PROXY`/`NO_PROXY` as before. Packages
    /// clause P18: a sandbox or a library caller cannot always set the process environment.
    ///
    /// It builds the [`client`](Self::client), so options that already hold one are an
    /// `Error::Logic`, as is anything but an `http://` or `https://` URL (reqwest would take
    /// `socks5://` here and fail only later: this crate does not enable its `socks`
    /// feature). The message never repeats the URL.
    ///
    /// ```
    /// # use truewire_core::{HttpClient, HttpClientOptions};
    /// let http = HttpClient::new(HttpClientOptions::default().with_proxy("http://127.0.0.1:3128")?);
    /// # Ok::<(), truewire_core::Error>(())
    /// ```
    pub fn with_proxy(mut self, url: &str) -> Result<Self> {
        if url.is_empty() {
            return Ok(self);
        }
        if self.client.is_some() {
            return Err(Error::logic("HttpClientOptions: pass `client` or a proxy, not both"));
        }
        let parsed = Url::parse(url).map_err(|e| Error::logic("`proxy` is not a URL").with_source(e))?;
        if !matches!(parsed.scheme(), "http" | "https") {
            return Err(Error::logic(format!(
                "`proxy` must be an http:// or https:// URL, not {}://",
                parsed.scheme()
            )));
        }
        let proxy =
            reqwest::Proxy::all(parsed).map_err(|e| Error::logic("`proxy` is not a proxy URL").with_source(e))?;
        let client = reqwest::Client::builder()
            .proxy(proxy)
            .build()
            .map_err(|e| Error::logic("Cannot build an HTTP client for `proxy`").with_source(e))?;
        self.client = Some(client);
        Ok(self)
    }
}

type Hooks = Arc<Mutex<HashMap<u64, ExchangeHook>>>;

/// Attempts a request gets with [`HttpClient::with_retry`], the first included.
pub const RETRY_ATTEMPTS: u32 = 3;
/// Statuses retried with [`HttpClient::with_retry`]: the server said it did not handle the request.
pub const RETRY_STATUSES: [u16; 2] = [429, 503];
/// Longest `Retry-After` a retry waits; a longer one returns the reply as is.
pub const RETRY_AFTER_CAP: Duration = Duration::from_secs(30);
/// The wait before the first retry when the server names no `Retry-After`; doubled after.
pub const RETRY_BACKOFF: Duration = Duration::from_millis(500);

/// The reply's `Retry-After` as a wait from now, or `None` when absent or unreadable:
/// delay-seconds (`120`) or an HTTP date, never negative.
pub fn retry_after(headers: &HeaderMap) -> Option<Duration> {
    let value = headers.get(reqwest::header::RETRY_AFTER)?.to_str().ok()?.trim();
    if !value.is_empty() && value.bytes().all(|b| b.is_ascii_digit()) {
        return value.parse().ok().map(Duration::from_secs);
    }
    let when = chrono::DateTime::parse_from_rfc2822(value).ok()?;
    let wait = when.with_timezone(&chrono::Utc) - chrono::Utc::now();
    Some(wait.to_std().unwrap_or(Duration::ZERO))
}

/// A scoped recording: exchanges appended in order until [`stop`](Recording::stop) or drop.
pub struct Recording {
    exchanges: Arc<Mutex<Vec<Exchange>>>,
    hooks: Hooks,
    id: u64,
}

impl Recording {
    /// Every exchange recorded so far, in order.
    pub fn exchanges(&self) -> Vec<Exchange> {
        self.exchanges.lock().expect("recording lock").clone()
    }

    /// The most recent exchange, if any.
    pub fn last(&self) -> Option<Exchange> {
        self.exchanges.lock().expect("recording lock").last().cloned()
    }

    /// How many exchanges were recorded.
    pub fn len(&self) -> usize {
        self.exchanges.lock().expect("recording lock").len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// Stop recording; what was recorded stays readable.
    pub fn stop(&self) {
        self.hooks.lock().expect("hooks lock").remove(&self.id);
    }
}

impl Drop for Recording {
    fn drop(&mut self) {
        self.stop();
    }
}

/// Managed HTTP client over `reqwest`.
///
/// Many concurrent [`request`](Self::request) calls are fine; there is no connection to
/// own, so nothing to open or close. Cloning shares the client, its hooks and its pace.
///
/// [`with_rate`](Self::with_rate) paces the client to a number of requests per second
/// (`[policy].rate`, workspace clause W15): request starts are spaced `1 / rate` seconds
/// apart, shared by every caller of this client and its clones, so ten requests at rate 5
/// span 1.8 s. Without it, each request goes at once.
///
/// [`with_retry`](Self::with_retry) (`[policy].retry`) sends a request again, up to
/// [`RETRY_ATTEMPTS`] in all, when it did not reach the server (a failure to connect,
/// before anything was sent) or the reply is 429 or 503. A retry waits the reply's
/// `Retry-After` (seconds or an HTTP date), else [`RETRY_BACKOFF`] doubling; a
/// `Retry-After` over [`RETRY_AFTER_CAP`] returns the reply instead. Nothing else is
/// retried: not another status, and not a connection lost after the request was sent,
/// which the server may have acted on. Each attempt is paced and recorded like any
/// request. Connection-error retries are limited to safe methods, since reqwest can
/// fail after a redirect has already acted on a POST. Without it, each request is sent once.
#[derive(Clone)]
pub struct HttpClient {
    client: reqwest::Client,
    timeout: Option<Duration>,
    hooks: Hooks,
    next_hook: Arc<Mutex<u64>>,
    rate: Option<f64>,
    retry: bool,
    next_start: Arc<AsyncMutex<Option<Instant>>>,
}

impl Default for HttpClient {
    fn default() -> Self {
        Self::new(HttpClientOptions::default())
    }
}

impl std::fmt::Debug for HttpClient {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("HttpClient")
            .field("timeout", &self.timeout)
            .field("rate", &self.rate)
            .field("retry", &self.retry)
            .finish_non_exhaustive()
    }
}

impl HttpClient {
    pub fn new(options: HttpClientOptions) -> Self {
        let client = Self {
            client: options.client.unwrap_or_default(),
            timeout: options.timeout,
            hooks: Arc::new(Mutex::new(HashMap::new())),
            next_hook: Arc::new(Mutex::new(0)),
            rate: None,
            retry: false,
            next_start: Arc::new(AsyncMutex::new(None)),
        };
        if let Some(hook) = options.on_exchange {
            client.add_hook(hook);
        }
        client
    }

    /// The `reqwest` client underneath.
    pub fn inner(&self) -> &reqwest::Client {
        &self.client
    }

    /// The default per-request timeout.
    pub fn timeout(&self) -> Option<Duration> {
        self.timeout
    }

    /// This client paced to `rate` requests per second (`[policy].rate`), or unpaced for
    /// `None`; see the type's docs. The pace starts afresh, shared only with later clones.
    ///
    /// # Panics
    ///
    /// When `rate` is not a positive, finite number.
    pub fn with_rate(mut self, rate: Option<f64>) -> Self {
        if let Some(rate) = rate {
            assert!(
                rate.is_finite() && rate > 0.0,
                "rate must be a positive number of requests per second; got {rate}"
            );
        }
        self.rate = rate;
        self.next_start = Arc::new(AsyncMutex::new(None));
        self
    }

    /// This client retrying a 429 or 503, or a connection failure for a safe method
    /// (GET, HEAD, OPTIONS, TRACE). Redirected replies are not retried.
    /// (`[policy].retry`); see the type's docs.
    pub fn with_retry(mut self, retry: bool) -> Self {
        self.retry = retry;
        self
    }

    /// Requests per second this client paces to, if any.
    pub fn rate(&self) -> Option<f64> {
        self.rate
    }

    /// Whether this client retries on its own.
    pub fn retry(&self) -> bool {
        self.retry
    }

    /// Wait for this request's start slot: `1 / rate` seconds after the previous one's.
    async fn pace(&self) {
        let Some(rate) = self.rate else { return };
        // Hold the FIFO lock while waiting, and commit only a used start slot.
        // Dropping a queued or sleeping future leaves the next slot available.
        let mut next = self.next_start.lock().await;
        if let Some(start) = *next {
            tokio::time::sleep_until(start).await;
        }
        *next = Some(Instant::now() + Duration::from_secs_f64(1.0 / rate));
    }

    fn add_hook(&self, hook: ExchangeHook) -> u64 {
        let mut next = self.next_hook.lock().expect("hook counter");
        let id = *next;
        *next += 1;
        self.hooks.lock().expect("hooks lock").insert(id, hook);
        id
    }

    /// Record every exchange this client makes until the recording is stopped or dropped,
    /// in order. Recordings may overlap.
    ///
    /// Everything the client sent is in here, in order, not just the call's own request:
    /// a token mint, a refresh, a retry. Pick the exchange you mean by its request, never
    /// by position -- the last one may be another endpoint's, and its body a credential.
    ///
    /// ```ignore
    /// let rec = client.http.recording();
    /// let pet = client.pets.get_pet(request).await?;
    /// let mine = rec.exchanges().into_iter().filter(|x| x.request.url.path().ends_with("/pets/42"));
    /// let status = mine.last().unwrap().response.status;
    /// ```
    pub fn recording(&self) -> Recording {
        let exchanges: Arc<Mutex<Vec<Exchange>>> = Arc::new(Mutex::new(Vec::new()));
        let sink = exchanges.clone();
        let id = self.add_hook(Arc::new(move |exchange: &Exchange| {
            sink.lock().expect("recording lock").push(exchange.clone());
        }));
        Recording {
            exchanges,
            hooks: self.hooks.clone(),
            id,
        }
    }

    /// Send one request; the reply, whatever its status. Failing to get one is a `NetworkError`.
    /// An HTTPS URL without a TLS feature is a `LogicError`, before any network traffic.
    pub async fn request(&self, method: &str, url: &str, options: RequestOptions) -> Result<Response> {
        self.request_prepared(method, url, || Ok(options.clone())).await
    }

    /// Build and sign fresh options after pacing on every attempt, including retries.
    /// Each call returns options whose signature covers the bytes it sends; it runs once
    /// per attempt, after pacing. Errors stop without sending.
    /// This leaves the 0.1 `RequestOptions` struct-literal contract unchanged.
    pub async fn request_prepared<F>(&self, method: &str, url: &str, mut prepare: F) -> Result<Response>
    where
        F: FnMut() -> Result<RequestOptions> + Send,
    {
        let method = Method::from_bytes(method.to_ascii_uppercase().as_bytes())
            .map_err(|e| Error::logic(format!("Not an HTTP method: {method:?}")).with_source(e))?;
        let url = Url::parse(url).map_err(|e| Error::logic(format!("Not a URL: {url:?}")).with_source(e))?;
        // Without TLS, reqwest can forward an HTTPS URL in cleartext to an HTTP proxy.
        // Refuse it before sending, for explicit, environment and prebuilt clients alike.
        #[cfg(not(any(feature = "rustls-tls", feature = "native-tls")))]
        if url.scheme() == "https" {
            return Err(Error::logic(
                "https:// needs TLS: enable truewire-core's `rustls-tls` or `native-tls` feature",
            ));
        }
        for attempt in 1..=RETRY_ATTEMPTS {
            self.pace().await;
            let options = prepare()?;
            let mut target = url.clone();
            {
                let mut pairs = target.query_pairs_mut();
                for (name, value) in &options.query {
                    if let Some(value) = value {
                        pairs.append_pair(name, value);
                    }
                }
            }
            if target.query() == Some("") {
                target.set_query(None);
            }
            let mut headers = HeaderMap::new();
            for (name, value) in &options.headers {
                let name = HeaderName::from_bytes(name.as_bytes())
                    .map_err(|e| Error::logic(format!("Not a header name: {name:?}")).with_source(e))?;
                let value = HeaderValue::from_str(value)
                    .map_err(|e| Error::logic(format!("Not a header value for {name}: {value:?}")).with_source(e))?;
                headers.append(name, value);
            }
            let body = match &options.json {
                Some(json) => {
                    if !headers.contains_key(CONTENT_TYPE) {
                        headers.insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
                    }
                    Some(Bytes::from(serde_json::to_vec(json).map_err(|e| {
                        Error::logic("Cannot serialize JSON body").with_source(e)
                    })?))
                }
                None => options.body.clone(),
            };
            let describe = || format!("{} {}", method, target);
            let last = !self.retry || attempt == RETRY_ATTEMPTS;
            let backoff = RETRY_BACKOFF * 2u32.pow(attempt - 1);
            let mut builder = self
                .client
                .request(method.clone(), target.clone())
                .headers(headers.clone());
            if let Some(body) = &body {
                builder = builder.body(body.clone());
            }
            if let Some(timeout) = options.timeout.or(self.timeout) {
                builder = builder.timeout(timeout);
            }
            let reply = match builder.send().await {
                Ok(reply) => reply,
                // reqwest can follow redirects, including a loop back to the original
                // URL. A connect error alone cannot prove a POST never executed.
                Err(e)
                    if e.is_connect()
                        && !last
                        && matches!(method, Method::GET | Method::HEAD | Method::OPTIONS | Method::TRACE) =>
                {
                    tokio::time::sleep(backoff).await;
                    continue;
                }
                Err(e) => return Err(Error::network(format!("Error sending request to {}", describe())).with_source(e)),
            };
            let status = reply.status().as_u16();
            let reply_headers = reply.headers().clone();
            let reply_url = reply.url().clone();
            let reply_body = reply
                .bytes()
                .await
                .map_err(|e| Error::network(format!("Error reading the reply to {}", describe())).with_source(e))?;
            let response = Response {
                status,
                headers: reply_headers,
                body: reply_body,
                url: reply_url,
            };
            let hooks: Vec<ExchangeHook> = self.hooks.lock().expect("hooks lock").values().cloned().collect();
            if !hooks.is_empty() {
                let exchange = Exchange {
                    request: Request {
                        method: method.clone(),
                        url: target.clone(),
                        headers: headers.clone(),
                        body: body.clone(),
                    },
                    response: response.clone(),
                };
                for hook in hooks {
                    hook(&exchange);
                }
            }
            if last || response.url != target || !RETRY_STATUSES.contains(&status) {
                return Ok(response);
            }
            let delay = retry_after(&response.headers);
            if delay.is_some_and(|delay| delay > RETRY_AFTER_CAP) {
                return Ok(response);
            }
            tokio::time::sleep(delay.unwrap_or(backoff)).await;
        }
        unreachable!("the last attempt returns")
    }
}
