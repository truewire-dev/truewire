//! Converters between a wire timestamp and a real [`DateTime<Utc>`], one per shape an API
//! puts on the wire: epoch seconds/millis/micros/nanos, RFC 3339 date-times, and plain
//! calendar dates.
//!
//! Arithmetic is exact integer arithmetic on `i128` throughout, and a `DateTime<Utc>`
//! holds nanoseconds, so every unit round-trips exactly: a nanosecond value parsed and
//! dumped again is the same digits. Each converter's `parse` also accepts an
//! already-parsed value (a `DateTime<Utc>` for the timestamp converters, a `NaiveDate` for
//! [`DateConverter`]) and returns it unchanged, so a request that holds the real value
//! its generated field asks for goes through the same path as a wire value.
//!
//! The newtypes generated code uses for each spec format ([`crate::types`]) are built on
//! the module-level instances [`TIMESTAMP_SECONDS`], [`TIMESTAMP_MILLIS`],
//! [`TIMESTAMP_MICROS`], [`TIMESTAMP_NANOS`], [`TIMESTAMP_ISO`] and [`date_iso`].

use chrono::{DateTime, NaiveDate, SecondsFormat, Utc};

use crate::errors::{Error, Result};

const NANOS_PER_SECOND: i128 = 1_000_000_000;

/// What an epoch timestamp looks like when it reaches a converter: a JSON number, a numeral
/// string (`"1786302600000"`, as some APIs send it), or a value already parsed.
#[derive(Debug, Clone, PartialEq)]
pub enum EpochValue {
    Int(i128),
    /// Truncated toward zero, as the TypeScript runtime does.
    Float(f64),
    Str(String),
    Parsed(DateTime<Utc>),
}

macro_rules! epoch_from_int {
    ($($t:ty),*) => {$(
        impl From<$t> for EpochValue {
            fn from(value: $t) -> Self { Self::Int(value as i128) }
        }
    )*};
}
epoch_from_int!(i8, i16, i32, i64, i128, u8, u16, u32, u64);

impl From<f64> for EpochValue {
    fn from(value: f64) -> Self {
        Self::Float(value)
    }
}

impl From<&str> for EpochValue {
    fn from(value: &str) -> Self {
        Self::Str(value.to_string())
    }
}

impl From<String> for EpochValue {
    fn from(value: String) -> Self {
        Self::Str(value)
    }
}

impl From<DateTime<Utc>> for EpochValue {
    fn from(value: DateTime<Utc>) -> Self {
        Self::Parsed(value)
    }
}

impl From<&serde_json::Value> for EpochValue {
    fn from(value: &serde_json::Value) -> Self {
        match value {
            serde_json::Value::Number(n) => {
                if let Some(i) = n.as_i64() {
                    Self::Int(i as i128)
                } else if let Some(u) = n.as_u64() {
                    Self::Int(u as i128)
                } else {
                    Self::Float(n.as_f64().unwrap_or(f64::NAN))
                }
            }
            serde_json::Value::String(s) => Self::Str(s.clone()),
            other => Self::Str(other.to_string()),
        }
    }
}

/// Floor division (`/` on integers truncates toward zero).
fn floor_div(n: i128, d: i128) -> i128 {
    let q = n / d;
    if n % d < 0 {
        q - 1
    } else {
        q
    }
}

fn not_epoch(what: &str, value: impl std::fmt::Debug) -> Error {
    Error::logic(format!("Not an epoch {what}: {value:?}"))
}

/// Converter for epoch timestamps in a specific unit.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct EpochConverter {
    /// Units per second: `1_000` for milliseconds, `1` for seconds, ...
    pub unit: i128,
}

impl EpochConverter {
    pub const fn new(unit: i128) -> Self {
        Self { unit }
    }

    pub const fn seconds() -> Self {
        Self::new(1)
    }

    pub const fn milliseconds() -> Self {
        Self::new(1_000)
    }

    pub const fn microseconds() -> Self {
        Self::new(1_000_000)
    }

    pub const fn nanoseconds() -> Self {
        Self::new(1_000_000_000)
    }

