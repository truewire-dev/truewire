//! Pins the error taxonomy a generated client's callers program against: API-returned
//! errors are `Error::Api` with a kind, transport/format/SDK failures are siblings, every
//! error renders as its concrete name and message, and carries the same string code the
//! TypeScript runtime uses.

use std::error::Error as _;

use serde_json::json;
use truewire_core::{ApiError, ApiKind, Error, Issue, ValidationError};

#[test]
fn api_errors_carry_a_kind_status_and_body() {
    let err = Error::rate_limited("slow down")
        .with_status(429)
        .with_body(json!({"error": "EGeneral:Too many requests"}));
    let api = err.as_api().expect("an api error");
    assert_eq!(api.kind, ApiKind::RateLimited);
    assert_eq!(api.status, Some(429));
    assert_eq!(api.body, Some(json!({"error": "EGeneral:Too many requests"})));
    assert!(err.is_api(ApiKind::RateLimited));
    assert!(!err.is_api(ApiKind::Auth));
}

#[test]
fn siblings_are_not_api_errors() {
    // A caller matching `Error::Api` must not swallow a dropped connection or an SDK bug.
    for err in [Error::network("x"), Error::validation("x"), Error::logic("x")] {
        assert!(err.as_api().is_none(), "{err}");
    }
    assert!(Error::network("x").is_network());
    assert!(Error::validation("x").is_validation());
    assert!(Error::logic("x").is_logic());
    assert!(Error::network("x").with_status(500).as_api().is_none());
}

#[test]
fn renders_as_the_concrete_name_and_the_message() {
    assert_eq!(Error::auth("bad key").to_string(), "AuthError: bad key");
    assert_eq!(Error::bad_request("x").to_string(), "BadRequest: x");
    assert_eq!(Error::api("x").to_string(), "ApiError: x");
    assert_eq!(Error::logic("").to_string(), "LogicError");
    assert_eq!(Error::network("GET /x").to_string(), "NetworkError: GET /x");
    assert_eq!(Error::validation("bad").to_string(), "ValidationError: bad");
}

#[test]
fn carries_a_string_code_per_kind() {
    assert_eq!(Error::auth("").code(), "auth");
    assert_eq!(Error::rate_limited("").code(), "rate-limited");
    assert_eq!(Error::bad_request("").code(), "bad-request");
    assert_eq!(Error::api("").code(), "api");
    assert_eq!(Error::validation("").code(), "validation");
    assert_eq!(Error::network("").code(), "network");
    assert_eq!(Error::logic("").code(), "logic");
}

#[test]
fn keeps_a_cause() {
    let err = Error::network("GET /x").with_source(std::io::Error::other("timeout"));
    assert_eq!(err.source().expect("a source").to_string(), "timeout");
    assert_eq!(err.to_string(), "NetworkError: GET /x");
    let cloned = err.clone();
    assert_eq!(cloned.source().expect("a source").to_string(), "timeout");
}

#[test]
fn validation_error_carries_its_path_and_issues() {
    let err = ValidationError::with_issues(
        "expected string at /a/0",
        vec![Issue {
            path: "/a/0".into(),
            message: "expected string".into(),
        }],
    );
    assert_eq!(err.path(), "/a/0");
    assert_eq!(err.issues.len(), 1);
    assert_eq!(ValidationError::new("root").path(), "");
    let err: Error = err.into();
    assert_eq!(err.as_validation().expect("validation").path(), "/a/0");
}

#[test]
fn leaves_convert_into_error() {
    let err: Error = ApiError::new(ApiKind::Auth, "nope").with_status(401).into();
    assert_eq!(err.name(), "AuthError");
    assert_eq!(err.message(), "nope");
    assert_eq!(err.as_api().and_then(|e| e.status), Some(401));
}
