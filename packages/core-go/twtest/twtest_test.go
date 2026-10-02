package twtest_test

import (
	"os"
	"path/filepath"
	"testing"

	"truewire.dev/core/twtest"
)

func write(t *testing.T, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestExampleDiscovery(t *testing.T) {
	root := t.TempDir()
	get := filepath.Join(root, "spec", "endpoints", "repos", "get")
	write(t, filepath.Join(get, "endpoint.json"), `{"spec":{"kind":"rpc","transports":["http"]}}`)
	write(t, filepath.Join(get, "examples", "b.request.json"), `{"request":{"owner":"o"}}`)
	write(t, filepath.Join(get, "examples", "b.response.json"), `{"status":200,"payload":{}}`)
	write(t, filepath.Join(get, "examples", "a.request.json"), `{"parameters":{"owner":"p"}}`)
	write(t, filepath.Join(get, "examples", "a.response.json"), `{"status":200,"payload":{}}`)
	write(t, filepath.Join(get, "examples", "orphan.request.json"), `{}`)
	ticker := filepath.Join(root, "spec", "endpoints", "streams", "ticker")
	write(t, filepath.Join(ticker, "endpoint.json"), `{"function":"market.ticker","spec":{"kind":"stream"}}`)
	write(t, filepath.Join(ticker, "examples", "x.parameters.json"), `{"parameters":{"symbol":"BTC"}}`)
	write(t, filepath.Join(ticker, "examples", "x.reply.json"), `{"ok":true}`)
	write(t, filepath.Join(ticker, "examples", "x.messages.json"), `[{"p":1},{"p":2}]`)

	balance := filepath.Join(root, "spec", "endpoints", "bank", "balance")
	write(t, filepath.Join(balance, "endpoint.json"), `{"spec":{"kind":"grpc","service":"cosmos.bank.v1beta1.Query","rpc":"Balance"}}`)
	write(t, filepath.Join(balance, "examples", "g.request.json"), `{"address":"a"}`)
	write(t, filepath.Join(balance, "examples", "g.response.json"), `{"balance":null}`)

	http, err := twtest.HTTPExamples(root)
	if err != nil || len(http) != 2 {
		t.Fatal(http, err)
	}
	if http[0].ID != "a" || http[0].Function != "repos.get" || string(http[0].Request) != `{"owner":"p"}` || string(http[1].Request) != `{"owner":"o"}` {
		t.Fatalf("%+v", http)
	}
	streams, err := twtest.StreamExamples(root)
	if err != nil || len(streams) != 1 || streams[0].Function != "market.ticker" || len(streams[0].Messages) != 2 {
		t.Fatalf("%+v %v", streams, err)
	}
}

func TestFirstDifference(t *testing.T) {
	cases := map[string][2]string{
		"": {`{"a":[1,2.0,1e3,-0.50],"b":null}`, `{"b":null,"a":[1.0,2,1000,-5E-1]}`},
		// Precision loss that float64 comparison would hide: a big integer and a long decimal.
		"/wei: 12345678901234567891 vs 12345678901234567890": {`{"wei":12345678901234567891}`, `{"wei":12345678901234567890}`},
		"/p: 0.10000000000000000001 vs 0.1":                  {`{"p":0.10000000000000000001}`, `{"p":0.1}`},
		"/a/1: 2 vs 3":                                       {`{"a":[1,2]}`, `{"a":[1,3]}`},
		"/b: present on one side only":                       {`{"a":1}`, `{"a":1,"b":2}`},
		"/: 1 vs 2 items":                                    {`[1]`, `[1,2]`},
	}
	for want, pair := range cases {
		if got := twtest.FirstDifference([]byte(pair[0]), []byte(pair[1])); got != want {
			t.Errorf("%s vs %s: %q, want %q", pair[0], pair[1], got, want)
		}
	}
}

func TestFirstDifferenceComparesDateTimesByInstant(t *testing.T) {
	same := [][2]string{
		{`{"at":"2026-05-21T05:09:35Z"}`, `{"at":"2026-05-21T05:09:35.000Z"}`},
		{`["2026-05-21T05:09:35.5Z"]`, `["2026-05-21T07:09:35.500+02:00"]`},
	}
	for _, pair := range same {
		if d := twtest.FirstDifference([]byte(pair[0]), []byte(pair[1])); d != "" {
			t.Errorf("%s vs %s: %s", pair[0], pair[1], d)
		}
	}
	different := [][2]string{
		{`{"at":"2026-05-21T05:09:35Z"}`, `{"at":"2026-05-21T05:09:35.001Z"}`},
		{`{"at":"2016-05-19T11:09:40Z"}`, `{"at":"2016-05-19T11:09:41.000Z"}`},
		{`{"at":"2026-05-21"}`, `{"at":"2026-05-21T00:00:00Z"}`},
		{`{"s":"a"}`, `{"s":"b"}`},
	}
	for _, pair := range different {
		if d := twtest.FirstDifference([]byte(pair[0]), []byte(pair[1])); d == "" {
			t.Errorf("%s vs %s: reported equal", pair[0], pair[1])
		}
	}
}

func TestStreamExampleMessagesKeepIntegersBeyond2To53(t *testing.T) {
	root := t.TempDir()
	ticker := filepath.Join(root, "spec", "endpoints", "streams", "ticker")
	write(t, filepath.Join(ticker, "endpoint.json"), `{"spec":{"kind":"stream"}}`)
	write(t, filepath.Join(ticker, "examples", "x.parameters.json"), `{"parameters":{}}`)
	write(t, filepath.Join(ticker, "examples", "x.messages.json"), "[\n  {\"ts\": 1786799475005395520, \"p\": \"1.50\"}\n]")
	write(t, filepath.Join(ticker, "examples", "y.parameters.json"), `{"parameters":{}}`)
	write(t, filepath.Join(ticker, "examples", "y.messages.json"), `{"time": 1786799359144867232}`)

	streams, err := twtest.StreamExamples(root)
	if err != nil || len(streams) != 2 {
		t.Fatal(streams, err)
	}
	if len(streams[0].Messages) != 1 || string(streams[0].Messages[0]) != `{"ts":1786799475005395520,"p":"1.50"}` {
		t.Fatalf("list form: %s", streams[0].Messages)
	}
	if len(streams[1].Messages) != 1 || string(streams[1].Messages[0]) != `{"time":1786799359144867232}` {
		t.Fatalf("single frame: %s", streams[1].Messages)
	}
}
