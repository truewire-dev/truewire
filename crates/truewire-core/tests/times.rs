//! Pins the converters' RFC 3339 handling and exact integer epoch arithmetic: venues send
//! `Z`-suffixed timestamps with anywhere from no fractional digits to nanoseconds, and epoch
//! values in every unit, some as numeral strings.

use chrono::{DateTime, NaiveDate, TimeZone, Utc};
use serde_json::json;
use truewire_core::times::{date_iso, DateConverter, EpochConverter, IsoConverter, TIMESTAMP_ISO, TIMESTAMP_MILLIS};
use truewire_core::Error;

fn utc(y: i32, mo: u32, d: u32, h: u32, mi: u32, s: u32, nanos: u32) -> DateTime<Utc> {
    let base = Utc.with_ymd_and_hms(y, mo, d, h, mi, s).single().expect("valid");
    DateTime::from_timestamp(base.timestamp(), nanos).expect("valid")
}

fn is_logic(result: Result<impl std::fmt::Debug, Error>) -> bool {
    matches!(result, Err(Error::Logic(_)))
}

// -- IsoConverter -----------------------------------------------------------------------

#[test]
fn iso_parse_z_suffixed_nine_digit_fraction_keeps_nanoseconds() {
    assert_eq!(
        IsoConverter.parse("2024-05-30T12:34:56.123456789Z").expect("parses"),
        utc(2024, 5, 30, 12, 34, 56, 123_456_789)
    );
}

#[test]
fn iso_parse_pads_a_short_fraction_and_cuts_a_long_one() {
    assert_eq!(
        IsoConverter.parse("2024-05-30T12:34:56.12Z").expect("parses"),
        utc(2024, 5, 30, 12, 34, 56, 120_000_000)
    );
    assert_eq!(
        IsoConverter
            .parse("2024-05-30T12:34:56.1234567891234Z")
            .expect("parses"),
        utc(2024, 5, 30, 12, 34, 56, 123_456_789)
    );
}

#[test]
fn iso_parse_no_fraction_and_lowercase_forms() {
    assert_eq!(
        IsoConverter.parse("2024-05-30T12:34:56Z").expect("parses"),
        utc(2024, 5, 30, 12, 34, 56, 0)
    );
    assert_eq!(
        IsoConverter.parse("2024-05-30t12:34:56z").expect("parses"),
        utc(2024, 5, 30, 12, 34, 56, 0)
    );
    assert_eq!(
        IsoConverter.parse("2024-05-30 12:34:56Z").expect("parses"),
        utc(2024, 5, 30, 12, 34, 56, 0)
    );
}

#[test]
fn iso_parse_an_explicit_offset_is_the_same_instant() {
    assert_eq!(
        IsoConverter.parse("2024-05-30T14:34:56+02:00").expect("parses"),
        utc(2024, 5, 30, 12, 34, 56, 0)
    );
    assert_eq!(
        IsoConverter.parse("2024-05-30T10:04:56.5-02:30").expect("parses"),
        utc(2024, 5, 30, 12, 34, 56, 500_000_000)
    );
}

#[test]
fn iso_parse_a_date_time_without_an_offset_is_utc() {
    assert_eq!(
        IsoConverter.parse("1970-01-01T00:00:00").expect("parses"),
        utc(1970, 1, 1, 0, 0, 0, 0)
    );
    assert_eq!(
        IsoConverter.parse("2025-11-03T09:12:44.123456789").expect("parses"),
        utc(2025, 11, 3, 9, 12, 44, 123_456_789)
    );
    assert_eq!(
        IsoConverter.parse("2024-05-30 12:34:56.5").expect("parses"),
        utc(2024, 5, 30, 12, 34, 56, 500_000_000)
    );
}

#[test]
fn iso_parse_rejects_what_is_not_an_rfc_3339_date_time() {
    for bad in [
        "2024-05-30",
        "2024-05-30T12:34",
        "2024-05-30T12:34:56+0200",
        "2024-05-30T12:34:56.",
        "2024-13-01T00:00:00Z",
        "yesterday",
        "1717072496",
    ] {
        assert!(is_logic(IsoConverter.parse(bad)), "{bad}");
    }
}

#[test]
fn iso_parse_passes_an_already_parsed_value_through() {
    let dt = utc(2024, 5, 30, 12, 34, 56, 7);
    assert_eq!(TIMESTAMP_ISO.parse(dt).expect("passes"), dt);
}

#[test]
fn iso_dump_renders_utc_z_suffixed_with_a_fraction_only_when_needed() {
    assert_eq!(
        IsoConverter.dump(&utc(2024, 5, 30, 12, 34, 56, 0)),
        "2024-05-30T12:34:56Z"
    );
    assert_eq!(
        IsoConverter.dump(&utc(2024, 5, 30, 12, 34, 56, 961_000_000)),
        "2024-05-30T12:34:56.961Z"
    );
    assert_eq!(
        IsoConverter.dump(&utc(2024, 5, 30, 12, 34, 56, 961_500_000)),
        "2024-05-30T12:34:56.961500Z"
    );
    assert_eq!(
        IsoConverter.dump(&utc(2024, 5, 30, 12, 34, 56, 123_456_789)),
        "2024-05-30T12:34:56.123456789Z"
    );
}

