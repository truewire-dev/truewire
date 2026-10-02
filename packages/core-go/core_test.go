package core_test

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	truewire "truewire.dev/core"
)

// -- errors ---------------------------------------------------------------------------

func TestErrorKindsMatchTheirParents(t *testing.T) {
	err := truewire.APIError(truewire.KindAuth, "bad key").WithStatus(401)
	var wrapped error = err
	if !errors.Is(wrapped, truewire.ErrAuth) || !errors.Is(wrapped, truewire.ErrAPI) || !errors.Is(wrapped, truewire.ErrTruewire) {
		t.Fatal("an auth error is an API error and a truewire error")
	}
	if errors.Is(wrapped, truewire.ErrRateLimited) || errors.Is(wrapped, truewire.ErrNetwork) {
		t.Fatal("an auth error is not a sibling kind")
	}
	cause := io.ErrUnexpectedEOF
	network := truewire.NetworkError("down", cause)
	if !errors.Is(network, cause) || errors.Is(network, truewire.ErrAPI) {
		t.Fatal("a network error unwraps to its cause and is no API error")
	}
	var e *truewire.Error
	if !errors.As(error(err), &e) || e.Status != 401 {
		t.Fatal("errors.As reads the details")
	}
}

// -- decimal --------------------------------------------------------------------------

func TestDecimalKeepsDigitsAndComparesExactly(t *testing.T) {
	d, err := truewire.Decode[truewire.Decimal]([]byte(`"10.50"`))
	if err != nil || d != "10.50" {
		t.Fatalf("got %q, %v", d, err)
	}
	out, _ := truewire.Dump(d)
	if string(out) != `"10.50"` {
		t.Fatalf("dumped %s", out)
	}
	if _, err := truewire.Decode[truewire.Decimal]([]byte(`10.5`)); !errors.Is(err, truewire.ErrValidation) {
		t.Fatal("a bare number is not a decimal string")
	}
	cases := []struct {
		a, b truewire.Decimal
		want int
	}{{"1.50", "1.5", 0}, {"-1", "1", -1}, {"2e3", "1999.9", 1}, {"0.001", "1e-3", 0}, {"-0", "0", 0}, {"-2", "-1", -1}}
	for _, c := range cases {
		if got := c.a.Compare(c.b); got != c.want {
			t.Errorf("%s vs %s: %d, want %d", c.a, c.b, got, c.want)
		}
	}
}

// -- times ----------------------------------------------------------------------------

func TestEpochConvertersAreExact(t *testing.T) {
	ts, err := truewire.Decode[truewire.TimestampMillis]([]byte(`1786302600123`))
	if err != nil || ts.UnixMilli() != 1786302600123 {
		t.Fatalf("got %v, %v", ts, err)
	}
	fromString, err := truewire.Decode[truewire.TimestampMillis]([]byte(`"1786302600123"`))
	if err != nil || !fromString.Equal(ts.Time) {
		t.Fatal("a numeral string parses the same")
	}
	nanos, err := truewire.Decode[truewire.TimestampNanos]([]byte(`1786302600123456789`))
	if err != nil || nanos.UnixNano() != 1786302600123456789 {
		t.Fatalf("nanos: %v %v", nanos.UnixNano(), err)
	}
	out, _ := truewire.Dump(nanos)
	if string(out) != "1786302600123456789" {
		t.Fatalf("nanos dump %s", out)
	}
	fractional, err := truewire.Decode[truewire.TimestampSeconds]([]byte(`1763410056.903966`))
	if err != nil || fractional.Nanosecond() != 903966000 {
		t.Fatalf("fractional seconds: %v %v", fractional.Nanosecond(), err)
	}
	if out, _ := truewire.Dump(fractional); string(out) != "1763410056.903966" {
		t.Fatalf("fractional dump %s", out)
	}
	quoted, _ := truewire.Decode[truewire.TimestampNanos]([]byte(`"1786622308334567536"`))
	if out, _ := truewire.Dump(quoted); string(out) != `"1786622308334567536"` {
		t.Fatalf("a numeral string stays one: %s", out)
	}
	if out, _ := truewire.Dump(truewire.TimestampMillis{Time: quoted.Time}); string(out) != "1786622308334" && string(out) != "1786622308334.567536" {
		t.Fatalf("a built value is a number: %s", out)
	}
	neg, _ := truewire.EpochMillis.Parse("-1")
	if truewire.EpochSeconds.Dump(neg) != -1 || truewire.EpochMillis.Dump(neg) != -1 {
		t.Fatal("negative epochs floor")
	}
	if _, err := truewire.Decode[truewire.TimestampSeconds]([]byte(`"soon"`)); !errors.Is(err, truewire.ErrValidation) {
		t.Fatal("not an epoch")
	}
}

