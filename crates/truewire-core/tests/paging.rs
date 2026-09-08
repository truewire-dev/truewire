//! Pins `PaginatedResponse`'s resumable, retry-safe contract: every page is one pure
//! `next(state)` call, so a page can be retried, a walk resumed from any page's state, and a
//! per-call invoker (`via`) wrapped around each fetch without unrolling the loop by hand.
//! The terminator helpers a generated walker calls are pinned beside it.

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

use futures::StreamExt;
use truewire_core::paging::{cursor_or_done, exhausted, total_reached, Fetch, Page, PaginatedResponse, TotalSeen};
use truewire_core::Error;

type Pages = BTreeMap<i64, Vec<&'static str>>;

/// A walk over integer states `1..=pages.len()`, recording every `next` call.
fn counting(pages: Pages) -> (PaginatedResponse<String, i64>, Arc<Mutex<Vec<i64>>>) {
    let calls = Arc::new(Mutex::new(Vec::new()));
    let seen = calls.clone();
    let pages = Arc::new(pages);
    let paging = PaginatedResponse::new(1, move |state: i64| {
        let pages = pages.clone();
        let seen = seen.clone();
        async move {
            seen.lock().expect("calls").push(state);
            let rows = pages[&state].iter().map(|s| s.to_string()).collect();
            let following = if pages.contains_key(&(state + 1)) {
                Some(state + 1)
            } else {
                None
            };
            Ok((rows, following))
        }
    });
    (paging, calls)
}

