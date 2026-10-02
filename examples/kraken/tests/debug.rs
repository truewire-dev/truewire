//! `Debug` on `CoreOptions` leaves the secrets out (rust.md: a type that holds a secret
//! writes `Debug` by hand). The values below are synthetic sentinels.

use kraken::core::{CoreOptions, Credentials};

const PRIVATE_KEY: &str = "SENTINEL_PRIVATE_KEY";
const PROXY: &str = "http://u:SENTINEL_PASSWORD@h:1";

fn options() -> CoreOptions {
    CoreOptions {
        credentials: Some(Credentials {
            api_key: "public-key".into(),
            private_key: PRIVATE_KEY.into(),
        }),
        proxy: Some(PROXY.into()),
        ..CoreOptions::default()
    }
}

#[test]
fn core_options_debug_omits_private_key_and_proxy() {
    for printed in [format!("{:?}", options()), format!("{:#?}", options())] {
        for secret in ["SENTINEL", "@h:1"] {
            assert!(!printed.contains(secret), "{secret:?} in {printed}");
        }
        assert!(printed.contains("public-key"), "{printed}");
        assert!(printed.contains("<redacted>"), "{printed}");
    }
}

#[test]
fn core_options_debug_says_when_the_proxy_is_absent() {
    let printed = format!("{:?}", CoreOptions::default());
    assert!(printed.contains("proxy: None"), "{printed}");
}