func TestDateTimeAndDate(t *testing.T) {
	ts, err := truewire.Decode[truewire.TimestampIso]([]byte(`"2026-08-03T10:00:00.5+02:00"`))
	if err != nil {
		t.Fatal(err)
	}
	out, _ := truewire.Dump(ts)
	if string(out) != `"2026-08-03T08:00:00.500Z"` {
		t.Fatalf("dumped %s", out)
	}
	whole, _ := truewire.Decode[truewire.TimestampIso]([]byte(`"2026-08-03T08:00:00Z"`))
	if out, _ := truewire.Dump(whole); string(out) != `"2026-08-03T08:00:00Z"` {
		t.Fatalf("dumped %s", out)
	}
	if _, err := truewire.Decode[truewire.TimestampIso]([]byte(`"2026-08-03"`)); err == nil {
		t.Fatal("a date is not a date-time")
	}
	// No offset at all (Hyperliquid's `lastDeployerFeeScaleChangeTime`): UTC, as Python reads it.
	naive, err := truewire.Decode[truewire.TimestampIso]([]byte(`"2026-02-20T19:01:52.647760965"`))
	if err != nil {
		t.Fatal(err)
	}
	if out, _ := truewire.Dump(naive); string(out) != `"2026-02-20T19:01:52.647760965Z"` {
		t.Fatalf("dumped %s", out)
	}
	if epoch, err := truewire.ParseDateTime("1970-01-01T00:00:00"); err != nil || !epoch.Equal(time.Unix(0, 0)) {
		t.Fatal(epoch, err)
	}
	d, err := truewire.Decode[truewire.DateIso]([]byte(`"2024-02-29"`))
	if err != nil || d.Day() != 29 {
		t.Fatal(err)
	}
	if _, err := truewire.Decode[truewire.DateIso]([]byte(`"2023-02-29"`)); err == nil {
		t.Fatal("not a calendar date")
	}
}

func TestStringNarrowedScalars(t *testing.T) {
	i, err := truewire.Decode[truewire.IntegerString]([]byte(`"-42"`))
	if n, ok := i.Int64(); err != nil || !ok || n != -42 {
		t.Fatal(i, err)
	}
	if out, _ := truewire.Dump(i); string(out) != `"-42"` {
		t.Fatal(string(out))
	}
	// Beyond int64 (a wei amount, an ERC-1155 token id): exact, and round-trips verbatim.
	const wei = "115792089237316195423570985008687907853269984665640564039457584007913129639935"
	big, err := truewire.Decode[truewire.IntegerString]([]byte(`"` + wei + `"`))
	if err != nil || big.BigInt().String() != wei {
		t.Fatal(big, err)
	}
	if _, ok := big.Int64(); ok {
		t.Fatal("a 78-digit integer does not fit int64")
	}
	if out, _ := truewire.Dump(big); string(out) != `"`+wei+`"` {
		t.Fatal(string(out))
	}
	if big.Add(1).Compare(big) != 1 || truewire.IntegerString("007").Compare("7") != 0 || truewire.IntegerString("-8").Compare("+3") != -1 {
		t.Fatal("compare")
	}
	if truewire.IntegerStringOf(-5).Add(7) != "2" {
		t.Fatal("add")
	}
	for _, bad := range []string{`42`, `"4.2"`, `"1e3"`, `""`, `" 1"`, `"0x1f"`, `null`} {
		if _, err := truewire.Decode[truewire.IntegerString]([]byte(bad)); err == nil || !strings.Contains(err.Error(), "expected integer string") {
			t.Fatalf("%s: %v", bad, err)
		}
	}
	if _, err := truewire.Dump(truewire.IntegerString("12a")); err == nil {
		t.Fatal("an invalid value built in code must not encode")
	}
	if _, err := truewire.ParseIntegerString("1.5"); err == nil {
		t.Fatal("parse")
	}
	b, err := truewire.Decode[truewire.BooleanString]([]byte(`"true"`))
	if err != nil || !bool(b) {
		t.Fatal(b, err)
	}
	if _, err := truewire.Decode[truewire.BooleanString]([]byte(`true`)); err == nil {
		t.Fatal("a bare boolean is not a boolean string")
	}
}

