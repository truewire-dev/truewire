// Package core is the hand-written core of the github client: transport, auth and errors.
//
// Every generated endpoint holds this core behind `truewire.HttpEndpoint` and calls
// Request on it; this is the one place that knows how to reach the upstream API. Adapt it
// (base URL, headers, signing, envelope unwrapping, error mapping) to your API; the
// generated code never changes when you do.
package core

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"

	truewire "truewire.dev/core"

	"truewire.dev/examples/github/src/github/meta"
)

// DefaultBaseURL is api.github.com.
const DefaultBaseURL = "https://api.github.com"

// Options is what New takes.
type Options struct {
	// BaseURL defaults to DefaultBaseURL; point it at `truewire mock` in tests.
	BaseURL string
	// Token is sent as `Authorization: Bearer` (public endpoints accept it too, and it
	// raises the rate limit).
	Token string
	// HTTP is the client to send through; one is made when nil.
	HTTP *truewire.HttpClient
}

// Core is the shared HTTP transport: base URL, GitHub's headers, the optional token and
// error mapping.
type Core struct {
	baseURL string
	token   string
	http    *truewire.HttpClient
}

// New returns a core from options.
func New(options Options) *Core {
	base := options.BaseURL
	if base == "" {
		base = DefaultBaseURL
	}
	client := options.HTTP
	if client == nil {
		client = &truewire.HttpClient{}
	}
	return &Core{baseURL: strings.TrimRight(base, "/"), token: options.Token, http: client}
}

// Request sends one call: it fills the path's placeholders from the dumped request, sends
// the rest as the query (or as a JSON body for POST/PUT/PATCH), and returns the reply
// body, a non-2xx status mapped to the API error kind it calls for.
func (c *Core) Request(ctx context.Context, call truewire.HttpCall) (json.RawMessage, error) {
	if _, ok := call.Meta.(meta.DefaultMeta); !ok && call.Meta != nil {
		return nil, truewire.LogicError("unexpected meta %T", call.Meta)
	}
	object, err := truewire.Object(call.Request)
	if err != nil {
		return nil, err
	}
	path := truewire.FillPath(call.Path, object)
	method := strings.ToUpper(call.Method)
	if method == "" {
		method = "GET"
	}
	options := truewire.RequestOptions{Timeout: call.Options.Timeout}
	options.Header = map[string][]string{
		"Accept":               {"application/vnd.github+json"},
		"X-Github-Api-Version": {"2022-11-28"},
		"User-Agent":           {"truewire-example-github"},
	}
	if c.token != "" {
		options.Header["Authorization"] = []string{"Bearer " + c.token}
	}
	switch method {
	case "POST", "PUT", "PATCH":
		options.JSON = object
	default:
		options.Query = truewire.QueryFrom(object)
	}
	response, err := c.http.Request(ctx, method, c.baseURL+"/"+strings.TrimLeft(path, "/"), options)
	if err != nil {
		return nil, err
	}
	if response.Status >= 400 {
		return nil, mapError(method, path, response)
	}
	return response.JSON()
}

// mapError turns a non-2xx reply into the API error kind its status calls for.
func mapError(method, path string, response *truewire.Response) error {
	body := response.Decoded()
	detail := response.Text()
	if object, ok := body.(map[string]any); ok {
		if message, ok := object["message"].(string); ok {
			detail = message
		}
	}
	if len(detail) > 200 {
		detail = detail[:200]
	}
	message := fmt.Sprintf("%s %s: HTTP %d: %s", method, path, response.Status, detail)
	kind := truewire.KindAPI
	switch {
	case response.Status == 429 || (response.Status == 403 && response.Header.Get("X-Ratelimit-Remaining") == "0"):
		kind = truewire.KindRateLimited
	case response.Status == 401 || response.Status == 403:
		kind = truewire.KindAuth
	case response.Status == 400 || response.Status == 404 || response.Status == 422:
		kind = truewire.KindBadRequest
	}
	return truewire.APIError(kind, message).WithStatus(response.Status).WithBody(body)
}
