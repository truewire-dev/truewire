package core_test

import (
	"cmp"
	"context"
	"encoding/json"
	"errors"
	"testing"

	truewire "truewire.dev/core"
)

// venue serves rows whose key is the row value itself, keeping `cap` rows adjacent to
// the anchored bound of each requested [start, end] range.
func venue(rows []int64, cap int, keepNewest bool, calls *[][2]*int64) func(ctx context.Context, pos, edge *int64) ([]int64, error) {
	return func(ctx context.Context, pos, edge *int64) ([]int64, error) {
		*calls = append(*calls, [2]*int64{pos, edge})
		var in []int64
		for _, r := range rows {
			if keepNewest {
				if (pos == nil || r <= *pos) && (edge == nil || r >= *edge) {
					in = append(in, r)
				}
			} else if (pos == nil || r >= *pos) && (edge == nil || r <= *edge) {
				in = append(in, r)
			}
		}
		if keepNewest && len(in) > cap {
			in = in[len(in)-cap:]
		} else if len(in) > cap {
			in = in[:cap]
		}
		return in, nil
	}
}

func key(r int64) (int64, bool) { return r, true }

func TestSeekAscendingWalksFullPagesAndDedupsTheBoundary(t *testing.T) {
	var calls [][2]*int64
	walk := truewire.Seek[int64, int64]{Walker: "w", Unique: true, Ordered: true, Compare: cmp.Compare[int64], Cap: 3, Key: key,
		Fetch: venue([]int64{1, 2, 3, 4, 5, 6, 7}, 3, false, &calls)}
	start := int64(1)
	all, err := walk.Response(&start).All(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(all) != 7 || all[0] != 1 || all[6] != 7 {
		t.Fatalf("rows %v", all)
	}
}

func TestSeekDescendingWithoutCapKeepsGoingWhileItProgresses(t *testing.T) {
	var calls [][2]*int64
	walk := truewire.Seek[int64, int64]{Walker: "w", Unique: true, Ordered: true, Descending: true, Compare: cmp.Compare[int64], Cap: -1, Key: key,
		Fetch: venue([]int64{1, 2, 3, 4, 5}, 2, true, &calls)}
	all, err := walk.Response(nil).All(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(all) != 5 || all[0] != 4 || all[len(all)-1] != 1 {
		t.Fatalf("rows %v", all)
	}
}

func TestSeekSpanChunksTheRange(t *testing.T) {
	var calls [][2]*int64
	far := int64(10)
	walk := truewire.Seek[int64, int64]{Walker: "w", Unique: true, Ordered: true, Compare: cmp.Compare[int64], Cap: 100, Key: key, Far: &far,
		Edge:  func(pos int64) int64 { return pos + 4 },
		Fetch: venue([]int64{0, 2, 4, 6, 8, 10}, 100, false, &calls)}
	start := int64(0)
	all, err := walk.Response(&start).All(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(all) != 6 || len(calls) != 3 || *calls[2][1] != 10 {
		t.Fatalf("rows %v over %d calls", all, len(calls))
	}
}

func TestSeekFullPageOfOneKeyIsALogicError(t *testing.T) {
	walk := truewire.Seek[int64, int64]{Walker: "w", Unique: false, Ordered: true, Compare: cmp.Compare[int64], Cap: 2, Key: key,
		Fetch: func(ctx context.Context, pos, edge *int64) ([]int64, error) { return []int64{5, 5}, nil }}
	start := int64(5)
	if _, err := walk.Response(&start).All(context.Background()); !errors.Is(err, truewire.ErrLogic) {
		t.Fatal(err)
	}
}

func TestSeekNonUniqueDedupsByContentAndDetectsVanishedRows(t *testing.T) {
	type fill struct{ T, ID int64 }
	pages := [][]fill{{{1, 1}, {2, 2}, {2, 3}}, {{2, 2}, {3, 4}}}
	i := 0
	walk := truewire.Seek[int64, fill]{Walker: "w", Unique: false, Ordered: true, Compare: cmp.Compare[int64], Cap: 3,
		Key: func(f fill) (int64, bool) { return f.T, true },
		Fetch: func(ctx context.Context, pos, edge *int64) ([]fill, error) {
			page := pages[i]
			i++
			return page, nil
		}}
	start := int64(1)
	_, err := walk.Response(&start).All(context.Background())
	if !errors.Is(err, truewire.ErrLogic) {
		t.Fatalf("a carried row that vanished is a logic error, got %v", err)
	}
}

func TestSeekPastDropsTheRowBeyondTheFarBoundAndEndsOnItsPage(t *testing.T) {
	// Rows are trade ids; trade 4 carries a time past the caller's far bound.
	var calls [][2]*int64
	walk := truewire.Seek[int64, int64]{Walker: "w", Unique: true, Ordered: true, Compare: cmp.Compare[int64], Cap: 2, Key: key,
		Fetch: venue([]int64{1, 2, 3, 4, 5}, 2, false, &calls), Past: func(r int64) bool { return r == 4 }}
	start := int64(1)
	all, err := walk.Response(&start).All(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(all) != 3 || all[2] != 3 || len(calls) != 3 {
		t.Fatalf("rows %v after %d calls", all, len(calls))
	}
}

func TestSeekPastDropsEveryRowBeyondTheFarBoundOnItsPage(t *testing.T) {
	// One page, [3, 4 (past), 5]: the in-range row after the past one is kept.
	var calls [][2]*int64
	walk := truewire.Seek[int64, int64]{Walker: "w", Unique: true, Ordered: true, Compare: cmp.Compare[int64], Cap: 3, Key: key,
		Fetch: venue([]int64{1, 2, 3, 4, 5}, 3, false, &calls), Past: func(r int64) bool { return r == 4 }}
	start := int64(3)
	all, err := walk.Response(&start).All(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(all) != 2 || all[0] != 3 || all[1] != 5 || len(calls) != 1 {
		t.Fatalf("rows %v after %d calls", all, len(calls))
	}
}

func TestSubscribedDecodesMessagesAndReply(t *testing.T) {
	items := []string{`{"p":1}`}
	raw := truewire.NewStream[json.RawMessage](json.RawMessage(`{"ok":true}`), func(ctx context.Context) (json.RawMessage, error) {
		if len(items) == 0 {
			return nil, truewire.ErrStreamDone
		}
		item := items[0]
		items = items[1:]
		return json.RawMessage(item), nil
	}, nil)
	type ack struct {
		OK bool `json:"ok"`
	}
	type msg struct {
		P int `json:"p"`
	}
	sub, err := truewire.Subscribed[msg, ack](raw, nil)
	if err != nil || !sub.Reply.OK {
		t.Fatal(sub, err)
	}
	m, err := sub.Next(context.Background())
	if err != nil || m.P != 1 {
		t.Fatal(m, err)
	}
	if got := truewire.FillTemplate("book.{symbol}.{depth}", map[string]any{"symbol": "BTC/USD", "depth": 10}); got != "book.BTC/USD.10" {
		t.Fatal(got)
	}
	if truewire.EpochMillis.FromEpoch(-1).UnixMilli() != -1 {
		t.Fatal("negative epoch")
	}
}
