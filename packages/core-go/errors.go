// Package core is the Go runtime of a truewire-generated client: the Go half of
// `truewire_core` (Python), `@truewire/core` (TypeScript) and `truewire-core` (Rust).
//
// Generated code imports it as `truewire "truewire.dev/core"`. It holds the error
// taxonomy, the scalar wire formats (decimal strings, epoch and RFC 3339 timestamps),
// validation (JSON decoding with every failure located by a JSON pointer), the HTTP
// transport with exchange recording, resumable pagination, and the contract between a
// hand-written core and the generated endpoints. WebSocket clients live in `core/ws`,
// test helpers (mock spawn, example replay) in `core/twtest`.
package core

import (
	"errors"
	"fmt"
	"strings"
)

// Kind classifies an Error, the Go form of the other runtimes' exception classes.
type Kind string

const (
	// KindError is the base kind: any truewire error.
	KindError Kind = "error"
	// KindNetwork is a failure reaching the server.
	KindNetwork Kind = "network"
	// KindValidation is a value that does not have the shape the spec declares.
	KindValidation Kind = "validation"
	// KindAPI is an error the API returned.
	KindAPI Kind = "api"
	// KindBadRequest is an API error for an invalid request.
	KindBadRequest Kind = "bad-request"
	// KindAuth is an API error for missing or invalid credentials.
	KindAuth Kind = "auth"
	// KindRateLimited is an API error for an exceeded rate limit.
	KindRateLimited Kind = "rate-limited"
	// KindLogic is a broken assumption or a bug on the client side.
	KindLogic Kind = "logic"
)

// parent is the kind a kind specialises; `errors.Is(err, ErrAPI)` holds for every API kind.
func (k Kind) parent() Kind {
	switch k {
	case KindBadRequest, KindAuth, KindRateLimited:
		return KindAPI
	case KindError:
		return ""
	default:
		return KindError
	}
}

// Issue is one failed check inside a value, located by a JSON pointer.
type Issue struct {
	// Path is a JSON pointer to the offending value, "" for the root.
	Path    string
	Message string
}

// Error is every error the runtime and generated code return.
//
// Match a kind with `errors.Is(err, core.ErrAuth)`; API kinds also match `ErrAPI`, and
// every kind matches `ErrTruewire`. Read the details with `errors.As(err, &e)`.
type Error struct {
	Kind    Kind
	Message string
	// Status is the HTTP status of the reply that carried an API error, 0 when none did.
	Status int
	// Body is the decoded body of the reply that carried an API error, when there was one.
	Body any
	// Issues lists every failed check of a validation error, in order.
	Issues []Issue
	// Cause is the underlying error, if any.
	Cause error
}

// Sentinel errors to match kinds with `errors.Is`.
var (
	ErrTruewire    = &Error{Kind: KindError}
	ErrNetwork     = &Error{Kind: KindNetwork}
	ErrValidation  = &Error{Kind: KindValidation}
	ErrAPI         = &Error{Kind: KindAPI}
	ErrBadRequest  = &Error{Kind: KindBadRequest}
	ErrAuth        = &Error{Kind: KindAuth}
	ErrRateLimited = &Error{Kind: KindRateLimited}
	ErrLogic       = &Error{Kind: KindLogic}
)

func (e *Error) Error() string {
	msg := e.Message
	if msg == "" {
		msg = string(e.Kind)
	}
	if e.Cause != nil {
		return msg + ": " + e.Cause.Error()
	}
	return msg
}

// Unwrap returns the cause.
func (e *Error) Unwrap() error { return e.Cause }

// Is reports whether target is a sentinel of this error's kind or of a kind it specialises.
func (e *Error) Is(target error) bool {
	t, ok := target.(*Error)
	if !ok || t.Message != "" || t.Cause != nil {
		return false
	}
	for k := e.Kind; k != ""; k = k.parent() {
		if k == t.Kind {
			return true
		}
	}
	return false
}

// Path is the JSON pointer of the first issue of a validation error, "" otherwise.
func (e *Error) Path() string {
	if len(e.Issues) == 0 {
		return ""
	}
	return e.Issues[0].Path
}

// WithStatus sets Status and returns the error.
func (e *Error) WithStatus(status int) *Error { e.Status = status; return e }

// WithBody sets Body and returns the error.
func (e *Error) WithBody(body any) *Error { e.Body = body; return e }

// WithCause sets Cause and returns the error.
func (e *Error) WithCause(cause error) *Error { e.Cause = cause; return e }

// NewError builds an error of kind with a formatted message.
func NewError(kind Kind, format string, args ...any) *Error {
	return &Error{Kind: kind, Message: fmt.Sprintf(format, args...)}
}

// NetworkError is a failure reaching the server.
func NetworkError(message string, cause error) *Error {
	return &Error{Kind: KindNetwork, Message: message, Cause: cause}
}

// LogicError is a broken client-side assumption.
func LogicError(format string, args ...any) *Error { return NewError(KindLogic, format, args...) }

// APIError is an error the API returned, of kind (KindAPI or one beneath it).
func APIError(kind Kind, message string) *Error { return &Error{Kind: kind, Message: message} }

// ValidationError is a failed check at a JSON pointer.
func ValidationError(path, message string) *Error {
	return &Error{Kind: KindValidation, Message: message + " at " + pointerOrRoot(path), Issues: []Issue{{Path: path, Message: message}}}
}

func pointerOrRoot(path string) string {
	if path == "" {
		return "/"
	}
	return path
}

// escapeKey escapes one JSON pointer reference token.
func escapeKey(key string) string {
	return strings.ReplaceAll(strings.ReplaceAll(key, "~", "~0"), "/", "~1")
}

// prefixed returns err with every issue located under prefix (a JSON pointer).
func prefixed(prefix string, err error) error {
	var e *Error
	if !errors.As(err, &e) || e.Kind != KindValidation {
		return ValidationError(prefix, describeDecodeError(err))
	}
	issues := make([]Issue, len(e.Issues))
	for i, issue := range e.Issues {
		issues[i] = Issue{Path: prefix + issue.Path, Message: issue.Message}
	}
	if len(issues) == 0 {
		issues = []Issue{{Path: prefix, Message: e.Message}}
	}
	return &Error{Kind: KindValidation, Message: issues[0].Message + " at " + pointerOrRoot(issues[0].Path), Issues: issues, Cause: e.Cause}
}
