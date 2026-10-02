# Changelog

## 0.2.0 (2026-10-01)

- **`IsoConverter.dump` writes the shortest fraction of 0, 3, 6 or 9 digits** that holds the
  value, as the Rust and Go runtimes write it: `.733340Z` dumps back as `.733340Z`, not
  `.73334Z`. The source width is not kept: redundant zero groups go (`.000Z` is `Z`,
  `.500000Z` is `.500Z`), an offset is written as `Z`, and a fraction of another width
  (`.1234`) comes back padded (`.123400`).
- **`IsoConverter.dump` and `toPreciseISOString` keep years outside 0..9999 intact.** A
  `PreciseDate` with a sub-millisecond part in year `+010000` or `-000001` came out with its
  fraction cut into the time (`+010000-01-01T00:00.00...`).

- **`t.tuple`** validates a fixed sequence of item codecs, with an optional rest codec
  for trailing items. The inferred tuple type preserves each item's type.
- **`t.integerString` returns `bigint` (breaking).** In 0.1.1 it returned `number`;
  it now preserves integer strings beyond the safe integer range. Use bigint operands
  and literals (for example `1n`) when doing arithmetic. Dumping still writes a numeral
  string to the wire.
- **Lossless JSON integers.** `parseJsonText` reads an unsafe integer literal as
  `bigint`, while safe integers and fractions remain `number`. `stringifyJson` writes
  bigint values as bare JSON integers. `t.int64` accepts safe integer numbers and
  bigint values, preserving digits that `JSON.parse` would round before validation.
- **Nanosecond timestamps.** `PreciseDate` extends `Date` with sub-millisecond
  nanoseconds; `epochNanoseconds` and `fromEpochNanoseconds` convert to and from an
  exact bigint count. Date-times without an offset are interpreted as UTC.
- **Offset date-times can cross a month boundary in UTC.** Calendar validation checks
  the local date before applying the offset. Offsets are bounded, the resulting UTC
  year must be 0000–9999, and years 0–99 are read as written.
- **`seek` pagination** returns a resumable `PaginatedResponse`, advancing a bound
  from row cursor values and deduplicating boundary rows (ADR 0013). `SeekKey`,
  `SeekKeys`, `SeekOptions` and `SeekState` describe its keys, options and state.
  `rowField` reads a nested row field by string and numeric path segments.
- **Transport and stream-reply contracts.** `DualEndpoint`, `Transport`,
  `TransportCall` and `TransportOptions` describe a core serving the HTTP and WebSocket
  halves of a request/reply endpoint. `ReplyStreamEndpoint` provides the subscription
  acknowledgement as well as the stream's messages.

- **A fractional epoch keeps its fraction.** `EpochConverter.parse` reads a fractional
  `number` (kraken's `1688669448.4712` seconds) through its shortest decimal form, to the
  nearest nanosecond, where it used to truncate to whole units; a `PreciseDate` when it
  falls below the millisecond. New `EpochConverter.dumpNumber` writes the fraction back
  (whole units stay integers), and the codecs `epochSecondsFloat`, `epochMillisFloat`,
  `epochMicrosFloat` and `epochNanosFloat` use it for an `epoch-*` field on a `number`
  schema, as Rust's `Timestamp*Float` newtypes do. `epochSeconds` and its siblings, for
  an `integer` schema, still dump whole units.

- `RequestOptions.prepare` signs each attempt after pacing and retry waits. Cancelled
  pacing waiters release their slots. Unsafe methods are not retried after ambiguous
  connection errors with automatic redirects; redirected responses are not retried.

- Proxied `HttpClient.fetch(Request)` rejects promptly when an upload is aborted
  during body buffering, cancels the body reader, and preserves the abort reason
  even when the producer's cancellation hook stalls or rejects.

- Proxied `HttpClient.fetch(Request, init)` preserves integrity, cache, credentials,
  keepalive, mode, referrer and referrer policy. Request overrides retain native
  inheritance rules and replacement bodies leave the original body unread.

- Proxy imports survive webpack server bundling while remaining external to browser
  bundles. Malformed proxy URLs no longer expose credentials through error causes or
  scheme messages; HTTP and socket objects expose only a redacted proxy URL.

- **`proxy` on `HttpClient` and every WebSocket class** (packages clause P18). An HTTP(S)
  proxy URL (`http://host:3128`, credentials in the userinfo) that requests and
  connections go through: undici's own `fetch` over a `ProxyAgent` (an absolute-form
  request for `http://`, `CONNECT` for `https://`) and undici's `WebSocket` over one
  (`CONNECT` for `ws://` and `wss://` alike). `undici` is a new optional peer dependency
  (`^7 || ^8`), imported on first use, never by a client without `proxy` and never by a
  browser bundle. The globals are not used with an undici dispatcher, since a dispatcher
  from another undici major fails inside Node's bundled one (undici 8 on Node 24: `invalid
  onRequestStart method`). A `LogicError`, never a silent direct connection: `proxy` in a
  browser, a URL that is not `http://`/`https://`, `proxy` beside `fetch` or
  `createWebSocket` (at construction), and a missing `undici` (on first use). Omitted or
  `''`, nothing changes. Tested on Node 22.14 and 24.21, undici 7.30 and 8.11.

