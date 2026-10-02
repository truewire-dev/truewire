// Package twtest is the Go half of `truewire.testing`: spawn `truewire mock` for a project,
// discover its recorded examples, and replay every one through a generated client.
//
// A generated package renders a `replay` sub-package whose Table maps every function path
// to a Call; a project's test is then three lines:
//
//	mock := twtest.StartMock(t, projectRoot)
//	client := github.New(core.Options{BaseURL: mock.HTTPBaseURL})
//	twtest.ReplayHTTP(t, projectRoot, replay.Table(client))
package twtest

import (
	"bufio"
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"math/big"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"

	truewire "truewire.dev/core"
)

// Mock is a running `truewire mock`.
type Mock struct {
	HTTPBaseURL string
	// WSURL is the WebSocket server's URL, "" when the project records no WebSocket examples.
	WSURL string
	cmd   *exec.Cmd
}

// Binary is the truewire executable: $TRUEWIRE_BIN, else `.venv/bin/truewire` in the
// nearest ancestor of dir that has one, else `truewire` on PATH.
func Binary(dir string) string {
	if bin := os.Getenv("TRUEWIRE_BIN"); bin != "" {
		return bin
	}
	abs, err := filepath.Abs(dir)
	if err == nil {
		for d := abs; ; d = filepath.Dir(d) {
			candidate := filepath.Join(d, ".venv", "bin", "truewire")
			if info, err := os.Stat(candidate); err == nil && !info.IsDir() {
				return candidate
			}
			if filepath.Dir(d) == d {
				break
			}
		}
	}
	return "truewire"
}

// StartMock starts `truewire mock` for the project at root on free ports and stops it
// when the test ends.
func StartMock(t testing.TB, root string) *Mock {
	t.Helper()
	cmd := exec.Command(Binary(root), "mock", "--project", root, "--http-port", "0", "--ws-port", "0")
	cmd.Env = append(os.Environ(), "PYTHONUNBUFFERED=1")
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := cmd.Start(); err != nil {
		t.Fatalf("truewire mock does not start: %v", err)
	}
	mock := &Mock{cmd: cmd}
	t.Cleanup(func() {
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
	})
	lines := bufio.NewScanner(stdout)
	ready := make(chan error, 1)
	go func() {
		for lines.Scan() {
			fields := strings.Fields(lines.Text())
			if len(fields) < 2 {
				continue
			}
			switch fields[0] {
			case "HTTP":
				mock.HTTPBaseURL = fields[1]
			case "WS":
				if strings.Contains(fields[1], "://") {
					mock.WSURL = fields[1]
				}
				ready <- nil
				for lines.Scan() {
				}
				return
			}
		}
		ready <- fmt.Errorf("truewire mock exited before announcing its ports: %s", stderr.String())
	}()
	select {
	case err := <-ready:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(60 * time.Second):
		t.Fatal("truewire mock did not announce its ports within 60 s")
	}
	return mock
}

// Example is one recorded exchange of an endpoint.
type Example struct {
	// Function is the endpoint's dotted function path.
	Function string
	ID       string
	// Dir is the endpoint's directory.
	Dir string
	// Request is the recorded request value (`request`, else legacy `parameters`), `{}` when none.
	Request json.RawMessage
	// Response is the recorded reply file, for HTTP (`{"status", "payload"}`).
	Response json.RawMessage
	// Handwritten is true when the endpoint's `surface` names a hand-written method, which
	// the generated replay table does not hold.
	Handwritten bool
	// Reply and Messages are a WebSocket stream example's acknowledgement and pushes.
	Reply    json.RawMessage
	Messages []json.RawMessage
	// Frames are the binary pushes of a `<id>.messages.protobuf.json` sidecar, decoded from
	// base64, in recorded order: what the mock sends in place of Messages (ADR 0016).
	Frames [][]byte
}

type endpointFile struct {
	Function string `json:"function"`
	Surface  struct {
		Kind string `json:"kind"`
	} `json:"surface"`
	Spec struct {
		Kind       string   `json:"kind"`
		Transports []string `json:"transports"`
	} `json:"spec"`
}

func readJSON(path string, into any) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	return json.Unmarshal(data, into)
}

