package core

import (
	"bytes"
	"encoding/json"
	"errors"
	"math/big"
	"reflect"
	"regexp"
	"strconv"
	"strings"
)

// Validation: the runtime half of a generated type.
//
// A generated record is a struct whose MarshalJSON/UnmarshalJSON call EncodeObject and
// DecodeObject with one field descriptor per property (Required, RequiredNullable,
// Optional, OptionalNullable). Decoding checks that required keys are present and
// non-nullable values are not null, keeps undocumented keys in the record's Extra map, and
// locates every failure by a JSON pointer. A literal is a named string type decoded by
// DecodeLiteral, a union a struct of variant pointers decoded by DecodeUnion (variants
// tried in order), a tuple a struct of positions decoded by DecodeTuple.
//
// Decode and Dump are the two ends of a call: Dump turns a typed request into the wire
// bytes a core sends, Decode turns the wire bytes a core returns into the typed response.
// A generated `<Method>Raw` is Decode never called.

var numberPattern = regexp.MustCompile(`^-?\d+(\.\d+)?([eE][+-]?\d+)?$`)

// Decode parses wire JSON into T; any mismatch is a validation error at its JSON pointer.
func Decode[T any](data json.RawMessage) (T, error) {
	var out T
	if !json.Valid(data) {
		err := json.Unmarshal(data, &out)
		return out, &Error{Kind: KindValidation, Message: "invalid JSON", Issues: []Issue{{Path: "", Message: "invalid JSON"}}, Cause: err}
	}
	if err := decodeValue(data, reflect.ValueOf(&out).Elem()); err != nil {
		var syntax *json.SyntaxError
		if errors.As(err, &syntax) {
			return out, &Error{Kind: KindValidation, Message: "invalid JSON: " + syntax.Error(), Issues: []Issue{{Path: "", Message: "invalid JSON"}}, Cause: err}
		}
		return out, prefixed("", err)
	}
	return out, nil
}

// Dump renders a typed value as its wire JSON.
func Dump(value any) (json.RawMessage, error) {
	out, err := json.Marshal(value)
	if err != nil {
		var e *Error
		if errors.As(err, &e) {
			return nil, e
		}
		return nil, &Error{Kind: KindValidation, Message: "cannot dump value: " + err.Error(), Cause: err}
	}
	return out, nil
}

// DumpWith dumps an object value and sets fixed keys on it: wire plumbing (a required
// single-value enum) the generated method fills in for the caller.
func DumpWith(value any, fixed map[string]any) (json.RawMessage, error) {
	raw, err := Dump(value)
	if err != nil {
		return nil, err
	}
	var object map[string]json.RawMessage
	if err := json.Unmarshal(raw, &object); err != nil {
		return nil, LogicError("fixed keys need an object request").WithCause(err)
	}
	for key, v := range fixed {
		encoded, err := json.Marshal(v)
		if err != nil {
			return nil, LogicError("cannot dump fixed key %q", key).WithCause(err)
		}
		object[key] = encoded
	}
	return json.Marshal(object)
}

// Ptr returns a pointer to v: the way to fill an optional field inline.
func Ptr[T any](v T) *T { return &v }

// Optional is a field both optional and nullable: absent (Set false), null (Set true,
// Value the zero value of a nil-able T), or a value.
type Optional[T any] struct {
	Set   bool
	Value T
}

// Some returns a set Optional.
func Some[T any](v T) Optional[T] { return Optional[T]{Set: true, Value: v} }

// IsZero reports whether the field is absent, for `omitzero`.
func (o Optional[T]) IsZero() bool { return !o.Set }

// MarshalJSON writes Value (only meaningful when Set).
func (o Optional[T]) MarshalJSON() ([]byte, error) { return json.Marshal(o.Value) }

// UnmarshalJSON sets Value and marks the field present.
func (o *Optional[T]) UnmarshalJSON(data []byte) error {
	o.Set = true
	return json.Unmarshal(data, &o.Value)
}

// Field describes one property of a record for EncodeObject and DecodeObject.
type Field struct {
	Key      string
	required bool
	nullable bool
	decode   func([]byte) error
	encode   func() (json.RawMessage, bool, error)
}

func isNull(data []byte) bool { return string(bytes.TrimSpace(data)) == "null" }

