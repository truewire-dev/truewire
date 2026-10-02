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
use serde::de::DeserializeOwned;
use serde::Serialize;
use serde_json::Value;

use crate::errors::{Error, Result};
use crate::types::{
    DateIso, IntegerString, TimestampIso, TimestampMicros, TimestampMicrosFloat, TimestampMicrosString,
    TimestampMillis, TimestampMillisFloat, TimestampMillisString, TimestampNanos, TimestampNanosFloat,
    TimestampNanosString, TimestampSeconds, TimestampSecondsFloat, TimestampSecondsString,
};

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
///
/// No generated walker uses it since ADR 0013, which dropped the strict check: a walk over
/// live data is racy whether or not `total` moves, and the shared state broke `next`'s
/// purity. `total` now only decides when to stop. Kept for code written against it.
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

// -- seek (ADR 0013) ------------------------------------------------------------------

/// The unit a `seek` walk's `span` is declared in. A timestamp bound moves by that much
/// time; a numeric bound (a block height, an id) moves by `span` of its own ticks.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum SpanUnit {
    Micros,
    Millis,
    Seconds,
}

/// A type a `seek` walk's moving bound can have: what a row's cursor field is read as and
/// compared with.
pub trait SeekKey: Clone + PartialEq + PartialOrd + DeserializeOwned + Send + Sync + 'static {
    /// Whether keys compare by order. A plain string id compares by equality only, and the
    /// last row of a page in wire order stands in for its extreme.
    const ORDERED: bool = true;

    /// This key moved by `span` (in `unit`, for a timestamp), backwards when `backwards`.
    fn shifted(&self, span: i64, unit: SpanUnit, backwards: bool) -> Self;

    /// This key as an instant: `Some` for a timestamp, `None` for an id or a day.
    fn instant(&self) -> Option<chrono::DateTime<chrono::Utc>> {
        None
    }

    /// The key at `instant`: `Some` for a timestamp, `None` for an id or a day.
    fn from_instant(_instant: chrono::DateTime<chrono::Utc>) -> Option<Self> {
        None
    }
}

impl SeekKey for i64 {
    fn shifted(&self, span: i64, _unit: SpanUnit, backwards: bool) -> Self {
        if backwards {
            self.saturating_sub(span)
        } else {
            self.saturating_add(span)
        }
    }
}

impl SeekKey for f64 {
    fn shifted(&self, span: i64, _unit: SpanUnit, backwards: bool) -> Self {
        if backwards {
            self - span as f64
        } else {
            self + span as f64
        }
    }
}

impl SeekKey for IntegerString {
    fn shifted(&self, span: i64, _unit: SpanUnit, backwards: bool) -> Self {
        let span = num_bigint::BigInt::from(span);
        Self(if backwards { &self.0 - span } else { &self.0 + span })
    }
}

impl SeekKey for String {
    const ORDERED: bool = false;

    /// A string id has no arithmetic; a span over one is refused by `truewire check`.
    fn shifted(&self, _span: i64, _unit: SpanUnit, _backwards: bool) -> Self {
        self.clone()
    }
}

fn span_duration(span: i64, unit: SpanUnit) -> chrono::Duration {
    match unit {
        SpanUnit::Micros => chrono::Duration::microseconds(span),
        SpanUnit::Millis => chrono::Duration::milliseconds(span),
        SpanUnit::Seconds => chrono::Duration::seconds(span),
    }
}

macro_rules! seek_timestamp {
    ($($name:ident),*) => {$(
        impl SeekKey for $name {
            fn shifted(&self, span: i64, unit: SpanUnit, backwards: bool) -> Self {
                let by = span_duration(span, unit);
                Self(if backwards { self.0 - by } else { self.0 + by })
            }

            fn instant(&self) -> Option<chrono::DateTime<chrono::Utc>> {
                Some(self.0)
            }

            fn from_instant(instant: chrono::DateTime<chrono::Utc>) -> Option<Self> {
                Some(Self(instant))
            }
        }
    )*};
}

