//! The `seek` walk (ADR 0013) against a fake venue: every row exactly once, requests never
//! outside the caller's own range, and the two `LogicError`s.

use std::sync::{Arc, Mutex};

use serde::{Deserialize, Serialize};
use truewire_core::serde_json::{json, Value};
use truewire_core::{
    Error, PaginatedResponse, Result, Seek, SeekState, SpanUnit, TimestampIso, TimestampMillis, TimestampSeconds,
};

/// A venue serving one candle per tick over the inclusive range `[start, end]`, keeping the
/// `cap` rows nearest `anchor`. Rows are `[time, "o"]`.
fn candles(start: i64, end: i64, anchor_end: bool, cap: usize) -> Vec<Value> {
    let rows: Vec<Value> = (start..=end).map(|t| json!([t, "o"])).collect();
    if anchor_end {
        rows[rows.len().saturating_sub(cap)..].to_vec()
    } else {
        rows.into_iter().take(cap).collect()
    }
}

type Calls = Arc<Mutex<Vec<(Option<i64>, Option<i64>)>>>;

fn walk(
    seek: Seek,
    start: Option<i64>,
    end: Option<i64>,
    anchor_end: bool,
    cap: usize,
) -> (PaginatedResponse<Value, SeekState<i64, Value>>, Calls) {
    let calls: Calls = Arc::default();
    let log = calls.clone();
    let init = SeekState::new(if anchor_end { end } else { start });
    let next = move |state: SeekState<i64, Value>| {
        let seek = seek.clone();
        let log = log.clone();
        async move {
            let (request_start, request_end) = if anchor_end {
                (start, state.pos)
            } else {
                (state.pos, end)
            };
            log.lock().unwrap().push((request_start, request_end));
            let rows = candles(request_start.unwrap_or(0), request_end.unwrap_or(9), anchor_end, cap);
            let far = if anchor_end { start } else { end };
            seek.step(&state, rows, None, far.as_ref())
        }
    };
    (PaginatedResponse::new(init, next), calls)
}

fn times(pages: &[Value]) -> Vec<i64> {
    pages.iter().map(|row| row[0].as_i64().unwrap()).collect()
}

#[tokio::test]
async fn an_ascending_walk_covers_a_range_wider_than_one_page() {
    let (paged, calls) = walk(
        Seek::new("candles_paged", "[-1][0]", true, false).cap(Some(3)),
        Some(0),
        Some(9),
        false,
        3,
    );
    let rows = paged.await.unwrap();
    assert_eq!(times(&rows), (0..10).collect::<Vec<_>>());
    let starts: Vec<_> = calls.lock().unwrap().iter().map(|call| call.0.unwrap()).collect();
    assert_eq!(starts, vec![0, 2, 4, 6, 8]);
}

#[tokio::test]
async fn a_descending_walk_moves_the_end_bound() {
    let (paged, calls) = walk(
        Seek::new("candles_paged", "[-1][0]", true, true).cap(Some(3)),
        Some(0),
        Some(9),
        true,
        3,
    );
    let mut pages = Vec::new();
    let mut stream = paged.rows();
    use futures::StreamExt;
    while let Some(page) = stream.next().await {
        pages.push(times(&page.unwrap()));
    }
    assert_eq!(pages, vec![vec![7, 8, 9], vec![5, 6], vec![3, 4], vec![1, 2], vec![0]]);
    let ends: Vec<_> = calls.lock().unwrap().iter().map(|call| call.1.unwrap()).collect();
    assert_eq!(ends, vec![9, 7, 5, 3, 1]);
}

#[tokio::test]
async fn without_a_cap_the_walk_confirms_exhaustion_with_one_more_request() {
    let (paged, calls) = walk(
        Seek::new("candles_paged", "[-1][0]", true, false),
        Some(0),
        Some(4),
        false,
        3,
    );
    assert_eq!(times(&paged.await.unwrap()), vec![0, 1, 2, 3, 4]);
    let starts: Vec<_> = calls.lock().unwrap().iter().map(|call| call.0.unwrap()).collect();
    assert_eq!(starts, vec![0, 2, 4]);
}

