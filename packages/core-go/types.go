package core

import (
	"encoding/json"
	"strconv"
	"time"
)

// The scalar formats a spec narrows a wire value with (`docs/spec/authoring.md` rules 3,
// 12, 13 and 15). Each is a named type that validates on decode and writes the wire form
// back on encode. The timestamps embed time.Time, so `ts.Time` is the instant.

// TimestampSeconds is an `epoch-seconds` value. A value decoded from a numeral string
// (`"1786302600"`) is written back as one; a value built in code is written as a number.
type TimestampSeconds struct {
	time.Time
	quoted bool
}

// TimestampMillis is an `epoch-millis` value; see TimestampSeconds.
type TimestampMillis struct {
	time.Time
	quoted bool
}

// TimestampMicros is an `epoch-micros` value; see TimestampSeconds.
type TimestampMicros struct {
	time.Time
	quoted bool
}

// TimestampNanos is an `epoch-nanos` value; see TimestampSeconds.
type TimestampNanos struct {
	time.Time
	quoted bool
}

// TimestampIso is a `date-time` (RFC 3339) value.
type TimestampIso struct{ time.Time }

// DateIso is a `date` (RFC 3339 full-date) value: midnight UTC of that day.
type DateIso struct{ time.Time }

// IntegerString is an `integer-string` value: "42" on the wire. Like Python's `int` it has
// no size limit (a wei amount or an ERC-1155 token id exceeds int64), so it holds exactly
// the digits the API sent, validated on decode and encode; see integer.go.

// BooleanString is a `boolean-string` value: "true"/"false" on the wire.
type BooleanString bool

func epochJSON(data []byte, conv EpochConverter, what string) (time.Time, bool, error) {
	text := string(data)
	quoted := len(data) > 0 && data[0] == '"'
	if quoted {
		var s string
		if err := json.Unmarshal(data, &s); err != nil || !numberPattern.MatchString(s) {
			return time.Time{}, false, ValidationError("", "expected "+what+", got "+describeRaw(data))
		}
		text = s
	} else if !numberPattern.MatchString(text) {
		return time.Time{}, false, ValidationError("", "expected "+what+", got "+describeRaw(data))
	}
	t, err := conv.Parse(text)
	if err != nil {
		return time.Time{}, false, ValidationError("", "expected "+what+", got "+describeRaw(data))
	}
	return t, quoted, nil
}

func epochText(conv EpochConverter, t time.Time, quoted bool) []byte {
	text := conv.DumpText(t)
	if quoted {
		return []byte(`"` + text + `"`)
	}
	return []byte(text)
}

func (t TimestampSeconds) MarshalJSON() ([]byte, error) {
	return epochText(EpochSeconds, t.Time, t.quoted), nil
}

func (t *TimestampSeconds) UnmarshalJSON(data []byte) (err error) {
	t.Time, t.quoted, err = epochJSON(data, EpochSeconds, "epoch seconds")
	return err
}

func (t TimestampMillis) MarshalJSON() ([]byte, error) {
	return epochText(EpochMillis, t.Time, t.quoted), nil
}

func (t *TimestampMillis) UnmarshalJSON(data []byte) (err error) {
	t.Time, t.quoted, err = epochJSON(data, EpochMillis, "epoch milliseconds")
	return err
}

func (t TimestampMicros) MarshalJSON() ([]byte, error) {
	return epochText(EpochMicros, t.Time, t.quoted), nil
}

func (t *TimestampMicros) UnmarshalJSON(data []byte) (err error) {
	t.Time, t.quoted, err = epochJSON(data, EpochMicros, "epoch microseconds")
	return err
}

func (t TimestampNanos) MarshalJSON() ([]byte, error) {
	return epochText(EpochNanos, t.Time, t.quoted), nil
}

func (t *TimestampNanos) UnmarshalJSON(data []byte) (err error) {
	t.Time, t.quoted, err = epochJSON(data, EpochNanos, "epoch nanoseconds")
	return err
}

func (t TimestampIso) MarshalJSON() ([]byte, error) { return json.Marshal(DumpDateTime(t.Time)) }

func (t *TimestampIso) UnmarshalJSON(data []byte) error {
	var s string
	if err := json.Unmarshal(data, &s); err != nil {
		return ValidationError("", "expected RFC 3339 date-time, got "+describeRaw(data))
	}
	parsed, err := ParseDateTime(s)
	if err != nil {
		return ValidationError("", "expected RFC 3339 date-time, got "+describeRaw(data))
	}
	t.Time = parsed
	return nil
}

func (d DateIso) MarshalJSON() ([]byte, error) { return json.Marshal(d.Time.Format("2006-01-02")) }

func (d *DateIso) UnmarshalJSON(data []byte) error {
	var s string
	if err := json.Unmarshal(data, &s); err != nil {
		return ValidationError("", "expected RFC 3339 date, got "+describeRaw(data))
	}
	parsed, err := ParseDate(s)
	if err != nil {
		return ValidationError("", "expected RFC 3339 date, got "+describeRaw(data))
	}
	d.Time = parsed
	return nil
}

func (b BooleanString) MarshalJSON() ([]byte, error) {
	return json.Marshal(strconv.FormatBool(bool(b)))
}

func (b *BooleanString) UnmarshalJSON(data []byte) error {
	var s string
	if err := json.Unmarshal(data, &s); err != nil || (s != "true" && s != "false") {
		return ValidationError("", "expected boolean string, got "+describeRaw(data))
	}
	*b = s == "true"
	return nil
}