// walk calls visit for every endpoint directory under root/spec/endpoints, with its
// function path (the authored `function`, else the directory path).
func walk(root string, visit func(dir, function string, endpoint endpointFile)) error {
	base := filepath.Join(root, "spec", "endpoints")
	return filepath.WalkDir(base, func(path string, d os.DirEntry, err error) error {
		if err != nil || d.IsDir() || d.Name() != "endpoint.json" {
			return err
		}
		var endpoint endpointFile
		if err := readJSON(path, &endpoint); err != nil {
			return fmt.Errorf("%s: %w", path, err)
		}
		dir := filepath.Dir(path)
		rel, _ := filepath.Rel(base, dir)
		function := endpoint.Function
		if function == "" {
			function = strings.ReplaceAll(filepath.ToSlash(rel), "/", ".")
		}
		visit(dir, function, endpoint)
		return nil
	})
}

func exampleIDs(dir, suffix string) []string {
	matches, _ := filepath.Glob(filepath.Join(dir, "examples", "*"+suffix))
	ids := make([]string, 0, len(matches))
	for _, m := range matches {
		ids = append(ids, strings.TrimSuffix(filepath.Base(m), suffix))
	}
	sort.Strings(ids)
	return ids
}

func requestValue(path string) json.RawMessage {
	var recorded struct {
		Request    json.RawMessage `json:"request"`
		Parameters json.RawMessage `json:"parameters"`
	}
	if readJSON(path, &recorded) != nil {
		return json.RawMessage(`{}`)
	}
	if recorded.Request != nil && string(recorded.Request) != "null" {
		return recorded.Request
	}
	if recorded.Parameters != nil {
		return recorded.Parameters
	}
	return json.RawMessage(`{}`)
}

// HTTPExamples is every recorded `<id>.request.json` with a `<id>.response.json` beside it,
// sorted by function path then id.
func HTTPExamples(root string) ([]Example, error) {
	var out []Example
	err := walk(root, func(dir, function string, endpoint endpointFile) {
		if endpoint.Spec.Kind == "grpc" {
			// A gRPC recording is replayed by grpctest, not over HTTP (ADR 0017).
			return
		}
		for _, id := range exampleIDs(dir, ".request.json") {
			responsePath := filepath.Join(dir, "examples", id+".response.json")
			response, err := os.ReadFile(responsePath)
			if err != nil {
				continue
			}
			out = append(out, Example{Function: function, ID: id, Dir: dir, Request: requestValue(filepath.Join(dir, "examples", id+".request.json")), Response: response, Handwritten: endpoint.Surface.Kind == "handwritten"})
		}
	})
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].Function != out[j].Function {
			return out[i].Function < out[j].Function
		}
		return out[i].ID < out[j].ID
	})
	return out, err
}

// StreamExamples is every recorded stream example: `<id>.parameters.json` with its
// `.reply.json` and `.messages.json` when present.
func StreamExamples(root string) ([]Example, error) {
	var out []Example
	err := walk(root, func(dir, function string, endpoint endpointFile) {
		if endpoint.Spec.Kind != "stream" {
			return
		}
		for _, id := range exampleIDs(dir, ".parameters.json") {
			ex := Example{Function: function, ID: id, Dir: dir, Request: requestValue(filepath.Join(dir, "examples", id+".parameters.json"))}
			if data, err := os.ReadFile(filepath.Join(dir, "examples", id+".reply.json")); err == nil {
				ex.Reply = data
			}
			ex.Messages = recordedMessages(filepath.Join(dir, "examples", id+".messages.json"))
			frames, err := BinaryFrames(filepath.Join(dir, "examples", id+".messages.protobuf.json"))
			if err == nil {
				ex.Frames = frames
			}
			out = append(out, ex)
		}
	})
	return out, err
}

