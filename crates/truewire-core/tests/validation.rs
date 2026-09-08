//! Pins the validation contract: a generated struct parses wire JSON into the typed value
//! and dumps it back, undocumented keys survive both ways, every narrowed scalar format
//! round-trips through its newtype, and every mismatch is a `ValidationError` naming the
//! JSON pointer of the offending value.

use chrono::{DateTime, NaiveDate, TimeZone, Utc};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use truewire_core::types::{
    BooleanString, DateIso, DecimalString, IntegerString, TimestampIso, TimestampMicros, TimestampMillis,
    TimestampNanos, TimestampSeconds,
};
use truewire_core::validation::{decode, decode_ref, double_option, dump, dump_json, parse_json, parse_value};
use truewire_core::{Error, ValidationError};

/// The shape a generated record takes: wire names verbatim, an optional key, and a
/// flattened map keeping every undocumented key.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
struct Order {
    id: String,
    amount: DecimalString,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    note: Option<String>,
    #[serde(flatten)]
    extra: Map<String, Value>,
}

fn order(id: &str, amount: &str) -> Order {
    Order {
        id: id.into(),
        amount: amount.parse().expect("decimal"),
        note: None,
        extra: Map::new(),
    }
}

fn failure<T: std::fmt::Debug>(result: Result<T, Error>) -> ValidationError {
    match result {
        Err(Error::Validation(e)) => e,
        other => panic!("expected a ValidationError, got {other:?}"),
    }
}

fn dt(y: i32, mo: u32, d: u32, h: u32, mi: u32, s: u32, nanos: u32) -> DateTime<Utc> {
    Utc.with_ymd_and_hms(y, mo, d, h, mi, s)
        .single()
        .expect("valid")
        .with_nanosecond(nanos)
}

trait WithNanos {
    fn with_nanosecond(self, nanos: u32) -> Self;
}

impl WithNanos for DateTime<Utc> {
    fn with_nanosecond(self, nanos: u32) -> Self {
        DateTime::from_timestamp(self.timestamp(), nanos).expect("valid")
    }
}

// -- parse ------------------------------------------------------------------------------

