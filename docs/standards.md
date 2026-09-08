# Production Standards

Binding rules for a generated client's public surface, not a checklist to work through once. This file's predecessor called itself a checklist and was read as advisory: nothing checked most of its rules, and every client audited against it violated them. Every rule below states whether a machine checks it today. A rule with no check is an invitation to build one, not a permanent exception.

## How to read a rule

Each rule (`S1`, `S2`, ...) has three parts:

- **Rule**: one or two sentences, precise enough to grep for a violation.
- **Why**: usually a real defect the rule closes.
- **Enforcement**: one of:
  - `truewire <command>`: a real, run-today command fails or warns on a violation.
  - review checklist (manual): a reviewer reads the code against the rule; no command checks it.
  - not applicable: the rule belonged to a multi-project release process.

Rule IDs are stable once assigned. Code cites them (`check_reserved_names` cites S8, `no_call.py` cites S29), so a retired number stays retired, gaps included.

## Entry point

Run every mechanically enforced check in one pass:

```bash
truewire standards
```

It runs `truewire check`, `truewire surface`, `truewire examples --require-verified`, `truewire docs check` and `truewire docs lint`, plus six in-process checks (`links`/S1, `docstrings`/S3, `duplicate-schemas`/S6, `secret-placeholders`/S16, `router-coverage`/S26, `no-call`/S29), and reports one summary. `--only`/`--skip` filter by slug; `--list-checks` lists them. `links` is excluded from a plain run because it makes live HTTP requests to documentation hosts; select it with `--only links`. A green run is a precondition for calling a client done, not proof: a rule enforced by the review checklist still needs that review.

## General

**S1. Every upstream documentation URL resolves to the exact endpoint or topic page cited, not a stale redirect or a domain-level landing page.**

Why: a link that 404s or lands somewhere generic reads as verified when it was never checked.

Enforcement: `truewire standards --only links`. It fetches every URL cited in `upstream.md`, `README.md`, `docs/**` and `router.json` files and flags a non-2xx status or a redirect to a domain-level page. `warning`-severity, opt-in, and a heuristic: a human still judges each finding.

**S2. Public docs describe the real, current client surface. A doc section either reflects what the package does, or is deleted, never a stale scaffold placeholder.**

Why: one client shipped its docs as a literal unedited scaffold, down to a misspelled env var.

Enforcement: partial. `truewire docs check` fails a placeholder code example, since a call to a method nobody filled in does not type-check (S20); `truewire docs lint` catches pipeline-voiced prose (S21). Placeholder prose with no code stays manual.

## Docstring Formatting

**S3. Every function, class and module carries a docstring in the Google-compatible section style: `Args:`/`Returns:`/`Raises:`/`Examples:`/`References:`, no custom section markers, and a multi-line docstring's closing `"""` on its own line.**

Why: a docstring reaches a user through editor hover and any generated reference, and a renderer mis-renders a non-standard section rather than erroring on it.

Enforcement: `truewire standards --only docstrings`. It walks the package source with `ast` and flags a closing `"""` sharing a line with prose, and a `Word:` header line outside the five sections above. `warning`-severity. Docstring presence is not checked.

**S26. Every grouping directory under `spec/endpoints/` declares a `router.json` with a real description and a working upstream link, so every hover point in the client's attribute chain carries a real docstring. A directory spanning several pages cites the one covering the most of them, or reuses a parent's link. Inventing one is refused.**

Why: `docs/spec/authoring.md` rule 14. A router class rendered no docstring before this, so a fresh caller had no way to orient from the code.

Enforcement: presence is `truewire standards --only router-coverage`, `error`-severity; shape is validated on load; reachability is `--only links` (S1). Whether the text reaches the generated class's docstring stays manual.

## Typing

**S4. Every published package ships `py.typed`.**

Why: without it a downstream type checker treats every import from the package as untyped.

Enforcement: manual. `truewire init` writes the file; nothing checks that it survives to the built distribution.

**S5. A value with a documented, finite set of possible values is typed `Literal[...]` via a spec `enum`, never a bare `str`/`int`.**

Why: `docs/spec/authoring.md` rule 2. A `Literal` catches a typo at the call site.

Enforcement: `truewire check`, the `enum` rule, `warning`-severity by design: a field name that usually denotes a closed set is a heuristic, not proof.

**S6. A public response or item type carries an idiomatic, semantic name for the API concept it represents (`OrderStatus`, `AccountTrade`), never a generated placeholder (`Response200`), an anonymous schema-position name, or an unreduced duplicate of a shared shape that should have been reused.**

