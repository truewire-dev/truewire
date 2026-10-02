//! Kraken Spot's REST authentication -- `API-Key`/`API-Sign` headers over a strictly
//! increasing nonce -- and the WebSocket token the private connection sends instead.

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use hmac::{Hmac, Mac};
use sha2::{Digest, Sha256, Sha512};
use truewire_core::{Error, Result};

/// A Kraken API key pair. The private key is base64 and never leaves the process.
#[derive(Clone)]
pub struct Credentials {
    pub api_key: String,
    pub private_key: String,
}

impl std::fmt::Debug for Credentials {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Credentials")
            .field("api_key", &self.api_key)
            .finish_non_exhaustive()
    }
}

/// The `API-Sign` header value: HMAC-SHA512, keyed by the decoded private key, over the
/// request path followed by `SHA256(nonce + body)`, base64-encoded.
///
/// `encoded_body` is the exact body being sent, `nonce` included: the signature covers the
/// bytes on the wire, not a reconstruction of them.
///
/// See <https://docs.kraken.com/api/docs/guides/spot-rest-auth>.
pub fn sign(path: &str, nonce: u64, encoded_body: &str, private_key: &str) -> Result<String> {
    let key = STANDARD
        .decode(private_key)
        .map_err(|e| Error::auth(format!("the private key is not base64: {e}")))?;
    let digest = Sha256::digest(format!("{nonce}{encoded_body}").as_bytes());
    let mut mac = Hmac::<Sha512>::new_from_slice(&key).map_err(|e| Error::auth(e.to_string()))?;
    mac.update(path.as_bytes());
    mac.update(&digest);
    Ok(STANDARD.encode(mac.finalize().into_bytes()))
}

/// A strictly increasing nonce, as Kraken requires per key: the millisecond clock, bumped
/// by one whenever two calls land in the same millisecond.
#[derive(Debug, Default)]
pub struct Nonce {
    last: std::sync::Mutex<u64>,
}

impl Nonce {
    pub fn next(&self) -> u64 {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_millis() as u64)
            .unwrap_or(0);
        let mut last = self
            .last
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        *last = if now > *last { now } else { *last + 1 };
        *last
    }
}

/// A `GetWebSocketsToken` fetch: the token, and how many seconds it stays valid.
pub type TokenFetch =
    Arc<dyn Fn() -> Pin<Box<dyn Future<Output = Result<(String, u64)>> + Send>> + Send + Sync>;

/// Refresh the WebSocket token this long before its ~900 s lifetime ends.
const REFRESH_BUFFER: Duration = Duration::from_secs(30);

/// The WebSocket token, fetched on first use and refreshed before it expires. Concurrent
/// callers share one fetch.
#[derive(Clone)]
pub struct TokenCache {
    fetch: TokenFetch,
    cached: Arc<tokio::sync::Mutex<Option<(String, Instant)>>>,
}

impl TokenCache {
    pub fn new<F>(fetch: F) -> Self
    where
        F: Fn() -> Pin<Box<dyn Future<Output = Result<(String, u64)>> + Send>>
            + Send
            + Sync
            + 'static,
    {
        Self {
            fetch: Arc::new(fetch),
            cached: Arc::default(),
        }
    }

    pub async fn get(&self) -> Result<String> {
        let mut cached = self.cached.lock().await;
        if let Some((token, until)) = cached.as_ref() {
            if Instant::now() < *until {
                return Ok(token.clone());
            }
        }
        let (token, expires) = (self.fetch)().await?;
        let lifetime = Duration::from_secs(expires).saturating_sub(REFRESH_BUFFER);
        *cached = Some((token.clone(), Instant::now() + lifetime));
        Ok(token)
    }
}
