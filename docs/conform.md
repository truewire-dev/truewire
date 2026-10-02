# `truewire conform`

A conformance run calls every recorded request of one project against the live API, once,
through the project's own generated Python client and hand-written core, and writes a
report of what changed. It is phase one of [ADR 0012](adr/0012-conformance-runs.md): HTTP
`rpc` endpoints, Python. Streams, write scenarios and the other languages come later.

```sh
truewire conform --project examples/kraken --state ~/conformance --new public=true
```

## What one call is checked for

The response to each recorded `<id>.request.json` is judged three ways:

1. **The raw wire body against the response schema**, with the same validator `truewire
   check` uses on recordings. A violation is a `drift` finding: the API no longer sends
   what the spec says.
2. **The same response as the generated client handled it**, with validation on. When the
   body passed step 1 and the client still raised, the finding is `client:python`: the API
   is as specced and our code is wrong. When the body failed step 1, the client's
   rejection is the API's doing and is noted on the example, not counted again.
3. **The body's shape against the recorded `<id>.response.json`.** Values are never
   compared; a balance or a timestamp differs every night.

| check | kind | what it means |
|---|---|---|
| `key_added` | drift | a key the recording did not have and the schema does not declare |
| `key_removed` | drift | a key the recording had that is gone, where the schema requires it or does not declare it |
| `type_changed` | drift | the wire type at a pointer changed (`integer` and `number` are one type) |
| `nullability` | drift | null where it was not, or no longer null, and the schema does not declare both |
| `enum_value` | drift | a value outside the schema's `enum`; the value is quoted, it is the finding |
| `array_element` | drift | a tuple (`prefixItems`) changed length |
| `status` | drift | an HTTP status other than the recorded one |
| `not_json` | drift | a 2xx body that is not JSON; `actual` is the media type (`text/html`), without parameters |
| `schema:<keyword>` | drift | any other schema violation (`minimum`, `pattern`, `oneOf`, ...) |
| `client` | client:python | the client rejected a body the schema accepts; `actual` is pydantic's error type |

A difference the schema already allows on both sides is not a finding: an optional key
present last time and absent tonight, a nullable field that was null in the recording.

A value no alternative of an `anyOf` or `oneOf` accepts is named by what the alternatives
say. A new value of a nullable enum (`anyOf: [{enum}, {type: null}]`) is `enum_value`, with
every declared value as `expected`. A value of a type no alternative takes is `type_changed`,
with every declared type. Anything else is the finding for the failing alternative
`jsonschema` ranks most relevant, at its own pointer. `schema:oneOf` remains for a value
that more than one alternative accepts.

Pointers are JSON pointers into the wire body. An array index and a map key (an object
whose keys the schema says are data, via `additionalProperties` or `patternProperties`)
are written `*`, so one change on every element is one finding with a `count`, and a
newly listed market does not make every finding new. A tuple position keeps its index.
A `client` finding's pointer comes from pydantic's error location, read against the
response schema: a key the schema declares keeps its name and everything else is `*`, so a
map key, or a key under an object the schema does not describe, is never written.

## What is not called

Every endpoint in the spec gets an entry. The ones not called say why:

| reason | |
|---|---|
| `no_recording` | no `<id>.request.json`; the run never invents a request |
| `missing_credentials` | the core raised `AuthError` before sending, or the API answered 401/403 while a `[secrets].required` variable is unset |
| `not_a_read` | not a GET. Phase one sends no writes. A read the API sends as a POST (Kraken's private half) is named with `--allow` |
| `stream: phase 2`, `ws: phase 2` | WebSocket endpoints |
| `stale_recording` | called, and not judged: see below |

### Stale recordings

A recorded request can age out while the API stays the same: a time window past the API's
lookback, an option that has expired, a trade id past retention. The API refuses it with a
4xx, and calling that drift would page someone every night for a recording, not an API. So
a 4xx where the recording has a 2xx is `skipped`, `stale_recording`, when the recorded
request pins something that ages:

- a time before the run's date: an integer in epoch seconds to nanoseconds (2000 to 2100)
  under a parameter the request schema declares a time (`format` `epoch-seconds`,
  `epoch-millis`, `epoch-micros`, `epoch-nanos`, `date-time`, `date`) or whose name says
  so (`startTime`, `end_timestamp`, `since`, `dateFrom`, `end_ts`, `createdAt`), or a
  string that starts with an ISO date. An id, a hash or a row offset under any other name
  (`subUid`, `announcement_id`, `cursor`, a bare `start` with no time format) is not a
  time, even in that range;
- an identifier with a date code from 2000 on and before the run's date: a `YYMMDD` or
  `YYYYMMDD` group bounded by `-`, `_` or the value's edge (`BTC-260810-65000-C`);
- a cursor into history: a parameter named `fromId`, `from_id`, `startId`, `sinceId`,
  `afterId` and the like, whatever its value.

The detail names each pinned parameter and its date, and says to re-record it. A 4xx on a
request that pins nothing is still drift, and so is any status change on one that does
while its time is still ahead. The rule is a heuristic, and it can hide one real change: an
API that starts refusing a parameter it accepted, on a request that also pins a time. The
endpoint's other examples still run, and the report lists every stale example.

A 429, a 408 or any 5xx is an `error`, not drift: it says "not now" rather than "not
like this". So is a 401 or 403 the recording does not have, with every secret set: an
expired key, or GitHub's exhausted rate limit, which is a 403. So is a 2xx the core reads as an error in the body (Kraken's `{"error": [...]}`).

