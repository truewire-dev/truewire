//! Wire-shape newtypes generated code refers to by name, one per spec `format` that
//! changes the rendered type (`docs/spec/authoring.md` rules 3, 12, 13, 15).
//!
//! | format           | type                 | client value      | wire value                          |
//! | ---------------- | -------------------- | ----------------- | ----------------------------------- |
//! | `epoch-seconds`  | [`TimestampSeconds`] | `DateTime<Utc>`   | integer seconds since the Unix epoch |
//! | `epoch-millis`   | [`TimestampMillis`]  | `DateTime<Utc>`   | integer milliseconds                 |
//! | `epoch-micros`   | [`TimestampMicros`]  | `DateTime<Utc>`   | integer microseconds                 |
//! | `epoch-nanos`    | [`TimestampNanos`]   | `DateTime<Utc>`   | integer nanoseconds                  |
//! | `date-time`      | [`TimestampIso`]     | `DateTime<Utc>`   | RFC 3339 date-time string            |
//! | `date`           | [`DateIso`]          | `NaiveDate`       | RFC 3339 full-date string            |
//! | `decimal-string` | [`DecimalString`]    | `Decimal`         | the digits, as a string              |
//! | `integer-string` | [`IntegerString`]    | `i64`             | `"42"`                               |
//! | `boolean-string` | [`BooleanString`]    | `bool`            | `"true"` / `"false"`                 |
//!
//! Each is a transparent wrapper: it derefs to the value inside, converts `From` it (so a
//! request holding a real `DateTime<Utc>` is `.into()` away from its field, the
//! already-parsed path the converters share), and implements `Serialize`/`Deserialize` in
//! the wire form. Being ordinary types, they compose anywhere in the plan's type tree:
//! `Vec<TimestampMillis>`, `(DecimalString, DecimalString)`, `Option<DateIso>`, a union
//! variant.

use std::fmt;
use std::ops::Deref;

use chrono::{DateTime, NaiveDate, Utc};
use serde::{Deserialize, Deserializer, Serialize, Serializer};

pub use crate::decimal::DecimalString;
use crate::times::{
    EpochConverter, EpochValue, DATE_ISO_PATTERN, TIMESTAMP_ISO, TIMESTAMP_MICROS, TIMESTAMP_MILLIS, TIMESTAMP_NANOS,
    TIMESTAMP_SECONDS,
};

/// The JSON forms an epoch field arrives in.
#[derive(Deserialize)]
#[serde(untagged)]
enum EpochWire {
    Int(i64),
    UInt(u64),
    Float(f64),
    Str(String),
}

impl From<EpochWire> for EpochValue {
    fn from(value: EpochWire) -> Self {
        match value {
            EpochWire::Int(i) => Self::Int(i as i128),
            EpochWire::UInt(u) => Self::Int(u as i128),
            EpochWire::Float(f) => Self::Float(f),
            EpochWire::Str(s) => Self::Str(s),
        }
    }
}

macro_rules! timestamp_newtype {
    ($(#[$doc:meta])* $name:ident) => {
        $(#[$doc])*
        #[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
        pub struct $name(pub DateTime<Utc>);

        impl $name {
            pub fn new(value: DateTime<Utc>) -> Self { Self(value) }
            pub fn into_inner(self) -> DateTime<Utc> { self.0 }
            /// The current time.
            pub fn now() -> Self { Self(Utc::now()) }
        }

        impl Deref for $name {
            type Target = DateTime<Utc>;
            fn deref(&self) -> &DateTime<Utc> { &self.0 }
        }

        impl From<DateTime<Utc>> for $name {
            fn from(value: DateTime<Utc>) -> Self { Self(value) }
        }

        impl From<$name> for DateTime<Utc> {
            fn from(value: $name) -> Self { value.0 }
        }

        impl fmt::Display for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result { fmt::Display::fmt(&self.0, f) }
        }
    };
}

macro_rules! epoch_newtype {
    ($(#[$doc:meta])* $name:ident, $converter:expr) => {
        timestamp_newtype!($(#[$doc])* $name);

        impl $name {
            /// The converter behind this type.
            pub const CONVERTER: EpochConverter = $converter;
        }

        impl Serialize for $name {
            fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
                serializer.serialize_i64(Self::CONVERTER.dump(&self.0))
            }
        }

        impl<'de> Deserialize<'de> for $name {
            fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
                let wire = EpochWire::deserialize(deserializer)
                    .map_err(|_| serde::de::Error::custom("expected epoch timestamp"))?;
                Self::CONVERTER
                    .parse(EpochValue::from(wire))
                    .map(Self)
                    .map_err(|e| serde::de::Error::custom(format!("expected epoch timestamp: {}", e.message())))
            }
        }
    };
}

epoch_newtype!(
    /// An `epoch-seconds` field.
    TimestampSeconds, TIMESTAMP_SECONDS
);
epoch_newtype!(
    /// An `epoch-millis` field.
    TimestampMillis, TIMESTAMP_MILLIS
);
epoch_newtype!(
    /// An `epoch-micros` field.
    TimestampMicros, TIMESTAMP_MICROS
);
epoch_newtype!(
    /// An `epoch-nanos` field.
    TimestampNanos, TIMESTAMP_NANOS
);

timestamp_newtype!(
    /// A `date-time` (RFC 3339) field: UTC and `Z`-suffixed on the wire.
    TimestampIso
);

impl Serialize for TimestampIso {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&TIMESTAMP_ISO.dump(&self.0))
    }
}

