# Get Server Time

[`GET /public/Time`](https://docs.kraken.com/api-reference/market-data/get-server-time)

Returns Kraken's current server time. Public, unauthenticated, no parameters. Useful for
sanity-checking clock drift before signing private requests (the HMAC scheme doesn't
depend on it, but a nonce built from local wall-clock time does).

Example response body (`result`, already unwrapped):

```json
{
  "unixtime": 1688669448,
  "rfc1123": "Thu, 06 Jul 23 18:50:48 +0000"
}
```
