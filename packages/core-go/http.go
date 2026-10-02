package core

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"sync"
	"time"
)

// Exchange is one request and the response it got, exactly as they crossed the wire.
type Exchange struct {
	Method      string
	URL         string
	Header      http.Header
	RequestBody []byte
	Response    *Response
}

// Response is a fully read HTTP reply.
type Response struct {
	Status int
	Header http.Header
	Body   []byte
}

// Text is the body as a string.
func (r *Response) Text() string { return string(r.Body) }

// JSON is the body as raw JSON, a validation error when it is not JSON.
func (r *Response) JSON() (json.RawMessage, error) {
	trimmed := bytes.TrimSpace(r.Body)
	if !json.Valid(trimmed) {
		return nil, &Error{Kind: KindValidation, Message: "invalid JSON in the response body", Issues: []Issue{{Path: "", Message: "invalid JSON"}}}
	}
	return json.RawMessage(trimmed), nil
}

// Decoded is the body decoded as a generic JSON value, or the text when it is not JSON.
func (r *Response) Decoded() any {
	var v any
	if err := json.Unmarshal(r.Body, &v); err != nil {
		return r.Text()
	}
	return v
}

// Param is one query parameter.
type Param struct{ Key, Value string }

// RequestOptions is what one HttpClient.Request sends beside its method and URL.
type RequestOptions struct {
	// Query parameters appended to the URL, in order.
	Query []Param
	// Header values; set after the client defaults.
	Header http.Header
	// Body is sent as-is.
	Body []byte
	// JSON, when not nil, is serialized and sent as `application/json` instead of Body.
	JSON any
	// Timeout overrides the client default for this request; 0 keeps the default.
	Timeout time.Duration
}

// HttpClient is the managed HTTP transport of one generated client: every failure to reach
// the server is a network error, and every exchange can be recorded (`truewire capture`).
// Safe for concurrent use.
type HttpClient struct {
	// Client sends the requests; http.DefaultClient when nil.
	Client *http.Client
	// Timeout is the default per-request timeout; none when 0.
	Timeout time.Duration

	mu    sync.Mutex
	hooks map[int]func(Exchange)
	next  int
}

// OnExchange registers a permanent exchange hook and returns a function removing it.
func (c *HttpClient) OnExchange(hook func(Exchange)) (remove func()) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.hooks == nil {
		c.hooks = map[int]func(Exchange){}
	}
	id := c.next
	c.next++
	c.hooks[id] = hook
	return func() {
		c.mu.Lock()
		defer c.mu.Unlock()
		delete(c.hooks, id)
	}
}

// Recording collects exchanges until Stop.
type Recording struct {
	mu        sync.Mutex
	exchanges []Exchange
	stop      func()
}

// Exchanges is every exchange recorded so far, in order. Pick the one you mean by its
// request, never by position: a token mint or a retry is in here too.
func (r *Recording) Exchanges() []Exchange {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]Exchange(nil), r.exchanges...)
}

// Stop ends the recording.
func (r *Recording) Stop() { r.stop() }

// Recording records every exchange this client makes until Stop.
func (c *HttpClient) Recording() *Recording {
	rec := &Recording{}
	rec.stop = c.OnExchange(func(x Exchange) {
		rec.mu.Lock()
		defer rec.mu.Unlock()
		rec.exchanges = append(rec.exchanges, x)
	})
	return rec
}