func isNilValue(v any) bool {
	if v == nil {
		return true
	}
	rv := reflect.ValueOf(v)
	switch rv.Kind() {
	case reflect.Pointer, reflect.Slice, reflect.Map, reflect.Interface:
		return rv.IsNil()
	}
	return false
}

func field[T any](key string, p *T, required, nullable bool, present func(*T) bool) Field {
	return Field{
		Key: key, required: required, nullable: nullable,
		decode: func(data []byte) error { return decodeValue(data, reflect.ValueOf(p).Elem()) },
		encode: func() (json.RawMessage, bool, error) {
			if !present(p) {
				return nil, false, nil
			}
			out, err := json.Marshal(*p)
			return out, true, err
		},
	}
}

// Required is a required, non-nullable property.
func Required[T any](key string, p *T) Field {
	return field(key, p, true, false, func(*T) bool { return true })
}

// RequiredNullable is a required property that may be null; the field is a nil-able type.
func RequiredNullable[T any](key string, p *T) Field {
	return field(key, p, true, true, func(*T) bool { return true })
}

// Optional is an optional, non-nullable property; the field is a nil-able type, nil when absent.
func OptionalField[T any](key string, p *T) Field {
	return field(key, p, false, false, func(p *T) bool { return !isNilValue(*p) })
}

// OptionalNullable is a property both optional and nullable.
func OptionalNullable[T any](key string, p *Optional[T]) Field {
	f := field(key, p, false, true, func(p *Optional[T]) bool { return p.Set })
	return f
}

// DecodeObject decodes a JSON object into fields, keeping every key no field names in
// extra (which may be nil to drop them).
func DecodeObject(data []byte, extra *map[string]json.RawMessage, fields ...Field) error {
	var object map[string]json.RawMessage
	if len(bytes.TrimSpace(data)) == 0 || bytes.TrimSpace(data)[0] != '{' {
		return ValidationError("", "expected object, got "+describeRaw(data))
	}
	if err := json.Unmarshal(data, &object); err != nil {
		return ValidationError("", "expected object, got "+describeRaw(data))
	}
	var issues []Issue
	var cause error
	for _, f := range fields {
		at := "/" + escapeKey(f.Key)
		raw, ok := object[f.Key]
		if !ok {
			if f.required {
				issues = append(issues, Issue{Path: at, Message: "missing required key"})
			}
			continue
		}
		delete(object, f.Key)
		if isNull(raw) && !f.nullable {
			issues = append(issues, Issue{Path: at, Message: "expected a value, got null"})
			continue
		}
		if err := f.decode(raw); err != nil {
			var e *Error
			if errors.As(prefixed(at, err), &e) {
				issues = append(issues, e.Issues...)
				if cause == nil {
					cause = e.Cause
				}
			}
		}
	}
	if extra != nil {
		if len(object) > 0 {
			*extra = object
		} else {
			*extra = nil
		}
	}
	if len(issues) > 0 {
		return &Error{Kind: KindValidation, Message: issues[0].Message + " at " + pointerOrRoot(issues[0].Path), Issues: issues, Cause: cause}
	}
	return nil
}

// EncodeObject encodes fields (absent optional ones left out) and then extra's keys a
// field did not already write, as one JSON object with keys in declaration order.
func EncodeObject(extra map[string]json.RawMessage, fields ...Field) ([]byte, error) {
	var buf bytes.Buffer
	buf.WriteByte('{')
	written := make(map[string]bool, len(fields))
	first := true
	put := func(key string, value []byte) {
		if !first {
			buf.WriteByte(',')
		}
		first = false
		k, _ := json.Marshal(key)
		buf.Write(k)
		buf.WriteByte(':')
		buf.Write(value)
	}
	for _, f := range fields {
		value, present, err := f.encode()
		if err != nil {
			return nil, prefixed("/"+escapeKey(f.Key), err)
		}
		if !present {
			continue
		}
		written[f.Key] = true
		put(f.Key, value)
	}
	keys := make([]string, 0, len(extra))
	for key := range extra {
		if !written[key] {
			keys = append(keys, key)
		}
	}
	sortStrings(keys)
	for _, key := range keys {
		put(key, extra[key])
	}
	buf.WriteByte('}')
	return buf.Bytes(), nil
}

