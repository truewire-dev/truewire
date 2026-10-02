package core

import (
	"context"
	"errors"
	"iter"
	"sync"
)

// ErrStreamDone is what Stream.Next returns once the stream was unsubscribed or ended.
var ErrStreamDone = errors.New("stream done")

// Stream is a live subscription: the reply that acknowledged it, the pushed messages as
// they arrive (Next, or ranging over Seq), and Unsubscribe.
type Stream[T any] struct {
	// Reply is the subscription's acknowledgement as the core returned it; nil when none.
	Reply       any
	next        func(ctx context.Context) (T, error)
	unsubscribe func(ctx context.Context) error
	once        sync.Once
	unsubErr    error
}

// NewStream is a stream whose messages come from next and which unsubscribe ends.
func NewStream[T any](reply any, next func(ctx context.Context) (T, error), unsubscribe func(ctx context.Context) error) *Stream[T] {
	if unsubscribe == nil {
		unsubscribe = func(context.Context) error { return nil }
	}
	return &Stream[T]{Reply: reply, next: next, unsubscribe: unsubscribe}
}

// Next waits for the next message; ErrStreamDone once the stream has ended.
func (s *Stream[T]) Next(ctx context.Context) (T, error) { return s.next(ctx) }

// Unsubscribe ends the subscription; later calls return the first call's result.
func (s *Stream[T]) Unsubscribe(ctx context.Context) error {
	s.once.Do(func() { s.unsubErr = s.unsubscribe(ctx) })
	return s.unsubErr
}

// Seq yields every message until the stream ends (not yielding ErrStreamDone) or fails
// (yielding the error once).
func (s *Stream[T]) Seq(ctx context.Context) iter.Seq2[T, error] {
	return func(yield func(T, error) bool) {
		for {
			msg, err := s.next(ctx)
			if errors.Is(err, ErrStreamDone) {
				return
			}
			if !yield(msg, err) || err != nil {
				return
			}
		}
	}
}

// MapStream is s with every message passed through f (a decode, typically); an error
// from f is returned by Next in place of that message.
func MapStream[A, B any](s *Stream[A], f func(A) (B, error)) *Stream[B] {
	return &Stream[B]{Reply: s.Reply, next: func(ctx context.Context) (B, error) {
		msg, err := s.next(ctx)
		if err != nil {
			var zero B
			return zero, err
		}
		return f(msg)
	}, unsubscribe: s.Unsubscribe}
}

// FilterStream is s without the messages keep rejects.
func FilterStream[T any](s *Stream[T], keep func(T) bool) *Stream[T] {
	return &Stream[T]{Reply: s.Reply, next: func(ctx context.Context) (T, error) {
		for {
			msg, err := s.next(ctx)
			if err != nil || keep(msg) {
				return msg, err
			}
		}
	}, unsubscribe: s.Unsubscribe}
}