// recordedMessages reads a `.messages.json` file (a list of frames, or one bare frame) as
// each frame's own bytes, compacted. The frames are never decoded into Go values, so an
// integer beyond 2^53 (a nanosecond timestamp) keeps every digit.
func recordedMessages(path string) []json.RawMessage {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	compact := func(raw []byte) json.RawMessage {
		var b bytes.Buffer
		if json.Compact(&b, raw) != nil {
			return nil
		}
		return b.Bytes()
	}
	var list []json.RawMessage
	if json.Unmarshal(data, &list) == nil {
		out := make([]json.RawMessage, 0, len(list))
		for _, m := range list {
			if c := compact(m); c != nil {
				out = append(out, c)
			}
		}
		return out
	}
	if c := compact(data); c != nil && string(c) != "null" {
		return []json.RawMessage{c}
	}
	return nil
}

type encodedFrame struct {
	Encoding string `json:"encoding"`
	Data     string `json:"data"`
}

// BinaryFrames reads a protobuf frames sidecar: a list of `{content_type, encoding, data}`
// entries, a `{"frames": [...]}` envelope of them, or one bare entry. An error when the
// file is missing or an entry is not base64.
func BinaryFrames(path string) ([][]byte, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var entries []encodedFrame
	if err := json.Unmarshal(data, &entries); err != nil {
		var envelope struct {
			Frames []encodedFrame `json:"frames"`
		}
		if err := json.Unmarshal(data, &envelope); err == nil && envelope.Frames != nil {
			entries = envelope.Frames
		} else {
			var one encodedFrame
			if err := json.Unmarshal(data, &one); err != nil {
				return nil, fmt.Errorf("%s: %w", path, err)
			}
			entries = []encodedFrame{one}
		}
	}
	frames := make([][]byte, 0, len(entries))
	for i, entry := range entries {
		if entry.Encoding != "" && entry.Encoding != "base64" {
			return nil, fmt.Errorf("%s: frame %d: unsupported encoding %q", path, i, entry.Encoding)
		}
		frame, err := base64.StdEncoding.DecodeString(entry.Data)
		if err != nil {
			return nil, fmt.Errorf("%s: frame %d: %w", path, i, err)
		}
		frames = append(frames, frame)
	}
	return frames, nil
}

// Call replays one recorded request through a generated method: the typed result dumped
// back to the wire, and the `<Method>Raw` body as it came.
type Call func(ctx context.Context, request json.RawMessage) (typed, raw json.RawMessage, err error)

// ReplayHTTP replays every recorded HTTP example of the project through table (function
// path -> Call), each as a subtest, and fails when a call errors or the typed value does
// not dump back to exactly the wire body. An example whose function has no entry fails too,
// and an endpoint whose `surface` is hand-written is skipped (its own tests replay it).
func ReplayHTTP(t *testing.T, root string, table map[string]Call) {
	t.Helper()
	examples, err := HTTPExamples(root)
	if err != nil {
		t.Fatal(err)
	}
	if len(examples) == 0 {
		t.Fatal("no recorded HTTP examples found")
	}
	for _, ex := range examples {
		t.Run(ex.Function+"/"+ex.ID, func(t *testing.T) {
			if ex.Handwritten {
				t.Skipf("%s: a hand-written surface, replayed by its own tests", ex.Function)
			}
			call, ok := table[ex.Function]
			if !ok {
				t.Fatalf("%s: no generated method to replay it through", ex.Function)
			}
			ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
			defer cancel()
			typed, raw, err := call(ctx, ex.Request)
			if err != nil {
				t.Fatalf("%s (%s): %v", ex.Function, ex.ID, err)
			}
			if diff := FirstDifference(typed, raw); diff != "" {
				t.Fatalf("%s (%s): the typed value does not round-trip to the wire body at %s", ex.Function, ex.ID, diff)
			}
		})
	}
}

// FirstDifference is the first JSON pointer at which two JSON documents differ (numbers
// compared by value), "" when they are equal.
func FirstDifference(left, right json.RawMessage) string {
	var a, b any
	if err := decodeNumbers(left, &a); err != nil {
		return "(left is not JSON: " + err.Error() + ")"
	}
	if err := decodeNumbers(right, &b); err != nil {
		return "(right is not JSON: " + err.Error() + ")"
	}
	return difference(a, b, "")
}

func decodeNumbers(data []byte, into *any) error {
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber()
	return dec.Decode(into)
}

