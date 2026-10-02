package ws_test

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/coder/websocket"

	truewire "truewire.dev/core"
	"truewire.dev/core/ws"
)

// frame is the toy protocol the test server speaks: requests `{"id":n,"method":m,"params":p}`
// answered by `{"id":n,"result":...}`; `subscribe`/`unsubscribe` acknowledged the same way
// and followed by pushes `{"channel":c,"data":k}`; `close` drops the connection.
type frame struct {
	ID      *int64          `json:"id,omitempty"`
	Method  string          `json:"method,omitempty"`
	Params  json.RawMessage `json:"params,omitempty"`
	Result  json.RawMessage `json:"result,omitempty"`
	Channel string          `json:"channel,omitempty"`
	Data    json.RawMessage `json:"data,omitempty"`
}

func server(t *testing.T) string {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := websocket.Accept(w, r, nil)
		if err != nil {
			return
		}
		ctx := r.Context()
		var mu sync.Mutex
		write := func(f frame) {
			data, _ := json.Marshal(f)
			mu.Lock()
			defer mu.Unlock()
			_ = c.Write(ctx, websocket.MessageText, data)
		}
		for {
			_, data, err := c.Read(ctx)
			if err != nil {
				return
			}
			var f frame
			_ = json.Unmarshal(data, &f)
			switch f.Method {
			case "close":
				_ = c.Close(websocket.StatusGoingAway, "bye")
				return
			case "silent":
				// never answered
			case "subscribe":
				var channel string
				_ = json.Unmarshal(f.Params, &channel)
				write(frame{ID: f.ID, Result: json.RawMessage(`"subscribed"`)})
				go func() {
					for i := 0; i < 3; i++ {
						write(frame{Channel: channel, Data: json.RawMessage(`` + string(rune('0'+i)))})
					}
				}()
			default:
				write(frame{ID: f.ID, Result: f.Params})
			}
		}
	}))
	t.Cleanup(srv.Close)
	return "ws" + strings.TrimPrefix(srv.URL, "http")
}

func client(url string) *ws.StreamsRpc[frame, json.RawMessage, json.RawMessage] {
	c := &ws.StreamsRpc[frame, json.RawMessage, json.RawMessage]{}
	c.URL = url
	c.Timeout = 2 * time.Second
	c.Parse = func(d ws.Data) (ws.Message[json.RawMessage, json.RawMessage], bool, error) {
		var f frame
		if err := json.Unmarshal(d.Bytes, &f); err != nil {
			return ws.Message[json.RawMessage, json.RawMessage]{}, false, err
		}
		if f.ID != nil {
			return ws.Message[json.RawMessage, json.RawMessage]{IsReply: true, ID: *f.ID, Reply: f.Result}, true, nil
		}
		return ws.Message[json.RawMessage, json.RawMessage]{Channel: f.Channel, Message: f.Data}, true, nil
	}
	c.SendRequest = func(ctx context.Context, conn *ws.Connection, id int64, request frame) error {
		request.ID = &id
		data, _ := json.Marshal(request)
		return conn.Write(ctx, ws.Text, data)
	}
	c.RequestSubscription = func(ctx context.Context, channel string, params any) (any, error) {
		p, _ := json.Marshal(channel)
		return c.Request(ctx, frame{Method: "subscribe", Params: p})
	}
	c.RequestUnsubscription = func(ctx context.Context, channel string, params any) error {
		_, err := c.Request(ctx, frame{Method: "unsubscribe"})
		return err
	}
	return c.Init()
}

func TestConcurrentRequestsCorrelateById(t *testing.T) {
	c := client(server(t))
	defer c.Close()
	ctx := context.Background()
	var wg sync.WaitGroup
	errs := make(chan error, 20)
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			params, _ := json.Marshal(i)
			reply, err := c.Request(ctx, frame{Method: "echo", Params: params})
			if err == nil && string(reply) != string(params) {
				err = errors.New("reply " + string(reply) + " for " + string(params))
			}
			errs <- err
		}(i)
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatal(err)
		}
	}
}

