/**
 * Resumable, retry-safe pagination: a walk is a pure `next(state)` step, not a generator.
 *
 * A generated `<method>Paged` returns a `PaginatedResponse` rather than an async generator
 * because a generator that throws is dead: nothing can retry the one page that failed and
 * carry on. Here every page is one call to `next(state)`, and the contract below is what
 * makes calling it again safe.
 *
 * The contract every `next` must honour:
 *
 * - `next` is a pure function of `state`. It reads no closure variable, no instance
 *   attribute, no clock. Calling it twice with the same `state` makes the same request.
 * - `state` fully determines the request. Any bound the API would otherwise default at
 *   call time is pinned into `init` once, before the first page.
 * - The request is a read. Repeating it never changes anything upstream.
 *
 * Under that contract a caller may retry `next(state)` after a transient failure, resume a
 * walk from any page's `Page.next`, or run two iterations of one response concurrently,
 * and see exactly the pages a single uninterrupted walk would have produced.
 */

/** One page of a walk, with the state on either side of it. */
export interface Page<T, S> {
  /** Rows this page carried. May be empty. */
  rows: T[]
  /** State this page was fetched with. Re-fetching from it yields this page again. */
  state: S
  /** State the following page is fetched with, or `null` when this was the last page. */
  next: S | null
}

/** Fetch one page: `[rows, nextState]`, `nextState` being `null` after the last page. */
export type Next<T, S> = (state: S) => Promise<[T[], S | null]>

/** Per-fetch invoker: receives one zero-argument fetch and returns its result. */
export type Invoker<T, S> = (fetch: () => Promise<[T[], S | null]>) => Promise<[T[], S | null]>

/**
 * A paginated walk: `init` is the first page's state, `next` fetches one page from a
 * state and returns its rows plus the following state, `null` once the walk is done.
 *
 * Thenable (awaiting flattens every page into one array) and async-iterable (one page's
 * rows at a time, empty pages skipped). `pages()` additionally exposes each page's own
 * state, for checkpointing; `resume()` restarts from a saved one; `via()` routes every page
 * fetch through a caller-supplied invoker, which is how a retry or logging layer wraps each
 * page as one ordinary call without ever unrolling the loop by hand.
 *
 * `next` must honour the contract in this module's docs: pure in `state`, no clock,
 * read-only on the wire. Nothing here can enforce it, but everything here assumes it.
 *
 * ```ts
 * const paging = client.account.tradesPaged({ symbol: 'BTCUSDT' })
 * const trades = await paging                       // every row, flattened
 * for await (const rows of paging) ...              // one page at a time
 * for await (const page of paging.via(retried).pages()) checkpoint(page.next)
 * ```
 *
 * A method returning one must be a plain (non-async) function: an `async` function would
 * resolve the thenable itself and hand back the flattened array.
 */
export class PaginatedResponse<T, S> implements PromiseLike<T[]>, AsyncIterable<T[]> {
  constructor(
    /** State the first page is fetched with. */
    readonly init: S,
    /** Fetch one page: `[rows, nextState]`, `nextState` being `null` after the last page. */
    readonly next: Next<T, S>,
  ) {}

  /** Yield every page, empty ones included, each with the state before and after it. */
  async *pages(): AsyncGenerator<Page<T, S>, void, undefined> {
    let state: S | null = this.init
    while (state !== null) {
      const [rows, following]: [T[], S | null] = await this.next(state)
      yield { rows, state, next: following }
      state = following
    }
  }

  /**
   * The same walk, started from `state` instead of `init`: a state a previous page
   * reported as `Page.next` (or `Page.state`, to refetch that page itself).
   */
  resume(state: S): PaginatedResponse<T, S> {
    return new PaginatedResponse(state, this.next)
  }

  /**
   * The same walk, with every page fetch routed through `call`.
   *
   * `call` receives a zero-argument function performing one `next(state)` and returns its
   * result, so a retry policy, a logger, or any other per-call middleware sees each page as
   * one plain call. Purity of `next` is what makes wrapping it this way safe: a retried
   * fetch is just the same page fetched again.
   */
  via(call: Invoker<T, S>): PaginatedResponse<T, S> {
    const fetch = this.next
    return new PaginatedResponse(this.init, state => call(() => fetch(state)))
  }

  /** Rows of each non-empty page, in walk order. */
  async *[Symbol.asyncIterator](): AsyncGenerator<T[], void, undefined> {
    for await (const page of this.pages()) {
      if (page.rows.length) yield page.rows
    }
  }

  /** Every row of every page, flattened. */
  async all(): Promise<T[]> {
    const out: T[] = []
    for await (const rows of this) out.push(...rows)
    return out
  }

  then<R1 = T[], R2 = never>(
    onfulfilled?: ((value: T[]) => R1 | PromiseLike<R1>) | null,
    onrejected?: ((reason: unknown) => R2 | PromiseLike<R2>) | null,
  ): Promise<R1 | R2> {
    return this.all().then(onfulfilled, onrejected)
  }
}