impl<'de> Deserialize<'de> for TimestampIso {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let text =
            String::deserialize(deserializer).map_err(|_| serde::de::Error::custom("expected RFC 3339 date-time"))?;
        TIMESTAMP_ISO
            .parse(text.as_str())
            .map(Self)
            .map_err(|e| serde::de::Error::custom(format!("expected RFC 3339 date-time: {}", e.message())))
    }
}

/// A `date` (RFC 3339 full-date) field: `YYYY-MM-DD` on the wire, a [`NaiveDate`] here.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct DateIso(pub NaiveDate);

impl DateIso {
    pub fn new(value: NaiveDate) -> Self {
        Self(value)
    }

    pub fn into_inner(self) -> NaiveDate {
        self.0
    }

    /// Today's date, in UTC.
    pub fn today() -> Self {
        Self(Utc::now().date_naive())
    }

    /// The UTC calendar date of an instant.
    pub fn from_datetime(dt: &DateTime<Utc>) -> Self {
        Self(dt.date_naive())
    }

    /// Midnight UTC on that date.
    pub fn to_datetime(&self) -> DateTime<Utc> {
        self.0.and_hms_opt(0, 0, 0).expect("midnight exists").and_utc()
    }
}

impl Deref for DateIso {
    type Target = NaiveDate;

    fn deref(&self) -> &NaiveDate {
        &self.0
    }
}

impl From<NaiveDate> for DateIso {
    fn from(value: NaiveDate) -> Self {
        Self(value)
    }
}

impl From<DateIso> for NaiveDate {
    fn from(value: DateIso) -> Self {
        value.0
    }
}

impl fmt::Display for DateIso {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0.format(DATE_ISO_PATTERN))
    }
}

impl Serialize for DateIso {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&self.to_string())
    }
}

impl<'de> Deserialize<'de> for DateIso {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let text = String::deserialize(deserializer).map_err(|_| serde::de::Error::custom("expected RFC 3339 date"))?;
        NaiveDate::parse_from_str(&text, DATE_ISO_PATTERN)
            .map(Self)
            .map_err(|_| serde::de::Error::custom(format!("expected RFC 3339 date, got {text:?}")))
    }
}

/// An `integer-string` field: `"42"` on the wire, `42` in the client.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Default)]
pub struct IntegerString(pub i64);

impl IntegerString {
    pub fn new(value: i64) -> Self {
        Self(value)
    }

    pub fn into_inner(self) -> i64 {
        self.0
    }
}

impl Deref for IntegerString {
    type Target = i64;

    fn deref(&self) -> &i64 {
        &self.0
    }
}

impl From<i64> for IntegerString {
    fn from(value: i64) -> Self {
        Self(value)
    }
}

impl From<IntegerString> for i64 {
    fn from(value: IntegerString) -> Self {
        value.0
    }
}

impl fmt::Display for IntegerString {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        fmt::Display::fmt(&self.0, f)
    }
}

impl Serialize for IntegerString {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&self.0.to_string())
    }
}

impl<'de> Deserialize<'de> for IntegerString {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let text =
            String::deserialize(deserializer).map_err(|_| serde::de::Error::custom("expected integer string"))?;
        text.trim()
            .parse::<i64>()
            .map(Self)
            .map_err(|_| serde::de::Error::custom(format!("expected integer string, got {text:?}")))
    }
}

/// A `boolean-string` field: `"true"`/`"false"` on the wire, a `bool` in the client.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Default)]
pub struct BooleanString(pub bool);

impl BooleanString {
    pub fn new(value: bool) -> Self {
        Self(value)
    }

    pub fn into_inner(self) -> bool {
        self.0
    }
}

impl Deref for BooleanString {
    type Target = bool;

    fn deref(&self) -> &bool {
        &self.0
    }
}

impl From<bool> for BooleanString {
    fn from(value: bool) -> Self {
        Self(value)
    }
}

impl From<BooleanString> for bool {
    fn from(value: BooleanString) -> Self {
        value.0
    }
}

impl fmt::Display for BooleanString {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        fmt::Display::fmt(&self.0, f)
    }
}

impl Serialize for BooleanString {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(if self.0 { "true" } else { "false" })
    }
}

impl<'de> Deserialize<'de> for BooleanString {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let text =
            String::deserialize(deserializer).map_err(|_| serde::de::Error::custom("expected boolean string"))?;
        match text.as_str() {
            "true" => Ok(Self(true)),
            "false" => Ok(Self(false)),
            _ => Err(serde::de::Error::custom(format!(
                "expected boolean string, got {text:?}"
            ))),
        }
    }
}
