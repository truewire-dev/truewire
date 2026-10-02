//! Packages clause P18 through the hand-written core: one `proxy` reaches both sockets and
//! the REST transport, and a bad one is an error rather than a direct connection. That the
//! runtime then sends through it is truewire-core's own `tests/proxy.rs`.

use kraken::core::{CoreOptions, Transports};
use kraken::Kraken;
use truewire_core::HttpClient;

const PROXY: &str = "http://proxy.example:3128";

#[test]
fn one_proxy_reaches_both_sockets() {
    let transports = Transports::try_new(CoreOptions {
        proxy: Some(PROXY.to_string()),
        ..CoreOptions::default()
    })
    .expect("transports");
    for socket in [&transports.market_client, &transports.private_client] {
        let proxy = socket.socket().proxy().map(|url| url.to_string());
        assert_eq!(proxy.as_deref(), Some("http://proxy.example:3128/"));
    }
    let direct = Transports::new(CoreOptions::default());
    assert!(direct.market_client.socket().proxy().is_none());
}

#[test]
fn a_bad_proxy_or_one_beside_http_is_an_error() {
    let socks = CoreOptions {
        proxy: Some("socks5://127.0.0.1:1080".to_string()),
        ..CoreOptions::default()
    };
    let error = Kraken::try_new(socks).err().expect("refused");
    assert_eq!(error.code(), "logic");
    let both = CoreOptions {
        proxy: Some(PROXY.to_string()),
        http: Some(HttpClient::default()),
        ..CoreOptions::default()
    };
    let error = Transports::try_new(both).err().expect("refused");
    assert!(
        error
            .to_string()
            .contains("pass `http` or `proxy`, not both"),
        "{error}"
    );
}