- **`seek` takes `until`** (`{ read, keys, value }`): a far bound the venue refuses beside
  the moving one (`exclusive.far`, ADR 0013), kept on the rows. A page holding a row past
  `value` ends the walk, every such row dropped. `SeekKeys` names the `keys` union.

- **`HttpClient({ rate, retry })`**, the runtime side of `[policy]` (packages clause P19).
  `rate` spaces request starts `1000 / rate` ms apart across every caller of the client
  (ten requests at `rate: 5` span 1.8 s); a rate that is not positive and finite throws a
  `RangeError`. `retry: true` sends a request again, up to `RETRY_ATTEMPTS` (3) in all,
  when Node's `fetch` failed to connect (`ECONNREFUSED`, `ENOTFOUND`, `EAI_AGAIN`,
  `EHOSTUNREACH`, `ENETUNREACH`, `UND_ERR_CONNECT_TIMEOUT`) or the reply is 429/503,
  waiting its `Retry-After` (seconds or an HTTP date; `retryAfter(response)`) or else
  500 ms doubling. A `Retry-After` over `RETRY_AFTER_CAP` (30 s) returns the reply. No
  other status and no `ReadableStream` body is retried; a browser's `fetch` names no
  cause, so there a connection failure is not retried either. Each attempt gets a fresh
  timeout, is paced and is recorded; the caller's `signal` aborts a wait too. Both
  default off, which changes nothing.
- `RETRY_BACKOFF` (500 ms) and `RETRY_STATUSES` (429 and 503) are exported alongside
  the retry attempt and delay-cap constants.
- **gRPC (`@truewire/core/grpc`, ADR 0017).** `GrpcEndpoint<Meta>` is the contract a
  generated gRPC endpoint calls (`unary({ method, request, meta, signal, timeoutMs,
  headers })`, with the method descriptor from protobuf-es stubs), and `GrpcClient` the core
  that satisfies it over one lazily opened HTTP/2 connection (`@connectrpc/connect-node`).
  Statuses map into the shared taxonomy: `UNAVAILABLE`/`DEADLINE_EXCEEDED` are a
  `NetworkError`, the rest an `ApiError` (`BadRequest`, `AuthError`, `RateLimited` where one
  fits) whose `body` is the `GrpcStatus`. The protobuf and Connect packages are optional
  peer dependencies, needed only by a client with gRPC endpoints.

## 0.1.1 (2026-09-09)

- **`HttpClient` was unusable in a browser.** It held `globalThis.fetch` unbound and called
  it as `this.fetch(...)`, which makes the receiver the client; a browser refuses that with
  `TypeError: Failed to execute 'fetch' on 'Window': Illegal invocation`, while Node's
  `fetch` does not care. So every generated TypeScript client failed on its first call in a
  browser, and no test caught it: the runtime's 147 and a showcase client's eleven all run
  on Node. The default is bound to the global now. The regression test stubs a `fetch` as
  picky as a browser is, and reverting the bind makes it fail with the browser's own
  message. Found by loading a generated client into Chromium rather than reasoning about
  whether it would work.

## 0.1.0 (2026-09-08)

- `Stream` and `Subscription` are exported from the package root beside `PaginatedResponse`,
  so a generated `stream` endpoint's return type reads `Subscription<TickerMessage>` with
  one `@truewire/core` import; `ws.Subscription` still names the same class.
- `@truewire/core/contract`: the interfaces a generated client's core satisfies
  (`HttpEndpoint<Meta>`, `CommandEndpoint<Meta>`, `StreamEndpoint<Meta>`) and the
  `CallOptions` every generated method takes (`validate`, `signal`), the TypeScript half
  of `truewire_core.contract`.

First release. The runtime behind TypeScript clients generated by `truewire`, a port of
`truewire-core` (Python) 0.1.1: `HttpClient` over `fetch` with wire-level `recording()`,
the `Socket`/`Rpc`/`Streams`/`StreamsRpc`/`SerialReplies` WebSocket classes over the global
`WebSocket`, `PaginatedResponse` with `pages()`/`resume()`/`via()`, codec combinators in
place of pydantic validators, `EpochConverter`/`IsoConverter`/`DateConverter` with exact
integer epoch arithmetic, and the `TruewireError` hierarchy.
