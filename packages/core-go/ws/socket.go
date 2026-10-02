// Package ws holds the WebSocket clients a hand-written core is built from: the Go half of
// `truewire_core.ws` and `@truewire/core`'s `ws`.
//
// A Socket owns one lazily opened connection, routes every incoming frame to OnMessage,
// optionally pings, and fails every pending Wait when the connection goes away. The
// correlation strategies are components a core composes beside it and feeds from its
// OnMessage: Replies (id-correlated request/reply), Subscriptions (channel streams) and
// Serial (replies with no correlation id, matched by arrival order). Rpc, Streams and
// StreamsRpc wire the common combinations.
package ws

import (
	"context"
	"errors"
	"sync"
	"time"

	"github.com/coder/websocket"

	truewire "truewire.dev/core"
)

// MessageType is a frame's type.
type MessageType int

const (
	Text   MessageType = MessageType(websocket.MessageText)
	Binary MessageType = MessageType(websocket.MessageBinary)
)

// Data is one received frame.
type Data struct {
	Type  MessageType
	Bytes []byte
}

// Text is the frame's payload as a string.
func (d Data) Text() string { return string(d.Bytes) }

// Conn is the raw connection a Socket drives.
type Conn interface {
	Read(ctx context.Context) (MessageType, []byte, error)
	Write(ctx context.Context, typ MessageType, data []byte) error
	Close(code int, reason string) error
}

// Dialer opens a raw connection to url.
type Dialer func(ctx context.Context, url string) (Conn, error)

type coderConn struct{ c *websocket.Conn }

func (c coderConn) Read(ctx context.Context) (MessageType, []byte, error) {
	typ, data, err := c.c.Read(ctx)
	return MessageType(typ), data, err
}

func (c coderConn) Write(ctx context.Context, typ MessageType, data []byte) error {
	return c.c.Write(ctx, websocket.MessageType(typ), data)
}

func (c coderConn) Close(code int, reason string) error {
	return c.c.Close(websocket.StatusCode(code), reason)
}

// DefaultDialer dials with github.com/coder/websocket, with no read limit.
func DefaultDialer(ctx context.Context, url string) (Conn, error) {
	c, _, err := websocket.Dial(ctx, url, nil)
	if err != nil {
		return nil, err
	}
	c.SetReadLimit(-1)
	return coderConn{c}, nil
}

// Connection is one live connection of a Socket.
type Connection struct {
	conn   Conn
	done   chan struct{}
	once   sync.Once
	err    error
	cancel context.CancelFunc
}

// Done is closed once the connection is gone; Err then says why.
func (c *Connection) Done() <-chan struct{} { return c.done }

// Err is why the connection ended, nil while it is live.
func (c *Connection) Err() error {
	select {
	case <-c.done:
		return c.err
	default:
		return nil
	}
}

// Abort ends the connection's background work and fails every Wait with reason.
func (c *Connection) Abort(reason error) {
	c.once.Do(func() {
		c.err = reason
		c.cancel()
		close(c.done)
	})
}

// Write sends one frame on this connection.
func (c *Connection) Write(ctx context.Context, typ MessageType, data []byte) error {
	if err := c.Err(); err != nil {
		return err
	}
	if err := c.conn.Write(ctx, typ, data); err != nil {
		return truewire.NetworkError("WebSocket write failed", err)
	}
	return nil
}

// Socket is a lazily connected WebSocket client. Configure the fields, then use it from
// any number of goroutines; the connection opens on first use and reopens after the peer
// closed it.
type Socket struct {
	URL string
	// Timeout bounds opening and closing the connection; 10 s when 0.
	Timeout time.Duration
	// PingInterval is how often Ping runs while connected; never when 0.
	PingInterval time.Duration
	// Dial opens the raw connection; DefaultDialer when nil.
	Dial Dialer
	// OnMessage handles one incoming frame; an error fails the connection. Required.
	OnMessage func(Data) error
	// Ping pings the server every PingInterval, if set.
	Ping func(ctx context.Context, conn *Connection) error
	// OnOpen runs on a new connection before it is handed to any caller: a handshake or an
	// authentication. It may Write and Wait on conn.
	OnOpen func(ctx context.Context, conn *Connection) error

	mu      sync.Mutex
	current *Connection
	opening chan struct{}
}

