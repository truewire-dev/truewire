//! Hand-written core for the github client: transport, auth and errors.
//!
//! Every generated struct holds its core as an `Arc<dyn HttpEndpoint<DefaultMeta>>` from
//! `truewire_core` and calls `request` on it; this is the one place that knows how to reach
//! the upstream API. Adapt it (base URL, headers, signing, envelope unwrapping, error
//! mapping) to your API; the generated code never changes when you do.

use async_trait::async_trait;
use truewire_core::http::{query_from, RequestOptions, Response};
use truewire_core::serde_json::Value;
use truewire_core::{Error, HttpCall, HttpClient, HttpEndpoint, Result};

use crate::meta::DefaultMeta;

const DEFAULT_BASE_URL: &str = "https://api.github.com";

/// What `Core::new` takes.
#[derive(Debug, Clone, Default)]
pub struct CoreOptions {
    /// Defaults to `https://api.github.com`; point it at `truewire mock` in tests.
    pub base_url: Option<String>,
    /// A token, sent as `Authorization: Bearer` (public endpoints accept it too, and it
    /// raises the rate limit).
    pub token: Option<String>,
    /// The HTTP client to send through; one is made when omitted.
    pub http: Option<HttpClient>,
}

/// The shared HTTP transport: base URL, GitHub's headers, the optional token and error
/// mapping.
#[derive(Debug)]
pub struct Core {
    base_url: String,
    token: Option<String>,
    http: HttpClient,
}

impl Core {
    pub fn new(options: CoreOptions) -> Self {
        let base_url = options
            .base_url
            .unwrap_or_else(|| DEFAULT_BASE_URL.to_string());
        Self {
            base_url: base_url.trim_end_matches('/').to_string(),
            token: options.token,
            http: options.http.unwrap_or_default(),
        }
    }

    /// Headers for one call: GitHub's media type, API version and a User-Agent, plus the
    /// token when one was given.
    fn headers(&self, _meta: &DefaultMeta) -> Vec<(String, String)> {
        let mut headers = vec![
            (
                "Accept".to_string(),
                "application/vnd.github+json".to_string(),
            ),
            ("X-GitHub-Api-Version".to_string(), "2022-11-28".to_string()),
            (
                "User-Agent".to_string(),
                "truewire-example-github".to_string(),
            ),
        ];
        if let Some(token) = &self.token {
            headers.push(("Authorization".to_string(), format!("Bearer {token}")));
        }
        headers
    }
}

#[async_trait]
impl HttpEndpoint<DefaultMeta> for Core {
    /// Send one request: fill the path's `{placeholders}` from the dumped request, send
    /// the rest as the query (or as a JSON body for POST/PUT/PATCH), and return the
    /// decoded reply, a non-2xx status mapped to the `Error::Api` kind it calls for.
    async fn request(&self, call: HttpCall<'_, DefaultMeta>) -> Result<Value> {
        let mut path = call.path.to_string();
        let mut params = Vec::new();
        if let Some(Value::Object(fields)) = call.request {
            for (name, value) in fields {
                let placeholder = format!("{{{name}}}");
                if path.contains(&placeholder) {
                    path = path.replace(&placeholder, &percent_encode(&plain(&value)));
                } else {
                    params.push((name, value));
                }
            }
        }
        let method = call.method.unwrap_or("GET").to_uppercase();
        let with_body = matches!(method.as_str(), "POST" | "PUT" | "PATCH");
        let url = format!("{}/{}", self.base_url, path.trim_start_matches('/'));
        let mut options = RequestOptions::new().headers(self.headers(call.meta));
        if let Some(timeout) = call.options.timeout {
            options = options.timeout(timeout);
        }
        options = if with_body {
            options.json(Value::Object(params.into_iter().collect()))
        } else {
            options.query(query_from(params))
        };
        let response = self.http.request(&method, &url, options).await?;
        if response.status >= 400 {
            return Err(map_error(&method, &path, &response));
        }
        response.json()
    }
}

/// A dumped value as the text a path placeholder takes: a string as it is, anything else
/// as JSON.
fn plain(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        other => other.to_string(),
    }
}

/// `encodeURIComponent` for one path segment.
fn percent_encode(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for byte in text.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(byte as char)
            }
            _ => out.push_str(&format!("%{byte:02X}")),
        }
    }
    out
}

/// A non-2xx reply as the `Error::Api` kind its status calls for.
fn map_error(method: &str, path: &str, response: &Response) -> Error {
    let text = response.text();
    let body = response
        .json()
        .unwrap_or_else(|_| Value::String(text.clone()));
    let detail = body
        .get("message")
        .and_then(Value::as_str)
        .map(str::to_string)
        .unwrap_or_else(|| text.chars().take(200).collect());
    let message = format!("{method} {path}: HTTP {}: {detail}", response.status);
    let rate_limited = response.status == 429
        || (response.status == 403 && response.header("x-ratelimit-remaining") == Some("0"));
    let error = if rate_limited {
        Error::rate_limited(message)
    } else if matches!(response.status, 401 | 403) {
        Error::auth(message)
    } else if matches!(response.status, 400 | 404 | 422) {
        Error::bad_request(message)
    } else {
        Error::api(message)
    };
    error.with_status(response.status).with_body(body)
}
