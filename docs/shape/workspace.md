# The workspace

A repository that uses Truewire. One upstream API, one spec, one package per language, one
documentation site. `truewire init` writes the skeleton; the skills fill it in; `truewire
score` says how much of it is real.

```
.agents/                  skills and rules, vendored from the toolchain
  skills/                   one <name>/SKILL.md per step
  rules/                    one <language>.md per backend
.claude/                    symlinks into .agents/
.github/workflows/          CI: the same gates the agent runs
.truewire/
  codegen/<language>.json   what codegen wrote, committed
  cache/                    fetched schemas and scratch, ignored
dev/
  capture/                  how each recording was produced
docs/
  docs.yml                  nav, quickstart, metadata
  index.md                  what this API is and why this client
  api-keys.md               how a caller obtains and scopes credentials
  how-to/                   task-shaped pages
  reference/                surface-shaped pages
spec/
  schemas.json              shared schemas
  <router>/
    router.json
    schemas.json            schemas shared within the router
    <endpoint>/
      endpoint.json
      examples/             recorded request/response pairs
test/
  scenarios/                cross-language behaviour, if any
packages/
  python/  typescript/  rust/  go/
conformance/
  scenarios/                write-path scripts (ADR 0012)
  reports/                  nightly output, public half only
AGENTS.md                   the entrypoint an agent reads
CLAUDE.md                   one line: @AGENTS.md
README.md                   the entrypoint a human reads
LICENSE                     MIT
truewire.toml
.env                        ignored; names come from [secrets].required
.gitignore
```

**W1.** The repository is named after the API, lives under `truewire-dev/`, and is public.
A project whose upstream API has no public documentation is the one exception, and it says
so in its README.

**W2.** Exactly four kinds of file live here: **specification** (`spec/`), which is
recorded and hand-corrected; **hand-written code** (each package's core), which a person or
an agent authors; **generated code**, which lives in a directory that can be deleted and
rebuilt; and **prose** (`docs/`, `README.md`, `AGENTS.md`). Nothing is a mixture. This is
[I4](README.md#the-invariants) applied to the tree.

## The spec

**W3.** The spec is the product. `spec/<router>/.../<endpoint>/endpoint.json` declares the
request schema, the response schema, the transport, the kind (`rpc` or `stream`), and any
declared behaviour: pagination, envelope, redaction, core meta. `examples/` beside it holds
at least one recorded pair per endpoint, or an `unverified` block naming a reason.

**W4.** Schemas are shared at the narrowest scope that fits: inside one endpoint, then
`<router>/schemas.json`, then `spec/schemas.json`. `truewire standards` reports a schema
duplicated across siblings; it is an error, because a duplicated schema is two schemas that
will drift.

**W5.** Routers mirror the API's own naming, not the URL structure, and never the docs'
marketing sections. `pets.get` is a router and an endpoint; `v2.public.pets.get` is a URL.

## Recordings and how they were made

**W6.** `dev/capture/` holds one file per capture session: the `truewire capture`
invocations that produced the recordings, including their `--scrub` flags and any setup a
write endpoint needed. A read endpoint is reproducible from its recorded request alone; a
write endpoint is not, and the difference is why this directory exists. It is prose and
scripts, never credentials: a capture file reads `$BIT2ME_API_KEY`, it never contains one.

**W7.** Recordings from endpoints that require credentials may carry account-shaped values
— balances, order ids, handles. They are scrubbed at capture time, and a project whose
private half cannot be scrubbed keeps those recordings out of the public repository
entirely and says so in `conformance/`.

## Tests

**W8.** Every package's test suite runs against `truewire mock`, which replays the
recordings. A test that needs the live API is not a test; it is a conformance run.

**W9.** `test/scenarios/` holds behaviour that spans calls and must hold in every language:
authenticate then call, paginate to the last page, subscribe and survive a reconnect. Each
scenario is declarative, and each language's suite executes the same file, so that four
clients cannot quietly disagree. A project with no such behaviour has no `test/`. *(open:
the scenario format is not designed, and no backend executes one yet. Until it is, this is
a claim, not a gate.)*

## Documentation

**W10.** `docs/docs.yml` carries the nav, the quickstart with one code block per language,
and the metadata the site needs. It validates against a published schema, referenced by a
`$schema` key, so an editor completes it and CI rejects a typo.

**W11.** `docs/index.md` answers what the API is and why a caller wants a validated client
for it. `docs/api-keys.md` is the page a new caller actually needs: where to get a key,
which permissions to grant, which to refuse. `how-to/` is task-shaped, `reference/` is
surface-shaped. Reference pages are generated from the spec; how-to pages are written.

**W12.** Every code block in `docs/` and in every package README type-checks against the
generated package. `truewire docs check` is the gate. A snippet that does not compile is
worse than no snippet.

## Configuration

**W13.** `truewire.toml` is the only project file. It carries a `#:schema` comment pointing
at the published schema, `[project]`, `[spec]`, `[secrets].required`, `[policy]`, one
`[cores.<name>]` block declaring each core's `meta` schema, and one `[<language>]` block
per backend naming the package, the source root, and the language's own knobs.

**W14.** `[secrets].required` names environment variables and nothing else. No value, no
path, no vault. `.env` is git-ignored and is the only place a developer keeps one locally.
`truewire standards` fails the build when a recording contains a value that any of those
variables held at capture time.

**W15.** `[policy]` declares what the client is allowed to do on its own behalf: the
request rate it paces itself to, whether it retries, and which endpoints are refused
outright. A trading client with a withdrawal endpoint in its spec declares the refusal
here, so that the refusal is generated code rather than a warning in a README.

## Generated state

**W16.** `.truewire/codegen/<language>.json` lists every file that codegen wrote. It is
committed, because it is how `--delete` knows what is an orphan and how `--check` knows
whether the tree is current. `.truewire/cache/` is ignored and holds fetched schemas.

**W17.** Schemas are served from `truewire.dev` and cached locally. They are not vendored
into the repository: a vendored schema is a schema that is one release out of date, and
every editor already resolves a URL.

## CI

**W18.** `.github/workflows/` runs the same commands the agent runs, in the same order, and
adds nothing of its own. A gate that exists only in CI is a gate the agent cannot satisfy
before pushing, and it will be discovered as a red build every time.