// -- validation -----------------------------------------------------------------------

type Side string

func (s *Side) UnmarshalJSON(data []byte) error {
	return truewire.DecodeLiteral(data, s, "buy", "sell")
}

func (s Side) MarshalJSON() ([]byte, error) { return truewire.EncodeLiteral(s, "buy", "sell") }

type Order struct {
	ID     int64
	Side   Side
	Price  truewire.Decimal
	Note   *string
	Tags   []string
	Parent truewire.Optional[*Order]
	Fills  []Fill
	Extra  map[string]json.RawMessage
}

func (o *Order) UnmarshalJSON(data []byte) error {
	return truewire.DecodeObject(data, &o.Extra,
		truewire.Required("id", &o.ID),
		truewire.Required("side", &o.Side),
		truewire.Required("price", &o.Price),
		truewire.RequiredNullable("note", &o.Note),
		truewire.OptionalField("tags", &o.Tags),
		truewire.OptionalNullable("parent", &o.Parent),
		truewire.OptionalField("fills", &o.Fills),
	)
}

func (o Order) MarshalJSON() ([]byte, error) {
	return truewire.EncodeObject(o.Extra,
		truewire.Required("id", &o.ID),
		truewire.Required("side", &o.Side),
		truewire.Required("price", &o.Price),
		truewire.RequiredNullable("note", &o.Note),
		truewire.OptionalField("tags", &o.Tags),
		truewire.OptionalNullable("parent", &o.Parent),
		truewire.OptionalField("fills", &o.Fills),
	)
}

// Fill is a tuple: [price, size].
type Fill struct {
	V0 truewire.Decimal
	V1 float64
}

func (f *Fill) UnmarshalJSON(data []byte) error {
	return truewire.DecodeTuple(data, truewire.ItemOf(&f.V0), truewire.ItemOf(&f.V1))
}

func (f Fill) MarshalJSON() ([]byte, error) {
	return truewire.EncodeTuple(truewire.ItemOf(&f.V0), truewire.ItemOf(&f.V1))
}

// IDOrName is a union.
type IDOrName struct {
	Int64  *int64
	String *string
}

func (u *IDOrName) UnmarshalJSON(data []byte) error {
	return truewire.DecodeUnion(data, truewire.VariantOf(&u.Int64), truewire.VariantOf(&u.String))
}

func (u IDOrName) MarshalJSON() ([]byte, error) {
	return truewire.EncodeUnion(truewire.VariantOf(&u.Int64), truewire.VariantOf(&u.String))
}

func TestRecordsRoundTripWithExtraKeys(t *testing.T) {
	wire := `{"id":1,"side":"buy","price":"1.10","note":null,"parent":null,"fills":[["1.1",2]],"venue":"x"}`
	order, err := truewire.Decode[Order]([]byte(wire))
	if err != nil {
		t.Fatal(err)
	}
	if order.Note != nil || !order.Parent.Set || order.Parent.Value != nil || order.Tags != nil || string(order.Extra["venue"]) != `"x"` {
		t.Fatalf("decoded %+v", order)
	}
	out, err := truewire.Dump(order)
	if err != nil {
		t.Fatal(err)
	}
	if string(out) != wire {
		t.Fatalf("round trip:\n%s\n%s", out, wire)
	}
	absent, _ := truewire.Decode[Order]([]byte(`{"id":1,"side":"sell","price":"1","note":"n"}`))
	if absent.Parent.Set {
		t.Fatal("an absent optional nullable is not set")
	}
	if out, _ := truewire.Dump(absent); string(out) != `{"id":1,"side":"sell","price":"1","note":"n"}` {
		t.Fatalf("absent keys stay absent: %s", out)
	}
}