Why: found in three generators: `Response200` on every `ping` endpoint, and one shared `Money` shape inlined eight times, each copy titled the same, so the disambiguator appended path segments until the names differed (`LimitLimitGtc0OrderConfigurationAnyOf3LimitLimitGtc`). Two mechanisms: a missing `title`, which rule 1 now requires on an empty object too, and a missing `$ref` for a shared shape (rule 0), which no `title` requirement catches.

Enforcement: the empty-object half is `truewire check`, the `title-empty-object` rule, `warning`-severity. The duplicate half is `truewire standards --only duplicate-schemas`: an inline object schema's property-name signature shared by two or more occurrences across the spec tree (at two properties or more, to cut noise) is one finding naming the locations. `warning`-severity.

## Timestamps

**S7. A wire timestamp declares its real format: `epoch-seconds`, `epoch-millis`, `epoch-micros`, `epoch-nanos`, or `date-time` for an RFC 3339 string, never a bare `string`/`integer` with the shape only in prose. A plain calendar date declares `date`. Applies identically to a request parameter, a request body property and a response field.**

Why: `docs/spec/authoring.md` rule 3. An undeclared timestamp renders as a raw `int`/`str`, and a request-side one is sent to the wire wrong. One client read `0 errors` while declaring a format on 0 of 218 endpoints.

Enforcement: `truewire check`, the `timestamp-format` rule, `warning`-severity: a field name ending in `time`/`timestamp`/`date`/`datetime`/`ts`/`since`/`at` with no declared format is flagged. Only the last word of a split name is matched, so `timeInForce` is never mistaken for a timestamp. Each format's runtime type is an alias in `truewire_core.types`, built from `truewire_core.times` converters.

**S27. Every timestamp and date alias pairs its `BeforeValidator` with a matching `PlainSerializer(converter.dump, when_used='json')`.**

Why: ADR 0008. Without the serializer half, `validator(Type).dump()` (S28) silently renders ISO-8601 regardless of the declared wire format. Latent on every client at the time.

Enforcement: `truewire_core.types` ships the six aliases generated code uses, paired, and its tests round-trip each one. A project-specific alias added beside the core follows the same pairing by hand.

## Validation And Errors

**S8. Every generated `rpc` endpoint method accepts `validate: bool | None = None` (`None` follows the client-level default). When an API's own wire parameter is named `validate`, the codegen backend resolves the Python parameter to a distinct name.**

Why: two hand-rolled generators accepted no override on most methods. Worse, one client's order endpoints reused the wire name `validate` for the API's own dry-run flag, so `validate=True` there meant "don't submit the order," the opposite of its meaning everywhere else.

Enforcement: `truewire surface` reports a `missing_validate` entry for every generated `rpc` method lacking the parameter, and fails like a surface gap. `truewire check`'s `reserved-param` rule flags a spec parameter named `validate`, `warning`-severity: the spec is right to use the real wire name, so this never promotes to `error`.

**S9. API error envelopes map to `truewire_core.exceptions` (`ApiError`, `BadRequest`, `AuthError`, `RateLimited`), not ad hoc local exception classes. A missing credential raises `AuthError` from an early credential-resolution path, never a raw `os.environ[...]` lookup left to surface a bare `KeyError`. A rate-limit failure raises `RateLimited` where the API distinguishes one.**

Why: one client's `resolve_credentials()` correctly raised `AuthError`, and every constructor bypassed it for a raw `os.environ[...]` lookup, so a missing credential raised `KeyError` anyway.

Enforcement: review checklist (manual).

## Core And Instantiation

**S10. A client subclasses `truewire_core` transport (`truewire_core.http.HttpClient`, or `Socket`/`Streams`/`Rpc`/`StreamsRpc` from `truewire_core.ws`) and never constructs a raw `httpx.AsyncClient`, opens a raw `websockets.connect` socket, or defines its own class shadowing a runtime primitive's name without inheriting from it. If the runtime cannot express what an API needs, extend the runtime; never keep a private copy.**

Why: one client overrode `Socket.force_open` with a direct `websockets.connect(...)` call to lift `max_size` for a 5MB snapshot, "otherwise identical" by its own docstring: a private copy of a change every client would benefit from.

Enforcement: review checklist (manual). The repository-wide AST test suite that enforced this in the original monorepo is not applicable to a standalone project; `grep -rn "httpx.AsyncClient\|websockets.connect" src/` covers the first half.

