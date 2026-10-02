# ADR 0019: A list in the query string is written as the endpoint declares under `match.query_arrays`

- Status: accepted
- Date: 2026-09-30

## Context

A request field whose value is a list has two common wire forms in a query string: one item per value (`?state=WA&state=OR`, OpenAPI's default `explode: true`) and one item joining them (`?state=WA,OR`, `style: form, explode: false`). The mock knew only the first. It expands a recorded list into repeated items and compares those with the parsed query, so a client that sent `state=WA,OR` could never match its own recording.

api.weather.gov reads every array filter comma-separated, and a repeated key keeps only the last value. Measured on 2026-09-30: `/stations?id=KSEA&id=KPDX` returned KPDX alone, `id=KSEA,KPDX` returned both; `/zones?area=WA&area=OR&type=forecast` returned OR's 56 zones, `area=WA,OR` returned 124. The weather-gov cores sent repeated keys, so every multi-value filter dropped values without an error, and a mock that accepts only repeated keys pinned that bug in place.

Making the mock accept either form was rejected. It would pass a client that sends the form the API misreads, which is the bug that went unnoticed. A per-project setting was also rejected: the form is a property of how one endpoint parses its query, like `redacted` and `match.ignore` (ADR 0018), and one API can mix both.

## Decision

An endpoint declares `match: {"query_arrays": "comma"}` when its API reads a list in the query string as one comma-separated item. The default is `repeat`. For a `comma` endpoint, each recorded list expects one comma-separated query item. Its elements retain the existing scalar number/boolean matching tolerance (`1.0` matches `1`, and `True` matches `true`), while order and multiplicity stay strict. Scalar strings containing commas are compared whole. An empty list is left out, as it is for a repeated key. A repeated key, or the values in another order, is a 422.

This declaration applies to query-string serialization. Under ADR 0006, query-role fields can also arrive in a JSON body: arrays there are compared structurally, with their original order, rather than converted to comma text. Supplying the same array in both the query and JSON body is refused.

A JSON body must carry an array for a recorded array; a JSON string such as `"A,B"` does not match `["A", "B"]`. Form-urlencoded bodies retain the declared comma wire form. Pooled JSON arrays distinguish booleans from numbers at every nesting level, while equivalent numbers such as `1` and `1.0` still match.

HTTP mismatch and ambiguity diagnostics name each candidate's `query_arrays` form alongside its recorded request. Declared redacted names are removed from that diagnostic request, including inside recording envelopes.

`match` now holds `ignore`, `query_arrays` or both, and a `match` block with neither is refused.

The core writes the wire form. The declaration tells the mock what that form is; the generated code does not read it.

## Consequences

A recording with two values in one filter now proves the wire form in every language that replays it through `truewire mock`, because a core that sends the other form gets a 422. A recording with one value per filter proves nothing about the form, since `[x]` renders `x=x` either way.

A value containing a comma cannot be told apart from two values on the wire. That is the API's own ambiguity, and it is not solved here.

Other forms (`pipeDelimited`, `spaceDelimited`, `deepObject`) are not admitted until an API needs one.