func (s *Socket) timeout() time.Duration {
	if s.Timeout == 0 {
		return 10 * time.Second
	}
	return s.Timeout
}

// IsOpen reports whether a live connection exists.
func (s *Socket) IsOpen() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.current != nil && s.current.Err() == nil
}

// Open returns the live connection, opening one first if there is none.
func (s *Socket) Open(ctx context.Context) (*Connection, error) {
	for {
		s.mu.Lock()
		if s.current != nil && s.current.Err() == nil {
			c := s.current
			s.mu.Unlock()
			return c, nil
		}
		if s.opening != nil {
			wait := s.opening
			s.mu.Unlock()
			select {
			case <-wait:
				continue
			case <-ctx.Done():
				return nil, ctx.Err()
			}
		}
		opening := make(chan struct{})
		s.opening = opening
		s.mu.Unlock()
		c, err := s.connect(ctx)
		s.mu.Lock()
		s.opening = nil
		if err == nil {
			s.current = c
		}
		s.mu.Unlock()
		close(opening)
		return c, err
	}
}

func (s *Socket) connect(ctx context.Context) (*Connection, error) {
	if s.OnMessage == nil {
		return nil, truewire.LogicError("ws.Socket needs an OnMessage handler")
	}
	dial := s.Dial
	if dial == nil {
		dial = DefaultDialer
	}
	dialCtx, cancelDial := context.WithTimeout(ctx, s.timeout())
	raw, err := dial(dialCtx, s.URL)
	cancelDial()
	if err != nil {
		return nil, truewire.NetworkError("Failed to connect to "+s.URL, err)
	}
	background, cancel := context.WithCancel(context.Background())
	c := &Connection{conn: raw, done: make(chan struct{}), cancel: cancel}
	go s.listen(background, c)
	if s.Ping != nil && s.PingInterval > 0 {
		go s.pinger(background, c)
	}
	if s.OnOpen != nil {
		if err := s.OnOpen(ctx, c); err != nil {
			c.Abort(err)
			_ = raw.Close(int(websocket.StatusNormalClosure), "")
			return nil, err
		}
	}
	return c, nil
}

func (s *Socket) listen(ctx context.Context, c *Connection) {
	for {
		typ, data, err := c.conn.Read(ctx)
		if err != nil {
			if ctx.Err() == nil {
				c.Abort(truewire.NetworkError("Connection closed", err))
			}
			return
		}
		if err := s.OnMessage(Data{Type: typ, Bytes: data}); err != nil {
			c.Abort(err)
			_ = c.conn.Close(int(websocket.StatusInternalError), "")
			return
		}
	}
}

func (s *Socket) pinger(ctx context.Context, c *Connection) {
	ticker := time.NewTicker(s.PingInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if err := s.Ping(ctx, c); err != nil {
				c.Abort(err)
				return
			}
		}
	}
}

// Send writes one frame on the live connection, opening it first if needed.
func (s *Socket) Send(ctx context.Context, typ MessageType, data []byte) error {
	c, err := s.Open(ctx)
	if err != nil {
		return err
	}
	return c.Write(ctx, typ, data)
}

// SendText writes one text frame.
func (s *Socket) SendText(ctx context.Context, text string) error {
	return s.Send(ctx, Text, []byte(text))
}

// Close closes the live connection, if any, and fails every pending Wait with a network
// error. The next use opens a new connection.
func (s *Socket) Close() error {
	s.mu.Lock()
	c := s.current
	s.current = nil
	s.mu.Unlock()
	if c == nil {
		return nil
	}
	c.Abort(truewire.NetworkError("Connection closed", nil))
	err := c.conn.Close(int(websocket.StatusNormalClosure), "")
	if err != nil && !errors.Is(err, context.Canceled) {
		var closeErr websocket.CloseError
		if errors.As(err, &closeErr) {
			return nil
		}
	}
	return nil
}

// Wait waits for a value on ch, failing if ctx ends or conn goes away first.
func Wait[T any](ctx context.Context, conn *Connection, ch <-chan T) (T, error) {
	var zero T
	select {
	case v := <-ch:
		return v, nil
	case <-conn.Done():
		// A value that raced the close still wins.
		select {
		case v := <-ch:
			return v, nil
		default:
		}
		return zero, conn.Err()
	case <-ctx.Done():
		return zero, ctx.Err()
	}
}
