//! Resumable, retry-safe pagination: a walk is a pure `next(state)` step, not a generator.
//!
//! A generated `<method>_paged` returns a [`PaginatedResponse`] rather than a bare stream
//! because a stream that fails is dead: nothing can retry the one page that failed and
//! carry on. Here every page is one call to `next(state)`, and the contract below is what
//! makes calling it again safe.
//!
//! The contract every `next` must honour:
//!
//! - `next` is a pure function of `state`. It reads no captured mutable variable, no
//!   clock. Calling it twice with the same `state` makes the same request.
//! - `state` fully determines the request. Any bound the API would otherwise default at
//!   call time is pinned into `init` once, before the first page.
//! - The request is a read. Repeating it never changes anything upstream.
//!
//! Under that contract a caller may retry `next(state)` after a transient failure, resume a
//! walk from any page's [`Page::next`], or run two iterations of one response concurrently,
//! and see exactly the pages a single uninterrupted walk would have produced.
//!
//! The helpers at the bottom ([`TotalSeen`], [`exhausted`], [`total_reached`],
//! [`cursor_or_done`]) are the terminator checks a generated walker performs on each
//! page, written once here for the `page`, `token`, `offset` and `seek` strategies the
//! plan declares.

use std::future::{Future, IntoFuture};
use std::pin::Pin;
use std::sync::Arc;

use futures::future::BoxFuture;
use futures::stream::{self, Stream, StreamExt};

use crate::errors::{Error, Result};

/// One page of a walk, with the state on either side of it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Page<T, S> {
    /// Rows this page carried. May be empty.
    pub rows: Vec<T>,
    /// State this page was fetched with. Re-fetching from it yields this page again.
    pub state: S,
    /// State the following page is fetched with, or `None` when this was the last page.
    pub next: Option<S>,
}

/// What one page fetch returns: the rows and the following state, `None` after the last page.
pub type Fetched<T, S> = Result<(Vec<T>, Option<S>)>;

/// Fetch one page from a state.
pub type Next<T, S> = Arc<dyn Fn(S) -> BoxFuture<'static, Fetched<T, S>> + Send + Sync>;

/// One zero-argument page fetch, as handed to an [`Invoker`].
pub type Fetch<T, S> = Box<dyn FnOnce() -> BoxFuture<'static, Fetched<T, S>> + Send>;

/// Per-fetch invoker: receives one zero-argument fetch and returns its result.
pub type Invoker<T, S> = Arc<dyn Fn(Fetch<T, S>) -> BoxFuture<'static, Fetched<T, S>> + Send + Sync>;

/// The stream [`PaginatedResponse::pages`] returns.
pub type PageStream<'a, T, S> = Pin<Box<dyn Stream<Item = Result<Page<T, S>>> + Send + 'a>>;

/// The stream [`PaginatedResponse::rows`] returns.
pub type RowStream<'a, T> = Pin<Box<dyn Stream<Item = Result<Vec<T>>> + Send + 'a>>;

/// A paginated walk: `init` is the first page's state, `next` fetches one page from a
/// state and returns its rows plus the following state, `None` once the walk is done.
///
/// Awaitable (`.await` flattens every page into one `Vec`, as [`all`](Self::all) does) and
/// streamable ([`rows`](Self::rows): one page's rows at a time, empty pages skipped).
/// [`pages`](Self::pages) additionally exposes each page's own state, for checkpointing;
/// [`resume`](Self::resume) restarts from a saved one; [`via`](Self::via) routes every page
/// fetch through a caller-supplied invoker, which is how a retry or logging layer wraps
/// each page as one ordinary call without ever unrolling the loop by hand.
///
/// `next` must honour the contract in this module's docs: pure in `state`, no clock,
/// read-only on the wire. Nothing here can enforce it, but everything here assumes it.
///
/// ```ignore
/// let paging = client.account.trades_paged(request);
/// let trades = paging.clone().await?;                          // every row, flattened
/// let mut rows = paging.rows();
/// while let Some(page) = rows.next().await { ... }             // one page at a time
/// let mut pages = paging.via(retried).pages();
/// while let Some(page) = pages.next().await { checkpoint(page?.next) }
/// ```
pub struct PaginatedResponse<T, S> {
    /// State the first page is fetched with.
    pub init: S,
    /// Fetch one page: `(rows, next_state)`, `next_state` being `None` after the last page.
    pub next: Next<T, S>,
}

impl<T, S> Clone for PaginatedResponse<T, S>
where
    S: Clone,
{
    fn clone(&self) -> Self {
        Self {
            init: self.init.clone(),
            next: self.next.clone(),
        }
    }
}

impl<T, S> std::fmt::Debug for PaginatedResponse<T, S>
where
    S: std::fmt::Debug,
{
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("PaginatedResponse")
            .field("init", &self.init)
            .finish_non_exhaustive()
    }
}