#[test]
fn iso_round_trips_and_now_is_in_wire_form() {
    let conv = IsoConverter;
    assert_eq!(
        conv.dump(&conv.parse("2024-05-07T14:08:30.961Z").expect("parses")),
        "2024-05-07T14:08:30.961Z"
    );
    let now = conv.parse(conv.now().as_str()).expect("parses");
    assert!((Utc::now() - now).num_seconds().abs() < 5);
}

// -- EpochConverter ---------------------------------------------------------------------

#[test]
fn epoch_milliseconds() {
    let dt = utc(2024, 5, 30, 12, 34, 56, 123_000_000);
    let conv = EpochConverter::milliseconds();
    assert_eq!(conv.dump(&dt), 1717072496123);
    assert_eq!(conv.parse(1717072496123i64).expect("parses"), dt);
}

#[test]
fn epoch_seconds_lose_sub_second_precision_on_dump_by_construction() {
    let dt = utc(2024, 5, 30, 12, 34, 56, 123_000_000);
    let conv = EpochConverter::seconds();
    assert_eq!(conv.dump(&dt), 1717072496);
    assert_eq!(conv.parse(1717072496).expect("parses"), utc(2024, 5, 30, 12, 34, 56, 0));
}

#[test]
fn epoch_microseconds() {
    let dt = utc(2024, 5, 30, 12, 34, 56, 123_000_000);
    let conv = EpochConverter::microseconds();
    assert_eq!(conv.dump(&dt), 1717072496123000);
    assert_eq!(conv.parse(1717072496123000i64).expect("parses"), dt);
    assert_eq!(conv.parse(conv.dump(&dt)).expect("parses"), dt);
}

#[test]
fn epoch_nanoseconds_are_exact() {
    let dt = utc(2024, 5, 30, 12, 34, 56, 123_456_789);
    let conv = EpochConverter::nanoseconds();
    assert_eq!(conv.dump_i128(&dt), 1717072496123456789);
    assert_eq!(conv.dump(&dt), 1717072496123456789);
    assert_eq!(conv.parse(1717072496123456789i64).expect("parses"), dt);
    assert_eq!(conv.parse("1717072496123456789").expect("parses"), dt);
    assert_eq!(conv.parse(conv.dump(&dt)).expect("parses"), dt);
}

#[test]
fn epoch_accepts_a_numeral_string_a_float_and_a_json_value() {
    let dt = utc(2024, 5, 30, 12, 34, 56, 123_000_000);
    assert_eq!(TIMESTAMP_MILLIS.parse("1717072496123").expect("string"), dt);
    assert_eq!(TIMESTAMP_MILLIS.parse(" +1717072496123 ").expect("signed string"), dt);
    assert_eq!(TIMESTAMP_MILLIS.parse(1717072496123.0f64).expect("whole float"), dt);
    assert_eq!(
        TIMESTAMP_MILLIS
            .parse(1717072496123.9f64)
            .expect("fractional float keeps its fraction"),
        utc(2024, 5, 30, 12, 34, 56, 123_900_000)
    );
    assert_eq!(TIMESTAMP_MILLIS.parse(&json!(1717072496123i64)).expect("value"), dt);
    assert_eq!(
        TIMESTAMP_MILLIS.parse(&json!("1717072496123")).expect("value string"),
        dt
    );
}

#[test]
fn epoch_passes_an_already_parsed_value_through() {
    let dt = utc(2024, 5, 30, 12, 34, 56, 123_456_789);
    assert_eq!(TIMESTAMP_MILLIS.parse(dt).expect("passes"), dt);
}

#[test]
fn epoch_floors_negative_values_like_python_does() {
    let conv = EpochConverter::microseconds();
    assert_eq!(
        conv.parse(-1).expect("parses"),
        DateTime::from_timestamp(-1, 999_999_000).expect("valid")
    );
    assert_eq!(
        conv.dump(&DateTime::from_timestamp(-1, 999_000_000).expect("valid")),
        -1000
    );
    assert_eq!(
        EpochConverter::seconds().parse(-1).expect("parses"),
        DateTime::from_timestamp(-1, 0).expect("valid")
    );
}

#[test]
fn epoch_rejects_what_is_not_an_epoch() {
    assert!(is_logic(EpochConverter::seconds().parse("12.5.1")));
    assert!(is_logic(EpochConverter::seconds().parse("1e3")));
    assert_eq!(
        EpochConverter::seconds()
            .parse("12.5")
            .expect("a fractional numeral string"),
        DateTime::from_timestamp(12, 500_000_000).expect("valid")
    );
    assert!(is_logic(EpochConverter::seconds().parse(f64::NAN)));
    assert!(is_logic(EpochConverter::seconds().parse("")));
    assert!(is_logic(EpochConverter::seconds().parse("abc")));
    assert!(is_logic(EpochConverter::seconds().parse(i128::MAX / 2)));
}

