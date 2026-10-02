package core

import (
	"context"
	"reflect"
	"slices"
)

// SeekState is the state of a `seek` walk (ADR 0013): the value the moving bound is sent as
// (nil when the caller gave none and the venue starts from its own default), and the rows
// already yielded that share that value, so the venue re-serving them costs nothing.
type SeekState[K, T any] struct {
	Pos     *K
	Carried []T
}

// Seek is one `seek` walk: the one cursor-from-rows strategy, whose next request bound is
// read off the rows of the previous page. The generated `<Method>Paged` fills it in and
// returns Response; every field but Edge and Equal is required.
//
// Ascending (anchor `start`) shown; Descending mirrors it:
//
//	edge  = far, or min(pos + span, far) with a span
//	rows  = fetch(pos, edge)
//	fresh = rows - carried            (by key when Unique, by content otherwise)
//	full page (Cap known, len >= Cap): move pos to the extreme key, carry rows at it
//	no cap known and the extreme moved: move pos the same way
//	otherwise the range [pos, edge] is exhausted: stop, or with a span move pos to edge
//
// With Past (a far bound the venue refuses beside the moving one, `exclusive.far`), a page
// holding a row past the caller's value ends the walk, and every such row is dropped.
type Seek[K, T any] struct {
	// Walker names the generated method in error messages.
	Walker string
	// Unique is `cursor.unique`: dedup by key, else by whole-row content.
	Unique bool
	// Descending is true when the venue anchors truncation to the end bound.
	Descending bool
	// Ordered is false for a key compared by equality only (a string id): the page's last
	// row in wire order stands in for its extreme.
	Ordered bool
	// Compare orders two keys (only equality is used when not Ordered).
	Compare func(a, b K) int
	// Cap is the row count a full page has; negative when none resolves.
	Cap int
	// Far is the caller's own far bound, nil when not given.
	Far *K
	// Edge adds (or, descending, subtracts) the span to pos; nil without a declared span.
	Edge func(pos K) K
	// Key reads one row's cursor field as a key; false when the row has none.
	Key func(T) (K, bool)
	// Equal compares two rows by content, for a non-unique cursor; reflect.DeepEqual when nil.
	Equal func(a, b T) bool
	// Past reports whether a row lies past the caller's own far bound on a parameter the
	// venue refuses beside the moving one, so the walk keeps it instead of sending it; nil
	// when there is none, or the caller gave no value.
	Past func(T) bool
	// Fetch requests one page with the moving bound at pos and, with a span, the far
	// bound at edge (nil edge: the caller's own far bound).
	Fetch func(ctx context.Context, pos *K, edge *K) ([]T, error)
}

// Response is the walk from pos (the caller's own moving bound, nil for none).
func (s Seek[K, T]) Response(pos *K) *PaginatedResponse[T, SeekState[K, T]] {
	return NewPaginatedResponse(SeekState[K, T]{Pos: pos}, s.Next)
}

// Next fetches one page of the walk from state.
func (s Seek[K, T]) Next(ctx context.Context, state SeekState[K, T]) ([]T, *SeekState[K, T], error) {
	var edge *K
	if s.Edge != nil {
		if state.Pos == nil || s.Far == nil {
			return nil, nil, LogicError("`%s` walks a bounded range in spans: pass both bounds", s.Walker)
		}
		e := s.Edge(*state.Pos)
		if s.beyond(e, *s.Far) {
			e = *s.Far
		}
		edge = &e
	}
	rows, err := s.Fetch(ctx, state.Pos, edge)
	if err != nil {
		return nil, nil, err
	}
	keys := make([]*K, len(rows))
	for i, row := range rows {
		if k, ok := s.Key(row); ok {
			keys[i] = &k
		}
	}
	fresh, err := s.dedup(rows, keys, state)
	if err != nil {
		return nil, nil, err
	}
	if s.Past != nil && slices.ContainsFunc(rows, s.Past) {
		return slices.DeleteFunc(slices.Clone(fresh), s.Past), nil, nil
	}
	var extreme *K
	for _, k := range keys {
		if k == nil {
			continue
		}
		if extreme == nil || !s.Ordered || s.beyond(*k, *extreme) {
			extreme = k
		}
	}
	at := func(value K) []T {
		var out []T
		for i, row := range rows {
			if keys[i] != nil && s.Compare(*keys[i], value) == 0 {
				out = append(out, row)
			}
		}
		return out
	}
	samePos := extreme != nil && state.Pos != nil && s.Compare(*extreme, *state.Pos) == 0
	if s.Cap >= 0 && len(rows) >= s.Cap {
		if extreme == nil || samePos {
			return nil, nil, LogicError("`%s` received a full page of %d rows all sharing one cursor value; the rest of that value is unreachable and advancing would drop it.", s.Walker, len(rows))
		}
		return fresh, &SeekState[K, T]{Pos: extreme, Carried: at(*extreme)}, nil
	}
	if s.Cap < 0 && extreme != nil && !samePos {
		return fresh, &SeekState[K, T]{Pos: extreme, Carried: at(*extreme)}, nil
	}
	if edge == nil || !s.beyond(*s.Far, *edge) {
		return fresh, nil, nil
	}
	return fresh, &SeekState[K, T]{Pos: edge, Carried: at(*edge)}, nil
}

// beyond reports whether a lies past b in the walk's direction.
func (s Seek[K, T]) beyond(a, b K) bool {
	if s.Descending {
		return s.Compare(a, b) < 0
	}
	return s.Compare(a, b) > 0
}

func (s Seek[K, T]) dedup(rows []T, keys []*K, state SeekState[K, T]) ([]T, error) {
	if len(state.Carried) == 0 {
		return rows, nil
	}
	var fresh []T
	if s.Unique {
		carried := make([]K, 0, len(state.Carried))
		for _, row := range state.Carried {
			if k, ok := s.Key(row); ok {
				carried = append(carried, k)
			}
		}
		for i, row := range rows {
			seen := false
			for _, k := range carried {
				if keys[i] != nil && s.Compare(*keys[i], k) == 0 {
					seen = true
					break
				}
			}
			if !seen {
				fresh = append(fresh, row)
			}
		}
		return fresh, nil
	}
	equal := s.Equal
	if equal == nil {
		equal = func(a, b T) bool { return reflect.DeepEqual(a, b) }
	}
	remaining := append([]T(nil), state.Carried...)
	for _, row := range rows {
		found := -1
		for j, carried := range remaining {
			if equal(row, carried) {
				found = j
				break
			}
		}
		if found >= 0 {
			remaining = append(remaining[:found], remaining[found+1:]...)
		} else {
			fresh = append(fresh, row)
		}
	}
	if len(remaining) > 0 {
		return nil, LogicError("`%s` re-requested from its last position and the venue no longer returned one or more rows it had already returned for that cursor value; the walk stopped instead of silently dropping or duplicating rows.", s.Walker)
	}
	return fresh, nil
}
