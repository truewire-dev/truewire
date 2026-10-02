# Changelog

## 0.2.0 (2026-09-30)

- **`IntegerString` holds a `BigInt` (breaking).** Its tuple field and `Deref` target
  change from `i64` to `num_bigint::BigInt`, and it is no longer `Copy`. Replace
  `IntegerString(5)` with `IntegerString::new(5)`; `new` accepts `impl Into<BigInt>`
  and `into_inner` returns `BigInt`. Use `to_i64()` for a checked conversion or
  `saturating_i64()` to clamp to the `i64` range. The wire value remains a numeral string.
- **`CallOptions.transport` selects a transport (breaking for struct literals).**
  The new public field is `Option<Transport>`; `None` uses the endpoint's first declared
  transport. A 0.1.1 literal such as `CallOptions { timeout: Some(d) }` must add
  `transport: None` or `..Default::default()`. The `CallOptions::transport()` builder
  accepts `Transport::Http` or `Transport::Ws`. Single-transport endpoints ignore it.
- **Number and string epoch types.** `TimestampSecondsFloat`, `TimestampMillisFloat`,
  `TimestampMicrosFloat` and `TimestampNanosFloat` wrap `DateTime<Utc>` and parse
  fractional epoch counts to nanosecond precision. Numeric serialization uses `f64`,
  so it can round the fraction and does not preserve every nanosecond.
  The corresponding `Timestamp*String` types serialize counts as numeral strings.
  Integer epoch types continue to serialize whole counts.
- **`Stream::map_reply`** converts both the subscription acknowledgement and the
  unsubscribe reply with the supplied fallible function, leaving notifications unchanged.
- **`Seek`, `SeekKey`, `SeekState` and `SpanUnit`** implement resumable seek pagination
  (ADR 0013), moving a bound from the rows' cursor keys and deduplicating boundary rows.
  A declared span splits a bounded range into requests within that span.
- **Protobuf support** is available through the `proto` module and feature (ADR 0016).
  `Protos` loads message descriptors from `.proto` sources or a descriptor set and
  converts protobuf bytes to and from ProtoJSON values, including binary WebSocket
  frames. Source compilation uses `protox` without requiring a `protoc` executable.

- **`Seek::cursor::<T>()`** reads a row's cursor value as `T`, the row field's own timestamp
  type, and converts it to the bound's: a row `timestamp` in epoch seconds under an
  epoch-milliseconds bound (TRU-197). Without it, a row value is read as the bound's type,
  as before. `SeekKey` gains two provided methods, `instant` and `from_instant` (`Some` for
  every timestamp type, `None` by default), so an implementor outside the crate is
  unaffected.

- `HttpClient::request_prepared` builds and signs fresh options after pacing on every
  attempt, without adding fields to `RequestOptions`. Cancelled pacing waiters release
  their slots. Connection-error retries are limited to safe methods, because reqwest
  can fail after a redirect has already acted on a POST.

- `Socket::proxy()` returns an owned URL with username and password removed, so
  displaying or debugging it does not expose proxy credentials. Connections retain
  their configured authentication.

- HTTPS requests without `rustls-tls` or `native-tls` fail with `Error::Logic` before
  sending anything. This prevents reqwest from forwarding the request in cleartext
  through an explicit or environment-configured HTTP proxy when TLS is disabled.

- **`HttpClientOptions::with_proxy` and `Socket::with_proxy`** (packages clause P18). An
  explicit proxy URL for HTTP and WebSocket alike. HTTP builds the `reqwest` client with
  `Proxy::all` (`http://` or `https://` proxy; the environment ignored, `NO_PROXY`
  included); options already holding a `client` are an `Error::Logic`. The socket opens
  every connection as a `CONNECT` tunnel through an `http://` proxy, with the URL's
  credentials as `Proxy-Authorization`, then runs `client_async_tls` in it for `ws://` and
  `wss://` (plain `ws://` only without a TLS feature); `tokio-tungstenite` has no proxy
  support of its own. A bad URL is an `Error::Logic` that never repeats it (`reqwest` alone
  takes `socks5://` and fails later). `""` means none. Additive: two methods, no new pub
  field on `HttpClientOptions`, so existing literals of that options type are unchanged. New direct
  dependencies `base64` and `percent-encoding` (already in the tree through `reqwest`), and
  tokio's `net` and `io-util` features.

- `Link::rpc_request` removes its reply slot when the call is dropped, as by a caller's
  `tokio::time::timeout`, not only when it returns. A late reply for that id is dropped.
- **`Seek::until(field)` and `Seek::step_until`**: a far bound the venue refuses beside the
  moving one (`exclusive.far`, ADR 0013), kept on the rows. `step_until` takes the caller's
  value (any `SeekKey`); a page holding a row past it ends the walk, every such row dropped.
  A value on a `Seek` with no `until` field is an `Error::Logic`, which now also covers a
  call the SDK refuses before sending anything.

