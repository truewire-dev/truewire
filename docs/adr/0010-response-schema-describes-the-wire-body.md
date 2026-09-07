# ADR 0010: The response schema describes the wire body; `envelope.payload` selects the returned value

- Status: accepted
- Date: 2026-09-07

## Context

ADR 0004 settled where an envelope is declared (per endpoint) and what a recording stores (the raw wire body). It left the response schema describing something else: the value the hand-written core hands back after unwrapping. Rule 6 of `docs/spec/authoring.md` said so in its title, "schemas describe what the core returns", and `envelope.payload` was the bridge, telling `truewire check` where in the recorded frame to find the value the schema described.

That definition anchors the spec to the behaviour of a class rather than to the wire. Three costs followed. `truewire check` carried an extraction special case: before validating a recording it read `envelope.payload` off the frame and validated only what it found, so the schema was never checked against the body the API actually sent. `truewire import openapi` could not be verbatim: an OpenAPI document describes the wire, so an imported schema was correct under the old rule only for an API with no envelope, and any later export would have had to re-wrap. And a reader of `endpoint.json` alone could not tell what the API sends without also reading the core; the one artifact that claims to be the truth about the wire described the wire only by reference to code.

Kraken, the first example project, has 64 request/reply endpoints declaring `envelope: {"payload": "result"}` whose recordings all carry `{"error": [], "result": ...}`. A registry of shared specs is planned. Every spec added under the old rule would need the same rewrite later, so the change had to land before the registry grew.

The alternative considered was to keep the rule and add an explicit, per-endpoint wire schema beside the returned one. That doubles every enveloped endpoint's schema for no new information: the returned value is a sub-tree of the wire body, and a path already names it.

## Decision

A response schema describes the wire body exactly as recorded. `envelope.payload` is a selector: a dotted path into that schema naming the value the generated method returns.

- `truewire check` validates every recorded `.response.json` (and a WS request/reply endpoint's `.reply.json`) against the whole response schema. There is no extraction step.
- The generator resolves `envelope.payload` inside the response schema and types the method's return value from the schema it finds there. The wrapper's own fields are never rendered; the method still returns the unwrapped value, and a core that unwraps today is unchanged.
- Pagination paths (`done.rows`, `done.path`, `cursor.from`) stay relative to what the method returns, that is, to the schema at `envelope.payload`. A page walk reads them off the value the core handed back, so a path rooted at the frame would name a field that value does not have.
- `truewire check` verifies statically that `envelope.payload` names a property of the response schema (`envelope`, `error`). A `$ref` or `anyOf` on the path is undecidable from the endpoint alone and is not flagged, the stance every other schema-path check already takes.
- `envelope.correlate`, the mock server and `truewire capture` do not change. They already worked on wire bodies. `envelope` stays declared per endpoint, for the reasons ADR 0004 gives.
- `truewire import openapi` keeps writing the document's response schema as it stands. Under this rule that is the right schema, not an approximation of one.
- `truewire migrate` rewrites a spec written under the old rule. For every endpoint declaring `envelope.payload` whose response schema does not contain that path, it wraps the schema into the frame shape derived from the endpoint's own recordings: a titled `<ResponseTitle>Frame` object whose other properties are inferred from the recorded keys, described as wire envelope fields, with the old schema under the payload key. It refuses an endpoint with no recording rather than guess a frame; `--template` lets the author name another recorded endpoint whose frame stands in. A second run changes nothing.

A stream endpoint's `payload` schema is out of scope here. Streams still validate a pushed message after reading `envelope.payload` off it, and the stream generator does not consult `envelope`. Every stream in the example projects declares `payload: ""`, so nothing moves today; when a stream needs the same treatment it gets its own ADR, since the message schema, the reply schema and the subscribe frame each raise questions this decision does not answer.

## Consequences

The spec is self-contained about the wire. A recording validates against its schema with no help from the core, an imported document's schema is correct as written, and the checker lost a branch that could disagree with the validator. The response schema is larger for an enveloped endpoint, by one object whose fields are described once as wire facts.

Harder: an old spec has to be rewritten, and a hand-written wrapper is easy to get subtly wrong (a `required` key the API sometimes omits). `truewire migrate` does the mechanical part from recordings; where it has no recording it says so and stops, because a frame nobody has seen is a guess. An empty recorded array gives no evidence of its element type; the command writes `items: {"type": "string"}` there and names each such field in its output, and the next recording that disagrees fails `truewire check`, which is the loop that corrects every other schema too.

Left open: stream payload schemas, as above; and the wrapper fields' descriptions, which the migration fills with a fixed sentence. Anyone who cares what `retExtInfo` means can replace it.
