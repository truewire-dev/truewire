package core

import (
	"encoding/json"
	"strings"

	truewire "truewire.dev/core"
)

var categoryKinds = map[string]truewire.Kind{
	"EAPI": truewire.KindAuth, "EAuth": truewire.KindAuth, "EAccount": truewire.KindAuth,
	"EGeneral": truewire.KindBadRequest, "ETrade": truewire.KindBadRequest, "EFunding": truewire.KindBadRequest,
}

var (
	rateLimitSubstrings  = []string{"Rate limit exceeded", "Too many requests", "Orders limit exceeded", "Domain rate limit exceeded", "Scheduled orders limit exceeded"}
	authSubstrings       = []string{"Invalid key", "Invalid signature", "Invalid nonce", "Permission denied", "Temporary lockout"}
	badRequestSubstrings = []string{"Invalid price", "Tick size check failed", "Order minimum not met", "Cost minimum not met"}
)

func containsAny(message string, needles []string) bool {
	for _, needle := range needles {
		if strings.Contains(message, needle) {
			return true
		}
	}
	return false
}

// RaiseError is the error a non-empty `error` array calls for; the first entry decides, as
// Kraken lists the primary failure first.
//
// See https://docs.kraken.com/api/docs/guides/global-errors.
func RaiseError(errors []string) error {
	message := "unknown error"
	if len(errors) > 0 {
		message = errors[0]
	}
	kind := truewire.KindAPI
	switch {
	case containsAny(message, rateLimitSubstrings):
		kind = truewire.KindRateLimited
	case containsAny(message, authSubstrings):
		kind = truewire.KindAuth
	case containsAny(message, badRequestSubstrings):
		kind = truewire.KindBadRequest
	default:
		if k, ok := categoryKinds[strings.SplitN(message, ":", 2)[0]]; ok {
			kind = k
		}
	}
	return truewire.APIError(kind, message).WithBody(errors)
}

// Unwrap is the envelope's `result`, or the error its `error` array or status calls for.
func Unwrap(response *truewire.Response) (json.RawMessage, error) {
	if response.Status >= 400 {
		kind := truewire.KindAPI
		switch {
		case response.Status == 401 || response.Status == 403:
			kind = truewire.KindAuth
		case response.Status == 429:
			kind = truewire.KindRateLimited
		case response.Status < 500:
			kind = truewire.KindBadRequest
		}
		text := response.Text()
		if len(text) > 200 {
			text = text[:200]
		}
		return nil, truewire.APIError(kind, "HTTP "+response.Text()[:0]+text).WithStatus(response.Status).WithBody(response.Decoded())
	}
	var envelope struct {
		Error  *[]string       `json:"error"`
		Result json.RawMessage `json:"result"`
	}
	if err := json.Unmarshal(response.Body, &envelope); err != nil {
		return nil, truewire.ValidationError("", "invalid JSON: "+err.Error())
	}
	if envelope.Error == nil {
		return nil, truewire.ValidationError("/error", "not a Kraken envelope: no `error` array")
	}
	if len(*envelope.Error) > 0 {
		return nil, RaiseError(*envelope.Error)
	}
	if envelope.Result == nil {
		return json.RawMessage("null"), nil
	}
	return envelope.Result, nil
}