func TestValidationLocatesEveryIssue(t *testing.T) {
	_, err := truewire.Decode[Order]([]byte(`{"id":"1","side":"hold","note":"n","fills":[["1",2],["x",1]],"tags":null}`))
	var e *truewire.Error
	if !errors.As(err, &e) || e.Kind != truewire.KindValidation {
		t.Fatalf("want a validation error, got %v", err)
	}
	paths := []string{}
	for _, issue := range e.Issues {
		paths = append(paths, issue.Path)
	}
	want := "/id /side /price /tags /fills/1/0"
	if strings.Join(paths, " ") != want {
		t.Fatalf("paths %q, want %q (%v)", strings.Join(paths, " "), want, e.Issues)
	}
	if e.Path() != "/id" {
		t.Fatal(e.Path())
	}
	if _, err := truewire.Decode[Order]([]byte(`[1]`)); !errors.Is(err, truewire.ErrValidation) {
		t.Fatal("an array is not an object")
	}
	if _, err := truewire.Decode[Order]([]byte(`{`)); !errors.Is(err, truewire.ErrValidation) {
		t.Fatal("broken JSON is a validation error")
	}
}

func TestUnionsTryVariantsInOrder(t *testing.T) {
	n, err := truewire.Decode[IDOrName]([]byte(`7`))
	if err != nil || n.Int64 == nil || *n.Int64 != 7 || n.String != nil {
		t.Fatal(n, err)
	}
	s, _ := truewire.Decode[IDOrName]([]byte(`"seven"`))
	if s.String == nil || *s.String != "seven" {
		t.Fatal(s)
	}
	if out, _ := truewire.Dump(s); string(out) != `"seven"` {
		t.Fatal(string(out))
	}
	if _, err := truewire.Decode[IDOrName]([]byte(`true`)); !errors.Is(err, truewire.ErrValidation) {
		t.Fatal("no variant matches a boolean")
	}
}

func TestDumpWithSetsFixedKeys(t *testing.T) {
	out, err := truewire.DumpWith(map[string]any{"a": 1}, map[string]any{"type": "limit"})
	if err != nil || string(out) != `{"a":1,"type":"limit"}` {
		t.Fatal(string(out), err)
	}
}

// -- http -----------------------------------------------------------------------------

func TestHttpClientSendsRecordsAndMapsFailures(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"path": r.URL.Path, "query": r.URL.RawQuery, "body": string(body), "type": r.Header.Get("Content-Type")})
	}))
	defer server.Close()
	client := &truewire.HttpClient{}
	rec := client.Recording()
	object := map[string]json.RawMessage{"owner": []byte(`"a b"`), "per_page": []byte(`3`), "labels": []byte(`["x","y"]`), "none": []byte(`null`)}
	path := truewire.FillPath("/repos/{owner}/commits", object)
	if path != "/repos/a%20b/commits" {
		t.Fatal(path)
	}
	resp, err := client.Request(context.Background(), "get", server.URL+path, truewire.RequestOptions{Query: truewire.QueryFrom(object)})
	if err != nil || resp.Status != 200 {
		t.Fatal(err)
	}
	var got map[string]string
	_ = json.Unmarshal(resp.Body, &got)
	if got["query"] != "labels=x&labels=y&per_page=3" || got["path"] != "/repos/a b/commits" {
		t.Fatalf("%v", got)
	}
	_, _ = client.Request(context.Background(), "POST", server.URL, truewire.RequestOptions{JSON: map[string]int{"n": 1}})
	rec.Stop()
	_, _ = client.Request(context.Background(), "GET", server.URL, truewire.RequestOptions{})
	exchanges := rec.Exchanges()
	if len(exchanges) != 2 || string(exchanges[1].RequestBody) != `{"n":1}` || exchanges[1].Method != "POST" {
		t.Fatalf("%d exchanges", len(exchanges))
	}
	unreachable := &truewire.HttpClient{Timeout: time.Second}
	if _, err := unreachable.Request(context.Background(), "GET", "http://127.0.0.1:1/", truewire.RequestOptions{}); !errors.Is(err, truewire.ErrNetwork) {
		t.Fatalf("want a network error, got %v", err)
	}
}

// -- paging ---------------------------------------------------------------------------

func pagesOf(rows [][]int) *truewire.PaginatedResponse[int, int64] {
	return truewire.NewPaginatedResponse(int64(0), func(ctx context.Context, state int64) ([]int, *int64, error) {
		next := state + 1
		if int(next) >= len(rows) {
			return rows[state], nil, nil
		}
		return rows[state], &next, nil
	})
}

