package core

import (
	"encoding/json"
	"math/big"
	"strconv"
	"strings"
)

// IntegerString is an `integer-string` wire value: an optionally signed run of decimal
// digits of any length, serialized back verbatim. Int64 and BigInt read it as a number.
type IntegerString string

// IsIntegerString reports whether s is in integer-string form.
func IsIntegerString(s string) bool { return integerPattern.MatchString(s) }

// ParseIntegerString returns s as an IntegerString, or a logic error when it is not one.
func ParseIntegerString(s string) (IntegerString, error) {
	if !IsIntegerString(s) {
		return "", LogicError("Not an integer string: %q", s)
	}
	return IntegerString(s), nil
}

// IntegerStringOf is n as an IntegerString.
func IntegerStringOf(n int64) IntegerString { return IntegerString(strconv.FormatInt(n, 10)) }

// IntegerStringOfBig is n as an IntegerString.
func IntegerStringOfBig(n *big.Int) IntegerString { return IntegerString(n.String()) }

// String returns the digits.
func (i IntegerString) String() string { return string(i) }

// BigInt is the value as a new *big.Int, or nil when i is not an integer string.
func (i IntegerString) BigInt() *big.Int {
	if !IsIntegerString(string(i)) {
		return nil
	}
	n, ok := new(big.Int).SetString(strings.TrimPrefix(string(i), "+"), 10)
	if !ok {
		return nil
	}
	return n
}

// Int64 is the value as an int64; ok is false when it is out of range or not an integer
// string.
func (i IntegerString) Int64() (n int64, ok bool) {
	if !IsIntegerString(string(i)) {
		return 0, false
	}
	n, err := strconv.ParseInt(string(i), 10, 64)
	return n, err == nil
}

// Compare compares two integer strings exactly: -1, 0 or 1. "007" equals "7".
func (i IntegerString) Compare(other IntegerString) int {
	a, b := i.BigInt(), other.BigInt()
	if a == nil || b == nil {
		return strings.Compare(string(i), string(other))
	}
	return a.Cmp(b)
}

// Add is i + n, exactly.
func (i IntegerString) Add(n int64) IntegerString {
	v := i.BigInt()
	if v == nil {
		v = new(big.Int)
	}
	return IntegerStringOfBig(v.Add(v, big.NewInt(n)))
}

// MarshalJSON writes the digits as a JSON string.
func (i IntegerString) MarshalJSON() ([]byte, error) {
	if !IsIntegerString(string(i)) {
		return nil, ValidationError("", "expected integer string, got "+strconv.Quote(string(i)))
	}
	return json.Marshal(string(i))
}

// UnmarshalJSON reads a JSON string of decimal digits.
func (i *IntegerString) UnmarshalJSON(data []byte) error {
	var s string
	if err := json.Unmarshal(data, &s); err != nil || !IsIntegerString(s) {
		return ValidationError("", "expected integer string, got "+describeRaw(data))
	}
	*i = IntegerString(s)
	return nil
}
