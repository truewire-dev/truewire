//! `CallOptions`: the builder, and the transport a dual-transport endpoint is called over.

use std::time::Duration;

use truewire_core::{CallOptions, Transport};

#[test]
fn call_options_default_to_the_cores_timeout_and_the_first_declared_transport() {
    let options = CallOptions::default();
    assert_eq!(options.timeout, None);
    assert_eq!(options.transport, None);
}

#[test]
fn call_options_builder_sets_timeout_and_transport() {
    let options = CallOptions::new()
        .timeout(Duration::from_secs(3))
        .transport(Transport::Ws);
    assert_eq!(options.timeout, Some(Duration::from_secs(3)));
    assert_eq!(options.transport, Some(Transport::Ws));
    assert_ne!(options, CallOptions::default().transport(Transport::Http));
}
