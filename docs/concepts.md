# Concepts

Truewire rests on five ideas. None of them is complicated. Together they are why a client generated from a Truewire spec behaves differently from one generated from an OpenAPI document: it was built against what the API actually sends, and it says so.

## 1. Recorded examples are the ground truth

An OpenAPI document is a claim about an API. A recorded example is evidence. Truewire treats the second as primary.

Every endpoint directory holds `endpoint.json`, the schema, and `examples/`, a set of paired files: `<id>.request.json` with the parameters the client sent, and `<id>.response.json` with the status and the payload it got back. For a WebSocket stream the pair is `<id>.parameters.json` (what was subscribed) and `<id>.messages.json` (the frames that arrived, in order).

The important word is *client*. An example is a recording of a call the generated client made, not a `curl` to the API. A capture taken outside the client proves the API's shape and says nothing about the request the client sends. One real client had 100% example coverage while its two most-used methods sent a body the API rejected, because the examples had been captured with a raw HTTP library and the client's own core was double-wrapping a parameter list. Replaying the example through the real method is what caught it.

This changes what the schema is for. The schema is not the truth that the example illustrates. The example is the truth that the schema must describe. `truewire check` validates every recorded response against its endpoint's response schema, and a schema that fails against a real recording is wrong, not the recording. When an API changes, you re-record, the check fails, and you fix the schema. The document never silently drifts from the wire.

The corpus Truewire came from holds 2,404 recorded HTTP pairs and 384 WebSocket captures across 3,638 endpoints. A generator that has only seen clean specs has never seen a price arrive as `"79746.95000"` or a boolean as `"true"`. Truewire has, and its type system exists for exactly those.

## 2. Evidence taxonomy: verified, or unverified with a reason

Not every endpoint can be recorded. The credential is the wrong tier. The account has no open orders to query. The call moves money and the build runs under a no-writes rule. A generator that ignores this ships a client whose coverage number means nothing.

Truewire makes every endpoint resolve to one of three outcomes, and only three (ADR 0001):

- **Verified.** The call was made and a real response recorded. Spec, examples, generated method, docs.
- **Unverified.** The endpoint cannot be called from here. Spec, generated method, docs, and an `unverified` declaration on the endpoint itself: a `reason` from a closed set (`missing_credentials`, `program_enrollment`, `unsafe`, `requires_state`, `runtime_error`) and a `detail` in prose. "Real call attempted with a personal API key; rejected with `EGeneral:Permission denied`" is a detail. "TODO" is not.
- **Excluded.** There is nothing to build: the endpoint is deprecated upstream, or speaks a protocol the client does not. Excluded endpoints produce nothing.

Unverified is the default for anything uncallable, never a fallback to excluded. A `place_order` that does not exist because nobody had a funded account is not a safer client, only a less useful one.

`truewire examples --require-verified` is the gate. It fails when any endpoint has neither a paired example nor an `unverified` declaration, and it fails on a stale declaration too: an `unverified` left in place after an example was recorded. It also reports the denominator, "142 of 147 endpoints verified, 5 excused," because a bare count cannot distinguish a small API from a mostly-unverified one.

A coverage claim becomes checkable by machine, and the doubt is written next to the thing it is about.

## 3. Declared pagination, envelopes, redaction and push

The things a schema cannot express are exactly the things that break clients: how pages chain, how responses are wrapped, which request fields can never be replayed, when a stream starts pushing. Most generators leave these to per-API code, so each client reinvents them and none is audited. Truewire declares them as data beside the schema, and checks the declaration.

**Pagination** (ADR 0002) is a block tagged by `strategy`: `page`, `token`, `offset`, `window` or `seek`. Each strategy names the request parameters it drives and states how the walk ends, with only the terminators that strategy can actually decide. `truewire check` verifies that every parameter named exists, that parameters the walk does arithmetic on are numeric, and that every response path resolves against the response schema. A declared block generates a `_paged` method that walks the pages, and the walk carries its own guards: a `window` whose page comes back as full as requested raises rather than silently skipping the rows the API truncated.

**Envelope** (ADR 0004) is two dotted paths: `payload`, from the wire frame to the value the core hands the caller, and `correlate`, how a request id threads into the reply. Declared per endpoint, never per project, because a real client can be half JSON-RPC and half REST under one package. A recorded example on an enveloped endpoint stores the raw wire frame; the check extracts before validating.

