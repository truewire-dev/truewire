package protoframes_test

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"google.golang.org/protobuf/reflect/protoreflect"

	truewire "truewire.dev/core"
	"truewire.dev/core/protoframes"
	"truewire.dev/core/twtest"
	"truewire.dev/core/ws"
)

// fixture is a protobuf-framed project: mexc's spot trade stream, its `.proto` and the
// decoded-frame goldens every language's decoder must reproduce.
var fixture = filepath.Join("..", "..", "testing-ts", "test", "fixture-protobuf")

type golden struct {
	Message string `json:"message"`
	Frames  []struct {
		Endpoint   string          `json:"endpoint"`
		ProtoField string          `json:"proto_field"`
		Data       string          `json:"data"`
		JSON       json.RawMessage `json:"json"`
	} `json:"frames"`
}

func loadGolden(t *testing.T) golden {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(fixture, "frames.golden.json"))
	if err != nil {
		t.Fatal(err)
	}
	var g golden
	if err := json.Unmarshal(data, &g); err != nil {
		t.Fatal(err)
	}
	return g
}

func sources(t *testing.T) map[string]string {
	t.Helper()
	dir := filepath.Join(fixture, "spec", "proto")
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	out := map[string]string{}
	for _, entry := range entries {
		if strings.HasSuffix(entry.Name(), ".proto") {
			text, err := os.ReadFile(filepath.Join(dir, entry.Name()))
			if err != nil {
				t.Fatal(err)
			}
			out[entry.Name()] = string(text)
		}
	}
	return out
}

func member(t *testing.T, object json.RawMessage, key string) json.RawMessage {
	t.Helper()
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(object, &fields); err != nil {
		t.Fatal(err)
	}
	return fields[key]
}

func TestDecodesEveryRecordedFrameToItsGolden(t *testing.T) {
	g := loadGolden(t)
	frames, err := protoframes.Compile(sources(t), g.Message)
	if err != nil {
		t.Fatal(err)
	}
	if len(g.Frames) == 0 {
		t.Fatal("no golden frames")
	}
	for _, want := range g.Frames {
		t.Run(want.Endpoint, func(t *testing.T) {
			data, _ := base64.StdEncoding.DecodeString(want.Data)
			frame, err := frames.Decode(data)
			if err != nil {
				t.Fatal(err)
			}
			got, err := frame.JSON()
			if err != nil {
				t.Fatal(err)
			}
			if diff := twtest.FirstDifference(got, want.JSON); diff != "" {
				t.Fatalf("differs at %s:\n got %s\nwant %s", diff, got, want.JSON)
			}
			if !frame.Has(want.ProtoField) || frame.OneofCase("body") != want.ProtoField {
				t.Fatalf("%s is not the set body member (%q)", want.ProtoField, frame.OneofCase("body"))
			}
			body, ok, err := frame.Field(want.ProtoField)
			fd := frames.Message.Fields().ByName(protoreflect.Name(want.ProtoField))
			if err != nil || !ok || twtest.FirstDifference(body, member(t, want.JSON, fd.JSONName())) != "" {
				t.Fatalf("Field(%s) = %s, %v, %v", want.ProtoField, body, ok, err)
			}
			if channel := frame.String("channel"); channel == "" || `"`+channel+`"` != string(member(t, want.JSON, "channel")) {
				t.Fatalf("String(channel) = %q", channel)
			}
		})
	}
}

func TestUnsetFieldsAreReportedUnset(t *testing.T) {
	g := loadGolden(t)
	frames := protoframes.MustCompile(sources(t), g.Message)
	data, _ := base64.StdEncoding.DecodeString(g.Frames[0].Data)
	frame, err := frames.Decode(data)
	if err != nil {
		t.Fatal(err)
	}
	other := "private_orders"
	if g.Frames[0].ProtoField == other {
		other = "public_deals"
	}
	if frame.Has(other) {
		t.Fatalf("%s reported set", other)
	}
	if value, ok, err := frame.Field(other); ok || err != nil || value != nil {
		t.Fatalf("Field(%s) = %s, %v, %v", other, value, ok, err)
	}
	if _, _, err := frame.Field("no_such_field"); err == nil {
		t.Fatal("an undeclared field is not an error")
	}
	if frame.String("symbol_id") != "" || frame.OneofCase("no_such_oneof") != "" {
		t.Fatal("unset or undeclared values are not empty")
	}
}

func TestRejectsBytesThatAreNotTheMessage(t *testing.T) {
	frames := protoframes.MustCompile(sources(t), loadGolden(t).Message)
	_, err := frames.Decode([]byte{0x0a, 0xff})
	var e *truewire.Error
	if !errors.As(err, &e) || e.Kind != truewire.KindValidation {
		t.Fatalf("got %v", err)
	}
}

func TestReportsSourcesThatDoNotCompile(t *testing.T) {
	if _, err := protoframes.Compile(map[string]string{"bad.proto": "message {"}, "X"); err == nil {
		t.Fatal("a broken source compiled")
	}
	if _, err := protoframes.Compile(sources(t), "NoSuchMessage"); err == nil {
		t.Fatal("an undeclared message was found")
	}
}