func TestPaginatedResponseWalksResumesAndWraps(t *testing.T) {
	ctx := context.Background()
	walk := pagesOf([][]int{{1, 2}, {}, {3}})
	all, err := walk.All(ctx)
	if err != nil || len(all) != 3 || all[2] != 3 {
		t.Fatal(all, err)
	}
	var states []int64
	for page, err := range walk.Pages(ctx) {
		if err != nil {
			t.Fatal(err)
		}
		states = append(states, page.State)
	}
	if len(states) != 3 {
		t.Fatal(states)
	}
	resumed, _ := walk.Resume(2).All(ctx)
	if len(resumed) != 1 {
		t.Fatal(resumed)
	}
	calls := 0
	wrapped := walk.Via(func(ctx context.Context, fetch func(context.Context) ([]int, *int64, error)) ([]int, *int64, error) {
		calls++
		return fetch(ctx)
	})
	if _, err := wrapped.All(ctx); err != nil || calls != 3 {
		t.Fatal(calls, err)
	}
	failing := truewire.NewPaginatedResponse(int64(0), func(ctx context.Context, state int64) ([]int, *int64, error) {
		return nil, nil, truewire.NetworkError("down", nil)
	})
	if _, err := failing.All(ctx); !errors.Is(err, truewire.ErrNetwork) {
		t.Fatal(err)
	}
}

func TestTerminators(t *testing.T) {
	if !truewire.Exhausted(0, -1) || truewire.Exhausted(3, -1) || !truewire.Exhausted(2, 3) || truewire.Exhausted(3, 3) {
		t.Fatal("exhausted")
	}
	if !truewire.TotalReached(true, 3, 1, -1, 5, 3) || truewire.TotalReached(false, 1, 1, 10, 10, 25) || !truewire.TotalReached(false, 3, 1, 10, 5, 25) {
		t.Fatal("total reached")
	}
	empty := ""
	if truewire.CursorOrDone(&empty) != nil || truewire.CursorOrDone[string](nil) != nil {
		t.Fatal("an empty cursor ends the walk")
	}
	var seen truewire.TotalSeen
	five, six := int64(5), int64(6)
	if _, err := seen.Check("w", &five); err != nil {
		t.Fatal(err)
	}
	if _, err := seen.Check("w", &six); !errors.Is(err, truewire.ErrLogic) {
		t.Fatal("a moving total is a logic error")
	}
	if _, err := seen.Check("w", nil); !errors.Is(err, truewire.ErrLogic) {
		t.Fatal("a missing total is a logic error")
	}
}

// -- streams --------------------------------------------------------------------------

func TestStreamMapsFiltersAndEnds(t *testing.T) {
	ctx := context.Background()
	items := []string{`1`, `2`, `3`, `4`}
	done := false
	raw := truewire.NewStream[json.RawMessage](nil, func(ctx context.Context) (json.RawMessage, error) {
		if done || len(items) == 0 {
			return nil, truewire.ErrStreamDone
		}
		item := items[0]
		items = items[1:]
		return json.RawMessage(item), nil
	}, func(ctx context.Context) error { done = true; return nil })
	typed := truewire.FilterStream(truewire.MapStream(raw, truewire.Decode[int]), func(n int) bool { return n%2 == 0 })
	var got []int
	for n, err := range typed.Seq(ctx) {
		if err != nil {
			t.Fatal(err)
		}
		got = append(got, n)
		if n == 2 {
			_ = typed.Unsubscribe(ctx)
		}
	}
	if len(got) != 1 || got[0] != 2 {
		t.Fatal(got)
	}
}

func TestIntegerFieldsAcceptAnIntegralNumeral(t *testing.T) {
	var got struct {
		Leverage int64
		Extra    map[string]json.RawMessage
	}
	decode := func(text string) error {
		return truewire.DecodeObject([]byte(text), &got.Extra, truewire.Required("leverage", &got.Leverage))
	}
	for text, want := range map[string]int64{`{"leverage":25.0}`: 25, `{"leverage":1e2}`: 100, `{"leverage":-3.00}`: -3, `{"leverage":7}`: 7} {
		if err := decode(text); err != nil || got.Leverage != want {
			t.Fatalf("%s: %d, %v", text, got.Leverage, err)
		}
	}
	for _, text := range []string{`{"leverage":25.5}`, `{"leverage":1e30}`, `{"leverage":"25"}`} {
		if err := decode(text); err == nil {
			t.Fatalf("%s decoded as %d", text, got.Leverage)
		}
	}
}
