package core

import (
	"encoding/json"
	"regexp"
	"strconv"
	"strings"
)

var decimalPattern = regexp.MustCompile(`^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$`)

// Decimal is a `decimal-string` wire value: exactly the digits the API sent, no rounding,
// serialized back verbatim. Hand it to a decimal library for arithmetic.
type Decimal string

// IsDecimal reports whether s is in decimal-string form.
func IsDecimal(s string) bool { return decimalPattern.MatchString(s) }

// ParseDecimal returns s as a Decimal, or a logic error when it is not one.
func ParseDecimal(s string) (Decimal, error) {
	if !IsDecimal(s) {
		return "", LogicError("Not a decimal string: %q", s)
	}
	return Decimal(s), nil
}

// String returns the digits.
func (d Decimal) String() string { return string(d) }

// Float64 is the nearest float64; digits beyond double precision are lost.
func (d Decimal) Float64() float64 {
	f, _ := strconv.ParseFloat(string(d), 64)
	return f
}

// MarshalJSON writes the digits as a JSON string.
func (d Decimal) MarshalJSON() ([]byte, error) {
	if !IsDecimal(string(d)) {
		return nil, ValidationError("", "expected decimal string, got "+strconv.Quote(string(d)))
	}
	return json.Marshal(string(d))
}

// UnmarshalJSON reads a JSON string in decimal form.
func (d *Decimal) UnmarshalJSON(data []byte) error {
	var s string
	if err := json.Unmarshal(data, &s); err != nil || !IsDecimal(s) {
		return ValidationError("", "expected decimal string, got "+describeRaw(data))
	}
	*d = Decimal(s)
	return nil
}

func splitDecimal(d string) (sign int, intPart, frac string) {
	sign = 1
	s := d
	if strings.HasPrefix(s, "-") {
		sign, s = -1, s[1:]
	} else if strings.HasPrefix(s, "+") {
		s = s[1:]
	}
	exp := 0
	if e := strings.IndexAny(s, "eE"); e >= 0 {
		exp, _ = strconv.Atoi(s[e+1:])
		s = s[:e]
	}
	intPart, frac = s, ""
	if dot := strings.IndexByte(s, '.'); dot >= 0 {
		intPart, frac = s[:dot], s[dot+1:]
	}
	if exp > 0 {
		move := frac
		if len(move) > exp {
			move = frac[:exp]
		}
		intPart += move + strings.Repeat("0", exp-len(move))
		frac = frac[len(move):]
	} else if exp < 0 {
		n := -exp
		move := intPart
		if len(move) > n {
			move = intPart[len(intPart)-n:]
		}
		frac = strings.Repeat("0", n-len(move)) + move + frac
		intPart = intPart[:len(intPart)-len(move)]
	}
	intPart = strings.TrimLeft(intPart, "0")
	frac = strings.TrimRight(frac, "0")
	if intPart == "" && frac == "" {
		sign = 1
	}
	return sign, intPart, frac
}

// Compare compares two decimals exactly: -1, 0 or 1. "1.50" equals "1.5".
func (d Decimal) Compare(other Decimal) int {
	sa, ia, fa := splitDecimal(string(d))
	sb, ib, fb := splitDecimal(string(other))
	if sa != sb {
		if sa < sb {
			return -1
		}
		return 1
	}
	cmp := 0
	switch {
	case len(ia) != len(ib):
		cmp = map[bool]int{true: -1, false: 1}[len(ia) < len(ib)]
	case ia != ib:
		cmp = strings.Compare(ia, ib)
	default:
		n := max(len(fa), len(fb))
		pa := fa + strings.Repeat("0", n-len(fa))
		pb := fb + strings.Repeat("0", n-len(fb))
		cmp = strings.Compare(pa, pb)
	}
	return cmp * sa
}