// Request sends one request and returns the reply, whatever its status. Failing to get one
// is a network error; a cancelled ctx returns ctx's error.
func (c *HttpClient) Request(ctx context.Context, method, target string, opts RequestOptions) (*Response, error) {
	method = strings.ToUpper(method)
	u, err := url.Parse(target)
	if err != nil {
		return nil, LogicError("invalid URL %q", target).WithCause(err)
	}
	if len(opts.Query) > 0 {
		q := u.Query()
		for _, p := range opts.Query {
			q.Add(p.Key, p.Value)
		}
		u.RawQuery = encodeOrdered(u.RawQuery, opts.Query)
		_ = q
	}
	body := opts.Body
	header := http.Header{}
	for k, vs := range opts.Header {
		header[k] = append([]string(nil), vs...)
	}
	if opts.JSON != nil {
		body, err = json.Marshal(opts.JSON)
		if err != nil {
			return nil, LogicError("cannot serialize the JSON body").WithCause(err)
		}
		if header.Get("Content-Type") == "" {
			header.Set("Content-Type", "application/json")
		}
	}
	timeout := opts.Timeout
	if timeout == 0 {
		timeout = c.Timeout
	}
	if timeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, timeout)
		defer cancel()
	}
	var reader io.Reader
	if body != nil {
		reader = bytes.NewReader(body)
	}
	req, err := http.NewRequestWithContext(ctx, method, u.String(), reader)
	if err != nil {
		return nil, LogicError("cannot build request").WithCause(err)
	}
	req.Header = header
	client := c.Client
	if client == nil {
		client = http.DefaultClient
	}
	resp, err := client.Do(req)
	if err != nil {
		if ctxErr := ctx.Err(); ctxErr != nil && errors.Is(ctxErr, context.Canceled) {
			return nil, ctxErr
		}
		return nil, NetworkError("Error sending request to "+method+" "+u.String(), err)
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, NetworkError("Error reading the response to "+method+" "+u.String(), err)
	}
	out := &Response{Status: resp.StatusCode, Header: resp.Header, Body: data}
	c.mu.Lock()
	hooks := make([]func(Exchange), 0, len(c.hooks))
	ids := make([]int, 0, len(c.hooks))
	for id := range c.hooks {
		ids = append(ids, id)
	}
	sort.Ints(ids)
	for _, id := range ids {
		hooks = append(hooks, c.hooks[id])
	}
	c.mu.Unlock()
	for _, hook := range hooks {
		hook(Exchange{Method: method, URL: u.String(), Header: header, RequestBody: body, Response: out})
	}
	return out, nil
}

func encodeOrdered(existing string, params []Param) string {
	var b strings.Builder
	b.WriteString(existing)
	for _, p := range params {
		if b.Len() > 0 {
			b.WriteByte('&')
		}
		b.WriteString(url.QueryEscape(p.Key))
		b.WriteByte('=')
		b.WriteString(url.QueryEscape(p.Value))
	}
	return b.String()
}

// QueryFrom turns a dumped request object's entries into query parameters: strings as
// they are, numbers and booleans as their JSON text, arrays as one parameter per item,
// nulls skipped, anything else as its JSON. Keys are sorted for a deterministic URL.
func QueryFrom(object map[string]json.RawMessage) []Param {
	keys := make([]string, 0, len(object))
	for k := range object {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var out []Param
	for _, k := range keys {
		raw := bytes.TrimSpace(object[k])
		if isNull(raw) {
			continue
		}
		if len(raw) > 0 && raw[0] == '[' {
			var items []json.RawMessage
			if json.Unmarshal(raw, &items) == nil {
				for _, item := range items {
					out = append(out, Param{k, PlainText(item)})
				}
				continue
			}
		}
		out = append(out, Param{k, PlainText(raw)})
	}
	return out
}

// PlainText is a raw JSON value as the text a URL takes: a string unquoted, anything else as JSON.
func PlainText(raw json.RawMessage) string {
	var s string
	if json.Unmarshal(raw, &s) == nil {
		return s
	}
	return string(bytes.TrimSpace(raw))
}

// Object decodes a dumped request into its entries; nil for no request.
func Object(raw json.RawMessage) (map[string]json.RawMessage, error) {
	if raw == nil || isNull(raw) {
		return map[string]json.RawMessage{}, nil
	}
	var object map[string]json.RawMessage
	if err := json.Unmarshal(raw, &object); err != nil {
		return nil, LogicError("the request is not an object").WithCause(err)
	}
	return object, nil
}

// FillPath replaces every `{name}` placeholder in template with the entry of object named
// name (path-escaped), removes those entries, and returns the filled path.
func FillPath(template string, object map[string]json.RawMessage) string {
	var b strings.Builder
	for {
		start := strings.IndexByte(template, '{')
		if start < 0 {
			b.WriteString(template)
			return b.String()
		}
		end := strings.IndexByte(template[start:], '}')
		if end < 0 {
			b.WriteString(template)
			return b.String()
		}
		name := template[start+1 : start+end]
		b.WriteString(template[:start])
		if raw, ok := object[name]; ok {
			b.WriteString(url.PathEscape(PlainText(raw)))
			delete(object, name)
		} else {
			b.WriteString(template[start : start+end+1])
		}
		template = template[start+end+1:]
	}
}
