# ADR 0008: A request body serializes through `validator(Type).dump()`, not per-field conversion

- Status: accepted
- Date: 2026-08-28 (carried over 2026-09-06)

## Context

The original body-serialization mechanism rewrote a body's formatted fields (a `Decimal` to a `decimal-string`, a `datetime` to `epoch-millis`) one level deep, with one generated `str(...)` line per field. One client's order-creation body nested `decimal-string` fields inside an `anyOf` of order-configuration variants, past that walk. Unconverted `Decimal`s reached the HTTP library and raised `TypeError` on a real call.

Porting a second client, whose bodies are flat and signed with HMAC over the exact wire bytes, proved the replacement under the strictest condition. That pass also found every client's timestamp types exported only the parse half (`BeforeValidator`) and never the serialize half (`PlainSerializer`), so dumping a body through pydantic would silently render ISO-8601 regardless of the declared wire format. Latent everywhere, because nothing dumped a body through pydantic yet.

## Decision

A generated `rpc` method with a request body serializes it as `content=validator(BodyType).dump(body)`, replacing the per-field conversion lines and the `json=` keyword. This needs:

- a module-level body adapter, named distinctly from the response adapter;
- `content=` at the transport call, with `Content-Type` set explicitly if the transport does not do it for raw content;
- the codegen's "does this module need `validator`" check to fire on a request body, not only on a response;
- every timestamp and date type in the runtime pairing its `BeforeValidator` with a `PlainSerializer(dump, when_used='json')`.

`decimal-string`, `integer-string` and `boolean-string` need no special serializer: pydantic renders a `Decimal`, `int` or `bool` declared as a string-typed field correctly on its own.

## Consequences

Body serialization is nesting-agnostic. A `Decimal` three levels down inside a union variant reaches the wire as the string the API expects, and a signed request signs the same bytes it sends.

The two mechanisms coexist in the generator until every backend has been audited; a backend that has not been touched keeps the old one. `truewire-core`'s timestamp types ship both halves from the start.
