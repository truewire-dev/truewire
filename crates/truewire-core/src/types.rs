//! Wire-shape newtypes generated code refers to by name, one per spec `format` that
//! changes the rendered type (`docs/spec/authoring.md` rules 3, 12, 13, 15).
//!
//! | format           | type                 | client value      | wire value                          |
//! | ---------------- | -------------------- | ----------------- | ----------------------------------- |
//! | `epoch-seconds`  | [`TimestampSeconds`] | `DateTime<Utc>`   | integer seconds since the Unix epoch |
//! | `epoch-millis`   | [`TimestampMillis`]  | `DateTime<Utc>`   | integer milliseconds                 |
//! | `epoch-micros`   | [`TimestampMicros`]  | `DateTime<Utc>`   | integer microseconds                 |
//! | `epoch-nanos`    | [`TimestampNanos`]   | `DateTime<Utc>`   | integer nanoseconds                  |
//! | `epoch-*` on a `string` | [`TimestampSecondsString`] ... [`TimestampNanosString`] | `DateTime<Utc>` | the same count, as a numeral string |
//! | `epoch-*` on a `number` | [`TimestampSecondsFloat`] ... [`TimestampNanosFloat`] | `DateTime<Utc>` | the count, fractional when it has a fraction |
//! | `date-time`      | [`TimestampIso`]     | `DateTime<Utc>`   | RFC 3339 date-time string            |
//! | `date`           | [`DateIso`]          | `NaiveDate`       | RFC 3339 full-date string            |
//! | `decimal-string` | [`DecimalString`]    | `BigDecimal`      | the digits, verbatim, any precision  |
//! | `integer-string` | [`IntegerString`]    | `BigInt`          | `"42"`, any size                     |
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
use num_bigint::BigInt;
use num_traits::ToPrimitive;
use serde::{Deserialize, Deserializer, Serialize, Serializer};

pub use crate::decimal::DecimalString;
use crate::times::{
    EpochConverter, EpochNumber, EpochValue, DATE_ISO_PATTERN, TIMESTAMP_ISO, TIMESTAMP_MICROS, TIMESTAMP_MILLIS,
    TIMESTAMP_NANOS, TIMESTAMP_SECONDS,
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
            /// Whole units, floored: an `integer` schema never receives a fraction, whatever
            /// precision the `DateTime` holds.
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

macro_rules! epoch_string_newtype {
    ($(#[$doc:meta])* $name:ident, $converter:expr) => {
        timestamp_newtype!($(#[$doc])* $name);

        impl $name {
            /// The converter behind this type.
            pub const CONVERTER: EpochConverter = $converter;
        }

        impl Serialize for $name {
            fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
                serializer.serialize_str(&Self::CONVERTER.dump_number(&self.0).to_string())
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

macro_rules! epoch_float_newtype {
    ($(#[$doc:meta])* $name:ident, $converter:expr) => {
        timestamp_newtype!($(#[$doc])* $name);

        impl $name {
            /// The converter behind this type.
            pub const CONVERTER: EpochConverter = $converter;
        }

        impl Serialize for $name {
            /// Whole units as an integer, a fractional count as the float it came as.
            fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
                match Self::CONVERTER.dump_number(&self.0) {
                    EpochNumber::Int(i) => serializer.serialize_i64(i),
                    EpochNumber::Float(f) => serializer.serialize_f64(f),
                }
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

epoch_float_newtype!(
    /// An `epoch-seconds` field on a `number` schema: fractional seconds (`1763410056.903966`).
    TimestampSecondsFloat, TIMESTAMP_SECONDS
);
epoch_float_newtype!(
    /// An `epoch-millis` field on a `number` schema.
    TimestampMillisFloat, TIMESTAMP_MILLIS
);
epoch_float_newtype!(
    /// An `epoch-micros` field on a `number` schema.
    TimestampMicrosFloat, TIMESTAMP_MICROS
);
epoch_float_newtype!(
    /// An `epoch-nanos` field on a `number` schema.
    TimestampNanosFloat, TIMESTAMP_NANOS
);

epoch_newtype!(
    /// An `epoch-seconds` field.
    TimestampSeconds, TIMESTAMP_SECONDS
);
epoch_string_newtype!(
    /// An `epoch-seconds` field on a `string` schema: the count as a numeral string.
    TimestampSecondsString, TIMESTAMP_SECONDS
);
epoch_string_newtype!(
    /// An `epoch-millis` field on a `string` schema.
    TimestampMillisString, TIMESTAMP_MILLIS
);
epoch_string_newtype!(
    /// An `epoch-micros` field on a `string` schema.
    TimestampMicrosString, TIMESTAMP_MICROS
);
epoch_string_newtype!(
    /// An `epoch-nanos` field on a `string` schema (kraken's `trades.last`).
    TimestampNanosString, TIMESTAMP_NANOS
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

/// An `integer-string` field: `"42"` on the wire, an arbitrary-precision integer in the
/// client, so a wei amount or a uint256 token id survives exactly. The wire form is a
/// string of optional sign and ASCII digits; a JSON number is refused, as the format says.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Default)]
pub struct IntegerString(pub BigInt);

impl IntegerString {
    pub fn new(value: impl Into<BigInt>) -> Self {
        Self(value.into())
    }

    pub fn into_inner(self) -> BigInt {
        self.0
    }

    /// The value as an `i64`, when it fits.
    pub fn to_i64(&self) -> Option<i64> {
        self.0.to_i64()
    }

    /// The value as an `i64`, clamped to its range: what a page walk's `total` needs.
    pub fn saturating_i64(&self) -> i64 {
        self.0.to_i64().unwrap_or(if self.0.sign() == num_bigint::Sign::Minus {
            i64::MIN
        } else {
            i64::MAX
        })
    }
}

impl Deref for IntegerString {
    type Target = BigInt;

    fn deref(&self) -> &BigInt {
        &self.0
    }
}

macro_rules! integer_string_from {
    ($($t:ty),*) => {$(
        impl From<$t> for IntegerString {
            fn from(value: $t) -> Self {
                Self(BigInt::from(value))
            }
        }
    )*};
}

integer_string_from!(i8, i16, i32, i64, i128, u8, u16, u32, u64, u128, BigInt);

impl std::str::FromStr for IntegerString {
    type Err = crate::errors::Error;

    fn from_str(text: &str) -> crate::errors::Result<Self> {
        let unsigned = text.strip_prefix(['+', '-']).unwrap_or(text);
        if unsigned.is_empty() || !unsigned.bytes().all(|b| b.is_ascii_digit()) {
            return Err(crate::errors::Error::validation(format!(
                "expected integer string, got {text:?}"
            )));
        }
        text.parse::<BigInt>()
            .map(Self)
            .map_err(|_| crate::errors::Error::validation(format!("expected integer string, got {text:?}")))
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
            .parse::<IntegerString>()
            .map_err(|e| serde::de::Error::custom(e.message().to_string()))
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
