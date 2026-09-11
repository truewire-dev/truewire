# ADR 0012: Conformance is a nightly replay of the recorded requests, and drift is a change of shape, never of value

- Status: accepted
- Date: 2026-09-11

## Context

Truewire's offer to an API owner has two halves. The artefacts are free: the spec, the generated clients, the docs, the mock. The service is vigilance: every method and every stream, public and private, exercised against the live API every night, with a release when the API moves and a reproducible report when it breaks. Nothing in the toolchain is that run yet. Its parts exist, and each does one piece: `truewire capture` calls one endpoint and records the pair; the registry's `record.py` re-records a spec's examples through a client generated from the spec itself; every generated client validates responses at runtime. What does not exist is the thing that ties them together, decides what counts as a finding, and writes a report a stranger can read.

Three forces shaped the design before any code was written.

**Values change every night; shapes do not.** A balance, an order id, a server timestamp and a ticker differ on every call. A conformance check that compares recorded values to live values finds drift every night and is ignored by its second week. What an API owner needs to know is whether the response still has the fields, types and enum values the schema promises, and whether the declared behaviours (pagination, envelopes, error shapes) still hold.

**Where the run executes was decided before what it does, and had to be undone.** The first proposal (2026-09-10 morning) put the nightly run on GitHub Actions for the public half and a Cloudflare Worker for the private half. Marcel objected twice: private calls with a trading key have no business on a shared runner, and two mechanisms for one job is one too many. The next proposal was a VPS running everything. He objected again, correctly: "Have you run them locally first? Otherwise I don't see a point in deploying it to a VPS already." So the order is fixed: build the run, execute it from wherever the CEO session runs, read the reports, and only then choose a machine. Actions remains CI for the repositories, nothing more.

**Keys are restricted by what they may do, not by where they are used from.** A key with query and trade permissions and no withdrawal permission bounds the damage of any leak to the small balance on a self-funded test account. Marcel suggested a rotating proxy against rate limits; the case against it is that nightly volume is one call per endpoint, paced by the runtime's declared rate limit, and that an exchange's risk system treats trading requests from many unknown IPs as the signature of abuse, which is the opposite of what an official SDK vendor wants to look like. One fixed egress IP, restricted by IP as well where the exchange offers it. If a nightly run hits a limit, the run is wrong, not the IP count. The proxy remains Marcel's call.

## Decision

A conformance run is `truewire conform --project <dir>`: one project, one run, one report. For every endpoint in the spec, in this order:

1. **Replay every recorded request live**, through the project's own generated client and hand-written core, so authentication, signing, envelope unwrapping and pagination are the real ones. Parameters come from the recorded `<id>.request.json`. An endpoint with no recording is `skipped` and listed as such; the run never invents a request.
2. **Validate the response** against the response schema with the runtime's validator, exactly as a user's client would. A failure is a finding of kind `schema`, with the JSON pointer and the validator's message.
3. **Diff the shape** against the previous recording: keys added, keys removed, type changes, enum values absent from the schema, nullability changes, array-element shape changes. Values are never compared. Each difference is a finding of kind `shape`, with the JSON pointer and both sides.
4. **Check the declared behaviours**: a declared pagination still pages (the first walk step returns the declared cursor or count); a declared `envelope.payload` still wraps; a declared error shape still matches on a deliberately bad request where the spec records one. Findings are of kind `behaviour`.
5. **Write the report**, JSON for machines and Markdown for people, one entry per endpoint: status (`ok`, `drift`, `error`, `skipped`), the findings, the request and response as recorded, and a first-seen date per finding carried forward from the previous report, so a report says "since 2026-09-14", not "today". The exit code is non-zero when any finding exists.

New recordings for the public half are written to a branch, so that the diff is a pull request and the release that follows it is reviewable. Recordings from the private half (balances, order ids, anything under an authenticated endpoint) are written to a private location and never to a public repository.

The run grows in four phases, each proven by running it before the next starts:

- **Streams**: subscribe for a fixed number of seconds through the generated client, validate every frame, diff frame shapes against the recorded `<id>.messages.json`. Same finding kinds.
- **Write endpoints**: per-project scenario scripts beside the spec (`conformance/scenarios/`): place the smallest order far from the market, record, cancel, record. Never a withdrawal, in any scenario, for any exchange. The scenario is data beside the endpoint, so the report says which one ran.
- **Other languages**: each generated package's replay suite gains a live mode, the same tests pointed at the live base URL with validation on. Their failures are findings of kind `client:<language>`. Python first, because the toolchain is Python.
- **The machine**: one runner that pulls its job list, runs `truewire conform` per project and pushes reports out. It is asked for when seven nightly reports have been produced and read from the CEO session, and not before.

## Consequences

The report is the product. What an API owner pays for is a nightly document that says every method and stream still behaves as documented, with the first-seen date on anything that does not, and a pull request or release already open when it does not. That is a stronger promise than "we have an SDK", and it is the promise the offer makes.

Drift becomes a release trigger rather than a support ticket. A `shape` finding on the public half is a recording pull request; merged, it is a spec change; the spec change is a client release. The run does not decide any of that; it produces the evidence.

The private half costs money and keys, and waits for both. Until a self-funded account with a permission-restricted key exists per exchange, private endpoints are `skipped` with `missing_credentials`, visibly, in every report. The run must not be judged on the public half alone.

Streams wait for the machine. The CEO session's outbound proxy refuses WebSocket upgrades, so phase one can be proven from the sandbox and streams cannot. That is an argument for the runner, and it is deliberately not allowed to bring the runner forward: HTTP conformance has to be readable first.

Shape diffing has heuristics that will be wrong at first: an optional key that was absent in one recording and present in the next is a shape change only if the schema did not already allow both. The first week of reports exists to find those; a finding that turns out to be the tool's fault is fixed in the tool, and the report format carries enough (both sides, the pointer) to tell the two apart.

Left open: whether the recorded request set is enough coverage for behaviours (one page of pagination proves paging works once, not that the last page terminates); how a scenario declares the market and size it uses without a per-exchange dialect; and the rotating-proxy question, which is Marcel's.
