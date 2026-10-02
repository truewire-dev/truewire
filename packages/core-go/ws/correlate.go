package ws

import (
	"context"
	"sync"

	truewire "truewire.dev/core"
)

// Replies correlates requests with replies by a numeric id. A core feeds it from
// OnMessage (Resolve) and calls Request; any number of concurrent requests is fine.
type Replies[R any] struct {
	mu      sync.Mutex
	counter int64
	pending map[int64]chan R
}

// Request sends under a fresh id (send writes the frame tagged with it) and waits for
// the reply Resolve delivers for that id.
func (r *Replies[R]) Request(ctx context.Context, socket *Socket, send func(ctx context.Context, conn *Connection, id int64) error) (R, error) {
	var zero R
	conn, err := socket.Open(ctx)
	if err != nil {
		return zero, err
	}
	r.mu.Lock()
	if r.pending == nil {
		r.pending = map[int64]chan R{}
	}
	id := r.counter
	r.counter++
	ch := make(chan R, 1)
	r.pending[id] = ch
	r.mu.Unlock()
	defer func() {
		r.mu.Lock()
		delete(r.pending, id)
		r.mu.Unlock()
	}()
	if err := send(ctx, conn, id); err != nil {
		return zero, err
	}
	return Wait(ctx, conn, ch)
}

// Resolve delivers the reply for id; false when no request waits for it.
func (r *Replies[R]) Resolve(id int64, reply R) bool {
	r.mu.Lock()
	ch, ok := r.pending[id]
	r.mu.Unlock()
	if ok {
		select {
		case ch <- reply:
		default:
		}
	}
	return ok
}

// Serial matches each request to the very next reply, by arrival order alone: the only
// correlation an API with no reply ids gives. Requests are serialized internally. A core
// routes replies into Push from OnMessage.
type Serial[R any] struct {
	lock    sync.Mutex
	mu      sync.Mutex
	replies []R
	signal  chan struct{}
}

func (s *Serial[R]) init() {
	if s.signal == nil {
		s.signal = make(chan struct{}, 1)
	}
}

// Push delivers one reply.
func (s *Serial[R]) Push(reply R) {
	s.mu.Lock()
	s.init()
	s.replies = append(s.replies, reply)
	s.mu.Unlock()
	select {
	case s.signal <- struct{}{}:
	default:
	}
}

// Request sends (under the lock) and returns the next reply.
func (s *Serial[R]) Request(ctx context.Context, socket *Socket, send func(ctx context.Context, conn *Connection) error) (R, error) {
	var zero R
	s.lock.Lock()
	defer s.lock.Unlock()
	conn, err := socket.Open(ctx)
	if err != nil {
		return zero, err
	}
	if err := send(ctx, conn); err != nil {
		return zero, err
	}
	for {
		s.mu.Lock()
		s.init()
		if len(s.replies) > 0 {
			reply := s.replies[0]
			s.replies = s.replies[1:]
			s.mu.Unlock()
			return reply, nil
		}
		signal := s.signal
		s.mu.Unlock()
		if _, err := Wait(ctx, conn, signal); err != nil {
			return zero, err
		}
	}
}

// queue is an unbounded FIFO with a wake-up channel.
type queue[T any] struct {
	mu     sync.Mutex
	items  []T
	signal chan struct{}
}

func newQueue[T any]() *queue[T] { return &queue[T]{signal: make(chan struct{}, 1)} }

func (q *queue[T]) push(item T) {
	q.mu.Lock()
	q.items = append(q.items, item)
	q.mu.Unlock()
	select {
	case q.signal <- struct{}{}:
	default:
	}
}

func (q *queue[T]) pop() (T, bool) {
	q.mu.Lock()
	defer q.mu.Unlock()
	var zero T
	if len(q.items) == 0 {
		return zero, false
	}
	item := q.items[0]
	q.items = q.items[1:]
	return item, true
}

// SubscribeOptions adjusts one subscription.
type SubscribeOptions[N any] struct {
	// RequestChannel names the channel in the subscribe request when it differs from the
	// local one.
	RequestChannel string
	// MessageKey derives the local channel of a message when the channel alone cannot.
	MessageKey func(N) string
}

// Subscriptions routes channel messages to their streams: one subscription per local
// channel at a time. A core feeds it from OnMessage (Dispatch) and calls Subscribe.
type Subscriptions[N any] struct {
	mu   sync.Mutex
	subs map[string]*queue[N]
	keys map[string]func(N) string
}

// Dispatch routes one message pushed on channel; false when nothing is subscribed to it.
func (s *Subscriptions[N]) Dispatch(channel string, message N) bool {
	s.mu.Lock()
	if key, ok := s.keys[channel]; ok {
		channel = key(message)
	}
	q, ok := s.subs[channel]
	s.mu.Unlock()
	if ok {
		q.push(message)
	}
	return ok
}

// Subscribe registers channel, sends the subscription (subscribe returns its reply) and
// returns the stream; Unsubscribe on the stream calls unsubscribe and drops the channel.
func Subscribe[N any](
	ctx context.Context, socket *Socket, subs *Subscriptions[N], channel string, opts SubscribeOptions[N],
	subscribe func(ctx context.Context, requestChannel string) (any, error),
	unsubscribe func(ctx context.Context, requestChannel string) error,
) (*truewire.Stream[N], error) {
	requestChannel := opts.RequestChannel
	if requestChannel == "" {
		requestChannel = channel
	}
	subs.mu.Lock()
	if subs.subs == nil {
		subs.subs = map[string]*queue[N]{}
		subs.keys = map[string]func(N) string{}
	}
	if _, taken := subs.subs[channel]; taken {
		subs.mu.Unlock()
		return nil, truewire.LogicError("Already subscribed to channel %q", channel)
	}
	q := newQueue[N]()
	subs.subs[channel] = q
	if opts.MessageKey != nil {
		subs.keys[requestChannel] = opts.MessageKey
	}
	subs.mu.Unlock()
	drop := func() {
		subs.mu.Lock()
		if subs.subs[channel] == q {
			delete(subs.subs, channel)
		}
		subs.mu.Unlock()
	}
	conn, err := socket.Open(ctx)
	if err != nil {
		drop()
		return nil, err
	}
	reply, err := subscribe(ctx, requestChannel)
	if err != nil {
		drop()
		return nil, err
	}
	done := make(chan struct{})
	var doneOnce sync.Once
	next := func(ctx context.Context) (N, error) {
		var zero N
		for {
			if item, ok := q.pop(); ok {
				return item, nil
			}
			select {
			case <-done:
				return zero, truewire.ErrStreamDone
			case <-q.signal:
			case <-conn.Done():
				if item, ok := q.pop(); ok {
					return item, nil
				}
				drop()
				return zero, conn.Err()
			case <-ctx.Done():
				return zero, ctx.Err()
			}
		}
	}
	unsub := func(ctx context.Context) error {
		defer doneOnce.Do(func() { close(done); drop() })
		if unsubscribe == nil || conn.Err() != nil {
			return nil
		}
		return unsubscribe(ctx, requestChannel)
	}
	return truewire.NewStream(reply, next, unsub), nil
}