    /// Parse an epoch timestamp in this unit, or pass an already-parsed `DateTime` through.
    /// Negative (pre-1970) values floor, as Python's `timedelta` arithmetic does.
    pub fn parse(&self, value: impl Into<EpochValue>) -> Result<DateTime<Utc>> {
        let ticks: i128 = match value.into() {
            EpochValue::Parsed(dt) => return Ok(dt),
            EpochValue::Int(i) => i,
            EpochValue::Float(f) => {
                if !f.is_finite() {
                    return Err(not_epoch("timestamp", f));
                }
                f.trunc() as i128
            }
            EpochValue::Str(s) => {
                let trimmed = s.trim();
                let digits = trimmed.strip_prefix(['+', '-']).unwrap_or(trimmed);
                if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
                    return Err(not_epoch("timestamp", s));
                }
                trimmed.parse::<i128>().map_err(|_| not_epoch("timestamp", s.clone()))?
            }
        };
        let nanos = ticks
            .checked_mul(NANOS_PER_SECOND)
            .map(|n| floor_div(n, self.unit))
            .ok_or_else(|| not_epoch("timestamp", ticks))?;
        let secs = floor_div(nanos, NANOS_PER_SECOND);
        let subsec = (nanos - secs * NANOS_PER_SECOND) as u32;
        let secs = i64::try_from(secs).map_err(|_| not_epoch("timestamp", ticks))?;
        DateTime::<Utc>::from_timestamp(secs, subsec).ok_or_else(|| not_epoch("timestamp", ticks))
    }

    /// Convert a `DateTime` back into an epoch timestamp in this unit, exact at any unit.
    pub fn dump_i128(&self, dt: &DateTime<Utc>) -> i128 {
        let nanos = dt.timestamp() as i128 * NANOS_PER_SECOND + dt.timestamp_subsec_nanos() as i128;
        floor_div(nanos.saturating_mul(self.unit), NANOS_PER_SECOND)
    }

    /// [`dump_i128`](Self::dump_i128) as an `i64`, the JSON number a generated struct writes.
    /// Nanoseconds fit until the year 2262.
    pub fn dump(&self, dt: &DateTime<Utc>) -> i64 {
        let value = self.dump_i128(dt);
        i64::try_from(value).unwrap_or(if value < 0 { i64::MIN } else { i64::MAX })
    }

    /// The current time, in this unit.
    pub fn now(&self) -> i64 {
        self.dump(&Utc::now())
    }
}

/// What an RFC 3339 timestamp looks like when it reaches [`IsoConverter::parse`].
#[derive(Debug, Clone, PartialEq)]
pub enum IsoValue {
    Str(String),
    Parsed(DateTime<Utc>),
}

impl From<&str> for IsoValue {
    fn from(value: &str) -> Self {
        Self::Str(value.to_string())
    }
}

impl From<String> for IsoValue {
    fn from(value: String) -> Self {
        Self::Str(value)
    }
}

impl From<DateTime<Utc>> for IsoValue {
    fn from(value: DateTime<Utc>) -> Self {
        Self::Parsed(value)
    }
}

/// Converter for RFC 3339 date-times, always UTC and `Z`-suffixed on the wire.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct IsoConverter;

impl IsoConverter {
    pub const fn new() -> Self {
        Self
    }

    /// Parse a `Z`-suffixed or offset RFC 3339 date-time with a fraction of any length
    /// (some APIs send milliseconds, others nanoseconds; digits beyond nine are dropped),
    /// or pass an already-parsed `DateTime` through.
    pub fn parse(&self, value: impl Into<IsoValue>) -> Result<DateTime<Utc>> {
        let text = match value.into() {
            IsoValue::Parsed(dt) => return Ok(dt),
            IsoValue::Str(s) => s,
        };
        let normalized = normalize_rfc3339(&text);
        DateTime::parse_from_rfc3339(&normalized)
            .map(|dt| dt.with_timezone(&Utc))
            .map_err(|e| Error::logic(format!("Not an RFC 3339 date-time: {text:?}")).with_source(e))
    }

