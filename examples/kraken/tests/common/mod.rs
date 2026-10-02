//! A `Kraken` client whose three transports all point at the local mock: the REST one
//! with a fake key pair (the mock checks the request's shape, never `API-Sign`), and the
//! two sockets on one URL with no token source, since the recorded subscribe frames carry
//! none -- the same client `test/client.ts` builds.

#![allow(dead_code)]

use std::sync::Arc;

use kraken::core::{Credentials, SocketCore, SpotCore, Transports};
use kraken::Kraken;
use truewire_testing::Mock;

/// Never real: the private key is base64 of thirty-two zero bytes.
pub fn fake_credentials() -> Credentials {
    Credentials {
        api_key: "mock-api-key".to_string(),
        private_key: "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=".to_string(),
    }
}

pub fn root() -> &'static str {
    env!("CARGO_MANIFEST_DIR")
}

pub fn mock() -> Mock {
    Mock::start(root()).expect("truewire mock starts")
}

pub fn transports(mock: &Mock) -> Transports {
    let ws_url = mock
        .ws_url
        .clone()
        .expect("kraken records WebSocket examples");
    Transports {
        spot_client: Arc::new(SpotCore::new(
            Some(mock.http_url.clone()),
            Some(fake_credentials()),
            None,
        )),
        market_client: Arc::new(SocketCore::new(ws_url.clone(), None)),
        private_client: Arc::new(SocketCore::new(ws_url, None)),
    }
}

pub fn client(transports: &Transports) -> Kraken {
    Kraken::from_transports(transports)
}
