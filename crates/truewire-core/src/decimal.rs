//! `decimal-string` wire values at any precision.
//!
//! The wire carries digits in a string (`"10.50"`). [`DecimalString`] keeps that text
//! verbatim, so a value dumps back exactly as the API sent it (`"1e-7"` stays `"1e-7"`,
//! `"10.50"` stays `"10.50"`), and holds the value as a [`BigDecimal`], which has no digit
//! limit: a 40-digit volume is neither rounded nor refused. Comparison, hashing and
//! arithmetic go by value (`"1.50" == "1.5"`); [`DecimalString::to_f64`] is the nearest
//! float for code that wants one.

use std::cmp::Ordering;
use std::fmt;
use std::hash::{Hash, Hasher};
use std::ops::{Add, Deref, Mul, Neg, Sub};
use std::str::FromStr;

use bigdecimal::{BigDecimal, ToPrimitive, Zero};
use serde::{Deserialize, Deserializer, Serialize, Serializer};

use crate::errors::{Error, Result};

/// Whether `text` is in decimal-string form: plain digits with an optional sign, fraction
/// and exponent (`"10.50"`, `"-0.1"`, `"1e-7"`, `".5"`, `"+3."`), surrounding whitespace
/// allowed.
pub fn is_decimal(text: &str) -> bool {
    let bytes = text.trim().as_bytes();
    let mut i = 0;
    if matches!(bytes.first(), Some(b'+' | b'-')) {
        i += 1;
    }
    let int_digits = digits(&bytes[i..]);
    i += int_digits;
    let mut frac_digits = 0;
    if bytes.get(i) == Some(&b'.') {
        i += 1;
        frac_digits = digits(&bytes[i..]);
        i += frac_digits;
    }
    if int_digits == 0 && frac_digits == 0 {
        return false;
    }
    if matches!(bytes.get(i), Some(b'e' | b'E')) {
        i += 1;
        if matches!(bytes.get(i), Some(b'+' | b'-')) {
            i += 1;
        }
        let exp_digits = digits(&bytes[i..]);
        if exp_digits == 0 {
            return false;
        }
        i += exp_digits;
    }
    i == bytes.len()
}

fn digits(bytes: &[u8]) -> usize {
    bytes.iter().take_while(|b| b.is_ascii_digit()).count()
}

/// Parse a wire decimal string into its exact value, at any number of digits.
pub fn parse(value: &str) -> Result<BigDecimal> {
    if !is_decimal(value) {
        return Err(Error::logic(format!("Not a decimal string: {value:?}")));
    }
    BigDecimal::from_str(&normalize(value.trim()))
        .map_err(|e| Error::logic(format!("Not a decimal string: {value:?}")).with_source(e))
}

/// Render a value in wire form: the digits with their scale, no exponent.
pub fn dump(value: &BigDecimal) -> String {
    value.to_plain_string()
}

/// `"+3."` -> `"3"`, `".5"` -> `"0.5"`, `"+.5e2"` -> `"0.5e2"`: the forms the wire uses that
/// `BigDecimal` does not read on its own.
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

/// A `decimal-string` field: the exact wire text, and its value as a [`BigDecimal`].
///
/// Equality, ordering and hashing are by value, so `"1.50"` equals `"1.5"` and `"1e-3"`
/// equals `"0.001"`; [`Display`](fmt::Display) and serialization write the text. A value
/// built from a `BigDecimal` (or by arithmetic) renders as its plain digits with scale.
#[derive(Clone)]
pub struct DecimalString {
    value: BigDecimal,
    text: String,
}

impl DecimalString {
    /// Parse wire text, keeping it verbatim.
    pub fn parse(text: &str) -> Result<Self> {
        Ok(Self {
            value: parse(text)?,
            text: text.to_string(),
        })
    }

    pub fn new(value: BigDecimal) -> Self {
        let text = dump(&value);
        Self { value, text }
    }