    /// Render UTC, `Z`-suffixed, with a fraction only when there is one (three, six or nine
    /// digits, as many as the value needs).
    pub fn dump(&self, dt: &DateTime<Utc>) -> String {
        dt.to_rfc3339_opts(SecondsFormat::AutoSi, true)
    }

    pub fn now(&self) -> String {
        self.dump(&Utc::now())
    }
}

/// `t`/space separators to `T`, `z` to `Z`, a fraction longer than nine digits cut to nine.
fn normalize_rfc3339(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let bytes = text.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        let c = bytes[i];
        match c {
            b't' | b' ' if i == 10 => out.push('T'),
            b'z' if i == bytes.len() - 1 => out.push('Z'),
            b'.' => {
                out.push('.');
                let start = i + 1;
                let mut end = start;
                while end < bytes.len() && bytes[end].is_ascii_digit() {
                    end += 1;
                }
                out.push_str(&text[start..(start + (end - start).min(9))]);
                i = end;
                continue;
            }
            _ => out.push(c as char),
        }
        i += 1;
    }
    out
}

/// What a calendar date looks like when it reaches [`DateConverter::parse`].
#[derive(Debug, Clone, PartialEq)]
pub enum DateValue {
    Str(String),
    Parsed(NaiveDate),
}

impl From<&str> for DateValue {
    fn from(value: &str) -> Self {
        Self::Str(value.to_string())
    }
}

impl From<String> for DateValue {
    fn from(value: String) -> Self {
        Self::Str(value)
    }
}

impl From<NaiveDate> for DateValue {
    fn from(value: NaiveDate) -> Self {
        Self::Parsed(value)
    }
}

/// Converter for a plain calendar date, with no time component: wire string in `pattern`
/// to a [`NaiveDate`] and back. Not a timestamp converter: a calendar date has no instant.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DateConverter {
    /// `strftime`-style pattern for the wire string; `"%Y%m%d"` reads a compact `YYYYMMDD`
    /// date with no separators. Defaults to RFC 3339's `%Y-%m-%d`.
    pub pattern: String,
}

impl Default for DateConverter {
    fn default() -> Self {
        Self::new("%Y-%m-%d")
    }
}

impl DateConverter {
    pub fn new(pattern: impl Into<String>) -> Self {
        Self {
            pattern: pattern.into(),
        }
    }

    /// Parse a wire date (`"2026-08-03"` for the default pattern), or pass a `NaiveDate` through.
    pub fn parse(&self, value: impl Into<DateValue>) -> Result<NaiveDate> {
        let text = match value.into() {
            DateValue::Parsed(d) => return Ok(d),
            DateValue::Str(s) => s,
        };
        NaiveDate::parse_from_str(&text, &self.pattern)
            .map_err(|e| Error::logic(format!("Not a date in {} form: {text:?}", self.pattern)).with_source(e))
    }

    /// Render a date back to the wire pattern.
    pub fn dump(&self, date: &NaiveDate) -> String {
        date.format(&self.pattern).to_string()
    }

    /// Today's date, in UTC.
    pub fn now(&self) -> NaiveDate {
        Utc::now().date_naive()
    }
}

/// Converter behind [`crate::types::TimestampSeconds`].
pub const TIMESTAMP_SECONDS: EpochConverter = EpochConverter::seconds();
/// Converter behind [`crate::types::TimestampMillis`].
pub const TIMESTAMP_MILLIS: EpochConverter = EpochConverter::milliseconds();
/// Converter behind [`crate::types::TimestampMicros`].
pub const TIMESTAMP_MICROS: EpochConverter = EpochConverter::microseconds();
/// Converter behind [`crate::types::TimestampNanos`].
pub const TIMESTAMP_NANOS: EpochConverter = EpochConverter::nanoseconds();
/// Converter behind [`crate::types::TimestampIso`].
pub const TIMESTAMP_ISO: IsoConverter = IsoConverter::new();

/// Converter behind [`crate::types::DateIso`]: the default `%Y-%m-%d` pattern.
pub fn date_iso() -> DateConverter {
    DateConverter::default()
}

/// The RFC 3339 full-date pattern [`DateIso`](crate::types::DateIso) uses.
pub const DATE_ISO_PATTERN: &str = "%Y-%m-%d";
