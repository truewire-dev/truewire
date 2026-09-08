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

type Hooks = Arc<Mutex<HashMap<u64, ExchangeHook>>>;

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
/// own, so nothing to open or close. Cloning shares the client and its hooks.
#[derive(Clone)]
pub struct HttpClient {
    client: reqwest::Client,
    timeout: Option<Duration>,
    hooks: Hooks,
    next_hook: Arc<Mutex<u64>>,
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
    pub async fn request(&self, method: &str, url: &str, options: RequestOptions) -> Result<Response> {
        let method = Method::from_bytes(method.to_ascii_uppercase().as_bytes())
            .map_err(|e| Error::logic(format!("Not an HTTP method: {method:?}")).with_source(e))?;
        let mut target = Url::parse(url).map_err(|e| Error::logic(format!("Not a URL: {url:?}")).with_source(e))?;
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
        let describe = || format!("{} {}", method, target);
        let reply = builder
            .send()
            .await
            .map_err(|e| Error::network(format!("Error sending request to {}", describe())).with_source(e))?;
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
                    method,
                    url: target,
                    headers,
                    body,
                },
                response: response.clone(),
            };
            for hook in hooks {
                hook(&exchange);
            }
        }
        Ok(response)
    }
}