**Redaction** (ADR 0007) is a list of request keys the mock server ignores when matching: a signature, a nonce, a session token. These are injected by the transport at send time and a recording can never hold a fixed value for them. Before this was declared, the mock server grew one hardcoded branch per API's auth scheme.

**Push** is for a stream with no subscribe frame at all: `{"trigger": "connect"}` when the API starts pushing the moment the socket opens, `{"trigger": "after_rpc", "method": "login"}` when it starts after a named call. Two shapes cover every API reviewed so far.

The pattern is the same each time: a behavior that used to be sniffed at runtime becomes a declaration the checker verifies and the generator renders. The declaration is not trusted. It is audited against the schema it sits beside.

## 4. Wire fidelity: why a price is a `Decimal`

Real APIs do not send clean JSON types. They send a price as `"79746.95000"`, a timestamp as `1700000000000` or `1787839575.177869` or `"2026-08-13T12:12:23.390802Z"`, a boolean as `"true"`, a count as `"15"`. A generator that maps `string` to `str` hands you a price you cannot compare and a boolean you test with `== "true"`.

Truewire's answer is a small set of `format` values that keep the JSON Schema type honest (the schema still says `string`, because that is what the recording holds) while telling the generator what the value means:

| Format | Wire | Python |
| --- | --- | --- |
| `decimal-string` | `"79746.95000"` | `Decimal('79746.95000')` |
| `integer-string` | `"15"` | `15` |
| `boolean-string` | `"true"` | `True` |
| `epoch-seconds`, `epoch-millis`, `epoch-micros`, `epoch-nanos` | `1700000000000` | `datetime` |
| `date-time` | `"2026-08-13T12:12:23Z"` | `datetime` |
| `date` | `"2026-08-13"` | `date` |

A price is a `Decimal` and not a `float` for the reason every trading system knows: `float` cannot represent `0.1`, and a `Decimal` constructed from the wire string preserves exactly the precision the API sent. A `Decimal` constructed from a JSON number would already have lost it. That is why the format is specifically for the string-wire case, and why the rule says never to re-type a JSON-number price as a string the API does not actually send.

The formats work in both directions. A request parameter declared `epoch-millis` is sent as milliseconds; a `Decimal` three levels deep inside a union variant is serialized as the string the API expects (ADR 0008).

Runtime validation is on by default and overridable per call with `validate=False`. Validation uses a tolerant base (ADR 0003): declared fields are typed and checked, undeclared fields ride along rather than crashing the call or vanishing. A new field the API adds tomorrow does not break production, and does not disappear either.

## 5. The mock server and the 409 rule

`truewire mock` serves a project's recorded examples over real HTTP and real WebSocket. A generated client pointed at it makes the same calls it would make against the API, through the same core, with the same signing and the same envelope unwrapping. The mock does not unwrap anything; it serves the raw recorded frame and lets the client do its job.

For WebSocket it replays the full lifecycle: a subscribe frame is matched against recorded parameters, the recorded messages are pushed in order, an unsubscribe is recognized through the declared `verb`, an RPC-over-WS call gets its recorded reply with the request's correlation id threaded in, and a `push` endpoint starts sending on connect or after the named RPC.

Matching is structural and strict. An incoming request is compared against every recorded example for its route, ignoring only the keys the endpoint declares as `redacted`. Then the mock applies one rule that most mocks do not: **exactly one example must match.** Zero matches is a 422, "the client sent something the corpus never recorded," with the candidates and what each expected. More than one match is a **409**, "ambiguous," with the tied candidates.

The 409 exists because first-match-wins is a silent failure, not an imprecision. One corpus had three endpoints with two examples each whose requests were byte-identical (recorded before and after a state change) but whose responses differed. The old matcher served the first response for both, every time, and the second was never exercised by any test. The fix was not a smarter matcher; there is no request-level signal to recover when the only variable was account state. The fix was in the spec: collapse the pair to one example. The 409 is what makes that visible.

The result is a test suite that runs with no network, no credentials and no rate limits, against responses the API really sent, and that fails loudly when the recordings and the client disagree. A red mock against a green coverage number means the example and the client disagree, and the client is wrong.

## Where these lead

Each idea is checkable. That is the common thread. Truewire's `check`, `examples`, `surface` and `standards` commands are those checks, and a client is not done until they are green.