// DecodeLiteral decodes a JSON string that must be one of values.
func DecodeLiteral[T ~string](data []byte, p *T, values ...T) error {
	var s string
	if err := json.Unmarshal(data, &s); err == nil {
		for _, v := range values {
			if string(v) == s {
				*p = v
				return nil
			}
		}
	}
	quoted := make([]string, len(values))
	for i, v := range values {
		quoted[i] = strconv.Quote(string(v))
	}
	return ValidationError("", "expected "+strings.Join(quoted, " | ")+", got "+describeRaw(data))
}

// EncodeLiteral encodes a string literal, checking it is one of values.
func EncodeLiteral[T ~string](v T, values ...T) ([]byte, error) {
	for _, allowed := range values {
		if allowed == v {
			return json.Marshal(string(v))
		}
	}
	return nil, ValidationError("", "cannot dump "+strconv.Quote(string(v))+": not one of the literal's values")
}

// Variant is one member of a union for DecodeUnion and EncodeUnion.
type Variant struct {
	try    func([]byte) error
	reset  func()
	encode func() (json.RawMessage, bool, error)
}

// VariantOf is the union member held in *p, nil unless it is the one that matched.
func VariantOf[T any](p **T) Variant {
	return Variant{
		try: func(data []byte) error {
			v := new(T)
			if err := decodeValue(data, reflect.ValueOf(v).Elem()); err != nil {
				return err
			}
			*p = v
			return nil
		},
		reset: func() { *p = nil },
		encode: func() (json.RawMessage, bool, error) {
			if *p == nil {
				return nil, false, nil
			}
			out, err := json.Marshal(**p)
			return out, true, err
		},
	}
}

// DecodeUnion tries each variant in order; the first that decodes wins and the others are nil.
func DecodeUnion(data []byte, variants ...Variant) error {
	for _, v := range variants {
		v.reset()
	}
	issues := []Issue{{Path: "", Message: "no variant matched"}}
	for _, v := range variants {
		err := v.try(data)
		if err == nil {
			return nil
		}
		var e *Error
		if errors.As(prefixed("", err), &e) {
			issues = append(issues, e.Issues...)
		}
		v.reset()
	}
	return &Error{Kind: KindValidation, Message: "no variant matched " + describeRaw(data) + " at /", Issues: issues}
}

// EncodeUnion encodes the one variant that is set.
func EncodeUnion(variants ...Variant) ([]byte, error) {
	for _, v := range variants {
		out, ok, err := v.encode()
		if ok || err != nil {
			return out, err
		}
	}
	return nil, ValidationError("", "cannot dump a union with no variant set")
}

// Item is one position of a tuple for DecodeTuple and EncodeTuple.
type Item struct {
	decode func([]byte) error
	encode func() ([]byte, error)
}

// ItemOf is the tuple position held in *p.
func ItemOf[T any](p *T) Item {
	return Item{
		decode: func(data []byte) error { return decodeValue(data, reflect.ValueOf(p).Elem()) },
		encode: func() ([]byte, error) { return json.Marshal(*p) },
	}
}

// DecodeTuple decodes a fixed-length JSON array, position i into items[i].
func DecodeTuple(data []byte, items ...Item) error {
	var array []json.RawMessage
	if err := json.Unmarshal(data, &array); err != nil || isNull(data) {
		return ValidationError("", "expected array, got "+describeRaw(data))
	}
	if len(array) != len(items) {
		return ValidationError("", "expected "+strconv.Itoa(len(items))+" items, got "+strconv.Itoa(len(array)))
	}
	for i, item := range items {
		if err := item.decode(array[i]); err != nil {
			return prefixed("/"+strconv.Itoa(i), err)
		}
	}
	return nil
}

// EncodeTuple encodes the positions as a JSON array.
func EncodeTuple(items ...Item) ([]byte, error) {
	parts := make([]json.RawMessage, len(items))
	for i, item := range items {
		out, err := item.encode()
		if err != nil {
			return nil, prefixed("/"+strconv.Itoa(i), err)
		}
		parts[i] = out
	}
	return json.Marshal(parts)
}

func describeRaw(data []byte) string {
	text := string(bytes.TrimSpace(data))
	switch {
	case text == "":
		return "nothing"
	case text == "null" || text == "true" || text == "false":
		return text
	case text[0] == '{':
		return "object"
	case text[0] == '[':
		return "array"
	case text[0] == '"':
		if len(text) > 42 {
			return text[:41] + "…\""
		}
		return text
	}
	return text
}

