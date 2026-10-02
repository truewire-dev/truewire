package core

import (
	"context"
	"encoding/json"
	"time"

	truewire "truewire.dev/core"
	"truewire.dev/core/ws"
)

// WebSocket v2 URLs: public market data, and the token-authenticated private connection.
const (
	SpotWSURL     = "wss://ws.kraken.com/v2"
	SpotWSAuthURL = "wss://ws-auth.kraken.com/v2"
)

// rawMethods are commands whose reply is the whole frame rather than its `result`.
var rawMethods = map[string]bool{"ping": true, "batch_cancel": true}

// pingInterval keeps a connection Kraken would close after about a minute idle.
const pingInterval = 30 * time.Second

type request struct {
	Method string                     `json:"method"`
	Params map[string]json.RawMessage `json:"params"`
	ReqID  *int64                     `json:"req_id,omitempty"`
}

type reply struct {
	Method  string          `json:"method"`
	Success *bool           `json:"success"`
	Result  json.RawMessage `json:"result"`
	Error   string          `json:"error"`
}

// SocketOptions is what NewSocket takes.
type SocketOptions struct {
	// URL is SpotWSURL (the default) for the public connection, SpotWSAuthURL for the private one.
	URL string
	// Token is the source of the token the private connection sends; none on the public one.
	Token func(ctx context.Context) (string, error)
	// Dial replaces the WebSocket dialer (a test double).
	Dial ws.Dialer
}

// Socket is the transport every `streams.*` and `trading_ws.*` endpoint calls. Kraken
// correlates every reply, trading-method replies and subscribe acks alike, by the client's
// `req_id`, so one StreamsRpc connection serves both Command and Subscribe.
type Socket struct {
	conn  ws.StreamsRpc[request, json.RawMessage, json.RawMessage]
	token func(ctx context.Context) (string, error)
}

// NewSocket returns one WebSocket transport.
func NewSocket(options SocketOptions) *Socket {
	s := &Socket{token: options.Token}
	s.conn.URL = options.URL
	if s.conn.URL == "" {
		s.conn.URL = SpotWSURL
	}
	s.conn.Dial = options.Dial
	s.conn.PingInterval = pingInterval
	s.conn.Ping = func(ctx context.Context, conn *ws.Connection) error {
		return conn.Write(ctx, ws.Text, []byte(`{"method":"ping"}`))
	}
	s.conn.Parse = func(d ws.Data) (ws.Message[json.RawMessage, json.RawMessage], bool, error) {
		var frame struct {
			ReqID   *int64  `json:"req_id"`
			Channel *string `json:"channel"`
		}
		if err := json.Unmarshal(d.Bytes, &frame); err != nil {
			return ws.Message[json.RawMessage, json.RawMessage]{}, false, nil
		}
		switch {
		case frame.ReqID != nil:
			return ws.Message[json.RawMessage, json.RawMessage]{IsReply: true, ID: *frame.ReqID, Reply: d.Bytes}, true, nil
		case frame.Channel != nil:
			return ws.Message[json.RawMessage, json.RawMessage]{Channel: *frame.Channel, Message: d.Bytes}, true, nil
		}
		return ws.Message[json.RawMessage, json.RawMessage]{}, false, nil
	}
	s.conn.SendRequest = func(ctx context.Context, conn *ws.Connection, id int64, r request) error {
		if s.token != nil {
			token, err := s.token(ctx)
			if err != nil {
				return err
			}
			if r.Params == nil {
				r.Params = map[string]json.RawMessage{}
			}
			encoded, _ := json.Marshal(token)
			r.Params["token"] = encoded
		}
		if r.Params == nil {
			r.Params = map[string]json.RawMessage{}
		}
		r.ReqID = &id
		data, err := json.Marshal(r)
		if err != nil {
			return truewire.LogicError("cannot encode the request").WithCause(err)
		}
		return conn.Write(ctx, ws.Text, data)
	}
	s.conn.RequestSubscription = func(ctx context.Context, channel string, params any) (any, error) {
		return s.verb(ctx, "subscribe", channel, params.(map[string]json.RawMessage))
	}
	s.conn.RequestUnsubscription = func(ctx context.Context, channel string, params any) error {
		_, err := s.verb(ctx, "unsubscribe", channel, params.(map[string]json.RawMessage))
		return err
	}
	s.conn.Init()
	return s
}

// IsOpen reports whether the connection is open.
func (s *Socket) IsOpen() bool { return s.conn.IsOpen() }

// Close closes the connection; the next use opens it again.
func (s *Socket) Close() error { return s.conn.Close() }

func (s *Socket) verb(ctx context.Context, method, channel string, params map[string]json.RawMessage) (json.RawMessage, error) {
	merged := map[string]json.RawMessage{}
	for k, v := range params {
		merged[k] = v
	}
	encoded, _ := json.Marshal(channel)
	merged["channel"] = encoded
	raw, err := s.conn.Request(ctx, request{Method: method, Params: merged})
	if err != nil {
		return nil, err
	}
	return raw, checked(method, raw)
}

func checked(method string, raw json.RawMessage) error {
	var r reply
	if err := json.Unmarshal(raw, &r); err != nil {
		return truewire.ValidationError("", "invalid reply frame")
	}
	if r.Success != nil && !*r.Success {
		message := r.Error
		if message == "" {
			message = `"` + method + `" failed`
		}
		return RaiseError([]string{message})
	}
	return nil
}

// Command sends one method call; the reply's `result` (the whole frame for `ping` and
// `batch_cancel`).
func (s *Socket) Command(ctx context.Context, call truewire.CommandCall) (json.RawMessage, error) {
	params, err := truewire.Object(call.Request)
	if err != nil {
		return nil, err
	}
	raw, err := s.conn.Request(ctx, request{Method: call.Path, Params: params})
	if err != nil {
		return nil, err
	}
	if err := checked(call.Path, raw); err != nil {
		return nil, err
	}
	if rawMethods[call.Path] {
		return raw, nil
	}
	var r reply
	_ = json.Unmarshal(raw, &r)
	return r.Result, nil
}

// Subscribe subscribes to one channel; the stream's messages are the pushed frames.
func (s *Socket) Subscribe(ctx context.Context, call truewire.SubscribeCall) (*truewire.Stream[json.RawMessage], error) {
	params, err := truewire.Object(call.Parameters)
	if err != nil {
		return nil, err
	}
	return s.conn.Subscribe(ctx, call.Channel, params, ws.SubscribeOptions[json.RawMessage]{})
}
