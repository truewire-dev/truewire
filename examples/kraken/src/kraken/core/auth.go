package core

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"crypto/sha512"
	"encoding/base64"
	"strconv"
	"sync"
	"time"
)

// Credentials is a Kraken API key pair. The private key is base64 and never leaves the process.
type Credentials struct {
	APIKey     string
	PrivateKey string
}

// Sign is the `API-Sign` header value: HMAC-SHA512, keyed by the decoded private key, over
// the request path followed by SHA256(nonce + body), base64-encoded. body is the exact body
// being sent, nonce included: the signature covers the bytes on the wire.
//
// See https://docs.kraken.com/api/docs/guides/spot-rest-auth.
func Sign(path string, nonce int64, body, privateKey string) (string, error) {
	key, err := base64.StdEncoding.DecodeString(privateKey)
	if err != nil {
		return "", err
	}
	digest := sha256.Sum256([]byte(strconv.FormatInt(nonce, 10) + body))
	mac := hmac.New(sha512.New, key)
	mac.Write([]byte(path))
	mac.Write(digest[:])
	return base64.StdEncoding.EncodeToString(mac.Sum(nil)), nil
}

// Nonce is a strictly increasing nonce, as Kraken requires per key: the millisecond clock,
// bumped by one whenever two calls land in the same millisecond.
type Nonce struct {
	mu   sync.Mutex
	last int64
}

// Next returns the next nonce.
func (n *Nonce) Next() int64 {
	n.mu.Lock()
	defer n.mu.Unlock()
	now := time.Now().UnixMilli()
	if now > n.last {
		n.last = now
	} else {
		n.last++
	}
	return n.last
}

// refreshBuffer is how long before its ~900 s lifetime the WebSocket token is refreshed.
const refreshBuffer = 30 * time.Second

// TokenCache is the WebSocket token, fetched through the signed REST call on first use and
// refreshed before it expires. One per private connection.
type TokenCache struct {
	Fetch func(ctx context.Context) (token string, expires time.Duration, err error)

	mu        sync.Mutex
	token     string
	expiresAt time.Time
}

// Get returns a valid token, fetching one when needed.
func (c *TokenCache) Get(ctx context.Context) (string, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.token != "" && time.Now().Before(c.expiresAt) {
		return c.token, nil
	}
	token, expires, err := c.Fetch(ctx)
	if err != nil {
		return "", err
	}
	c.token, c.expiresAt = token, time.Now().Add(expires-refreshBuffer)
	return token, nil
}
