//! Packages clause P18 through the hand-written core: `proxy` builds the HTTP transport,
//! and a bad one is an error rather than a direct connection. That the runtime then sends
//! through it is truewire-core's own `tests/proxy.rs`.

use github::core::{Core, CoreOptions};
use github::GitHub;
use truewire_core::HttpClient;

#[test]
fn a_proxy_builds_the_core() {
    let core = Core::try_new(CoreOptions {
        proxy: Some("http://proxy.example:3128".to_string()),
        ..CoreOptions::default()
    });
    assert!(core.is_ok(), "{:?}", core.err());
}

#[test]
fn an_empty_proxy_allows_a_prebuilt_http_client() {
    for http in [None, Some(HttpClient::default())] {
        let core = Core::try_new(CoreOptions {
            proxy: Some(String::new()),
            http,
            ..CoreOptions::default()
        });
        assert!(core.is_ok(), "{:?}", core.err());
    }
}

#[test]
fn a_bad_proxy_or_one_beside_http_is_an_error() {
    let socks = CoreOptions {
        proxy: Some("socks5://127.0.0.1:1080".to_string()),
        ..CoreOptions::default()
    };
    let error = GitHub::try_new(socks).err().expect("refused");
    assert_eq!(error.code(), "logic");
    let both = CoreOptions {
        proxy: Some("http://proxy.example:3128".to_string()),
        http: Some(HttpClient::default()),
        ..CoreOptions::default()
    };
    let error = Core::try_new(both).expect_err("refused");
    assert!(
        error
            .to_string()
            .contains("pass `http` or `proxy`, not both"),
        "{error}"
    );
}