func TestRendersScalarsAsProtoJSON(t *testing.T) {
	frames, err := protoframes.Compile(map[string]string{
		"extra.proto": `syntax = "proto3"; enum Side { SIDE_UNSET = 0; BUY = 1; } message Extra { int64 big = 1; bytes raw = 2; Side side = 3; repeated uint64 ids = 4; optional int32 zero = 5; }`,
	}, "Extra")
	if err != nil {
		t.Fatal(err)
	}
	// The same bytes as core-ts's test: big=9007199254740993, raw=0x0102, side=BUY, ids=[1,2], zero=0.
	frame, err := frames.Decode([]byte{0x08, 0x81, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x10, 0x12, 0x02, 0x01, 0x02, 0x18, 0x01, 0x22, 0x02, 0x01, 0x02, 0x28, 0x00})
	if err != nil {
		t.Fatal(err)
	}
	got, _ := frame.JSON()
	if diff := twtest.FirstDifference(got, json.RawMessage(`{"big":"9007199254740993","raw":"AQI=","side":"BUY","ids":["1","2"],"zero":0}`)); diff != "" {
		t.Fatalf("%s: %s", diff, got)
	}
}

func TestStreamExamplesCarryTheBinaryFrames(t *testing.T) {
	examples, err := twtest.StreamExamples(fixture)
	if err != nil {
		t.Fatal(err)
	}
	if len(examples) != 1 || len(examples[0].Frames) != 1 {
		t.Fatalf("got %+v", examples)
	}
	frames := protoframes.MustCompile(sources(t), loadGolden(t).Message)
	if _, err := frames.Decode(examples[0].Frames[0]); err != nil {
		t.Fatal(err)
	}
}

// TestReplaysProtobufFramesFromTheMock subscribes the way mexc's spot core does (a JSON
// subscribe and ack, binary pushes keyed by their `channel` field) against `truewire mock`
// and reads the recorded push narrowed to the endpoint's `meta.proto_field`.
func TestReplaysProtobufFramesFromTheMock(t *testing.T) {
	if testing.Short() {
		t.Skip("spawns truewire mock")
	}
	g := loadGolden(t)
	var endpoint struct {
		Meta struct {
			ProtoField string `json:"proto_field"`
		} `json:"meta"`
	}
	raw, err := os.ReadFile(filepath.Join(fixture, "spec", "endpoints", "market", "trades", "endpoint.json"))
	if err != nil || json.Unmarshal(raw, &endpoint) != nil {
		t.Fatal(err)
	}
	var parameters struct {
		Payload struct {
			Params []string `json:"params"`
		} `json:"payload"`
	}
	raw, err = os.ReadFile(filepath.Join(fixture, "spec", "endpoints", "market", "trades", "examples", "doc.parameters.json"))
	if err != nil || json.Unmarshal(raw, &parameters) != nil {
		t.Fatal(err)
	}
	field := endpoint.Meta.ProtoField
	frames := protoframes.MustCompile(sources(t), g.Message)
	mock := twtest.StartMock(t, fixture)

	acks := make(chan json.RawMessage, 1)
	streams := &ws.Streams[*protoframes.Frame]{}
	streams.URL = mock.WSURL
	streams.Parse = func(d ws.Data) (string, *protoframes.Frame, bool, error) {
		if d.Type != ws.Binary {
			select {
			case acks <- append(json.RawMessage(nil), d.Bytes...):
			default:
			}
			return "", nil, false, nil
		}
		frame, err := frames.Decode(d.Bytes)
		if err != nil {
			return "", nil, false, err
		}
		return frame.String("channel"), frame, true, nil
	}
	streams.RequestSubscription = func(ctx context.Context, channel string, _ any) (any, error) {
		body, _ := json.Marshal(map[string]any{"method": "SUBSCRIPTION", "params": []string{channel}})
		if err := streams.Send(ctx, ws.Text, body); err != nil {
			return nil, err
		}
		select {
		case ack := <-acks:
			return ack, nil
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
	streams.RequestUnsubscription = func(context.Context, string, any) error { return nil }
	streams.Init()
	defer streams.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	stream, err := streams.Subscribe(ctx, parameters.Payload.Params[0], nil, ws.SubscribeOptions[*protoframes.Frame]{})
	if err != nil {
		t.Fatal(err)
	}
	if ack, _ := stream.Reply.(json.RawMessage); !strings.Contains(string(ack), `"code"`) {
		t.Fatalf("reply = %s", stream.Reply)
	}
	narrowed := truewire.MapStream(
		truewire.FilterStream(stream, func(f *protoframes.Frame) bool { return f.Has(field) }),
		func(f *protoframes.Frame) (json.RawMessage, error) {
			body, _, err := f.Field(field)
			return body, err
		},
	)
	got, err := narrowed.Next(ctx)
	if err != nil {
		t.Fatal(err)
	}
	var want json.RawMessage
	for _, frame := range g.Frames {
		if frame.Endpoint == "spot.streams.market.trades" {
			want = member(t, frame.JSON, string(frames.Message.Fields().ByName(protoreflect.Name(field)).JSONName()))
		}
	}
	if diff := twtest.FirstDifference(got, want); want == nil || diff != "" {
		t.Fatalf("differs at %s: got %s want %s", diff, got, want)
	}
}
