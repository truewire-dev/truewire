//! Hand-written core for the kraken client: the three transports the generated `Kraken`
//! is built from.
//!
//! `spot_client` is the REST transport ([`SpotCore`], `HttpEndpoint<SpotMeta>`: signing,
//! envelope, errors); `market_client` and `private_client` are the two WebSocket v2
//! connections ([`SocketCore`], `CommandEndpoint + StreamEndpoint`), the private one
//! sending the token it fetches through the REST transport. `streams.private` and
//! `trading_ws` share the private one. Adapt this directory to the API; the generated code
//! never changes when you do.

mod auth;
mod envelope;
mod export;
mod socket;
mod spot;

use std::sync::Arc;

use truewire_core::{Error, HttpClient, HttpClientOptions, Result};

pub use auth::{sign, Credentials, Nonce, TokenCache};
pub use envelope::{raise_error, unwrap};
pub use socket::{KrakenDialect, SocketCore, RAW_METHODS, SPOT_WS_AUTH_URL, SPOT_WS_URL};
pub use spot::{SpotCore, EXPORT_PATH, JSON_BODY_PATHS, SPOT_API_URL};

/// What [`Transports::new`] and `Kraken::new` take. Its `Debug` leaves out the private key
/// and the proxy URL, which can carry a password.
#[derive(Clone, Default)]
pub struct CoreOptions {
    /// Defaults to `https://api.kraken.com`.
    pub base_url: Option<String>,
    /// Defaults to `wss://ws.kraken.com/v2`.
    pub ws_url: Option<String>,
    /// Defaults to `wss://ws-auth.kraken.com/v2`.
    pub ws_auth_url: Option<String>,
    /// The key pair; without one the client can only call public endpoints and channels.
    pub credentials: Option<Credentials>,
    /// The HTTP client to send through; one paced and retrying as `Kraken::RATE` and
    /// `Kraken::RETRY` say when omitted.
    pub http: Option<HttpClient>,
    /// An HTTP proxy URL for the REST calls and both WebSocket connections alike (packages
    /// clause P18). Not together with `http`.
    pub proxy: Option<String>,
}

impl std::fmt::Debug for CoreOptions {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("CoreOptions")
            .field("base_url", &self.base_url)
            .field("ws_url", &self.ws_url)
            .field("ws_auth_url", &self.ws_auth_url)
            .field("credentials", &self.credentials)
            .field("http", &self.http)
            .field("proxy", &self.proxy.as_ref().map(|_| "<redacted>"))
            .finish_non_exhaustive()
    }
}

/// The three transports, built together and shared with the client, so a caller can
/// close the sockets it opened.
#[derive(Clone)]
pub struct Transports {
    pub spot_client: Arc<SpotCore>,
    pub market_client: Arc<SocketCore>,
    pub private_client: Arc<SocketCore>,
}

impl Transports {
    /// # Panics
    ///
    /// When `options.proxy` is not an `http://` URL or comes with `http`;
    /// [`try_new`](Self::try_new) returns that as an error.
    pub fn new(options: CoreOptions) -> Self {
        Self::try_new(options).expect("CoreOptions: bad `proxy` (use try_new to handle it)")
    }

    /// The transports `options` describe; an `Error::Logic` when `proxy` is not an
    /// `http://` URL or comes with `http`.
    pub fn try_new(options: CoreOptions) -> Result<Self> {
        let proxy = options.proxy.unwrap_or_default();
        let http = match (options.http, proxy.is_empty()) {
            (Some(_), false) => {
                return Err(Error::logic(
                    "CoreOptions: pass `http` or `proxy`, not both",
                ))
            }
            (Some(http), true) => Some(http),
            (None, false) => Some(
                HttpClient::new(HttpClientOptions::default().with_proxy(&proxy)?)
                    .with_rate(crate::Kraken::RATE)
                    .with_retry(crate::Kraken::RETRY),
            ),
            (None, true) => None,
        };
        let spot = Arc::new(SpotCore::new(
            options.base_url,
            options.credentials.clone(),
            http,
        ));
        let market = SocketCore::new(
            options.ws_url.unwrap_or_else(|| SPOT_WS_URL.to_string()),
            None,
        )
        .with_proxy(&proxy)?;
        let token = options.credentials.map(|_| {
            let spot = spot.clone();
            TokenCache::new(move || {
                let spot = spot.clone();
                Box::pin(async move { spot.ws_token().await })
            })
        });
        let private = SocketCore::new(
            options
                .ws_auth_url
                .unwrap_or_else(|| SPOT_WS_AUTH_URL.to_string()),
            token,
        )
        .with_proxy(&proxy)?;
        Ok(Self {
            spot_client: spot,
            market_client: Arc::new(market),
            private_client: Arc::new(private),
        })
    }

    /// Close both WebSocket connections; the next use opens them again.
    pub async fn close(&self) {
        self.market_client.close().await;
        self.private_client.close().await;
    }
}

impl crate::Kraken {
    /// A client over the three transports `options` describe.
    ///
    /// # Panics
    ///
    /// When `options.proxy` is not an `http://` URL; [`try_new`](Self::try_new) returns
    /// that as an error.
    pub fn new(options: CoreOptions) -> Self {
        Self::from_transports(&Transports::new(options))
    }

    /// [`new`](Self::new), with a bad `options.proxy` as an error rather than a panic.
    pub fn try_new(options: CoreOptions) -> Result<Self> {
        Ok(Self::from_transports(&Transports::try_new(options)?))
    }

    /// A client over transports built (or pointed at a mock) by the caller.
    pub fn from_transports(transports: &Transports) -> Self {
        Self::from_cores(
            transports.market_client.clone(),
            transports.private_client.clone(),
            transports.spot_client.clone(),
        )
    }
}