impl<T, S> PaginatedResponse<T, S>
where
    T: Send + 'static,
    S: Clone + Send + Sync + 'static,
{
    /// A walk from `init` through `next`.
    pub fn new<F, Fut>(init: S, next: F) -> Self
    where
        F: Fn(S) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Fetched<T, S>> + Send + 'static,
    {
        Self {
            init,
            next: Arc::new(move |state| Box::pin(next(state))),
        }
    }

    /// Fetch the one page `state` names.
    pub async fn fetch(&self, state: S) -> Fetched<T, S> {
        (self.next)(state).await
    }

    /// Every page, empty ones included, each with the state before and after it. The
    /// stream ends after the last page, or after the first failed fetch.
    pub fn pages(&self) -> PageStream<'_, T, S> {
        let next = self.next.clone();
        Box::pin(stream::unfold(Some(self.init.clone()), move |state| {
            let next = next.clone();
            async move {
                let state = state?;
                match next(state.clone()).await {
                    Ok((rows, following)) => {
                        let page = Page {
                            rows,
                            state,
                            next: following.clone(),
                        };
                        Some((Ok(page), following))
                    }
                    Err(e) => Some((Err(e), None)),
                }
            }
        }))
    }

    /// Rows of each non-empty page, in walk order.
    pub fn rows(&self) -> RowStream<'_, T> {
        Box::pin(self.pages().filter_map(|page| async move {
            match page {
                Ok(page) if page.rows.is_empty() => None,
                Ok(page) => Some(Ok(page.rows)),
                Err(e) => Some(Err(e)),
            }
        }))
    }

    /// Every row of every page, flattened.
    pub async fn all(&self) -> Result<Vec<T>> {
        let mut out = Vec::new();
        let mut rows = self.rows();
        while let Some(page) = rows.next().await {
            out.extend(page?);
        }
        Ok(out)
    }

    /// The same walk, started from `state` instead of `init`: a state a previous page
    /// reported as [`Page::next`] (or [`Page::state`], to refetch that page itself).
    pub fn resume(&self, state: S) -> Self {
        Self {
            init: state,
            next: self.next.clone(),
        }
    }

    /// The same walk, with every page fetch routed through `call`.
    ///
    /// `call` receives a zero-argument function performing one `next(state)` and returns
    /// its result, so a retry policy, a logger, or any other per-call middleware sees each
    /// page as one plain call. Purity of `next` is what makes wrapping it this way safe: a
    /// retried fetch is just the same page fetched again.
    pub fn via<F, Fut>(&self, call: F) -> Self
    where
        F: Fn(Fetch<T, S>) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Fetched<T, S>> + Send + 'static,
    {
        let fetch = self.next.clone();
        let call: Invoker<T, S> = Arc::new(move |f| Box::pin(call(f)));
        Self {
            init: self.init.clone(),
            next: Arc::new(move |state: S| {
                let fetch = fetch.clone();
                let once: Fetch<T, S> = Box::new(move || fetch(state));
                call(once)
            }),
        }
    }
}

impl<T, S> IntoFuture for PaginatedResponse<T, S>
where
    T: Send + 'static,
    S: Clone + Send + Sync + 'static,
{
    type Output = Result<Vec<T>>;
    type IntoFuture = BoxFuture<'static, Result<Vec<T>>>;

    /// `.await` on the response is [`all`](Self::all): every row of every page.
    fn into_future(self) -> Self::IntoFuture {
        Box::pin(async move { self.all().await })
    }
}

// -- terminator helpers ---------------------------------------------------------------

/// Whether a page ends a `short_page`/`empty` walk: no rows, or (for `short_page` with a
/// page size the walk knows) fewer rows than the size. `size` is `None` when the
/// declaration measures against no size, or the size was left to the API's own default the
/// spec does not document.
pub fn exhausted(rows: usize, size: Option<usize>) -> bool {
    rows == 0 || size.is_some_and(|size| rows < size)
}

/// Whether a `page` walk ended by `total` is done after the page at `state` (1-based from
/// `start`): the number of pages seen reached `total` when it counts pages, or the rows
/// seen reached it when it counts items and the size is known; with no size to count by,
/// an empty page.
pub fn total_reached(counts_pages: bool, state: i64, start: i64, size: Option<usize>, rows: usize, total: i64) -> bool {
    let pages = state - start + 1;
    if counts_pages {
        return pages >= total;
    }
    match size {
        None => rows == 0,
        Some(size) => pages.saturating_mul(size as i64) >= total || rows == 0,
    }
}

/// The next cursor of a `token`/`seek` walk: `None` when the reply carried none, or an
/// empty one, which the `absent_cursor` terminator reads as the end.
pub fn cursor_or_done<S: CursorLike>(cursor: Option<S>) -> Option<S> {
    cursor.filter(|c| !c.is_zero())
}

/// A cursor type with a zero value the walk reads as "absent": `""`, `0`, `false`. The
/// plan's `has_zero_value` decides which state types seed a walker; these are them.
pub trait CursorLike {
    fn is_zero(&self) -> bool;
}

impl CursorLike for String {
    fn is_zero(&self) -> bool {
        self.is_empty()
    }
}

impl CursorLike for i64 {
    fn is_zero(&self) -> bool {
        *self == 0
    }
}

impl CursorLike for f64 {
    fn is_zero(&self) -> bool {
        *self == 0.0
    }
}

impl CursorLike for bool {
    fn is_zero(&self) -> bool {
        !*self
    }
}

/// The `total` a `page`/`offset` walk read on earlier pages, to check each new page's
/// against: a page that omits it, or reports a different value than an earlier page of the
/// same walk, is a `LogicError` (the API changed under the walk; retry it from the start).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct TotalSeen {
    seen: Option<i64>,
}

impl TotalSeen {
    pub fn new() -> Self {
        Self::default()
    }

    /// Record this page's `total` and return it, or fail as described above. `walker` names
    /// the generated method in the message.
    pub fn check(&mut self, walker: &str, total: Option<i64>) -> Result<i64> {
        match (total, self.seen) {
            (None, _) => Err(Error::logic(format!(
                "`{walker}` needs a `total` on every page. The API omitted it here; retry the whole walk from the start."
            ))),
            (Some(total), Some(seen)) if total != seen => Err(Error::logic(format!(
                "`{walker}` needs a `total` on every page. The API reported a value ({total}) that disagrees with an \
                 earlier page of this same walk ({seen}); retry the whole walk from the start."
            ))),
            (Some(total), _) => {
                self.seen = Some(total);
                Ok(total)
            }
        }
    }
}