**S23. When an API serves the same request/reply surface over both HTTP and WebSocket, the generated method takes a `transport` keyword choosing between them per call, never a transport chosen once at construction that yields two disconnected objects.**

An endpoint with more than one entry in `spec.transports` renders one method with `transport: Literal[...]`, in declaration order, defaulting to the first. A single-transport endpoint gets no parameter.

Why: binding the transport at construction forces a caller to pick one for the client's lifetime, or hold two instances.

Enforcement: review checklist (manual). A `surface`-style check that the method carries the keyword is buildable but not built.

## Package Structure

**S11. One endpoint method per module. The last-level package `__init__.py` composes sibling endpoint classes through inheritance (`MarketData(AssetPairs, Assets, Depth)`). A higher-level composite, root or mid-level, is a plain `@dataclass(kw_only=True)` composing grouped objects as fields, never further inheritance. A mid-level composite is defined inside the subpackage it composes (`Spot` in `spot/__init__.py`), never inline in the root's wiring file.**

"Plain" means wired through `__init__`, never `field(init=False)` + `__post_init__` + `object.__setattr__`, the pattern a composite falls into when it also subclasses a frozen endpoint base. Expose a `classmethod new(cls, *, client)` that builds every child and passes them to `cls(...)` instead.

Why: one generator did the `object.__setattr__` dance on every mid-level composite, found after the client's root class had been cited as a clean example. Review a composite at every level it appears.

Enforcement: review checklist (manual). `grep -rn "object\.__setattr__" src/` is a strong signal, as is any `class` beyond the root in the root wiring file.

**S12. A request body whose valid or required fields differ by a discriminant (order type, side) is spec'd as an `anyOf` of the variant shapes, never a flat list of named parameters with every variant-specific field optional.**

Why: two clients flattened `price`/`stopPrice`/`timeInForce` as `NotRequired` regardless of order type, so a market order with a `price`, or a limit order without one, type-checks cleanly. A spec defect, not a codegen gap: rule 0 states the `anyOf` shape, and the generator renders it as a tagged union.

Enforcement: review checklist (manual).

**S28. A generated `rpc` endpoint method with a request body serializes it through `content=validator(BodyType).dump(body)`, not per-field conversion lines.**

Why: ADR 0008. Per-field conversion rewrites formatted fields one level deep; one client's order body nested `decimal-string` fields inside an `anyOf` past that walk and raised `TypeError` on a real call. Needs a module-level body adapter named distinctly from the response adapter, and S27 first.

Enforcement: review checklist (manual).

**S29. No generated class defines `__call__`. An endpoint's method is always inherited directly onto its parent, real and distinctly named, never reached through a `cached_property` wrapping a `__call__`-only leaf class, at any depth, including a client's outermost transport root.**

Why: a leaf endpoint class defines no `__init__`, so multiply-inheriting several leaves onto a parent works at any depth; `__call__` is never needed. The motivating bug was a leaf with both a named sibling method and a `__call__`, producing two disagreeing docstrings for one call.

Enforcement: `truewire standards --only no-call`, `error`-severity.

**S30. A `spec/endpoints/` directory is a leaf endpoint or a router grouping, never both.**

Why: `docs/spec/authoring.md` rule 16. A directory holding its own `endpoint.json` beside an endpoint-bearing descendant has no name left to render its leaf under; the only mechanical answer is `__call__`, which S29 forbids. Every real instance found already had its leaf's `function` one segment deeper than its directory, which is exactly the subdirectory it needs to move into.

Enforcement: `truewire check`, `error`-severity. The generator also raises, naming the directory, when a leaf's resolved function collides with a node already in the function tree.

## Spec Coverage

**S13. `spec/` reflects the implemented public surface: one `endpoint.json` per endpoint, `upstream.md` where upstream docs exist, and at least one paired example where it is safe to capture one. An endpoint that is unsafe, stateful, credential-blocked or otherwise unavailable declares why on the endpoint itself (`unverified: {reason, detail}`), not as prose in a status document.**

Why: ADR 0001. A per-endpoint declaration is checkable; a paragraph is not. One status document asserted `0 errors` while a fresh run reported 202.

Enforcement: `truewire examples --require-verified` and `truewire check`. Whether any prose still agrees with what they report is manual: re-run before trusting a claim.

**S14. Pagination is declared per `docs/spec/authoring.md` rule 8 / ADR 0002, not hand-rolled per endpoint and not left for a caller to infer from bare `limit`/`cursor` parameters.**

Enforcement: `truewire check`, the `pagination` rule, `error`-severity.

