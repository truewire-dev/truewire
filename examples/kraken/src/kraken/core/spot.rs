//! The Spot REST transport: unsigned GETs for public endpoints, signed POSTs for private
//! ones (form-urlencoded, or JSON for the two paths Kraken rejects a form body on), and
//! the `{error, result}` envelope unwrapped.

use async_trait::async_trait;
use base64::Engine;
use truewire_core::http::{query_from, RequestOptions, Response};
use truewire_core::serde_json::{Map, Value};
use truewire_core::{Error, HttpCall, HttpClient, HttpEndpoint, Result};

use super::auth::{sign, Credentials, Nonce};
use super::envelope::unwrap;
use crate::meta::SpotMeta;

pub const SPOT_API_URL: &str = "https://api.kraken.com";

/// Private endpoints Kraken rejects a form-urlencoded body on; they are sent as JSON.
pub const JSON_BODY_PATHS: &[&str] = &["/0/private/AddOrderBatch", "/0/private/CancelOrderBatch"];

const WS_TOKEN_PATH: &str = "/0/private/GetWebSocketsToken";

/// The one private path whose reply is a zip archive rather than the JSON envelope.
pub const EXPORT_PATH: &str = "/0/private/RetrieveExport";

/// The transport every `spot.*` endpoint calls.
#[derive(Debug)]
pub struct SpotCore {
    base_url: String,
    credentials: Option<Credentials>,
    http: HttpClient,
    nonce: Nonce,
}

impl SpotCore {
    pub fn new(
        base_url: Option<String>,
        credentials: Option<Credentials>,
        http: Option<HttpClient>,
    ) -> Self {
        Self {
            base_url: base_url
                .unwrap_or_else(|| SPOT_API_URL.to_string())
                .trim_end_matches('/')
                .to_string(),
            credentials,
            http: http.unwrap_or_else(|| {
                HttpClient::default()
                    .with_rate(crate::Kraken::RATE)
                    .with_retry(crate::Kraken::RETRY)
            }),
            nonce: Nonce::default(),
        }
    }

    /// The WebSocket token the private socket sends with every request, and its lifetime in seconds.
    pub async fn ws_token(&self) -> Result<(String, u64)> {
        let result = self
            .signed(WS_TOKEN_PATH, Map::new(), RequestOptions::new())
            .await?;
        let token = result
            .get("token")
            .and_then(Value::as_str)
            .ok_or_else(|| Error::validation("no `token` in GetWebSocketsToken"))?;
        let expires = result.get("expires").and_then(Value::as_u64).unwrap_or(900);
        Ok((token.to_string(), expires))
    }

    async fn public(
        &self,
        path: &str,
        values: Map<String, Value>,
        options: RequestOptions,
    ) -> Result<Value> {
        let url = format!("{}{path}", self.base_url);
        let response = self
            .http
            .request("GET", &url, options.query(query_from(values)))
            .await?;
        unwrap(response.status, &response.text())
    }

    /// A finished export report: the zip archive's bytes, which no JSON envelope wraps. It
    /// backs the hand-written `spot.account.retrieve_export`. A JSON reply is an error
    /// envelope and is raised as one.
    pub async fn retrieve_export(&self, id: &str, options: RequestOptions) -> Result<Vec<u8>> {
        let mut values = Map::new();
        values.insert("id".to_string(), Value::from(id));
        let response = self.send(EXPORT_PATH, values, options).await?;
        if response.text().trim_start().starts_with('{') || !response.is_success() {
            unwrap(response.status, &response.text())?;
        }
        Ok(response.body.to_vec())
    }

    async fn signed(
        &self,
        path: &str,
        values: Map<String, Value>,
        options: RequestOptions,
    ) -> Result<Value> {
        let response = self.send(path, values, options).await?;
        unwrap(response.status, &response.text())
    }

    /// Sign and send a private POST, returning the reply as it came. The body is encoded
    /// once, and that string is both what the signature covers and what is sent.
    async fn send(
        &self,
        path: &str,
        values: Map<String, Value>,
        options: RequestOptions,
    ) -> Result<Response> {
        let Some(credentials) = &self.credentials else {
            return Err(Error::auth(
                "No credentials: this client can only call public endpoints.",
            ));
        };
        let url = format!("{}{path}", self.base_url);
        self.http
            .request_prepared("POST", &url, || {
                let nonce = self.nonce.next();
                let json = JSON_BODY_PATHS.contains(&path);
                let body = if json {
                    let mut object = Map::new();
                    object.insert("nonce".to_string(), Value::from(nonce));
                    object.extend(values.clone());
                    Value::Object(object).to_string()
                } else {
                    let mut pairs = vec![("nonce".to_string(), Value::from(nonce))];
                    pairs.extend(values.clone());
                    form_encode(query_from(pairs))
                };
                let options = options
                    .clone()
                    .header("API-Key", credentials.api_key.clone())
                    .header(
                        "API-Sign",
                        sign(path, nonce, &body, &credentials.private_key)?,
                    )
                    .header(
                        "Content-Type",
                        if json {
                            "application/json"
                        } else {
                            "application/x-www-form-urlencoded"
                        },
                    )
                    .body(body);
                Ok(options)
            })
            .await
    }
}

#[async_trait]
impl HttpEndpoint<SpotMeta> for SpotCore {
    async fn request(&self, call: HttpCall<'_, SpotMeta>) -> Result<Value> {
        let values = match call.request {
            Some(Value::Object(values)) => values,
            Some(other) => {
                return Err(Error::logic(format!(
                    "{}: a request must be an object, not {other}",
                    call.path
                )))
            }
            None => Map::new(),
        };
        let mut options = RequestOptions::new();
        if let Some(timeout) = call.options.timeout {
            options = options.timeout(timeout);
        }
        if call.path == EXPORT_PATH {
            // The contract carries JSON values: the archive crosses it as base64, which the
            // hand-written `Account::retrieve_export` decodes.
            let id = values.get("id").and_then(Value::as_str).unwrap_or_default();
            let archive = self.retrieve_export(id, options).await?;
            return Ok(Value::String(
                base64::engine::general_purpose::STANDARD.encode(archive),
            ));
        }
        if call.meta.signed == Some(true) {
            self.signed(call.path, values, options).await
        } else {
            self.public(call.path, values, options).await
        }
    }
}

/// `application/x-www-form-urlencoded`, as `URLSearchParams` writes it.
fn form_encode(pairs: impl IntoIterator<Item = (String, Option<String>)>) -> String {
    fn escape(text: &str) -> String {
        let mut out = String::with_capacity(text.len());
        for byte in text.bytes() {
            match byte {
                b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'*' | b'-' | b'.' | b'_' => {
                    out.push(byte as char)
                }
                b' ' => out.push('+'),
                _ => out.push_str(&format!("%{byte:02X}")),
            }
        }
        out
    }
    pairs
        .into_iter()
        .filter_map(|(name, value)| {
            value.map(|value| format!("{}={}", escape(&name), escape(&value)))
        })
        .collect::<Vec<_>>()
        .join("&")
}
