use serde_json::Value;

/// The first JSON pointer at which `left` and `right` differ, with both sides, or `None`
/// when they are equal. Object key order never counts, and neither does a number's
/// spelling: JSON has one number type, so `0` and `0.0` are the same value. Two strings that
/// are both RFC 3339 date-times compare by instant, so `11:58:00Z` equals
/// `11:58:00.000000000Z` (a typed timestamp writes the shortest exact form); a date-time with
/// no offset is read as UTC, so `11:58:00` equals `11:58:00Z`.
pub fn first_difference(left: &Value, right: &Value) -> Option<String> {
    difference(left, right, "")
}

fn difference(left: &Value, right: &Value, path: &str) -> Option<String> {
    match (left, right) {
        (Value::Object(a), Value::Object(b)) => {
            for key in a.keys().chain(b.keys().filter(|key| !a.contains_key(*key))) {
                let here = format!("{path}/{}", key.replace('~', "~0").replace('/', "~1"));
                match (a.get(key), b.get(key)) {
                    (Some(x), Some(y)) => {
                        if let Some(found) = difference(x, y, &here) {
                            return Some(found);
                        }
                    }
                    (Some(_), None) => return Some(format!("{here}: only on the left")),
                    _ => return Some(format!("{here}: only on the right")),
                }
            }
            None
        }
        (Value::Array(a), Value::Array(b)) => {
            if a.len() != b.len() {
                return Some(format!("{}: {} vs {} items", display(path), a.len(), b.len()));
            }
            a.iter()
                .zip(b)
                .enumerate()
                .find_map(|(i, (x, y))| difference(x, y, &format!("{path}/{i}")))
        }
        (Value::Number(a), Value::Number(b)) if same_number(a, b) => None,
        (Value::String(a), Value::String(b)) if same_instant(a, b) => None,
        _ if left == right => None,
        _ => Some(format!("{}: {left} vs {right}", display(path))),
    }
}

fn same_instant(a: &str, b: &str) -> bool {
    match (instant(a), instant(b)) {
        (Some(x), Some(y)) => x == y,
        _ => false,
    }
}

/// An RFC 3339 date-time, or one with no offset at all read as UTC (as the runtimes read it:
/// `1970-01-01T00:00:00` dumps back as `1970-01-01T00:00:00Z`).
fn instant(text: &str) -> Option<chrono::DateTime<chrono::FixedOffset>> {
    if let Ok(parsed) = chrono::DateTime::parse_from_rfc3339(text) {
        return Some(parsed);
    }
    let naive = chrono::NaiveDateTime::parse_from_str(text, "%Y-%m-%dT%H:%M:%S%.f").ok()?;
    Some(naive.and_utc().fixed_offset())
}

fn same_number(a: &serde_json::Number, b: &serde_json::Number) -> bool {
    if a == b {
        return true;
    }
    match (a.as_i64(), b.as_i64(), a.as_u64(), b.as_u64()) {
        (Some(x), Some(y), _, _) => x == y,
        (_, _, Some(x), Some(y)) => x == y,
        _ => a.as_f64().zip(b.as_f64()).is_some_and(|(x, y)| x == y),
    }
}

fn display(path: &str) -> &str {
    if path.is_empty() {
        "/"
    } else {
        path
    }
}
