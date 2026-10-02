use std::fmt::Display;
use std::future::Future;

use serde_json::Value;

use crate::json::first_difference;

/// What checking one example came to.
#[derive(Debug, Clone, PartialEq)]
pub enum Replayed {
    /// Passed.
    Ok,
    /// Not checked, and why (a recording the typed request cannot render back, say).
    Skipped(String),
    /// Failed, and why.
    Failed(String),
}

impl Replayed {
    /// The typed response dumped back to the wire must equal the raw one: nothing was lost
    /// or reshaped on the way through the generated types.
    pub fn compare(typed: Value, raw: Value) -> Self {
        match first_difference(&typed, &raw) {
            None => Self::Ok,
            Some(difference) => Self::Failed(format!(
                "the typed value does not round-trip to the wire body at {difference}"
            )),
        }
    }
}

/// Every example's outcome, labelled `function (id)`.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Report {
    pub passed: Vec<String>,
    pub skipped: Vec<(String, String)>,
    pub failed: Vec<(String, String)>,
}

impl Report {
    /// Panic naming every failure, or when nothing passed at all (a replay over zero
    /// examples proves nothing and must not read as success).
    pub fn assert_passed(&self) {
        if !self.failed.is_empty() {
            let lines: Vec<String> = self
                .failed
                .iter()
                .map(|(label, why)| format!("  {label}: {why}"))
                .collect();
            panic!(
                "{} of {} example(s) failed:\n{}",
                self.failed.len(),
                self.passed.len() + self.failed.len() + self.skipped.len(),
                lines.join("\n")
            );
        }
        assert!(
            !self.passed.is_empty(),
            "no example passed ({} skipped)",
            self.skipped.len()
        );
    }
}

/// Run `check` over every example in order and collect the outcomes; an `Err` from
/// `check` is a failure like any other, so one broken endpoint never hides the rest.
///
/// Each example is handed over by value, so the future `check` returns owns what it reads.
pub async fn replay<E, F, Fut, Err>(examples: &[E], check: F) -> Report
where
    E: Labelled + Clone,
    F: Fn(E) -> Fut,
    Fut: Future<Output = Result<Replayed, Err>>,
    Err: Display,
{
    let mut report = Report::default();
    for example in examples {
        let label = example.label();
        match check(example.clone()).await {
            Ok(Replayed::Ok) => report.passed.push(label),
            Ok(Replayed::Skipped(why)) => report.skipped.push((label, why)),
            Ok(Replayed::Failed(why)) => report.failed.push((label, why)),
            Err(error) => report.failed.push((label, error.to_string())),
        }
    }
    report
}

/// An example that can name itself in a report.
pub trait Labelled {
    fn label(&self) -> String;
}

impl Labelled for crate::HttpExample {
    fn label(&self) -> String {
        format!("{} ({})", self.function, self.id)
    }
}

impl Labelled for crate::WsExample {
    fn label(&self) -> String {
        format!("{} ({})", self.function, self.id)
    }
}