    /// The value.
    pub fn value(&self) -> &BigDecimal {
        &self.value
    }

    pub fn into_inner(self) -> BigDecimal {
        self.value
    }

    /// The text as the wire carried it (or the plain digits of a constructed value).
    pub fn as_str(&self) -> &str {
        &self.text
    }

    /// The nearest `f64`; digits beyond double precision are lost.
    pub fn to_f64(&self) -> f64 {
        normalize(self.text.trim())
            .parse::<f64>()
            .ok()
            .or_else(|| self.value.to_f64())
            .unwrap_or(f64::NAN)
    }
}

impl Default for DecimalString {
    fn default() -> Self {
        Self {
            value: BigDecimal::zero(),
            text: "0".to_string(),
        }
    }
}

impl fmt::Debug for DecimalString {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_tuple("DecimalString").field(&self.text).finish()
    }
}

impl PartialEq for DecimalString {
    fn eq(&self, other: &Self) -> bool {
        self.value == other.value
    }
}

impl Eq for DecimalString {}

impl PartialOrd for DecimalString {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for DecimalString {
    fn cmp(&self, other: &Self) -> Ordering {
        self.value.cmp(&other.value)
    }
}

impl Hash for DecimalString {
    /// By the normalized digits and exponent, so equal values hash alike without expanding
    /// an exponent into zeros.
    fn hash<H: Hasher>(&self, state: &mut H) {
        if self.value.is_zero() {
            0u8.hash(state);
        } else {
            let (digits, scale) = self.value.normalized().into_bigint_and_exponent();
            digits.hash(state);
            scale.hash(state);
        }
    }
}

impl Deref for DecimalString {
    type Target = BigDecimal;

    fn deref(&self) -> &BigDecimal {
        &self.value
    }
}

impl From<BigDecimal> for DecimalString {
    fn from(value: BigDecimal) -> Self {
        Self::new(value)
    }
}

impl From<DecimalString> for BigDecimal {
    fn from(value: DecimalString) -> Self {
        value.value
    }
}

macro_rules! from_integer {
    ($($t:ty),*) => {$(
        impl From<$t> for DecimalString {
            fn from(value: $t) -> Self {
                Self::new(BigDecimal::from(value))
            }
        }
    )*};
}
from_integer!(i32, i64, u32, u64);

impl FromStr for DecimalString {
    type Err = Error;

    fn from_str(s: &str) -> Result<Self> {
        Self::parse(s)
    }
}

impl fmt::Display for DecimalString {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.text)
    }
}

macro_rules! binary_op {
    ($trait:ident, $method:ident) => {
        impl $trait<&DecimalString> for &DecimalString {
            type Output = DecimalString;

            fn $method(self, other: &DecimalString) -> DecimalString {
                DecimalString::new((&self.value).$method(&other.value))
            }
        }

        impl $trait for DecimalString {
            type Output = DecimalString;

            fn $method(self, other: DecimalString) -> DecimalString {
                (&self).$method(&other)
            }
        }
    };
}
binary_op!(Add, add);
binary_op!(Sub, sub);
binary_op!(Mul, mul);

impl Neg for &DecimalString {
    type Output = DecimalString;

    fn neg(self) -> DecimalString {
        DecimalString::new(-&self.value)
    }
}

impl Neg for DecimalString {
    type Output = DecimalString;

    fn neg(self) -> DecimalString {
        -&self
    }
}

impl Serialize for DecimalString {
    fn serialize<S: Serializer>(&self, serializer: S) -> std::result::Result<S::Ok, S::Error> {
        serializer.serialize_str(&self.text)
    }
}

impl<'de> Deserialize<'de> for DecimalString {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> std::result::Result<Self, D::Error> {
        let text = String::deserialize(deserializer)?;
        Self::parse(&text).map_err(|e| serde::de::Error::custom(format!("expected decimal string: {}", e.message())))
    }
}