#[tokio::test]
async fn a_short_page_ends_the_walk_when_a_cap_resolves() {
    let (paged, calls) = walk(
        Seek::new("candles_paged", "[-1][0]", true, false).cap(Some(3)),
        Some(0),
        Some(1),
        false,
        3,
    );
    paged.await.unwrap();
    assert_eq!(calls.lock().unwrap().len(), 1);
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
struct Fill {
    t: TimestampMillis,
    px: String,
}

fn fill(t: i64, px: &str) -> Fill {
    truewire_core::decode(json!({"t": t, "px": px})).unwrap()
}

#[test]
fn a_non_unique_cursor_dedups_by_content_and_refuses_a_vanished_row() {
    let seek = Seek::new("fills_paged", "[-1].t", false, false).cap(Some(2));
    let first = SeekState::new(Some(fill(0, "a").t));
    let (fresh, next) = seek.step(&first, vec![fill(1, "a"), fill(2, "b")], None, None).unwrap();
    assert_eq!(fresh.len(), 2);
    let next = next.unwrap();
    assert_eq!(next.pos, Some(fill(2, "b").t));
    assert_eq!(next.carried, vec![fill(2, "b")]);
    let (fresh, following) = seek.step(&next, vec![fill(2, "b"), fill(3, "c")], None, None).unwrap();
    assert_eq!(fresh, vec![fill(3, "c")], "the carried row is dropped by content");
    assert_eq!(following.unwrap().pos, Some(fill(3, "c").t));
    let error = seek
        .step(&next, vec![fill(2, "z"), fill(3, "c")], None, None)
        .unwrap_err();
    assert!(
        error.is_logic() && error.message().contains("no longer returned"),
        "{error}"
    );
}

#[test]
fn a_full_page_on_one_key_is_a_logic_error() {
    let seek = Seek::new("fills_paged", "[-1].t", false, false).cap(Some(2));
    let state = SeekState::new(Some(fill(5, "a").t));
    let error: Error = seek
        .step(&state, vec![fill(5, "a"), fill(5, "b")], None, None)
        .unwrap_err();
    assert!(
        error.is_logic() && error.message().contains("all sharing one `t` value"),
        "{error}"
    );
    // Messages print the field relative to one row, never the spec's `[-1]`.
    assert!(!error.message().contains("[-1]"), "{error}");
}

#[test]
fn a_span_moves_in_chunks_up_to_the_far_bound() {
    let seek = Seek::new("candles_paged", "[-1][0]", true, false)
        .cap(Some(100))
        .span(4, SpanUnit::Millis);
    let far = 9_i64;
    let state = SeekState::<i64, Value>::new(Some(0));
    let edge = seek.edge(state.pos.as_ref(), Some(&far)).unwrap();
    assert_eq!(edge, Some(4));
    let (fresh, next) = seek
        .step(&state, candles(0, 4, false, 100), edge.as_ref(), Some(&far))
        .unwrap();
    assert_eq!(times(&fresh), vec![0, 1, 2, 3, 4]);
    let next = next.unwrap();
    assert_eq!(next.pos, Some(4));
    let edge = seek.edge(next.pos.as_ref(), Some(&far)).unwrap();
    assert_eq!(edge, Some(8));
    let state = SeekState::<i64, Value>::new(Some(8));
    let edge = seek.edge(state.pos.as_ref(), Some(&far)).unwrap();
    assert_eq!(edge, Some(9), "never past the caller's own far bound");
    let (_, next) = seek
        .step(&state, candles(8, 9, false, 100), edge.as_ref(), Some(&far))
        .unwrap();
    assert!(next.is_none());
    assert!(seek.edge::<i64>(None, Some(&far)).unwrap_err().is_logic());
    let millis = fill(1_000, "a").t;
    let moved = seek.edge(Some(&millis), Some(&fill(10_000, "a").t)).unwrap().unwrap();
    assert_eq!(moved, fill(1_004, "a").t);
}

#[test]
fn a_string_id_takes_the_last_row_and_numerals_read_as_numbers() -> Result<()> {
    let seek = Seek::new("ledger_paged", "[-1].id", true, true).cap(Some(2));
    let state = SeekState::<String, Value>::new(None);
    let (_, next) = seek.step(&state, vec![json!({"id": "b9"}), json!({"id": "a1"})], None, None)?;
    assert_eq!(next.unwrap().pos, Some("a1".to_string()));
    let numeric = Seek::new("ledger_paged", "[-1].id", true, false).cap(Some(2));
    let key: Option<i64> = numeric.key(&json!({"id": "42"}))?;
    assert_eq!(key, Some(42));
    let key: Option<String> = numeric.key(&json!({"id": 42}))?;
    assert_eq!(key.as_deref(), Some("42"));
    let key: Option<i64> = numeric.key(&json!({"other": 1}))?;
    assert_eq!(key, None);
    Ok(())
}

#[test]
fn a_cursor_in_its_own_timestamp_format_converts_to_the_bound() -> Result<()> {
    // lighter's fundings: a row `timestamp` in epoch seconds, the bound in milliseconds (TRU-197).
    let seek = Seek::new("fundings_paged", "[-1].timestamp", true, true).cap(Some(2));
    let millis = |value: i64| -> TimestampMillis { truewire_core::serde_json::from_value(json!(value)).unwrap() };
    let key: Option<TimestampMillis> = seek
        .clone()
        .cursor::<TimestampSeconds>()
        .key(&json!({"timestamp": 1783616400}))?;
    assert_eq!(key, Some(millis(1_783_616_400_000)));
    let key: Option<TimestampMillis> = seek
        .clone()
        .cursor::<TimestampIso>()
        .key(&json!({"timestamp": "2026-07-09T17:00:00Z"}))?;
    assert_eq!(key, Some(millis(1_783_616_400_000)));
    // Without it the row value is read as the bound's own type, as before.
    let key: Option<TimestampMillis> = seek.clone().key(&json!({"timestamp": 1783616400}))?;
    assert_eq!(key, Some(millis(1_783_616_400)));
    let state = SeekState::<TimestampMillis, Value>::new(Some(millis(1_783_700_000_000)));
    let rows = vec![json!({"timestamp": 1783616400}), json!({"timestamp": 1783620000})];
    let (_, next) = seek
        .clone()
        .cursor::<TimestampSeconds>()
        .step(&state, rows, None, None)?;
    assert_eq!(next.unwrap().pos, Some(millis(1_783_616_400_000)));
    // A timestamp cursor under an id bound is refused, not guessed.
    let refused = seek
        .cursor::<TimestampSeconds>()
        .key::<i64, _>(&json!({"timestamp": 1783616400}));
    assert!(matches!(refused, Err(Error::Validation(_))), "{refused:?}");
    Ok(())
}
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
struct Trade {
    id: i64,
    time: i64,
}

/// Trades 1..=5, one a second, except trade 4, stamped past everyone's `endTime`.
fn trades(from: i64, cap: usize) -> Vec<Trade> {
    (from..=5)
        .map(|id| Trade {
            id,
            time: if id == 4 { 9_000 } else { id * 1_000 },
        })
        .take(cap)
        .collect()
}

async fn walk_until(limit: Option<TimestampMillis>) -> (Vec<i64>, Vec<i64>) {
    let seek = Seek::new("trades_paged", "[-1].id", true, false)
        .cap(Some(2))
        .until("[-1].time");
    let calls: Arc<Mutex<Vec<i64>>> = Arc::default();
    let log = calls.clone();
    let next = move |state: SeekState<i64, Trade>| {
        let seek = seek.clone();
        let log = log.clone();
        async move {
            let from = state.pos.unwrap_or(1);
            log.lock().unwrap().push(from);
            seek.step_until(&state, trades(from, 2), None, None, limit.as_ref())
        }
    };
    let rows = PaginatedResponse::new(SeekState::new(Some(1)), next).await.unwrap();
    let ids = rows.iter().map(|trade| trade.id).collect();
    let calls = calls.lock().unwrap().clone();
    (ids, calls)
}

#[tokio::test]
async fn an_until_bound_drops_the_row_past_it_and_ends_on_its_page() {
    let limit: TimestampMillis = serde_json::from_value(json!(5_000)).unwrap();
    assert_eq!(walk_until(Some(limit)).await, (vec![1, 2, 3], vec![1, 2, 3]));
}

#[tokio::test]
async fn without_an_until_value_the_walk_runs_on() {
    assert_eq!(walk_until(None).await.0, vec![1, 2, 3, 4, 5]);
}

#[test]
fn every_row_past_the_until_bound_is_dropped_not_only_the_first() -> Result<()> {
    // One page, [3, 4 (past), 5]: the in-range trade after the past one is kept.
    let seek = Seek::new("trades_paged", "[-1].id", true, false)
        .cap(Some(3))
        .until("[-1].time");
    let bound: TimestampMillis = serde_json::from_value(json!(5_000)).unwrap();
    let (rows, next) = seek.step_until(&SeekState::new(Some(3_i64)), trades(3, 3), None, None, Some(&bound))?;
    assert_eq!(rows.iter().map(|trade| trade.id).collect::<Vec<_>>(), vec![3, 5]);
    assert!(next.is_none());
    Ok(())
}

#[test]
fn a_bound_on_a_walk_with_no_until_field_is_a_logic_error() {
    let seek = Seek::new("trades_paged", "[-1].id", true, false).cap(Some(3));
    let bound: TimestampMillis = serde_json::from_value(json!(5_000)).unwrap();
    let stepped = seek.step_until(&SeekState::new(Some(3_i64)), trades(3, 3), None, None, Some(&bound));
    assert!(matches!(stepped, Err(Error::Logic(_))));
}

#[test]
fn cursor_conversion_does_not_change_the_exclusive_far_field_codec() -> Result<()> {
    let seek = Seek::new("fundings_paged", "[-1].timestamp", true, true)
        .cap(Some(3))
        .cursor::<TimestampSeconds>()
        .until("[-1].settled_at");
    let millis = |value: i64| -> TimestampMillis { serde_json::from_value(json!(value)).unwrap() };
    let state = SeekState::<TimestampMillis, Value>::new(Some(millis(1_783_700_000_000)));
    let rows = vec![
        json!({"timestamp": 1783620000_i64, "settled_at": 1783620000000_i64}),
        json!({"timestamp": 1783616400_i64, "settled_at": 1783616400000_i64}),
        json!({"timestamp": 1783609200_i64, "settled_at": 1783609200000_i64}),
    ];
    let (kept, next) = seek.step_until(&state, rows.clone(), None, None, Some(&millis(1_783_616_400_000)))?;
    assert_eq!(kept, rows[..2]);
    assert!(next.is_none());
    Ok(())
}

#[test]
fn a_cursor_on_the_rows_last_element_reads_that_element() -> Result<()> {
    let last = Seek::new("trades_paged", "[-1][-1]", true, false);
    assert_eq!(last.key::<i64, _>(&json!(["o", 7]))?, Some(7));
    let nested = Seek::new("trades_paged", "[-1][-1][0]", true, false);
    assert_eq!(nested.key::<i64, _>(&json!(["o", [7, 1]]))?, Some(7));
    Ok(())
}

#[test]
fn a_bare_row_cursor_keys_on_the_row_and_names_it_in_words() -> Result<()> {
    let seek = Seek::new("ticks_paged", "[-1]", true, false).cap(Some(2));
    assert_eq!(seek.key::<i64, _>(&json!(7))?, Some(7));
    let error = seek.key::<i64, _>(&json!("seven")).unwrap_err();
    assert!(error.message().starts_with("`ticks_paged`: the row "), "{error}");
    let state = SeekState::new(Some(5_i64));
    let error = seek.step(&state, vec![json!(5), json!(5)], None, None).unwrap_err();
    assert!(error.message().contains("all sharing one row value"), "{error}");
    assert!(!error.message().contains("``"), "{error}");
    Ok(())
}