## Files

Under `<state>/<project>/`:

- `<YYYY-MM-DD>.json` and `.md`: one entry per endpoint, `ok`, `drift`, `client`, `error`
  or `skipped`, with each example's HTTP status, what the client made of it, and its
  findings with pointer and both sides.
- `ledger.json`: every finding seen, keyed by a fingerprint of endpoint, example, kind,
  check and pointer (plus the value, for `enum_value`). It carries `first_seen`,
  `last_seen` and `resolved`, so a report says "since 2026-10-02" rather than "today". A
  finding resolves on the first run that called its example, checked the part of the
  response the finding is about (status, body, or the client's verdict), and did not see
  it. A run that skipped the example, errored on it, or left it out with `--only`
  resolves nothing. A non-2xx or non-JSON answer resolves no body finding, and a body
  that fails the schema resolves no `client:python` finding. A body finding under an
  array or map (a `*` in its pointer) resolves only when tonight's body has an element
  there, one under a key only when the key's parent is an object, one on a tuple position
  only when the position is there, and a tuple-length (`array_element`) finding only when
  the tuple has elements: an empty order book says nothing about its rows, an empty row
  nothing about its length, and a null `fees` nothing about `fees/maker`.
  A resolved finding that comes back starts a new episode and remembers
  `previously_resolved`.
  A finding the run can never judge again is **withdrawn**: resolved on the run's date,
  with `withdrawn` saying why. That happens when its example is `stale_recording`, when
  its example is gone from an endpoint the run covered, when the run does not call its
  endpoint at all (`not_a_read`, a stream), when a run with no `--only` finds its endpoint
  gone from the spec (deleted or renamed), or when the core refused the call before
  sending it, no `[secrets].required` variable is set, and the endpoint's `meta` does not
  mark it public (`public: true`, `private: false`, or `signed: false` with `security`
  absent, `NONE` or `System`: Binance's unsigned `MARKET_DATA` reads need a key). A refusal of an
  endpoint the spec still marks public is the core's fault and withdraws nothing. A spec
  that marks only its private endpoints (Kraken's and Bybit's say `signed: true` and leave
  the key off the rest) cannot tell the two apart, and withdraws. A run with `--only` withdraws nothing for a missing endpoint: it
  cannot tell gone from left out. Keep `--allow` the same from night to night, or a read
  it stops naming is withdrawn as `not_a_read`. The last case assumes one ledger per surface: a
  public run withdraws what an authenticated run found on a private endpoint, so give each
  its own `--state`.

A report quotes method, path, status, exception class and shapes. It never quotes a
header, a request body or a response body, beyond an unknown enum value. An exception's
message is dropped for the same reason (pydantic's quotes its input), except a transport
failure's (`ConnectError`, a timeout). `--state` may not
point inside the spec tree; the run writes nothing under `spec/`.

## Pacing and exit code

Calls are sequential, one per recorded request, at least `--interval` seconds apart (1 by
default). The core's own rate limiter, where it has one, applies on top. `--timeout`
bounds one call (60 s).

The exit code is 1 when any finding exists, 2 when there is none but a call errored, and
0 otherwise.

## Flags

| flag | |
|---|---|
| `--state <dir>` | where reports and the ledger go (required) |
| `--only <glob>` | function (`spot.market_data.*`) or directory under `spec/endpoints`; repeatable |
| `--allow <glob>` | a non-GET endpoint that only reads; repeatable. Never a write |
| `--new key=value` | passed to the root client's `.new(...)`, as for `capture` |
| `--interval`, `--timeout` | pacing, in seconds |
| `--date YYYY-MM-DD` | the run's date (UTC today by default); names the report and dates the ledger |