func TestSubscriptionStreamsUntilUnsubscribed(t *testing.T) {
	c := client(server(t))
	defer c.Close()
	ctx := context.Background()
	stream, err := c.Subscribe(ctx, "ticker", nil, ws.SubscribeOptions[json.RawMessage]{})
	if err != nil {
		t.Fatal(err)
	}
	if string(stream.Reply.(json.RawMessage)) != `"subscribed"` {
		t.Fatalf("reply %v", stream.Reply)
	}
	if _, err := c.Subscribe(ctx, "ticker", nil, ws.SubscribeOptions[json.RawMessage]{}); !errors.Is(err, truewire.ErrLogic) {
		t.Fatal("one subscription per channel")
	}
	typed := truewire.MapStream(stream, truewire.Decode[int])
	var got []int
	for n, err := range typed.Seq(ctx) {
		if err != nil {
			t.Fatal(err)
		}
		got = append(got, n)
		if len(got) == 3 {
			if err := typed.Unsubscribe(ctx); err != nil {
				t.Fatal(err)
			}
		}
	}
	if len(got) != 3 || got[2] != 2 {
		t.Fatal(got)
	}
	if _, err := c.Subscribe(ctx, "ticker", nil, ws.SubscribeOptions[json.RawMessage]{}); err != nil {
		t.Fatal("the channel is free again after unsubscribing:", err)
	}
}

func TestADroppedConnectionFailsPendingRequestsAndReopens(t *testing.T) {
	c := client(server(t))
	defer c.Close()
	ctx := context.Background()
	done := make(chan error, 1)
	go func() {
		_, err := c.Request(ctx, frame{Method: "silent"})
		done <- err
	}()
	time.Sleep(100 * time.Millisecond)
	_ = c.SendText(ctx, `{"method":"close"}`)
	select {
	case err := <-done:
		if !errors.Is(err, truewire.ErrNetwork) {
			t.Fatalf("want a network error, got %v", err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("the pending request never failed")
	}
	reply, err := c.Request(ctx, frame{Method: "echo", Params: json.RawMessage(`1`)})
	if err != nil || string(reply) != "1" {
		t.Fatal("the socket reopens on next use:", err)
	}
}

func TestContextCancelsAWait(t *testing.T) {
	c := client(server(t))
	defer c.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	if _, err := c.Request(ctx, frame{Method: "silent"}); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal(err)
	}
}

func TestSerialMatchesRepliesByArrival(t *testing.T) {
	url := server(t)
	var serial ws.Serial[json.RawMessage]
	sock := &ws.Socket{URL: url, OnMessage: func(d ws.Data) error {
		var f frame
		_ = json.Unmarshal(d.Bytes, &f)
		serial.Push(f.Result)
		return nil
	}}
	defer sock.Close()
	ctx := context.Background()
	for i := 0; i < 3; i++ {
		params, _ := json.Marshal(i)
		reply, err := serial.Request(ctx, sock, func(ctx context.Context, conn *ws.Connection) error {
			data, _ := json.Marshal(frame{ID: new(int64), Method: "echo", Params: params})
			return conn.Write(ctx, ws.Text, data)
		})
		if err != nil || string(reply) != string(params) {
			t.Fatal(reply, err)
		}
	}
}

func TestOnOpenHandshakeAndDialFailure(t *testing.T) {
	url := server(t)
	opened := 0
	c := client(url)
	c.OnOpen = func(ctx context.Context, conn *ws.Connection) error {
		opened++
		return nil
	}
	defer c.Close()
	if _, err := c.Request(context.Background(), frame{Method: "echo", Params: json.RawMessage(`2`)}); err != nil || opened != 1 {
		t.Fatal(err, opened)
	}
	bad := client("ws://127.0.0.1:1/")
	if _, err := bad.Request(context.Background(), frame{Method: "echo"}); !errors.Is(err, truewire.ErrNetwork) {
		t.Fatal(err)
	}
}