- **`HttpClient::with_rate` and `with_retry`**, the runtime side of `[policy]` (packages
  clause P19). New builder methods, both defaulting off, so every call a 0.1 generated
  crate makes means what it did; `HttpClientOptions` is unchanged. `with_rate(Some(r))`
  spaces request starts `1 / r` seconds apart across the client and its clones (ten
  requests at 5 span 1.8 s) and panics on a rate that is not positive and finite.
  `with_retry(true)` sends a request again, up to `RETRY_ATTEMPTS` (3) in all, when
  `reqwest` failed to connect (`is_connect`) for a safe method (GET, HEAD, OPTIONS,
  TRACE) or the reply is 429/503. Redirected replies are not retried. It waits its
  `Retry-After` (seconds or an HTTP date; `http::retry_after`) or else 500 ms doubling. A
  `Retry-After` over `RETRY_AFTER_CAP` (30 s) returns the reply. No other status is
  retried. Each attempt is paced and recorded.
- **`DecimalString` holds any number of digits (breaking).** It keeps the wire text verbatim
  (`"1e-7"` dumps as `"1e-7"`) and the value as a `bigdecimal::BigDecimal` instead of a
  28-digit `rust_decimal::Decimal`, so a 33-digit volume replays instead of failing
  validation. It derefs to `BigDecimal`, compares and hashes by value, and offers `parse`,
  `as_str`, `value`, `to_f64`, `+ - *` and unary `-`. It is no longer `Copy` and no longer a
  tuple struct; `truewire_core::rust_decimal` is replaced by `truewire_core::bigdecimal`.

- **gRPC support (new since 0.1.1, ADR 0017).** `GrpcEndpoint` carries encoded messages.
  `GrpcCall` is `{ method, request:
  Vec<u8>, meta, options }` with `method` the HTTP/2 path, and the verb is
  `invoke(call) -> Result<Vec<u8>>`: a generated gRPC method encodes its `prost` request and
  decodes the reply, so one object-safe trait serves every message type without the
  contract depending on `prost`.
  The `grpc` module and feature provide `GrpcClient`, which implements
  `GrpcEndpoint<Meta>` for every `Meta` over a pass-through codec (`BytesCodec`,
  `GrpcClient::call`). Construct it with `GrpcClient::new(url)`;
  `with_protos(protos)` adds descriptors for the untyped JSON `unary` call.

## 0.1.1 (2026-09-09)

- **The endpoint traits are implemented for `Arc<T>`**, including the `dyn` form. A
  generated root now takes its core by value (`Weather::from_core(Core::new(...))`), which
  is the common case and the one that should read well; these impls let the shared case
  through the same door, so an `Arc<dyn HttpEndpoint<Meta>>` handed to two clients
  satisfies the same bound as the value it wraps. One constructor rather than one per
  ownership story. Additive: nothing that compiled against 0.1.0 stops.

## 0.1.0 (2026-09-09)

First release. The runtime behind Rust clients generated by `truewire`, the third
runtime after `truewire-core` (Python) 0.2.1 and `@truewire/core` (TypeScript) 0.1.0,
designed from the plan (`docs/plan.md`) rather than transliterated:

- `truewire_core::contract`: the traits a hand-written core implements (`HttpEndpoint<Meta>`,
  `CommandEndpoint<Meta>`, `StreamEndpoint<Meta>`) and the call shapes generated code hands
  them (`HttpCall`, `CommandCall`, `SubscribeCall`, `CallOptions`). A core returns the wire
  body as a `serde_json::Value`; generated code decodes it, or returns it raw for
  `validate: false`, so a core never validates.
- `truewire_core::http`: `HttpClient` over `reqwest` with wire-level `recording()` (an
  `Exchange` per request, body included, for `truewire capture`), `NetworkError` on every
  failure to reach the server, and the reply returned whatever its status.
- `truewire_core::validation`, `types`, `times`, `decimal`: `decode`/`dump`/`parse_json`
  over `serde` with `serde_path_to_error`, so every mismatch is a `ValidationError` naming a
  JSON pointer; one newtype per narrowing spec format (`TimestampSeconds`, `TimestampMillis`,
  `TimestampMicros`, `TimestampNanos`, `TimestampIso` over `chrono::DateTime<Utc>`,
  `DateIso` over `NaiveDate`, `DecimalString` over `rust_decimal::Decimal`, `IntegerString`,
  `BooleanString`), and the `EpochConverter`/`IsoConverter`/`DateConverter` behind them,
  with exact `i128` epoch arithmetic and nanosecond round trips.
- `truewire_core::paging`: `PaginatedResponse<T, S>` with `pages()`, `rows()`, `all()`
  (also `.await`), `resume()` and `via()`, plus the terminator helpers a generated walker
  calls (`exhausted`, `total_reached`, `cursor_or_done`, `TotalSeen`).
- `truewire_core::ws`: `Socket<D: Dialect>` over `tokio-tungstenite`: lazy connection,
  `rpc_request` correlated by id, `subscribe` returning a `Stream` with the acknowledging
  reply and `unsubscribe()`, `serial_request` for acknowledgements matched by arrival order,
  an optional ping, an `on_open` handshake hook, and `wait()` so a dropped connection fails
  every caller instead of hanging it.
- `truewire_core::errors`: one `Error` enum (`Network`, `Validation`, `Api` with
  `ApiKind::{Api, BadRequest, Auth, RateLimited}`, `Logic`) carrying the wire status and
  body of an API error and the same string codes as the TypeScript runtime.
