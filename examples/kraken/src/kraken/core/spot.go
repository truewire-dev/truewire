package core

import (
	"context"
	"encoding/json"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"time"

	truewire "truewire.dev/core"

	"truewire.dev/examples/kraken/src/kraken/meta"
)

// SpotAPIURL is Kraken's REST base URL.
const SpotAPIURL = "https://api.kraken.com"

// jsonBodyPaths are the private endpoints Kraken rejects a form body on; they are sent as JSON.
var jsonBodyPaths = map[string]bool{"/0/private/AddOrderBatch": true, "/0/private/CancelOrderBatch": true}

const wsTokenPath = "/0/private/GetWebSocketsToken"

// SpotOptions is what NewSpot takes.
type SpotOptions struct {
	// BaseURL defaults to SpotAPIURL; point it at `truewire mock` in tests.
	BaseURL string
	// Credentials sign private endpoints; without them only public endpoints can be called.
	Credentials *Credentials
	// HTTP is the client to send through; one is made when nil.
	HTTP *truewire.HttpClient
}

// Spot is the REST transport every `spot.*` endpoint calls: unsigned GETs for public
// endpoints, signed POSTs for private ones, the `{error, result}` envelope unwrapped.
type Spot struct {
	baseURL     string
	credentials *Credentials
	http        *truewire.HttpClient
	nonce       Nonce
}

// NewSpot returns the REST transport.
func NewSpot(options SpotOptions) *Spot {
	base := options.BaseURL
	if base == "" {
		base = SpotAPIURL
	}
	client := options.HTTP
	if client == nil {
		client = &truewire.HttpClient{}
	}
	return &Spot{baseURL: strings.TrimRight(base, "/"), credentials: options.Credentials, http: client}
}

// Request sends one call; the envelope's `result`.
func (s *Spot) Request(ctx context.Context, call truewire.HttpCall) (json.RawMessage, error) {
	values, err := truewire.Object(call.Request)
	if err != nil {
		return nil, err
	}
	signed := false
	if m, ok := call.Meta.(meta.SpotMeta); ok && m.Signed != nil {
		signed = *m.Signed
	}
	if signed {
		return s.signed(ctx, call.Path, values, call.Options.Timeout)
	}
	response, err := s.http.Request(ctx, "GET", s.baseURL+call.Path, truewire.RequestOptions{Query: formParams(values), Timeout: call.Options.Timeout})
	if err != nil {
		return nil, err
	}
	return Unwrap(response)
}

// WsToken fetches the WebSocket token the private socket sends with every request.
func (s *Spot) WsToken(ctx context.Context) (string, time.Duration, error) {
	raw, err := s.signed(ctx, wsTokenPath, map[string]json.RawMessage{}, 0)
	if err != nil {
		return "", 0, err
	}
	var token struct {
		Token   string `json:"token"`
		Expires int64  `json:"expires"`
	}
	if err := json.Unmarshal(raw, &token); err != nil || token.Token == "" {
		return "", 0, truewire.ValidationError("/token", "expected a WebSocket token")
	}
	return token.Token, time.Duration(token.Expires) * time.Second, nil
}

// RetrieveExport downloads a finished export report: the zip archive's bytes, which no
// JSON envelope wraps. It backs the hand-written `spot.account.RetrieveExport`.
func (s *Spot) RetrieveExport(ctx context.Context, id string) ([]byte, error) {
	values := map[string]json.RawMessage{}
	encoded, _ := json.Marshal(id)
	values["id"] = encoded
	response, err := s.send(ctx, "/0/private/RetrieveExport", values, 0)
	if err != nil {
		return nil, err
	}
	if strings.HasPrefix(strings.TrimSpace(response.Text()), "{") {
		if _, err := Unwrap(response); err != nil {
			return nil, err
		}
	}
	return response.Body, nil
}

// signed signs and sends a private POST. The body is encoded once, and that string is
// both what the signature covers and what is sent.
func (s *Spot) signed(ctx context.Context, path string, values map[string]json.RawMessage, timeout time.Duration) (json.RawMessage, error) {
	response, err := s.send(ctx, path, values, timeout)
	if err != nil {
		return nil, err
	}
	return Unwrap(response)
}

// send signs and sends a private POST and returns the reply as it came.
func (s *Spot) send(ctx context.Context, path string, values map[string]json.RawMessage, timeout time.Duration) (*truewire.Response, error) {
	if s.credentials == nil {
		return nil, truewire.APIError(truewire.KindAuth, "No credentials: this client can only call public endpoints.")
	}
	nonce := s.nonce.Next()
	header := http.Header{}
	var body string
	if jsonBodyPaths[path] {
		values["nonce"] = json.RawMessage(strconv.FormatInt(nonce, 10))
		encoded, err := json.Marshal(values)
		if err != nil {
			return nil, truewire.LogicError("cannot encode the request").WithCause(err)
		}
		body = string(encoded)
		header.Set("Content-Type", "application/json")
	} else {
		form := url.Values{}
		form.Set("nonce", strconv.FormatInt(nonce, 10))
		for _, p := range formParams(values) {
			form.Add(p.Key, p.Value)
		}
		body = form.Encode()
		header.Set("Content-Type", "application/x-www-form-urlencoded")
	}
	signature, err := Sign(path, nonce, body, s.credentials.PrivateKey)
	if err != nil {
		return nil, truewire.APIError(truewire.KindAuth, "the private key is not base64").WithCause(err)
	}
	header.Set("API-Key", s.credentials.APIKey)
	header.Set("API-Sign", signature)
	return s.http.Request(ctx, "POST", s.baseURL+path, truewire.RequestOptions{Body: []byte(body), Header: header, Timeout: timeout})
}

// formParams are query or form parameters from dumped request values: a scalar as its
// text, a nested value as its JSON, nulls skipped.
func formParams(values map[string]json.RawMessage) []truewire.Param {
	keys := make([]string, 0, len(values))
	for k := range values {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	out := make([]truewire.Param, 0, len(keys))
	for _, k := range keys {
		raw := strings.TrimSpace(string(values[k]))
		if raw == "null" {
			continue
		}
		out = append(out, truewire.Param{Key: k, Value: truewire.PlainText(values[k])})
	}
	return out
}
