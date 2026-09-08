# Notes from building `truewire-core` (Rust)

Where the plan (`docs/plan.md`) cannot express something the Rust runtime or a Rust
generator needs. Each item names the exact gap; none is worked around in the crate.

## Plan gaps

1. **Integer width and signedness.** `scalar{base: "integer"}` carries no range and no
   `format` narrower than the JSON kind, so a Rust backend has to render every integer as
   `i64`. A schema's `minimum: 0` (a `u64`), a `format: int32`, or an id that exceeds
   `i64` (a 128-bit snowflake sent as a JSON number) cannot be expressed on the plan and
   therefore cannot be rendered as `u64`/`i32`/`i128`. TypeScript has the same blind spot
   behind `Number.MAX_SAFE_INTEGER`; Python does not, because `int` is unbounded. A
   `format` for integer width (or `minimum`/`maximum` on the scalar node) would close it.

2. **Union discriminators.** `union{variants}` lists the variants in order and nothing
   else. Rust has to render it as `#[serde(untagged)]`, which tries variants in order (the
   behaviour the other runtimes have) but reports a mismatch as "data did not match any
   variant of untagged enum" at the union's own path, losing the inner path and message
   the TypeScript `union` codec keeps in `issues`. An OpenAPI `discriminator` (or a
   variant whose `literal` field could serve as one) is dropped by `truewire import
   openapi` and has no node on the plan; with it, a Rust backend would render
   `#[serde(tag = "...")]` and get exact errors.

## Runtime limitations that are not plan gaps

3. **`decimal-string` precision.** The plan promises the digits verbatim; the TypeScript
   runtime keeps the string and Python's `decimal.Decimal` is arbitrary-precision.
   `rust_decimal::Decimal` holds 96 bits of mantissa (28 significant digits), so a wire
   value with more digits than that fails to parse rather than round silently. No API in
   the examples sends one, and the failure is a `ValidationError` at the field's path.

4. **Mixed-type `literal` values.** `literal{values: [true, 0]}` is expressible on the
   plan and the TypeScript codec accepts it as written; a Rust `enum` with `serde` renames
   covers string literals only, so a mixed literal needs a hand-rendered `Deserialize`
   impl from the generator. Not a plan gap; a generator obligation to note before writing
   `truewire generate rust`.