**S15. A `kind: 'stream'` endpoint declares `envelope.verb`: the field, and its two literal values, that states subscribe-vs-unsubscribe intent on the wire.**

Why: ADR 0004. Without it the mock server guesses intent rather than reading a declared fact.

Enforcement: `truewire check`, the `ws-verb` rule, `warning`-severity while existing specs migrate; promoting it to `error` is that migration's last step.

**S16. A response field carrying a genuine secret, credential or PII gets an obviously fake, shape-preserving placeholder in a captured example: never the real value, and never something implausible about the field's shape.**

Why: `docs/spec/authoring.md` rule 6. `REDACTED_CLIENT_SECRET` in an API-key listing's response is the worked reference.

Enforcement: `truewire standards --only secret-placeholders`. The mock server never compares or redacts a response body (unlike a request-side credential, which `redacted`/ADR 0007 strips), so the check reads the project's `[secrets]` names from `truewire.toml` plus a built-in denylist (`apiKey`, `secret`, `privateKey`, ...) and flags a credential-shaped field in any recorded example whose value carries no fake marker (`REDACTED`, `FAKE`, `TEST`, ...). `warning`-severity: a heuristic, not proof.

## Runtime And Type Tests

**S17. A type-usage test file exists, exercises representative public call patterns (constructors, endpoint calls, response types, stream usage), and passes pyright. Not an empty stub, and not orphaned by a `pyrightconfig.json` pointing at a file that does not exist.**

Why: the one committed guardrail against a change silently degrading a public return type to `Any`, missing or empty on six of eight clients in one audit.

Enforcement: manual. `truewire generate` runs pyright when `[python].pyright` is set or a `pyrightconfig.json` sits at the project root, but nothing checks that the file exists or is non-trivial; what counts as non-trivial is a design call.

**S18. Every endpoint method with pagination parameters has an ergonomic `_paged` variant (unless the API's own pagination model cannot be made safe), covered by a mock-backed test walking several pages. One page cannot distinguish a correct walk from one that happens to terminate immediately.**

Why: off-by-one, cursor-advance and termination bugs are what a single-page test cannot see. A declared block generates the walk but does not prove it.

Enforcement: review checklist (manual).

**S24. A `_paged` variant is `truewire_core.util.paging.PaginatedResponse`-shaped (`async for page in x(...)` and `await x(...)` both work) wherever the generator has that rendering: `token` with `absent_cursor`, `page` with a `total` terminator and `done.rows`, and plain `seek` with no `overlap`. A plain async generator is the wrong choice on those three, and the only choice elsewhere until the rendering widens.**

Why: strictly more capable at zero cost to a caller who only iterates. One client's published, awaitable `_paged` methods were silently replaced with plain async generators during a regeneration, breaking downstream `await` callers.

Enforcement: review checklist (manual). Before regenerating a client, check whether it already has a published awaitable contract worth preserving.

**S19. Both HTTP and WebSocket examples are covered by mock-backed tests when the client supports both transports. A mock test uses a local base-URL override, never a live network call.**

Enforcement: review checklist (manual).

## Docs And Examples

**S20. Every code block in `README.md` and `docs/` type-checks against the package it documents.**

Enforcement: `truewire docs check`.

**S21. Docs describe a fact about the API or the credential when stating a limit ("this endpoint requires a partner whitelist"), never a fact about the build ("out of scope for this run," "deliberately deferred"), and never an internal path (`spec/`, `truewire.toml`, `codegen`).**

Enforcement: `truewire docs lint`, the `internal-reference` rule.

**S25. Every relative link in `README.md`, `docs/**/*.md` and `docs/quickstart.yaml` resolves to a real source file.**

Why: a same-repository link is knowable before publish and should never depend on a live HTTP probe.

Enforcement: `truewire docs lint`, the `broken-doc-link` rule, no network access. The rule's original second half, canonical routes on a hosted documentation site, is not applicable outside that host; see your own release process. Heading fragments are not validated.

## Export And Publish

**S22. Retired.** It tied a catalog tier to export sync between a monorepo and a public mirror, not applicable outside a multi-project monorepo; see your own release process. The point stands: source-tree quality that never reaches the published package reaches nobody.

## Notes For Future Rules

- Don't add a rule restating something `docs/spec/authoring.md` or an ADR states more precisely; point at it (S5, S7, S14, S15, S16 do). Two sources of truth for one fact is how this file's predecessor rotted.
- A rule with `manual` enforcement is a to-do, not a lesser rule. If you build the check, update the rule's `Enforcement` line and the slug list above in the same change.
