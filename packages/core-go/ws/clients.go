package ws

import (
	"context"

	truewire "truewire.dev/core"
)

// Rpc is a Socket with id-correlated request/reply. Set Parse to classify frames and
// SendRequest to write a request tagged with its id.
type Rpc[Req, Rep any] struct {
	Socket
	// Parse reads a frame as the reply to request id; ok false for frames that are not one.
	Parse func(Data) (id int64, reply Rep, ok bool, err error)
	// SendRequest writes request tagged with id.
	SendRequest func(ctx context.Context, conn *Connection, id int64, request Req) error
	Replies     Replies[Rep]
}

// Init wires OnMessage to Parse; call it once after setting the fields (NewRpc does).
func (r *Rpc[Req, Rep]) Init() *Rpc[Req, Rep] {
	r.OnMessage = func(d Data) error {
		id, reply, ok, err := r.Parse(d)
		if err != nil || !ok {
			return err
		}
		r.Replies.Resolve(id, reply)
		return nil
	}
	return r
}

// Request sends one request and waits for its reply.
func (r *Rpc[Req, Rep]) Request(ctx context.Context, request Req) (Rep, error) {
	return r.Replies.Request(ctx, &r.Socket, func(ctx context.Context, conn *Connection, id int64) error {
		return r.SendRequest(ctx, conn, id, request)
	})
}

// Streams is a Socket with channel subscriptions. Set Parse, RequestSubscription and
// RequestUnsubscription.
type Streams[N any] struct {
	Socket
	// Parse reads a frame as a message on channel; ok false for frames that are not one.
	Parse                 func(Data) (channel string, message N, ok bool, err error)
	RequestSubscription   func(ctx context.Context, channel string, params any) (any, error)
	RequestUnsubscription func(ctx context.Context, channel string, params any) error
	Subscriptions         Subscriptions[N]
}

// Init wires OnMessage to Parse.
func (s *Streams[N]) Init() *Streams[N] {
	s.OnMessage = func(d Data) error {
		channel, message, ok, err := s.Parse(d)
		if err != nil || !ok {
			return err
		}
		s.Subscriptions.Dispatch(channel, message)
		return nil
	}
	return s
}

// Subscribe subscribes to the local channel with params.
func (s *Streams[N]) Subscribe(ctx context.Context, channel string, params any, opts SubscribeOptions[N]) (*Stream[N], error) {
	return Subscribe(ctx, &s.Socket, &s.Subscriptions, channel, opts,
		func(ctx context.Context, requestChannel string) (any, error) {
			return s.RequestSubscription(ctx, requestChannel, params)
		},
		func(ctx context.Context, requestChannel string) error {
			return s.RequestUnsubscription(ctx, requestChannel, params)
		})
}

// Message is what a StreamsRpc frame parses to: a reply to request ID, or a message on Channel.
type Message[Rep, N any] struct {
	IsReply bool
	ID      int64
	Reply   Rep
	Channel string
	Message N
}

// StreamsRpc is a Socket with both id-correlated replies and channel subscriptions.
type StreamsRpc[Req, Rep, N any] struct {
	Socket
	// Parse classifies a frame; ok false for frames that are neither.
	Parse                 func(Data) (msg Message[Rep, N], ok bool, err error)
	SendRequest           func(ctx context.Context, conn *Connection, id int64, request Req) error
	RequestSubscription   func(ctx context.Context, channel string, params any) (any, error)
	RequestUnsubscription func(ctx context.Context, channel string, params any) error
	Replies               Replies[Rep]
	Subscriptions         Subscriptions[N]
}

// Init wires OnMessage to Parse.
func (s *StreamsRpc[Req, Rep, N]) Init() *StreamsRpc[Req, Rep, N] {
	s.OnMessage = func(d Data) error {
		msg, ok, err := s.Parse(d)
		if err != nil || !ok {
			return err
		}
		if msg.IsReply {
			s.Replies.Resolve(msg.ID, msg.Reply)
		} else {
			s.Subscriptions.Dispatch(msg.Channel, msg.Message)
		}
		return nil
	}
	return s
}

// Request sends one request and waits for its reply.
func (s *StreamsRpc[Req, Rep, N]) Request(ctx context.Context, request Req) (Rep, error) {
	return s.Replies.Request(ctx, &s.Socket, func(ctx context.Context, conn *Connection, id int64) error {
		return s.SendRequest(ctx, conn, id, request)
	})
}

// Subscribe subscribes to the local channel with params.
func (s *StreamsRpc[Req, Rep, N]) Subscribe(ctx context.Context, channel string, params any, opts SubscribeOptions[N]) (*Stream[N], error) {
	return Subscribe(ctx, &s.Socket, &s.Subscriptions, channel, opts,
		func(ctx context.Context, requestChannel string) (any, error) {
			return s.RequestSubscription(ctx, requestChannel, params)
		},
		func(ctx context.Context, requestChannel string) error {
			return s.RequestUnsubscription(ctx, requestChannel, params)
		})
}

// Stream is the runtime's stream type, re-exported for cores that only import ws.
type Stream[T any] = truewire.Stream[T]