// describeDecodeError turns an encoding/json error into an issue message.
func describeDecodeError(err error) string {
	var typeErr *json.UnmarshalTypeError
	if errors.As(err, &typeErr) {
		return "expected " + goKind(typeErr.Type) + ", got " + typeErr.Value
	}
	return err.Error()
}

func goKind(t reflect.Type) string {
	if t == nil {
		return "value"
	}
	switch t.Kind() {
	case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64,
		reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64:
		return "integer"
	case reflect.Float32, reflect.Float64:
		return "number"
	case reflect.String:
		return "string"
	case reflect.Bool:
		return "boolean"
	case reflect.Slice, reflect.Array:
		return "array"
	case reflect.Map, reflect.Struct:
		return "object"
	}
	return t.String()
}

func sortStrings(s []string) {
	for i := 1; i < len(s); i++ {
		for j := i; j > 0 && s[j] < s[j-1]; j-- {
			s[j], s[j-1] = s[j-1], s[j]
		}
	}
}

var rawMessageType = reflect.TypeOf(json.RawMessage(nil))

// decodeValue unmarshals data into target, walking slices and string-keyed maps itself so
// a failure inside an element is located by its index or key.
func decodeValue(data []byte, target reflect.Value) error {
	t := target.Type()
	trimmed := bytes.TrimSpace(data)
	switch {
	case t.Kind() == reflect.Slice && t != rawMessageType && t.Elem().Kind() != reflect.Uint8 && len(trimmed) > 0 && trimmed[0] == '[':
		var items []json.RawMessage
		if err := json.Unmarshal(trimmed, &items); err != nil {
			return ValidationError("", "expected array, got "+describeRaw(data))
		}
		out := reflect.MakeSlice(t, len(items), len(items))
		for i, item := range items {
			if err := decodeValue(item, out.Index(i)); err != nil {
				return prefixed("/"+strconv.Itoa(i), err)
			}
		}
		target.Set(out)
		return nil
	case t.Kind() == reflect.Map && t.Key().Kind() == reflect.String && len(trimmed) > 0 && trimmed[0] == '{':
		var entries map[string]json.RawMessage
		if err := json.Unmarshal(trimmed, &entries); err != nil {
			return ValidationError("", "expected object, got "+describeRaw(data))
		}
		out := reflect.MakeMapWithSize(t, len(entries))
		keys := make([]string, 0, len(entries))
		for k := range entries {
			keys = append(keys, k)
		}
		sortStrings(keys)
		for _, k := range keys {
			v := reflect.New(t.Elem()).Elem()
			if err := decodeValue(entries[k], v); err != nil {
				return prefixed("/"+escapeKey(k), err)
			}
			out.SetMapIndex(reflect.ValueOf(k).Convert(t.Key()), v)
		}
		target.Set(out)
		return nil
	case t.Kind() == reflect.Pointer && !isNull(trimmed):
		v := reflect.New(t.Elem())
		if err := decodeValue(data, v.Elem()); err != nil {
			return err
		}
		target.Set(v)
		return nil
	}
	if isIntegerKind(t.Kind()) && isFractionalNumeral(trimmed) {
		// JSON has one number type: `25.0` is the integer 25 (JSON Schema's `integer` is any
		// number with a zero fractional part), which encoding/json refuses for an int field.
		if n, ok := integralValue(trimmed); ok && !target.OverflowInt(n) {
			target.SetInt(n)
			return nil
		}
		return ValidationError("", "expected integer, got number "+describeRaw(data))
	}
	if err := json.Unmarshal(data, target.Addr().Interface()); err != nil {
		return prefixed("", err)
	}
	return nil
}

func isIntegerKind(k reflect.Kind) bool {
	switch k {
	case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
		return true
	}
	return false
}

// isFractionalNumeral reports whether data is a JSON number written with a fraction or an
// exponent (`25.0`, `1e2`), the spellings encoding/json refuses for an integer field.
func isFractionalNumeral(data []byte) bool {
	if len(data) == 0 || (data[0] != '-' && (data[0] < '0' || data[0] > '9')) {
		return false
	}
	return bytes.ContainsAny(data, ".eE")
}

// integralValue is the value of a numeral with a zero fractional part, when it fits an int64.
func integralValue(data []byte) (int64, bool) {
	f, _, err := big.ParseFloat(string(data), 10, 256, big.ToNearestEven)
	if err != nil || !f.IsInt() {
		return 0, false
	}
	n, accuracy := f.Int64()
	return n, accuracy == big.Exact
}