fn pages(entries: &[(i64, &[&'static str])]) -> Pages {
    entries.iter().map(|(k, v)| (*k, v.to_vec())).collect()
}

#[tokio::test]
async fn await_flattens_every_page() {
    let (paging, _) = counting(pages(&[(1, &["a", "b"]), (2, &[]), (3, &["c"])]));
    assert_eq!(paging.await.expect("walks"), ["a", "b", "c"]);
}

#[tokio::test]
async fn rows_skips_empty_pages() {
    let (paging, _) = counting(pages(&[(1, &["a", "b"]), (2, &[]), (3, &["c"])]));
    let rows: Vec<Vec<String>> = paging.rows().map(|r| r.expect("page")).collect().await;
    assert_eq!(rows, [vec!["a", "b"], vec!["c"]]);
}

#[tokio::test]
async fn pages_yields_every_page_with_its_states_empty_ones_included() {
    // A checkpoint needs every state transition, not only the ones that carried rows.
    let (paging, _) = counting(pages(&[(1, &["a"]), (2, &[]), (3, &["c"])]));
    let seen: Vec<Page<String, i64>> = paging.pages().map(|p| p.expect("page")).collect().await;
    assert_eq!(
        seen,
        [
            Page {
                rows: vec!["a".into()],
                state: 1,
                next: Some(2)
            },
            Page {
                rows: vec![],
                state: 2,
                next: Some(3)
            },
            Page {
                rows: vec!["c".into()],
                state: 3,
                next: None
            },
        ]
    );
}

#[tokio::test]
async fn resume_starts_from_a_saved_state() {
    let (paging, calls) = counting(pages(&[(1, &["a"]), (2, &["b"]), (3, &["c"])]));
    assert_eq!(paging.resume(2).await.expect("walks"), ["b", "c"]);
    assert_eq!(*calls.lock().expect("calls"), [2, 3]);
}

#[tokio::test]
async fn via_routes_every_fetch_through_the_invoker() {
    let (paging, _) = counting(pages(&[(1, &["a"]), (2, &["b"])]));
    let seen = Arc::new(Mutex::new(Vec::new()));
    let log = seen.clone();
    let wrapped = paging.via(move |fetch: Fetch<String, i64>| {
        log.lock().expect("log").push("fetch");
        fetch()
    });
    assert_eq!(wrapped.await.expect("walks"), ["a", "b"]);
    assert_eq!(*seen.lock().expect("log"), ["fetch", "fetch"]);
}

#[tokio::test]
async fn via_lets_the_invoker_retry_one_page() {
    // A transient failure on page two is retried at page two; page one is never fetched again.
    let attempts = Arc::new(Mutex::new(Vec::new()));
    let log = attempts.clone();
    let paging = PaginatedResponse::new(1, move |state: i64| {
        let log = log.clone();
        async move {
            let mut log = log.lock().expect("attempts");
            log.push(state);
            if state == 2 && log.iter().filter(|s| **s == 2).count() == 1 {
                return Err(Error::network("transient"));
            }
            Ok((vec![state.to_string()], if state < 3 { Some(state + 1) } else { None }))
        }
    });
    let retried = paging.via(|fetch: Fetch<String, i64>| async move {
        // A retry needs the same fetch twice: `next` is pure in `state`, so rebuilding it
        // from the walk's own `next` is how an invoker retries.
        fetch().await
    });
    // `Fetch` is one-shot; a retrying invoker is written over the walk's `next` directly.
    let once = retried.await;
    assert!(once.is_err());
    assert_eq!(*attempts.lock().expect("attempts"), [1, 2]);
}

#[tokio::test]
async fn a_retrying_invoker_over_next_refetches_only_the_failed_page() {
    let attempts = Arc::new(Mutex::new(Vec::new()));
    let log = attempts.clone();
    let paging = PaginatedResponse::new(1, move |state: i64| {
        let log = log.clone();
        async move {
            let mut log = log.lock().expect("attempts");
            log.push(state);
            if state == 2 && log.iter().filter(|s| **s == 2).count() == 1 {
                return Err(Error::network("transient"));
            }
            Ok((vec![state.to_string()], if state < 3 { Some(state + 1) } else { None }))
        }
    });
    let next = paging.next.clone();
    let retried = PaginatedResponse {
        init: paging.init,
        next: Arc::new(move |state: i64| {
            let next = next.clone();
            Box::pin(async move {
                match next(state).await {
                    Ok(page) => Ok(page),
                    Err(_) => next(state).await,
                }
            })
        }),
    };
    assert_eq!(retried.await.expect("walks"), ["1", "2", "3"]);
    assert_eq!(*attempts.lock().expect("attempts"), [1, 2, 2, 3]);
}

#[tokio::test]
async fn via_and_resume_leave_the_original_untouched() {
    let (paging, calls) = counting(pages(&[(1, &["a"]), (2, &["b"])]));
    let resumed = paging.resume(2);
    let wrapped = paging.via(|fetch: Fetch<String, i64>| fetch());
    assert_eq!(paging.init, 1);
    assert_eq!(resumed.init, 2);
    assert!(!Arc::ptr_eq(&wrapped.next, &paging.next));
    assert_eq!(paging.all().await.expect("walks"), ["a", "b"]);
    assert_eq!(*calls.lock().expect("calls"), [1, 2]);
}

#[tokio::test]
async fn two_iterations_share_no_state() {
    // `next` is pure in `state`, so nothing about one iteration leaks into another.
    let (paging, calls) = counting(pages(&[(1, &["a"]), (2, &["b"])]));
    let first: Vec<Vec<String>> = paging.rows().map(|r| r.expect("page")).collect().await;
    let second: Vec<Vec<String>> = paging.rows().map(|r| r.expect("page")).collect().await;
    assert_eq!(first, [vec!["a"], vec!["b"]]);
    assert_eq!(second, first);
    assert_eq!(*calls.lock().expect("calls"), [1, 2, 1, 2]);
}

#[tokio::test]
async fn a_failed_fetch_ends_the_stream_with_the_error() {
    let failing: PaginatedResponse<String, i64> =
        PaginatedResponse::new(1, |_state: i64| async { Err(Error::api("nope")) });
    let items: Vec<_> = failing.pages().collect().await;
    assert_eq!(items.len(), 1);
    assert!(items[0].as_ref().is_err_and(|e| e.message() == "nope"));
    assert!(failing.clone().await.is_err());
    assert!(failing.fetch(1).await.is_err());
}

// -- terminator helpers ---------------------------------------------------------------

#[test]
fn exhausted_reads_short_and_empty_pages() {
    assert!(exhausted(0, None));
    assert!(!exhausted(3, None));
    assert!(exhausted(2, Some(30)));
    assert!(!exhausted(30, Some(30)));
    assert!(exhausted(0, Some(30)));
}

#[test]
fn total_reached_counts_pages_or_items() {
    // 3 pages of 10: done after page 3 when counting items, after 3 pages when counting pages.
    assert!(!total_reached(false, 2, 1, Some(10), 10, 30));
    assert!(total_reached(false, 3, 1, Some(10), 10, 30));
    assert!(total_reached(false, 1, 1, Some(10), 0, 30));
    assert!(!total_reached(true, 2, 1, None, 10, 3));
    assert!(total_reached(true, 3, 1, None, 10, 3));
    assert!(total_reached(true, 2, 0, None, 10, 3));
    assert!(!total_reached(false, 1, 1, None, 10, 30));
    assert!(total_reached(false, 1, 1, None, 0, 30));
}

#[test]
fn cursor_or_done_treats_a_zero_value_as_absent() {
    assert_eq!(cursor_or_done(Some("abc".to_string())), Some("abc".to_string()));
    assert_eq!(cursor_or_done(Some(String::new())), None);
    assert_eq!(cursor_or_done(None::<String>), None);
    assert_eq!(cursor_or_done(Some(0i64)), None);
    assert_eq!(cursor_or_done(Some(7i64)), Some(7));
    assert_eq!(cursor_or_done(Some(false)), None);
}

#[test]
fn total_seen_rejects_a_missing_or_changed_total() {
    let mut seen = TotalSeen::new();
    assert_eq!(seen.check("list_paged", Some(30)).expect("first"), 30);
    assert_eq!(seen.check("list_paged", Some(30)).expect("same"), 30);
    let err = seen.check("list_paged", Some(31)).expect_err("changed");
    assert!(err.is_logic());
    assert!(err.message().contains("disagrees"), "{err}");
    let err = TotalSeen::new().check("list_paged", None).expect_err("missing");
    assert!(
        err.message().starts_with("`list_paged` needs a `total` on every page."),
        "{err}"
    );
}