func difference(a, b any, path string) string {
	switch x := a.(type) {
	case map[string]any:
		y, ok := b.(map[string]any)
		if !ok {
			return fmt.Sprintf("%s: object vs %v", orRoot(path), b)
		}
		keys := make([]string, 0, len(x)+len(y))
		for k := range x {
			keys = append(keys, k)
		}
		for k := range y {
			if _, seen := x[k]; !seen {
				keys = append(keys, k)
			}
		}
		sort.Strings(keys)
		for _, k := range keys {
			xv, xok := x[k]
			yv, yok := y[k]
			if !xok || !yok {
				return fmt.Sprintf("%s/%s: present on one side only", path, k)
			}
			if d := difference(xv, yv, path+"/"+k); d != "" {
				return d
			}
		}
		return ""
	case []any:
		y, ok := b.([]any)
		if !ok {
			return fmt.Sprintf("%s: array vs %v", orRoot(path), b)
		}
		if len(x) != len(y) {
			return fmt.Sprintf("%s: %d vs %d items", orRoot(path), len(x), len(y))
		}
		for i := range x {
			if d := difference(x[i], y[i], fmt.Sprintf("%s/%d", path, i)); d != "" {
				return d
			}
		}
		return ""
	case json.Number:
		y, ok := b.(json.Number)
		// Exact: "1.0" equals "1" and "1e3" equals "1000", but no digit beyond float64 is lost.
		if ok && (x == y || numbersEqual(string(x), string(y))) {
			return ""
		}
		return fmt.Sprintf("%s: %v vs %v", orRoot(path), a, b)
	case string:
		if y, ok := b.(string); ok && (x == y || sameInstant(x, y)) {
			return ""
		}
		return fmt.Sprintf("%s: %v vs %v", orRoot(path), a, b)
	default:
		if a == b {
			return ""
		}
		return fmt.Sprintf("%s: %v vs %v", orRoot(path), a, b)
	}
}

// sameInstant reports whether two strings are RFC 3339 date-times naming the same instant:
// a typed timestamp dumps the fraction digits it needs (`...:35Z`), while an API may spell
// the same instant with a zero fraction (`...:35.000Z`), as numbers compare by value.
func sameInstant(a, b string) bool {
	left, err := truewire.ParseDateTime(a)
	if err != nil {
		return false
	}
	right, err := truewire.ParseDateTime(b)
	return err == nil && left.Equal(right)
}

func numbersEqual(a, b string) bool {
	x, okX := new(big.Rat).SetString(a)
	y, okY := new(big.Rat).SetString(b)
	return okX && okY && x.Cmp(y) == 0
}

func orRoot(path string) string {
	if path == "" {
		return "/"
	}
	return path
}

// ReplayOf is the Call for a generated method taking a request, from its typed method
// and its `<Method>Raw` twin: `twtest.ReplayOf(client.Repos.Get, client.Repos.GetRaw)`.
func ReplayOf[Req, Res any, O any](
	typed func(context.Context, Req, ...O) (Res, error),
	raw func(context.Context, Req, ...O) (json.RawMessage, error),
) Call {
	return func(ctx context.Context, request json.RawMessage) (json.RawMessage, json.RawMessage, error) {
		var req Req
		if err := json.Unmarshal(request, &req); err != nil {
			return nil, nil, fmt.Errorf("the recorded request does not decode: %w", err)
		}
		value, err := typed(ctx, req)
		if err != nil {
			return nil, nil, err
		}
		dumped, err := json.Marshal(value)
		if err != nil {
			return nil, nil, err
		}
		body, err := raw(ctx, req)
		return dumped, body, err
	}
}

// ReplayOfNoRequest is ReplayOf for a generated method that takes no request.
func ReplayOfNoRequest[Res any, O any](
	typed func(context.Context, ...O) (Res, error),
	raw func(context.Context, ...O) (json.RawMessage, error),
) Call {
	return func(ctx context.Context, request json.RawMessage) (json.RawMessage, json.RawMessage, error) {
		value, err := typed(ctx)
		if err != nil {
			return nil, nil, err
		}
		dumped, err := json.Marshal(value)
		if err != nil {
			return nil, nil, err
		}
		body, err := raw(ctx)
		return dumped, body, err
	}
}