#[test]
fn parses_a_json_document() {
    let parsed: Order = parse_json(r#"{"id": "ord_1", "amount": "10.5"}"#).expect("parses");
    assert_eq!(parsed, order("ord_1", "10.5"));
}

#[test]
fn parses_a_decoded_value() {
    let parsed: Order = decode(json!({"id": "ord_1", "amount": "10.5"})).expect("parses");
    assert_eq!(parsed, order("ord_1", "10.5"));
    let value = json!({"id": "ord_1", "amount": "10.5"});
    let parsed: Order = decode_ref(&value).expect("parses");
    assert_eq!(parsed.id, "ord_1");
}

#[test]
fn keeps_an_undocumented_field_instead_of_rejecting_it() {
    let parsed: Order = parse_json(r#"{"id": "ord_1", "amount": "10.5", "extra": true}"#).expect("parses");
    assert_eq!(parsed.extra.get("extra"), Some(&json!(true)));
    let dumped = dump(&parsed).expect("dumps");
    assert_eq!(dumped, json!({"id": "ord_1", "amount": "10.5", "extra": true}));
}

#[test]
fn a_missing_required_key_is_a_validation_error_naming_the_key() {
    let err = failure(parse_json::<Order>(r#"{"id": "ord_1"}"#));
    // serde reports a missing key at the object holding it.
    assert_eq!(err.path(), "");
    assert!(err.message.contains("missing field `amount`"), "{}", err.message);
    assert!(err.message.ends_with(" at /"), "{}", err.message);
}

#[test]
fn a_missing_optional_key_is_fine_and_stays_absent() {
    let parsed: Order = decode(json!({"id": "x", "amount": "1"})).expect("parses");
    assert_eq!(parsed.note, None);
    assert_eq!(dump(&parsed).expect("dumps"), json!({"id": "x", "amount": "1"}));
    let parsed: Order = decode(json!({"id": "x", "amount": "1", "note": "n"})).expect("parses");
    assert_eq!(parsed.note.as_deref(), Some("n"));
}

#[test]
fn a_wrong_type_is_a_validation_error_naming_the_path_and_both_types() {
    let err = failure(decode::<Order>(json!({"id": 1, "amount": "10.5"})));
    assert_eq!(err.path(), "/id");
    assert_eq!(err.message, "invalid type: integer `1`, expected a string at /id");
}

#[test]
fn invalid_json_is_a_validation_error_chained_from_the_syntax_error() {
    let err = failure(parse_json::<Order>("{not json"));
    assert!(err.message.starts_with("invalid JSON: "), "{}", err.message);
    assert!(err.source.is_some());
    assert!(parse_value("{not json").is_err());
}

#[test]
fn a_non_object_root_is_reported_at_the_root() {
    let err = failure(decode::<Order>(json!([])));
    assert_eq!(err.path(), "");
    assert!(err.message.ends_with("expected struct Order at /"), "{}", err.message);
    let err = failure(decode::<Order>(json!(null)));
    assert_eq!(err.path(), "");
}

#[test]
fn escapes_json_pointer_segments() {
    #[derive(Deserialize, Debug)]
    struct Odd {
        #[serde(rename = "a/b")]
        #[allow(dead_code)]
        slash: String,
        #[serde(rename = "c~d")]
        #[allow(dead_code)]
        tilde: String,
    }
    assert_eq!(failure(decode::<Odd>(json!({"a/b": 1, "c~d": "x"}))).path(), "/a~1b");
    assert_eq!(failure(decode::<Odd>(json!({"a/b": "x", "c~d": 1}))).path(), "/c~0d");
}

// -- dump -------------------------------------------------------------------------------

#[test]
fn dumps_to_a_json_document() {
    assert_eq!(
        dump_json(&order("ord_1", "10.5")).expect("dumps"),
        r#"{"id":"ord_1","amount":"10.5"}"#
    );
}

#[test]
fn round_trips() {
    let value = order("ord_1", "10.50");
    let text = dump_json(&value).expect("dumps");
    let back: Order = parse_json(&text).expect("parses");
    assert_eq!(back, value);
    assert_eq!(text, r#"{"id":"ord_1","amount":"10.50"}"#);
}

#[test]
fn double_option_tells_absent_from_null() {
    #[derive(Debug, PartialEq, Serialize, Deserialize)]
    struct Row {
        #[serde(default, with = "double_option", skip_serializing_if = "Option::is_none")]
        note: Option<Option<String>>,
    }
    assert_eq!(decode::<Row>(json!({})).expect("parses").note, None);
    assert_eq!(decode::<Row>(json!({"note": null})).expect("parses").note, Some(None));
    assert_eq!(
        decode::<Row>(json!({"note": "n"})).expect("parses").note,
        Some(Some("n".into()))
    );
    assert_eq!(dump(&Row { note: None }).expect("dumps"), json!({}));
    assert_eq!(dump(&Row { note: Some(None) }).expect("dumps"), json!({"note": null}));
    assert_eq!(
        dump(&Row {
            note: Some(Some("n".into()))
        })
        .expect("dumps"),
        json!({"note": "n"})
    );
}

// -- scalars ----------------------------------------------------------------------------

#[test]
fn plain_scalars_are_serde_s_own() {
    assert_eq!(decode::<String>(json!("a")).expect("string"), "a");
    assert_eq!(decode::<f64>(json!(1.5)).expect("number"), 1.5);
    assert_eq!(decode::<i64>(json!(3)).expect("integer"), 3);
    assert!(!decode::<bool>(json!(false)).expect("boolean"));
    assert_eq!(decode::<Option<i64>>(json!(null)).expect("null"), None);
    assert_eq!(
        decode::<Value>(json!({"any": ["thing"]})).expect("any"),
        json!({"any": ["thing"]})
    );
    assert_eq!(
        failure(decode::<f64>(json!("1"))).message,
        "invalid type: string \"1\", expected f64 at /"
    );
    assert_eq!(
        failure(decode::<i64>(json!(1.5))).message,
        "invalid type: floating point `1.5`, expected i64 at /"
    );
    assert!(decode::<bool>(json!("true")).is_err());
}

#[test]
fn literal_enums_render_as_serde_enums() {
    #[derive(Debug, PartialEq, Serialize, Deserialize)]
    enum Side {
        #[serde(rename = "buy")]
        Buy,
        #[serde(rename = "sell")]
        Sell,
    }
    assert_eq!(decode::<Side>(json!("buy")).expect("buy"), Side::Buy);
    assert_eq!(dump(&Side::Sell).expect("sell"), json!("sell"));
    let err = failure(decode::<Side>(json!("hold")));
    assert_eq!(err.message, "unknown variant `hold`, expected `buy` or `sell` at /");
}

// -- wire formats -----------------------------------------------------------------------

#[test]
fn decimal_string_keeps_the_digits_the_wire_carried() {
    for (wire, back) in [
        ("10.50", "10.50"),
        ("-0.1", "-0.1"),
        ("1e-7", "0.0000001"),
        ("42", "42"),
        (".5", "0.5"),
        ("+3.", "3"),
    ] {
        let parsed: DecimalString = decode(json!(wire)).expect(wire);
        assert_eq!(dump(&parsed).expect("dumps"), json!(back), "{wire}");
    }
    assert_eq!(
        *decode::<DecimalString>(json!("1.50")).expect("parses"),
        Decimal::new(150, 2)
    );
    let err = failure(decode::<DecimalString>(json!(10.5)));
    assert_eq!(
        err.message,
        "invalid type: floating point `10.5`, expected a string at /"
    );
    assert!(decode::<DecimalString>(json!("1,5")).is_err());
    assert!(decode::<DecimalString>(json!("")).is_err());
    assert!(decode::<DecimalString>(json!("abc")).is_err());
}

#[test]
fn decimal_string_compares_by_value() {
    let d = |s: &str| s.parse::<DecimalString>().expect(s);
    assert_eq!(d("1.50"), d("1.5"));
    assert!(d("-2") < d("1"));
    assert!(d("10") > d("9.99"));
    assert_eq!(d("0.001"), d("1e-3"));
    assert_eq!(d("1.5e2"), d("150"));
    assert_eq!(d("-0"), d("0"));
    assert_eq!(d("10.5").to_string(), "10.5");
    assert_eq!(DecimalString::from(Decimal::new(42, 0)).to_string(), "42");
}

#[test]
fn integer_string() {
    assert_eq!(*decode::<IntegerString>(json!("42")).expect("42"), 42);
    assert_eq!(*decode::<IntegerString>(json!("-7")).expect("-7"), -7);
    assert_eq!(dump(&IntegerString(42)).expect("dumps"), json!("42"));
    let err = failure(decode::<IntegerString>(json!("4.2")));
    assert_eq!(err.message, "expected integer string, got \"4.2\" at /");
    assert!(decode::<IntegerString>(json!(42)).is_err());
}

#[test]
fn boolean_string() {
    assert!(*decode::<BooleanString>(json!("true")).expect("true"));
    assert!(!*decode::<BooleanString>(json!("false")).expect("false"));
    assert_eq!(dump(&BooleanString(true)).expect("dumps"), json!("true"));
    assert!(decode::<BooleanString>(json!("yes")).is_err());
    assert!(decode::<BooleanString>(json!(true)).is_err());
}

// -- timestamps -------------------------------------------------------------------------

#[test]
fn epoch_millis_the_shape_a_generated_client_uses() {
    let expected = dt(2024, 5, 30, 12, 34, 56, 123_000_000);
    assert_eq!(
        *decode::<TimestampMillis>(json!(1717072496123i64)).expect("parses"),
        expected
    );
    assert_eq!(dump_json(&TimestampMillis(expected)).expect("dumps"), "1717072496123");
}

#[test]
fn epoch_seconds_micros_nanos_round_trip_exactly_from_numbers_and_numeral_strings() {
    let expected = dt(2024, 5, 30, 12, 34, 56, 123_000_000);
    assert_eq!(
        *decode::<TimestampSeconds>(json!(1717072496)).expect("s"),
        dt(2024, 5, 30, 12, 34, 56, 0)
    );
    assert_eq!(dump(&TimestampSeconds(expected)).expect("dumps"), json!(1717072496));
    assert_eq!(
        *decode::<TimestampMicros>(json!(1717072496123000i64)).expect("us"),
        expected
    );
    assert_eq!(
        dump(&TimestampMicros(expected)).expect("dumps"),
        json!(1717072496123000i64)
    );
    let nanos = dt(2024, 5, 30, 12, 34, 56, 123_456_789);
    assert_eq!(
        *decode::<TimestampNanos>(json!("1717072496123456789")).expect("ns"),
        nanos
    );
    assert_eq!(dump_json(&TimestampNanos(nanos)).expect("dumps"), "1717072496123456789");
    assert_eq!(
        *decode::<TimestampMillis>(json!("1717072496123")).expect("ms string"),
        expected
    );
    let back: TimestampNanos = decode(dump(&TimestampNanos(nanos)).expect("dumps")).expect("parses");
    assert_eq!(back, TimestampNanos(nanos));
}

#[test]
fn rejects_non_epoch_values() {
    let err = failure(decode::<TimestampMillis>(json!("2024-05-30")));
    assert!(err.message.starts_with("expected epoch timestamp"), "{}", err.message);
    assert_eq!(err.path(), "");
    assert_eq!(
        *decode::<TimestampMillis>(json!(1.5)).expect("float truncates"),
        dt(1970, 1, 1, 0, 0, 0, 1_000_000)
    );
    assert!(decode::<TimestampMillis>(json!(null)).is_err());
    assert!(decode::<TimestampMillis>(json!({})).is_err());
}

#[test]
fn date_time_rfc_3339_forms() {
    let expected = dt(2024, 5, 30, 12, 34, 56, 123_456_789);
    assert_eq!(
        *decode::<TimestampIso>(json!("2024-05-30T12:34:56.123456789Z")).expect("ns"),
        expected
    );
    assert_eq!(
        *decode::<TimestampIso>(json!("2024-05-30T12:34:56.12Z")).expect("2 digits"),
        dt(2024, 5, 30, 12, 34, 56, 120_000_000)
    );
    assert_eq!(
        *decode::<TimestampIso>(json!("2024-05-30T14:34:56.123+02:00")).expect("offset"),
        dt(2024, 5, 30, 12, 34, 56, 123_000_000)
    );
    assert_eq!(
        dump(&TimestampIso(dt(2024, 5, 30, 12, 34, 56, 123_000_000))).expect("dumps"),
        json!("2024-05-30T12:34:56.123Z")
    );
    assert_eq!(
        dump(&TimestampIso(dt(2024, 5, 30, 12, 34, 56, 0))).expect("dumps"),
        json!("2024-05-30T12:34:56Z")
    );
    let back: TimestampIso = decode(dump(&TimestampIso(expected)).expect("dumps")).expect("parses");
    assert_eq!(*back, expected);
    let err = failure(decode::<TimestampIso>(json!("2024-05-30")));
    assert!(
        err.message.starts_with("expected RFC 3339 date-time"),
        "{}",
        err.message
    );
    assert!(decode::<TimestampIso>(json!(1717072496123i64)).is_err());
}

#[test]
fn date_rfc_3339_full_date() {
    let parsed: DateIso = decode(json!("2026-08-03")).expect("parses");
    assert_eq!(*parsed, NaiveDate::from_ymd_opt(2026, 8, 3).expect("valid"));
    assert_eq!(dump(&parsed).expect("dumps"), json!("2026-08-03"));
    assert!(decode::<DateIso>(json!("2026-02-30")).is_err());
    assert!(decode::<DateIso>(json!("20260803")).is_err());
    assert_eq!(parsed.to_datetime(), dt(2026, 8, 3, 0, 0, 0, 0));
    assert_eq!(DateIso::from_datetime(&dt(2026, 8, 3, 23, 59, 0, 0)), parsed);
}

#[test]
fn an_already_parsed_value_converts_into_its_newtype() {
    // The Rust form of the converters' pass-through: a request built from a real
    // `DateTime<Utc>` needs `.into()` and nothing else.
    let now = Utc::now();
    let field: TimestampMillis = now.into();
    assert_eq!(*field, now);
    let date: DateIso = NaiveDate::from_ymd_opt(2026, 1, 1).expect("valid").into();
    assert_eq!(date.to_string(), "2026-01-01");
}

// -- containers -------------------------------------------------------------------------

#[test]
fn arrays_name_the_failing_index() {
    let rows: Vec<Order> = parse_json(r#"[{"id": "a", "amount": "1"}, {"id": "b", "amount": "2"}]"#).expect("parses");
    assert_eq!(rows.iter().map(|r| r.id.as_str()).collect::<Vec<_>>(), ["a", "b"]);
    let err = failure(decode::<Vec<Order>>(
        json!([{"id": "a", "amount": "1"}, {"id": "b", "amount": 2}]),
    ));
    assert_eq!(err.path(), "/1/amount");
    assert_eq!(failure(decode::<Vec<Order>>(json!({}))).path(), "");
}

#[test]
fn tuples_prefix_items() {
    type Level = (DecimalString, DecimalString, TimestampMillis);
    let parsed: Level = decode(json!(["1.5", "2", 1717072496123i64])).expect("parses");
    assert_eq!(parsed.2, TimestampMillis(dt(2024, 5, 30, 12, 34, 56, 123_000_000)));
    assert_eq!(dump(&parsed).expect("dumps"), json!(["1.5", "2", 1717072496123i64]));
    assert_eq!(
        failure(decode::<Level>(json!(["1.5", "2"]))).message,
        "invalid length 2, expected a tuple of size 3 at /"
    );
    assert_eq!(failure(decode::<Level>(json!(["1.5", "x", 1]))).path(), "/1");
}

#[test]
fn maps_additional_properties() {
    let balances: std::collections::BTreeMap<String, DecimalString> =
        decode(json!({"BTC": "1.5", "ETH": "0"})).expect("parses");
    assert_eq!(balances["BTC"].to_string(), "1.5");
    assert_eq!(
        failure(decode::<std::collections::BTreeMap<String, DecimalString>>(
            json!({"BTC": 1.5})
        ))
        .path(),
        "/BTC"
    );
}

#[test]
fn untagged_unions_try_variants_in_order() {
    #[derive(Debug, PartialEq, Serialize, Deserialize)]
    #[serde(untagged)]
    enum IdOrOrder {
        Id(String),
        Order(Order),
    }
    assert_eq!(
        decode::<IdOrOrder>(json!("ord_1")).expect("id"),
        IdOrOrder::Id("ord_1".into())
    );
    assert_eq!(
        decode::<IdOrOrder>(json!({"id": "ord_1", "amount": "1"})).expect("order"),
        IdOrOrder::Order(order("ord_1", "1"))
    );
    let err = failure(decode::<IdOrOrder>(json!({"id": "ord_1"})));
    assert_eq!(err.path(), "");
    assert_eq!(dump(&IdOrOrder::Id("x".into())).expect("dumps"), json!("x"));
}

#[test]
fn nullable() {
    assert_eq!(decode::<Option<TimestampMillis>>(json!(null)).expect("null"), None);
    assert_eq!(
        decode::<Option<TimestampMillis>>(json!(0))
            .expect("epoch")
            .map(|t| t.timestamp()),
        Some(0)
    );
    assert_eq!(dump(&None::<TimestampMillis>).expect("dumps"), json!(null));
}

#[test]
fn recursive_shapes_work_with_boxes() {
    #[derive(Debug, PartialEq, Serialize, Deserialize)]
    struct Node {
        name: String,
        children: Vec<Node>,
    }
    let tree = json!({"name": "root", "children": [{"name": "leaf", "children": []}]});
    let parsed: Node = decode(tree.clone()).expect("parses");
    assert_eq!(dump(&parsed).expect("dumps"), tree);
    let err = failure(decode::<Node>(
        json!({"name": "root", "children": [{"name": 1, "children": []}]}),
    ));
    assert_eq!(err.path(), "/children/0/name");
}

#[test]
fn nested_paths_compose() {
    #[derive(Debug, Deserialize)]
    struct Book {
        #[allow(dead_code)]
        bids: Vec<(DecimalString, DecimalString)>,
        #[allow(dead_code)]
        ts: TimestampMillis,
    }
    let err = failure(decode::<Book>(json!({"bids": [["1", "2"], ["3", 4]], "ts": 0})));
    assert_eq!(err.path(), "/bids/1/1");
    assert_eq!(err.message, "invalid type: integer `4`, expected a string at /bids/1/1");
}
