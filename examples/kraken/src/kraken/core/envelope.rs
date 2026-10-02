//! Kraken Spot REST's `{error, result}` envelope, and its errors mapped onto
//! `truewire_core::Error` by Kraken's own error categories.
//!
//! See <https://docs.kraken.com/api/docs/guides/global-errors>.

use truewire_core::serde_json::Value;
use truewire_core::{Error, Result};

const RATE_LIMITED: &[&str] = &[
    "Rate limit exceeded",
    "Too many requests",
    "Orders limit exceeded",
    "Domain rate limit exceeded",
    "Scheduled orders limit exceeded",
];
const AUTH: &[&str] = &[
    "Invalid key",
    "Invalid signature",
    "Invalid nonce",
    "Permission denied",
    "Temporary lockout",
];
const BAD_REQUEST: &[&str] = &[
    "Invalid price",
    "Tick size check failed",
    "Order minimum not met",
    "Cost minimum not met",
];

/// The error a non-empty `error` array calls for; the first entry decides, as Kraken lists
/// the primary failure first.
pub fn raise_error(errors: &[String]) -> Error {
    let message = errors
        .first()
        .cloned()
        .unwrap_or_else(|| "unknown error".to_string());
    let body = Value::from(errors.to_vec());
    let has = |needles: &[&str]| needles.iter().any(|needle| message.contains(needle));
    let category = message.split(':').next().unwrap_or_default();
    let error = if has(RATE_LIMITED) {
        Error::rate_limited(message)
    } else if has(AUTH) || matches!(category, "EAPI" | "EAuth" | "EAccount") {
        Error::auth(message)
    } else if has(BAD_REQUEST) || matches!(category, "EGeneral" | "ETrade" | "EFunding") {
        Error::bad_request(message)
    } else {
        Error::api(message)
    };
    error.with_body(body)
}

/// A non-2xx status, rare for Kraken (it answers 200 to most logical errors).
fn http_status(status: u16, text: &str) -> Error {
    let body: Value =
        truewire_core::parse_json(text).unwrap_or_else(|_| Value::String(text.to_string()));
    let message = format!(
        "HTTP {status}: {}",
        text.chars().take(200).collect::<String>()
    );
    let error = match status {
        401 | 403 => Error::auth(message),
        429 => Error::rate_limited(message),
        400..=499 => Error::bad_request(message),
        _ => Error::api(message),
    };
    error.with_status(status).with_body(body)
}

/// The envelope's `result`, or the error its `error` array or status calls for.
pub fn unwrap(status: u16, text: &str) -> Result<Value> {
    if status >= 400 {
        return Err(http_status(status, text));
    }
    let envelope: Value = truewire_core::parse_json(text)?;
    let Some(errors) = envelope.get("error").and_then(Value::as_array) else {
        return Err(Error::validation("not a Kraken envelope: no `error` array"));
    };
    if !errors.is_empty() {
        let errors: Vec<String> = errors
            .iter()
            .map(|e| {
                e.as_str()
                    .map(str::to_string)
                    .unwrap_or_else(|| e.to_string())
            })
            .collect();
        return Err(raise_error(&errors));
    }
    Ok(envelope.get("result").cloned().unwrap_or(Value::Null))
}
