//! `decimal-string` wire values as a [`rust_decimal::Decimal`].
//!
//! The wire carries digits in a string (`"10.50"`); the client holds a `Decimal`, which
//! keeps the scale, so `"10.50"` dumps back as `"10.50"` and `"1e-7"` as `"0.0000001"`.
//! [`DecimalString`] is the newtype a generated struct uses for a field of that format:
//! it serializes to and from the string form, and derefs to the `Decimal` inside.

use std::fmt;
use std::ops::Deref;
use std::str::FromStr;

use rust_decimal::Decimal;
use serde::{Deserialize, Deserializer, Serialize, Serializer};

use crate::errors::{Error, Result};

/// Parse a wire decimal string: plain digits with an optional sign, fraction and exponent
/// (`"10.50"`, `"-0.1"`, `"1e-7"`, `".5"`, `"+3."`).
pub fn parse(value: &str) -> Result<Decimal> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return Err(Error::logic(format!("Not a decimal string: {value:?}")));
    }
    let text = normalize(trimmed);
    // `from_str_exact`, not `from_str`: a value with more digits than a `Decimal` holds is
    // an error, never a silently rounded price.
    let result = if text.contains(['e', 'E']) {
        Decimal::from_scientific(&text)
    } else {
        Decimal::from_str_exact(&text)
    };
    result.map_err(|e| Error::logic(format!("Not a decimal string: {value:?}")).with_source(e))
}

/// Render a `Decimal` in wire form: the digits with their scale, no exponent.
pub fn dump(value: &Decimal) -> String {
    value.to_string()
}

/// `"+3."` -> `"3"`, `".5"` -> `"0.5"`, `"+.5e2"` -> `"0.5e2"`: the forms the wire uses
/// that `rust_decimal` does not read on its own.
fn normalize(text: &str) -> String {
    let (sign, rest) = match text.as_bytes()[0] {
        b'+' => ("", &text[1..]),
        b'-' => ("-", &text[1..]),
        _ => ("", text),
    };
    let (mantissa, exponent) = match rest.find(['e', 'E']) {
        Some(i) => (&rest[..i], &rest[i..]),
        None => (rest, ""),
    };
    let mantissa = mantissa.strip_suffix('.').unwrap_or(mantissa);
    let mantissa = if mantissa.starts_with('.') {
        format!("0{mantissa}")
    } else {
        mantissa.to_string()
    };
    format!("{sign}{mantissa}{exponent}")
}

/// A `decimal-string` field: a [`Decimal`] on the client, a string on the wire.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Default)]
pub struct DecimalString(pub Decimal);

impl DecimalString {
    pub fn new(value: Decimal) -> Self {
        Self(value)
    }

    pub fn into_inner(self) -> Decimal {
        self.0
    }
}

impl Deref for DecimalString {
    type Target = Decimal;

    fn deref(&self) -> &Decimal {
        &self.0
    }
}

impl From<Decimal> for DecimalString {
    fn from(value: Decimal) -> Self {
        Self(value)
    }
}

impl From<DecimalString> for Decimal {
    fn from(value: DecimalString) -> Self {
        value.0
    }
}

impl FromStr for DecimalString {
    type Err = Error;

    fn from_str(s: &str) -> Result<Self> {
        parse(s).map(Self)
    }
}

impl fmt::Display for DecimalString {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&dump(&self.0))
    }
}

impl Serialize for DecimalString {
    fn serialize<S: Serializer>(&self, serializer: S) -> std::result::Result<S::Ok, S::Error> {
        serializer.serialize_str(&dump(&self.0))
    }
}

impl<'de> Deserialize<'de> for DecimalString {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> std::result::Result<Self, D::Error> {
        let text = String::deserialize(deserializer)?;
        parse(&text)
            .map(Self)
            .map_err(|e| serde::de::Error::custom(format!("expected decimal string: {}", e.message())))
    }
}