seek_timestamp!(
    TimestampSecondsFloat,
    TimestampMillisFloat,
    TimestampMicrosFloat,
    TimestampNanosFloat,
    TimestampSecondsString,
    TimestampMillisString,
    TimestampMicrosString,
    TimestampNanosString,
    TimestampSeconds,
    TimestampMillis,
    TimestampMicros,
    TimestampNanos,
    TimestampIso
);

impl SeekKey for DateIso {
    fn shifted(&self, span: i64, unit: SpanUnit, backwards: bool) -> Self {
        let by = span_duration(span, unit);
        let moved = if backwards {
            self.to_datetime() - by
        } else {
            self.to_datetime() + by
        };
        Self::from_datetime(&moved)
    }
}

/// A `seek` walk's state: the value the moving bound is sent as (the caller's own bound
/// at first, `None` when omitted), and the rows already yielded that share that key, so
/// the venue re-serving them on the next page costs nothing.
#[derive(Debug, Clone, PartialEq)]
pub struct SeekState<K, T> {
    pub pos: Option<K>,
    pub carried: Vec<T>,
}

impl<K, T> SeekState<K, T> {
    /// The first state: the caller's own moving bound, nothing carried.
    pub fn new(pos: Option<K>) -> Self {
        Self {
            pos,
            carried: Vec::new(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum Segment {
    Key(String),
    Index(i64),
}

/// One `seek` declaration as a generated walker runs it (ADR 0013, `docs/pagination.md`
/// §2.4): which row field the bound follows, whether it is unique per row, which way the
/// walk moves, the row cap a full page is measured against, and the span.
///
/// A generated `<method>_paged` builds one, sends each request from the state's `pos`
/// (and, with a span, up to [`edge`](Self::edge)), and folds the page back through
/// [`step`](Self::step), which owns the whole algorithm: dedup of re-served boundary rows
/// (by key when unique, by content otherwise), moving the bound to the extreme key of a
/// full page, and the `LogicError`s for a full page stuck on one key or a carried row the
/// venue stopped serving.
///
/// A far bound the venue refuses beside the moving one (`exclusive.far`) is kept on the rows
/// instead: [`until`](Self::until) names the row field, and
/// [`step_until`](Self::step_until) drops every row past the caller's value and ends the
/// walk on the page that held one.
#[derive(Debug, Clone, PartialEq)]
pub struct Seek {
    walker: String,
    /// The cursor field as messages print it: relative to one row, empty for the row itself.
    field: String,
    segments: Vec<Segment>,
    unique: bool,
    descending: bool,
    cap: Option<usize>,
    span: Option<(i64, SpanUnit)>,
    cursor: Option<RowTime>,
    until: Option<(String, Vec<Segment>)>,
}

/// How [`Seek::key`] reads a row's cursor value whose timestamp format is not the bound's:
/// through the row field's own type (`name`), then as the instant it names.
#[derive(Clone, Copy)]
struct RowTime {
    name: &'static str,
    read: fn(Value) -> std::result::Result<chrono::DateTime<chrono::Utc>, String>,
}

impl PartialEq for RowTime {
    fn eq(&self, other: &Self) -> bool {
        self.name == other.name
    }
}

impl std::fmt::Debug for RowTime {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.name)
    }
}

fn read_instant<T: SeekKey>(value: Value) -> std::result::Result<chrono::DateTime<chrono::Utc>, String> {
    lenient_key::<T>(value)?
        .instant()
        .ok_or_else(|| format!("is not a timestamp as `{}`", std::any::type_name::<T>()))
}

impl Seek {
    /// `walker` names the generated method in errors; `field` is the declared
    /// `cursor.field` (`[-1][0]`, `[-1].t`). Messages print it relative to one row (`[0]`,
    /// `t`), and a bare `[-1]` as the row itself.
    pub fn new(walker: &str, field: &str, unique: bool, descending: bool) -> Self {
        Self {
            walker: walker.to_string(),
            field: row_relative(field),
            segments: parse_row_field(field),
            unique,
            descending,
            cap: None,
            span: None,
            cursor: None,
            until: None,
        }
    }

    /// Read the cursor field as `T`, the row field's own timestamp type, when it is not the
    /// bound's: a row `timestamp` in epoch seconds under an epoch-milliseconds bound
    /// (`.cursor::<TimestampSeconds>()`) is read as seconds, then compared and sent as the
    /// same instant in the bound's type. Without it, a row value is read as the bound's type.
    pub fn cursor<T: SeekKey>(mut self) -> Self {
        self.cursor = Some(RowTime {
            name: std::any::type_name::<T>(),
            read: read_instant::<T>,
        });
        self
    }

    /// The row cap a full page is measured against; `None` when none resolves, and the
    /// walk then continues until a page brings nothing new.
    pub fn cap(mut self, cap: Option<usize>) -> Self {
        self.cap = cap;
        self
    }

    /// The widest range one request may cover.
    pub fn span(mut self, span: i64, unit: SpanUnit) -> Self {
        self.span = Some((span, unit));
        self
    }

    /// The row field (`[-1].time`) a far bound the venue refuses beside the moving one is
    /// kept on, by [`step_until`](Self::step_until).
    pub fn until(mut self, field: &str) -> Self {
        self.until = Some((row_relative(field), parse_row_field(field)));
        self
    }

    /// The far bound this request is sent with when a span is declared: the moving bound
    /// shifted by the span, never past the caller's own far bound. `None` without a span.
    pub fn edge<K: SeekKey>(&self, pos: Option<&K>, far: Option<&K>) -> Result<Option<K>> {
        let Some((span, unit)) = self.span else {
            return Ok(None);
        };
        let (Some(pos), Some(far)) = (pos, far) else {
            return Err(Error::logic(format!(
                "`{}` walks a bounded range in spans: pass both bounds",
                self.walker
            )));
        };
        let shifted = pos.shifted(span, unit, self.descending);
        let past = if self.descending {
            shifted < *far
        } else {
            shifted > *far
        };
        Ok(Some(if past { far.clone() } else { shifted }))
    }

    /// One row's cursor key, read through its wire form: `None` when the field is absent
    /// or null. A numeral string is read as the number a numeric bound takes, and a number
    /// as the string a string bound takes. With [`cursor`](Self::cursor), the value is read
    /// as the row's own timestamp type and converted to the bound's.
    pub fn key<K: SeekKey, T: Serialize>(&self, row: &T) -> Result<Option<K>> {
        self.read(&self.segments, &self.field, row, self.cursor)
    }

    /// One row's field at `segments` (declared as `field`), read as a `K`.
    fn read<K: SeekKey, T: Serialize>(
        &self,
        segments: &[Segment],
        field: &str,
        row: &T,
        cursor: Option<RowTime>,
    ) -> Result<Option<K>> {
        let mut value = serde_json::to_value(row).map_err(|e| Error::validation(e.to_string()))?;
        for segment in segments {
            let next = match (segment, value) {
                (Segment::Key(key), Value::Object(mut map)) => map.remove(key),
                (Segment::Index(index), Value::Array(mut items)) => {
                    let at = if *index < 0 { items.len() as i64 + index } else { *index };
                    if at < 0 || at as usize >= items.len() {
                        None
                    } else {
                        Some(items.swap_remove(at as usize))
                    }
                }
                _ => None,
            };
            match next {
                Some(found) => value = found,
                None => return Ok(None),
            }
        }
        if value.is_null() {
            return Ok(None);
        }
        let invalid = |message: String| Error::validation(format!("`{}`: {} {message}", self.walker, the_field(field)));
        if let Some(cursor) = cursor {
            let instant = (cursor.read)(value).map_err(invalid)?;
            return K::from_instant(instant)
                .map(Some)
                .ok_or_else(|| invalid("is a timestamp, and the moving bound is not".to_string()));
        }
        lenient_key(value).map(Some).map_err(invalid)
    }

    /// "`t`", or "row" when the row itself is the key.
    fn named(&self) -> String {
        if self.field.is_empty() {
            "row".to_string()
        } else {
            format!("`{}`", self.field)
        }
    }

    /// Fold one fetched page into the rows to yield and the state after it.
    ///
    /// `edge` is the span edge this request was sent with ([`edge`](Self::edge)); `far` the
    /// caller's own far bound.
    pub fn step<K, T>(
        &self,
        state: &SeekState<K, T>,
        rows: Vec<T>,
        edge: Option<&K>,
        far: Option<&K>,
    ) -> Fetched<T, SeekState<K, T>>
    where
        K: SeekKey,
        T: Serialize + Clone + PartialEq,
    {
        self.fold(state, rows, edge, far, |_| Ok(false))
    }

    /// [`step`](Self::step), keeping `bound`, the caller's own value of the far bound
    /// [`until`](Self::until) names: a page holding a row past it ends the walk, and every
    /// such row is dropped. `None` (the caller gave none) is a plain `step`; a `bound` on a
    /// walk declaring no [`until`](Self::until) field is a [`LogicError`](crate::LogicError),
    /// since there is no row field to keep it on.
    pub fn step_until<K, B, T>(
        &self,
        state: &SeekState<K, T>,
        rows: Vec<T>,
        edge: Option<&K>,
        far: Option<&K>,
        bound: Option<&B>,
    ) -> Fetched<T, SeekState<K, T>>
    where
        K: SeekKey,
        B: SeekKey,
        T: Serialize + Clone + PartialEq,
    {
        let Some(bound) = bound else {
            return self.step(state, rows, edge, far);
        };
        let Some((field, segments)) = &self.until else {
            return Err(Error::logic(format!(
                "`{}` was given a far bound to keep on the rows, but declares no `until` field to read it from",
                self.walker
            )));
        };
        self.fold(state, rows, edge, far, |row| {
            Ok(match self.read::<B, T>(segments, field, row, None)? {
                Some(value) if self.descending => value < *bound,
                Some(value) => value > *bound,
                None => false,
            })
        })
    }

    /// The walk over one page; a page holding a row `past` reports ends it, such rows dropped.
    fn fold<K, T>(
        &self,
        state: &SeekState<K, T>,
        rows: Vec<T>,
        edge: Option<&K>,
        far: Option<&K>,
        past: impl Fn(&T) -> Result<bool>,
    ) -> Fetched<T, SeekState<K, T>>
    where
        K: SeekKey,
        T: Serialize + Clone + PartialEq,
    {
        let keys = rows
            .iter()
            .map(|row| self.key::<K, T>(row))
            .collect::<Result<Vec<_>>>()?;
        let fresh: Vec<T> = if self.unique {
            let carried = state
                .carried
                .iter()
                .map(|row| self.key::<K, T>(row))
                .collect::<Result<Vec<_>>>()?;
            rows.iter()
                .zip(&keys)
                .filter(|(_, key)| !carried.contains(key))
                .map(|(row, _)| row.clone())
                .collect()
        } else {
            let mut remaining = state.carried.clone();
            let mut fresh = Vec::new();
            for row in &rows {
                match remaining.iter().position(|seen| seen == row) {
                    Some(index) => {
                        remaining.remove(index);
                    }
                    None => fresh.push(row.clone()),
                }
            }
            if !remaining.is_empty() {
                return Err(Error::logic(format!(
                    "`{}` requested from its last position and the venue no longer returned one or more rows it had \
                     already returned for that {}; row content was expected to stay available across requests, so \
                     the walk stopped instead of silently dropping or duplicating rows.",
                    self.walker,
                    self.named()
                )));
            }
            fresh
        };
        let beyond = rows.iter().map(&past).collect::<Result<Vec<_>>>()?;
        if beyond.contains(&true) {
            let mut kept = Vec::with_capacity(fresh.len());
            for row in fresh {
                if !past(&row)? {
                    kept.push(row);
                }
            }
            return Ok((kept, None));
        }
        let values: Vec<&K> = keys.iter().flatten().collect();
        let extreme: Option<K> = if K::ORDERED {
            values
                .iter()
                .copied()
                .reduce(|best, key| {
                    let better = if self.descending { key < best } else { key > best };
                    if better {
                        key
                    } else {
                        best
                    }
                })
                .cloned()
        } else {
            values.last().map(|key| (*key).clone())
        };
        let at = |target: &K| -> Vec<T> {
            rows.iter()
                .zip(&keys)
                .filter(|(_, key)| key.as_ref() == Some(target))
                .map(|(row, _)| row.clone())
                .collect()
        };
        let full = self.cap.is_some_and(|cap| rows.len() >= cap);
        if full {
            return match extreme {
                Some(extreme) if state.pos.as_ref() != Some(&extreme) => {
                    let carried = at(&extreme);
                    Ok((
                        fresh,
                        Some(SeekState {
                            pos: Some(extreme),
                            carried,
                        }),
                    ))
                }
                _ => Err(Error::logic(format!(
                    "`{}` received a full page of {} rows all sharing one {} value; the rest of that value is \
                     unreachable and advancing would drop it.",
                    self.walker,
                    rows.len(),
                    self.named()
                ))),
            };
        }
        if self.cap.is_none() {
            if let Some(extreme) = extreme {
                if state.pos.as_ref() != Some(&extreme) {
                    let carried = at(&extreme);
                    return Ok((
                        fresh,
                        Some(SeekState {
                            pos: Some(extreme),
                            carried,
                        }),
                    ));
                }
            }
        }
        match (edge, far) {
            (Some(edge), Some(far)) => {
                let reached = if self.descending { edge <= far } else { edge >= far };
                if reached {
                    Ok((fresh, None))
                } else {
                    let carried = at(edge);
                    Ok((
                        fresh,
                        Some(SeekState {
                            pos: Some(edge.clone()),
                            carried,
                        }),
                    ))
                }
            }
            _ => Ok((fresh, None)),
        }
    }
}

/// `[-1][0]` -> `[Index(0)]`; `[-1].t` -> `[Key("t")]`: a `cursor.field` relative to one row.
/// A declared row field (`[-1].t`, `[-1][0]`) relative to one row (`t`, `[0]`); empty for a
/// bare `[-1]`, the row itself.
fn row_relative(field: &str) -> String {
    let relative = field.strip_prefix("[-1]").unwrap_or(field);
    relative.strip_prefix('.').unwrap_or(relative).to_string()
}

/// "the row's `t`", or "the row" when the row itself is the key.
fn the_field(field: &str) -> String {
    if field.is_empty() {
        "the row".to_string()
    } else {
        format!("the row's `{field}`")
    }
}

fn parse_row_field(field: &str) -> Vec<Segment> {
    let rest = field.strip_prefix("[-1]").unwrap_or(field);
    let mut segments = Vec::new();
    let mut chars = rest.chars().peekable();
    while let Some(c) = chars.next() {
        match c {
            '.' => {}
            '[' => {
                let mut digits = String::new();
                for d in chars.by_ref() {
                    if d == ']' {
                        break;
                    }
                    digits.push(d);
                }
                if let Ok(index) = digits.trim().parse() {
                    segments.push(Segment::Index(index));
                }
            }
            other => {
                let mut name = String::from(other);
                while let Some(&next) = chars.peek() {
                    if next == '.' || next == '[' {
                        break;
                    }
                    name.push(next);
                    chars.next();
                }
                segments.push(Segment::Key(name));
            }
        }
    }
    segments
}

fn lenient_key<K: DeserializeOwned>(value: Value) -> std::result::Result<K, String> {
    let first = match serde_json::from_value::<K>(value.clone()) {
        Ok(key) => return Ok(key),
        Err(e) => e.to_string(),
    };
    let alternative = match &value {
        Value::String(text) => serde_json::from_str::<Value>(text).ok().filter(Value::is_number),
        Value::Number(number) => Some(Value::String(number.to_string())),
        _ => None,
    };
    alternative
        .and_then(|alt| serde_json::from_value::<K>(alt).ok())
        .ok_or(first)
}
