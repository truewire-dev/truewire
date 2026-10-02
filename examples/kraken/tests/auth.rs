//! Signing against known vectors (Kraken's own documented example, and one computed with
//! the Python core from the fake key the mock tests use), the nonce, and the envelope's
//! error mapping.

use kraken::core::{raise_error, sign, unwrap, Nonce};
use truewire_core::ApiKind;

#[test]
fn api_sign_matches_krakens_documented_example() {
    let signature = sign(
        "/0/private/AddOrder",
        1616492376594,
        "nonce=1616492376594&ordertype=limit&pair=XBTUSD&price=37500&type=buy&volume=1.25",
        "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg==",
    )
    .unwrap();
    assert_eq!(
        signature,
        "4/dpxb3iT4tp/ZCVEwSnEsLxx0bqyhLpdfOpc6fn7OR8+UClSV5n9E6aSS8MPtnRfp32bAb0nmbRn6H8ndwLUQ=="
    );
}

#[test]
fn api_sign_matches_the_python_core_for_the_fake_key() {
    let signature = sign(
        "/0/private/Balance",
        1,
        "nonce=1",
        "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
    )
    .unwrap();
    assert_eq!(
        signature,
        "9dYMHqQrEFL4Qwf96Zpa6woN4LGY4zxSmhDXV0oVYAJRrCoOsbXhuFdbyz2Tfa/Sa2q6usJ2lqUYpWDmY652Wg=="
    );
    assert!(sign("/0/private/Balance", 1, "nonce=1", "not base64!")
        .unwrap_err()
        .is_api(ApiKind::Auth));
}

#[test]
fn the_nonce_strictly_increases() {
    let nonce = Nonce::default();
    let values: Vec<u64> = (0..1000).map(|_| nonce.next()).collect();
    assert!(values.windows(2).all(|pair| pair[1] > pair[0]));
}

#[test]
fn envelope_errors_map_by_category_and_substring() {
    assert!(raise_error(&["EAPI:Invalid nonce".into()]).is_api(ApiKind::Auth));
    assert!(raise_error(&["EAPI:Rate limit exceeded".into()]).is_api(ApiKind::RateLimited));
    assert!(raise_error(&["EOrder:Order minimum not met".into()]).is_api(ApiKind::BadRequest));
    assert!(raise_error(&["EGeneral:Invalid arguments".into()]).is_api(ApiKind::BadRequest));
    assert!(raise_error(&["EService:Unavailable".into()]).is_api(ApiKind::Api));
    assert_eq!(
        unwrap(200, r#"{"error": [], "result": {"a": 1}}"#).unwrap()["a"],
        1
    );
    assert!(unwrap(200, r#"{"error": ["EQuery:Unknown asset pair"]}"#)
        .unwrap_err()
        .is_api(ApiKind::Api));
    assert!(unwrap(200, r#"{"result": 1}"#).unwrap_err().is_validation());
    assert_eq!(
        unwrap(429, "slow down")
            .unwrap_err()
            .as_api()
            .unwrap()
            .status,
        Some(429)
    );
}
