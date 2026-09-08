//! Validation: `serde` over generated structs, with every failure a
//! [`ValidationError`](crate::errors::ValidationError) naming the offending value by JSON
//! pointer.
//!
//! A generated package derives `Serialize`/`Deserialize` on one struct per wire shape,
//! using the newtypes in [`crate::types`] for every narrowed scalar. The functions here are
//! the two ends of a call: [`dump`] turns the typed request into the wire
//! [`Value`](serde_json::Value) a core sends, and [`decode`] turns the wire value a core
//! returns into the typed response. `validate: false` is [`decode`] never called: the
//! core's `Value` is the raw body, handed back as it came.
//!
//! There is no schema interpretation: `serde` is the whole validator, and
//! `serde_path_to_error` tracks the path so a mismatch deep inside a list of tuples is
//! reported at `/bids/1/1`, as the other runtimes report it.

use serde::de::DeserializeOwned;
use serde::Serialize;
use serde_json::Value;
use serde_path_to_error::{Path, Segment};

use crate::errors::{Error, Issue, Result, ValidationError};

/// A `serde_path_to_error` path as a JSON pointer: `""` for the root, `/bids/1/1` inside.
pub fn pointer(path: &Path) -> String {
    let mut out = String::new();
    for segment in path.iter() {
        match segment {
            Segment::Seq { index } => {
                out.push('/');
                out.push_str(&index.to_string());
            }
            Segment::Map { key } => {
                out.push('/');
                out.push_str(&key.replace('~', "~0").replace('/', "~1"));
            }
            Segment::Enum { .. } | Segment::Unknown => {}
        }
    }
    out
}

fn validation_error<E: std::error::Error + Send + Sync + 'static>(err: serde_path_to_error::Error<E>) -> Error {
    let path = pointer(err.path());
    let message = err.inner().to_string();
    let issue = Issue {
        path: path.clone(),
        message: message.clone(),
    };
    let at = if path.is_empty() { "/".to_string() } else { path };
    Error::Validation(ValidationError::with_issues(format!("{message} at {at}"), vec![issue]).with_source(err))
}

/// Decode a wire value into a generated type; a mismatch is a `ValidationError` at its path.
pub fn decode<T: DeserializeOwned>(value: Value) -> Result<T> {
    serde_path_to_error::deserialize(value).map_err(validation_error)
}

/// [`decode`] without consuming the value.
pub fn decode_ref<T: DeserializeOwned>(value: &Value) -> Result<T> {
    serde_path_to_error::deserialize(value).map_err(validation_error)
}

/// Parse a raw JSON document; a syntax error is a `ValidationError` too, with the
/// `serde_json` error as its source.
pub fn parse_json<T: DeserializeOwned>(text: &str) -> Result<T> {
    decode(parse_value(text)?)
}

/// Parse raw JSON bytes.
pub fn parse_slice<T: DeserializeOwned>(bytes: &[u8]) -> Result<T> {
    let value: Value = serde_json::from_slice(bytes).map_err(invalid_json)?;
    decode(value)
}

/// Parse a raw JSON document into a [`Value`] without validating it: the `validate: false` path.
pub fn parse_value(text: &str) -> Result<Value> {
    serde_json::from_str(text).map_err(invalid_json)
}

fn invalid_json(e: serde_json::Error) -> Error {
    Error::Validation(ValidationError::new(format!("invalid JSON: {e}")).with_source(e))
}

/// Dump a generated value to its wire form, checking it on the way (a request built wrong
/// fails before the wire).
pub fn dump<T: Serialize + ?Sized>(value: &T) -> Result<Value> {
    let mut buffer = Vec::new();
    let mut serializer = serde_json::Serializer::new(&mut buffer);
    serde_path_to_error::serialize(value, &mut serializer).map_err(validation_error)?;
    serde_json::from_slice(&buffer).map_err(invalid_json)
}

/// Dump a generated value to a JSON document.
pub fn dump_json<T: Serialize + ?Sized>(value: &T) -> Result<String> {
    let mut buffer = Vec::new();
    let mut serializer = serde_json::Serializer::new(&mut buffer);
    serde_path_to_error::serialize(value, &mut serializer).map_err(validation_error)?;
    String::from_utf8(buffer).map_err(|e| Error::logic("dumped JSON is not UTF-8").with_source(e))
}

/// `serde(with)` module for a field that is both optional and nullable: `Option<Option<T>>`
/// where the outer `None` is an absent key and the inner one a JSON `null`. Pair it with
/// `#[serde(default, skip_serializing_if = "Option::is_none")]`.
pub mod double_option {
    use serde::{Deserialize, Deserializer, Serialize, Serializer};

    pub fn serialize<T: Serialize, S: Serializer>(value: &Option<Option<T>>, serializer: S) -> Result<S::Ok, S::Error> {
        match value {
            Some(inner) => inner.serialize(serializer),
            None => serializer.serialize_none(),
        }
    }

    pub fn deserialize<'de, T: Deserialize<'de>, D: Deserializer<'de>>(
        deserializer: D,
    ) -> Result<Option<Option<T>>, D::Error> {
        Option::<T>::deserialize(deserializer).map(Some)
    }
}
