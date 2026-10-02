package core

import (
	"context"
	"iter"
	"sync"
)

// Resumable, retry-safe pagination: a walk is a pure next(state) step, not a loop.
//
// A generated `<Method>Paged` returns a PaginatedResponse because a loop that fails is
// dead: nothing can retry the one page that failed and carry on. Here every page is one
// call to Next(ctx, state), and the contract below makes calling it again safe:
//
//   - Next is a pure function of state: no captured mutable variable, no clock. Calling it
//     twice with the same state makes the same request.
//   - state fully determines the request; any bound the API would default at call time is
//     pinned into Init before the first page.
//   - The request is a read. Repeating it never changes anything upstream.
//
// Under that contract a caller may retry a page, resume from any page's Next state, or run
// two walks of one response concurrently, and see exactly the pages one uninterrupted walk
// would have produced.

// Page is one page of a walk, with the state on either side of it.
type Page[T, S any] struct {
	// Rows this page carried. May be empty.
	Rows []T
	// State this page was fetched with. Re-fetching from it yields this page again.
	State S
	// Next is the state the following page is fetched with, nil after the last page.
	Next *S
}

// NextFunc fetches one page from a state: its rows and the following state, nil when done.
type NextFunc[T, S any] func(ctx context.Context, state S) ([]T, *S, error)

// Invoker wraps one page fetch: a retry policy, a logger, any per-call middleware.
type Invoker[T, S any] func(ctx context.Context, fetch func(context.Context) ([]T, *S, error)) ([]T, *S, error)

// PaginatedResponse is a walk: Init is the first page's state, Next fetches one page.
type PaginatedResponse[T, S any] struct {
	Init S
	Next NextFunc[T, S]
}

// NewPaginatedResponse is a walk from init through next.
func NewPaginatedResponse[T, S any](init S, next NextFunc[T, S]) *PaginatedResponse[T, S] {
	return &PaginatedResponse[T, S]{Init: init, Next: next}
}

// Fetch fetches the one page state names.
func (p *PaginatedResponse[T, S]) Fetch(ctx context.Context, state S) ([]T, *S, error) {
	return p.Next(ctx, state)
}

// Pages yields every page, empty ones included; it stops after the last page or after
// yielding the first error.
func (p *PaginatedResponse[T, S]) Pages(ctx context.Context) iter.Seq2[Page[T, S], error] {
	return func(yield func(Page[T, S], error) bool) {
		state := p.Init
		for {
			rows, following, err := p.Next(ctx, state)
			if err != nil {
				yield(Page[T, S]{State: state}, err)
				return
			}
			if !yield(Page[T, S]{Rows: rows, State: state, Next: following}, nil) || following == nil {
				return
			}
			state = *following
		}
	}
}

// Rows yields the rows of each non-empty page, in walk order, then the first error if any.
func (p *PaginatedResponse[T, S]) Rows(ctx context.Context) iter.Seq2[[]T, error] {
	return func(yield func([]T, error) bool) {
		for page, err := range p.Pages(ctx) {
			if err != nil {
				yield(nil, err)
				return
			}
			if len(page.Rows) == 0 {
				continue
			}
			if !yield(page.Rows, nil) {
				return
			}
		}
	}
}

// All is every row of every page, flattened.
func (p *PaginatedResponse[T, S]) All(ctx context.Context) ([]T, error) {
	var out []T
	for rows, err := range p.Rows(ctx) {
		if err != nil {
			return out, err
		}
		out = append(out, rows...)
	}
	return out, nil
}

// Resume is the same walk started from state instead of Init.
func (p *PaginatedResponse[T, S]) Resume(state S) *PaginatedResponse[T, S] {
	return &PaginatedResponse[T, S]{Init: state, Next: p.Next}
}

// Via is the same walk with every page fetch routed through call.
func (p *PaginatedResponse[T, S]) Via(call Invoker[T, S]) *PaginatedResponse[T, S] {
	fetch := p.Next
	return &PaginatedResponse[T, S]{Init: p.Init, Next: func(ctx context.Context, state S) ([]T, *S, error) {
		return call(ctx, func(ctx context.Context) ([]T, *S, error) { return fetch(ctx, state) })
	}}
}

// -- terminator helpers -----------------------------------------------------------------

// Exhausted reports whether a page ends a `short_page`/`empty` walk: no rows, or fewer
// rows than size when the walk knows one (size < 0 for unknown).
func Exhausted(rows int, size int) bool { return rows == 0 || (size >= 0 && rows < size) }

// TotalReached reports whether a `page` walk ended by `total` is done after the page at
// state (from start): pages seen reached total when it counts pages, rows seen reached it
// when it counts items and the size is known (size < 0 for unknown), else an empty page.
func TotalReached(countsPages bool, state, start int64, size int, rows int, total int64) bool {
	pages := state - start + 1
	if countsPages {
		return pages >= total
	}
	if size < 0 {
		return rows == 0
	}
	return pages*int64(size) >= total || rows == 0
}

// Cursor is a state type with a zero value a walk reads as absent.
type Cursor interface {
	~string | ~int64 | ~float64 | ~bool
}

// CursorOrDone is the next state of a `token`/`seek` walk: nil when the reply carried none
// or a zero one, which the `absent_cursor` terminator reads as the end.
func CursorOrDone[S Cursor](cursor *S) *S {
	var zero S
	if cursor == nil || *cursor == zero {
		return nil
	}
	return cursor
}

// TotalSeen is the `total` a walk read on earlier pages: a page that omits it, or reports
// a different value, is a logic error (the API changed under the walk; retry it whole).
type TotalSeen struct {
	mu   sync.Mutex
	seen *int64
}

// Check records this page's total and returns it, or fails as described on TotalSeen.
func (t *TotalSeen) Check(walker string, total *int64) (int64, error) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if total == nil {
		return 0, LogicError("`%s` needs a `total` on every page. The API omitted it here; retry the whole walk from the start.", walker)
	}
	if t.seen != nil && *t.seen != *total {
		return 0, LogicError("`%s` needs a `total` on every page. The API reported a value (%d) that disagrees with an earlier page of this same walk (%d); retry the whole walk from the start.", walker, *total, *t.seen)
	}
	v := *total
	t.seen = &v
	return v, nil
}
