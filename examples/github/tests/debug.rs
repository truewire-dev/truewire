//! `Debug` on the core types leaves the secrets out (rust.md: a type that holds a secret
//! writes `Debug` by hand). The values below are synthetic sentinels.

use github::core::{Core, CoreOptions};

const TOKEN: &str = "ghp_SENTINEL_TOKEN";
const PROXY: &str = "http://user:SENTINEL_PASSWORD@127.0.0.1:3128";

fn options() -> CoreOptions {
    CoreOptions {
        token: Some(TOKEN.into()),
        proxy: Some(PROXY.into()),
        ..CoreOptions::default()
    }
}

fn assert_no_secret(printed: &str) {
    for secret in [TOKEN, "SENTINEL", "127.0.0.1"] {
        assert!(!printed.contains(secret), "{secret:?} in {printed}");
    }
}

#[test]
fn core_options_debug_omits_token_and_proxy() {
    let printed = format!("{:?}", options());
    assert_no_secret(&printed);
    assert!(printed.contains("token: Some(\"<redacted>\")"), "{printed}");
    assert!(printed.contains("proxy: Some(\"<redacted>\")"), "{printed}");
    assert_no_secret(&format!("{:#?}", options()));
}

#[test]
fn core_options_debug_says_when_a_secret_is_absent() {
    let printed = format!("{:?}", CoreOptions::default());
    assert!(printed.contains("token: None"), "{printed}");
    assert!(printed.contains("proxy: None"), "{printed}");
}

#[test]
fn core_debug_omits_token() {
    let core = Core::new(options());
    let printed = format!("{core:?}");
    assert_no_secret(&printed);
    assert!(printed.contains("token: Some(\"<redacted>\")"), "{printed}");
    assert!(printed.contains("https://api.github.com"), "{printed}");
}