#[test]
fn epoch_now_is_in_the_unit() {
    let conv = EpochConverter::milliseconds();
    let now = conv.now();
    assert!((Utc::now() - conv.parse(now).expect("parses")).num_seconds().abs() < 5);
}

// -- DateConverter ----------------------------------------------------------------------

#[test]
fn date_parses_a_plain_calendar_date() {
    assert_eq!(
        date_iso().parse("2026-08-03").expect("parses"),
        NaiveDate::from_ymd_opt(2026, 8, 3).expect("valid")
    );
}

#[test]
fn date_dumps_and_round_trips() {
    let conv = DateConverter::default();
    let date = NaiveDate::from_ymd_opt(2026, 8, 3).expect("valid");
    assert_eq!(conv.dump(&date), "2026-08-03");
    assert_eq!(conv.parse(conv.dump(&date)).expect("parses"), date);
    assert_eq!(conv.parse(date).expect("passes through"), date);
}

#[test]
fn date_a_compact_pattern_round_trips() {
    // Some broker-commission endpoints send `YYYYMMDD` with no separators.
    let conv = DateConverter::new("%Y%m%d");
    let date = NaiveDate::from_ymd_opt(2026, 1, 1).expect("valid");
    assert_eq!(conv.parse("20260101").expect("parses"), date);
    assert_eq!(conv.dump(&date), "20260101");
    assert_eq!(conv.parse(conv.dump(&date)).expect("parses"), date);
}

#[test]
fn date_the_default_pattern_is_rfc_3339() {
    assert_eq!(DateConverter::default().pattern, "%Y-%m-%d");
}

#[test]
fn date_rejects_what_does_not_match_the_pattern_or_the_calendar() {
    assert!(is_logic(DateConverter::default().parse("20260101")));
    assert!(is_logic(DateConverter::default().parse("2026-02-30")));
    assert!(is_logic(DateConverter::default().parse("2026/01/01")));
}

#[test]
fn date_now_is_today() {
    let today = DateConverter::default().now();
    assert_eq!(today, Utc::now().date_naive());
}

#[test]
fn a_fractional_epoch_keeps_its_fraction_and_dumps_back_to_the_same_number() {
    use truewire_core::times::{EpochNumber, TIMESTAMP_SECONDS};
    let seconds = TIMESTAMP_SECONDS
        .parse(1763410056.903966f64)
        .expect("fractional seconds");
    assert_eq!(seconds.timestamp(), 1763410056);
    assert_eq!(seconds.timestamp_subsec_micros(), 903966);
    assert_eq!(
        TIMESTAMP_SECONDS.dump_number(&seconds),
        EpochNumber::Float(1763410056.903966)
    );
    let text = TIMESTAMP_SECONDS
        .parse("1763410056.903966")
        .expect("fractional numeral string");
    assert_eq!(text.timestamp_subsec_nanos(), 903_966_000);
    let half = TIMESTAMP_MILLIS.parse(1717072496123.5f64).expect("half a millisecond");
    assert_eq!(half.timestamp_subsec_nanos(), 123_500_000);
    let negative = TIMESTAMP_MILLIS.parse("-1.5").expect("negative");
    assert_eq!(negative.timestamp_subsec_nanos(), 998_500_000);
    let whole = TIMESTAMP_SECONDS.parse(1763410056i64).expect("whole");
    assert_eq!(TIMESTAMP_SECONDS.dump_number(&whole), EpochNumber::Int(1763410056));
    for bad in ["1.", ".5", "1.2.3", "1e3", ""] {
        assert!(TIMESTAMP_SECONDS.parse(bad).is_err(), "{bad:?} parsed");
    }
}

#[test]
fn string_and_float_epoch_newtypes_keep_the_wire_form() {
    use truewire_core::{TimestampNanosString, TimestampSeconds, TimestampSecondsFloat, TimestampSecondsString};
    let last: TimestampNanosString = truewire_core::decode(json!("1786622308334567536")).expect("decodes");
    assert_eq!(truewire_core::dump(&last).expect("dumps"), json!("1786622308334567536"));
    let seconds: TimestampSecondsString = truewire_core::decode(json!("1763410056.903966")).expect("decodes");
    assert_eq!(
        truewire_core::dump(&seconds).expect("dumps"),
        json!("1763410056.903966")
    );
    let number: TimestampSecondsFloat = truewire_core::decode(json!(1763410056.903966)).expect("decodes");
    assert_eq!(truewire_core::dump(&number).expect("dumps"), json!(1763410056.903966));
    let whole: TimestampSecondsFloat = truewire_core::decode(json!(1763410056)).expect("decodes");
    assert_eq!(truewire_core::dump(&whole).expect("dumps"), json!(1763410056));
    // An `integer` schema never receives a fraction.
    let floored: TimestampSeconds = truewire_core::decode(json!(1763410056.903966)).expect("decodes");
    assert_eq!(truewire_core::dump(&floored).expect("dumps"), json!(1763410056));
}
