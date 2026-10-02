package core

import (
	"math/big"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// EpochConverter converts between epoch timestamps in one unit and time.Time.
type EpochConverter struct {
	// Unit is the number of units per second: 1 for seconds, 1000 for milliseconds, ...
	Unit int64
}

// Converters for each epoch unit the spec formats name.
var (
	EpochSeconds = EpochConverter{Unit: 1}
	EpochMillis  = EpochConverter{Unit: 1_000}
	EpochMicros  = EpochConverter{Unit: 1_000_000}
	EpochNanos   = EpochConverter{Unit: 1_000_000_000}
)

var integerPattern = regexp.MustCompile(`^[+-]?\d+$`)

// Parse reads an epoch timestamp given as a JSON number or a numeral string, exactly: a
// decimal fraction (`1763410056.903966` seconds) keeps every digit down to the nanosecond.
func (c EpochConverter) Parse(text string) (time.Time, error) {
	text = strings.TrimSpace(text)
	unit := big.NewInt(c.Unit)
	scaled, ok := decimalToNanoUnits(text, c.Unit)
	if !ok {
		f, err := strconv.ParseFloat(text, 64)
		if err != nil || f != f {
			return time.Time{}, LogicError("Not an epoch timestamp: %q", text)
		}
		n, _ := new(big.Float).Mul(big.NewFloat(f), big.NewFloat(1_000_000_000)).Int(nil)
		scaled = n
	}
	// scaled is the timestamp in units of 1e-9 of this converter's unit.
	perSecond := new(big.Int).Mul(unit, big.NewInt(1_000_000_000))
	sec, rem := new(big.Int), new(big.Int)
	sec.DivMod(scaled, perSecond, rem)
	if !sec.IsInt64() {
		return time.Time{}, LogicError("Epoch timestamp out of range: %q", text)
	}
	nanos := new(big.Int).Quo(rem, unit)
	return time.Unix(sec.Int64(), nanos.Int64()).UTC(), nil
}

// decimalToNanoUnits reads a plain decimal numeral as an integer count of 1e-9 units.
func decimalToNanoUnits(text string, unit int64) (*big.Int, bool) {
	if !numberPattern.MatchString(text) || strings.ContainsAny(text, "eE") {
		if !integerPattern.MatchString(text) {
			return nil, false
		}
	}
	whole, frac, _ := strings.Cut(text, ".")
	negative := strings.HasPrefix(whole, "-")
	whole = strings.TrimLeft(whole, "+-")
	if len(frac) > 9 {
		frac = frac[:9]
	}
	frac += strings.Repeat("0", 9-len(frac))
	n, ok := new(big.Int).SetString(whole+frac, 10)
	if !ok {
		return nil, false
	}
	if negative {
		n.Neg(n)
	}
	return n, true
}

// Dump returns t in this unit, floored.
func (c EpochConverter) Dump(t time.Time) int64 {
	sec := big.NewInt(t.Unix())
	out := new(big.Int).Mul(sec, big.NewInt(c.Unit))
	frac := new(big.Int).Mul(big.NewInt(int64(t.Nanosecond())), big.NewInt(c.Unit))
	frac.Quo(frac, big.NewInt(1_000_000_000))
	return out.Add(out, frac).Int64()
}

// DumpText returns t in this unit as a JSON numeral: an integer when t falls on a whole
// unit, else a decimal with the fraction digits it needs (none dropped).
func (c EpochConverter) DumpText(t time.Time) string {
	nanosPerUnit := int64(1_000_000_000) / c.Unit
	whole := c.Dump(t)
	remainder := int64(t.Nanosecond()) % nanosPerUnit
	if remainder == 0 {
		return strconv.FormatInt(whole, 10)
	}
	digits := len(strconv.FormatInt(nanosPerUnit, 10)) - 1
	frac := strings.TrimRight(leftPad(int(remainder), digits), "0")
	if whole < 0 {
		// Dump floors; a negative instant with a fraction is whole + remainder/unit.
		return strconv.FormatInt(whole, 10) + "." + frac
	}
	return strconv.FormatInt(whole, 10) + "." + frac
}

// Now is the current time in this unit.
func (c EpochConverter) Now() int64 { return c.Dump(time.Now()) }

var dateTimePattern = regexp.MustCompile(`^(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2}):(\d{2})(\.\d+)?([Zz]|[+-]\d{2}:\d{2})?$`)

// ParseDateTime reads an RFC 3339 date-time with a fraction of any length and a `Z` or offset.
// A date-time with no offset at all (Hyperliquid's `1970-01-01T00:00:00`) is read as UTC, as
// Python's converter reads it.
func ParseDateTime(text string) (time.Time, error) {
	match := dateTimePattern.FindStringSubmatch(text)
	if match == nil {
		return time.Time{}, LogicError("Not an RFC 3339 date-time: %q", text)
	}
	normalized := strings.Replace(strings.Replace(text, " ", "T", 1), "t", "T", 1)
	normalized = strings.Replace(normalized, "z", "Z", 1)
	if match[8] == "" {
		normalized += "Z"
	}
	t, err := time.Parse(time.RFC3339Nano, normalized)
	if err != nil {
		return time.Time{}, LogicError("Not an RFC 3339 date-time: %q", text).WithCause(err)
	}
	return t.UTC(), nil
}

// DumpDateTime renders t as UTC RFC 3339, `Z`-suffixed, with the fraction digits it needs
// in groups of three (milliseconds, microseconds or nanoseconds), none when it is zero.
func DumpDateTime(t time.Time) string {
	t = t.UTC()
	base := t.Format("2006-01-02T15:04:05")
	ns := t.Nanosecond()
	switch {
	case ns == 0:
		return base + "Z"
	case ns%1_000_000 == 0:
		return base + "." + leftPad(ns/1_000_000, 3) + "Z"
	case ns%1_000 == 0:
		return base + "." + leftPad(ns/1_000, 6) + "Z"
	default:
		return base + "." + leftPad(ns, 9) + "Z"
	}
}

func leftPad(n, width int) string {
	s := strconv.Itoa(n)
	return strings.Repeat("0", width-len(s)) + s
}

// ParseDate reads an RFC 3339 full-date (`YYYY-MM-DD`), checking it is a calendar date.
func ParseDate(text string) (time.Time, error) {
	t, err := time.Parse("2006-01-02", text)
	if err != nil || len(text) != 10 {
		return time.Time{}, LogicError("Not a calendar date: %q", text)
	}
	return t, nil
}
