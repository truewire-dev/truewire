package core

import (
	"encoding/json"
	"strings"
	"time"
)

// DecodeResult decodes a core's reply, passing an error through: the one-line body of a
// generated typed method, `return truewire.DecodeResult[Repo](e.GetRaw(ctx, request))`.
func DecodeResult[T any](raw json.RawMessage, err error) (T, error) {
	if err != nil {
		var zero T
		return zero, err
	}
	return Decode[T](raw)
}

// Subscription is a stream whose subscribe acknowledgement is typed (ADR 0014): the
// embedded Stream's messages, and Reply decoded into the declared reply type.
type Subscription[T, R any] struct {
	*Stream[T]
	Reply R
}

// Subscribed decodes raw's messages with Decode[T] and its reply with Decode[R]: what a
// generated typed stream method returns when the stream declares a reply schema. A core
// hands the reply over as json.RawMessage (or []byte); nil leaves Reply its zero value.
func Subscribed[T, R any](raw *Stream[json.RawMessage], err error) (*Subscription[T, R], error) {
	if err != nil {
		return nil, err
	}
	out := &Subscription[T, R]{Stream: MapStream(raw, Decode[T])}
	var data []byte
	switch reply := raw.Reply.(type) {
	case json.RawMessage:
		data = reply
	case []byte:
		data = reply
	case nil:
	default:
		encoded, err := json.Marshal(reply)
		if err != nil {
			return nil, LogicError("cannot read the subscription reply").WithCause(err)
		}
		data = encoded
	}
	if data != nil {
		decoded, err := Decode[R](data)
		if err != nil {
			return nil, err
		}
		out.Reply = decoded
		out.Stream.Reply = decoded
	}
	return out, nil
}

// Streamed decodes raw's messages with Decode[T], passing an error through: the body of a
// generated typed stream method with no declared reply.
func Streamed[T any](raw *Stream[json.RawMessage], err error) (*Stream[T], error) {
	if err != nil {
		return nil, err
	}
	return MapStream(raw, Decode[T]), nil
}

// FillTemplate replaces every `{name}` in template with values[name] as plain text (a
// string as it is, anything else as its JSON).
func FillTemplate(template string, values map[string]any) string {
	for name, value := range values {
		encoded, err := json.Marshal(value)
		text := ""
		if err == nil {
			text = PlainText(encoded)
		}
		template = strings.ReplaceAll(template, "{"+name+"}", text)
	}
	return template
}

// WithSpan overrides a `seek` walk's declared span (its widest request range, in the unit
// the spec declares) for one `<Method>Paged` call.
func WithSpan(span int64) CallOption { return func(o *CallOptions) { o.Span = span } }

// FromEpoch is n (in this converter's unit) as a time.Time.
func (c EpochConverter) FromEpoch(n int64) time.Time {
	sec, rem := n/c.Unit, n%c.Unit
	if rem < 0 {
		sec, rem = sec-1, rem+c.Unit
	}
	return time.Unix(sec, rem*(1_000_000_000/c.Unit)).UTC()
}
