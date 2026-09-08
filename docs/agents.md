# Truewire for coding agents

Truewire is built to be driven by a coding agent. Every step of a project ends in a command whose exit code says whether the step is done, so an agent never decides that for itself, and the two things an agent cannot be trusted to invent, the shape of a request and the shape of a response, come from recordings of the live API. This page says what to hand an agent, which commands gate its work, and gives three prompts to paste.

## What to hand an agent

**The six skills.** [`.agents/skills/`](../.agents/skills/README.md) holds one `SKILL.md` per step, in the format Claude Code, Cursor and Codex load on demand. Point the agent at the directory, or copy it into the project's own skills folder.

| Skill | One line |
| --- | --- |
| `discover` | Turn a docs URL into an endpoint inventory (`spec/inventory.md`) a human reviews once. |
| `spec` | Write `endpoint.json` per endpoint, routers and shared schemas from the inventory and the docs, until `truewire check` passes. |
| `core` | Adapt the hand-written core (transport, auth, envelope, errors) until one `truewire capture` succeeds against the live API. |
| `implement` | Record an example for every endpoint, generate the client, and make its tests pass against `truewire mock`. |
| `docs` | Write a README whose every code block type-checks against the generated client, verified by `truewire docs check`. |
| `review` | Review the finished project against `docs/standards.md` and every gate before it ships. |

**The templates.** `truewire init <name> --template bearer|hmac|jsonrpc|ws` writes a working core for the API's shape; [docs/cores.md](cores.md) says what each does and what to change. An agent that starts from the nearest template edits one file instead of inventing a transport.

**The references.** [docs/spec/authoring.md](spec/authoring.md) is the rule set `truewire check` enforces, with a wrong and a right example per rule. [docs/standards.md](standards.md) is the bar `truewire standards` and the review skill hold the project to. [docs/truewire-toml.md](truewire-toml.md) is the project file. `examples/github` and `examples/kraken` are finished projects to copy from.

**Credentials, as environment variables.** The core reads none of them; the caller passes them to `new(...)`, and `truewire capture --new api_key=$KEY` does the same on the command line. `[secrets].required` in `truewire.toml` names the variables so `truewire standards` can flag a leaked value.

## The gates

Each command below prints a plain result and exits non-zero on failure. An agent runs them in this order and stops at the first red one.

| Command | Passes when |
| --- | --- |
| `truewire check` | Every `endpoint.json`, router and recorded example satisfies the authoring rules and its own schema. |
| `truewire generate python` | The client renders, formats, and type-checks under pyright when the project has a `pyrightconfig.json`. |
| `truewire examples --require-verified` | Every endpoint has a recorded example or an `unverified` block with a reason. |
| `truewire surface` | Every endpoint in the spec is reachable as a method on the generated client. |
| `truewire standards` | Docstrings, duplicate schemas, secret placeholders, router coverage and the no-`__call__` rule hold. |
| `truewire docs check` | Every code block in the README and docs type-checks against the generated package. |
| `python -m pytest` | The project's tests pass against `truewire mock`, which replays the recorded examples over HTTP and WebSocket. |

`truewire generate python --check` reports whether generated files are current without rewriting them, for CI.

## `truewire capture`

`truewire capture <group.name> --request '{...}'` calls one endpoint against the live API through the project's own generated client and core, and writes the request and the response as an example pair beside the endpoint's spec. The pair carries what the API actually sent, before the core unwrapped anything, and `truewire check` validates it against the schema on the spot. `--id` names the pair, `--scrub KEY` replaces a secret in the response with `REDACTED_KEY`, and `--new key=value` passes constructor arguments to `new(...)`. A non-2xx answer is printed and nothing is written: examples record success, and errors belong to the core.

This is how an agent turns a guess into evidence. It writes the spec from the docs, captures, and the check either accepts the recording or names the field the docs got wrong. An endpoint it cannot call (wrong credential tier, moves money, needs state) gets an `unverified` block with a reason from the closed set, never a skip.

## `truewire mcp` versus the CLI

The two serve different agents.

`truewire mcp --project <dir> --new base_url=...` serves a generated client's endpoints to an agent that **calls the API**. One MCP tool per `rpc` endpoint, named after its function path (`pets.get_pet` becomes `pets_get_pet`), taking the endpoint's own request schema, answered through the generated client so the response is validated the same way it is for any caller. `--list` prints the tools. Install with `pip install 'truewire[mcp]'`. Use it when the job is "look up the balance", "list the open orders", "fetch the last ten commits".

The CLI plus the skills are for an agent that **builds the spec and the client**. It reads docs, writes `endpoint.json`, adapts the core, captures, generates, and runs the gates above. Use it when the job is "make me a typed client for this API".

The first needs a finished project. The second produces one.

## Prompts

Each prompt assumes the repository's skills are loadable and `truewire` is installed in the active environment. Replace the bracketed parts.

### Build a core for API X from these docs

```text
Build a Truewire project for [API name] from its documentation at [docs URL].

Load the truewire-discover, truewire-spec, truewire-core and truewire-implement skills from .agents/skills and follow them in that order. Each skill ends with a command whose exit code decides whether the step is done; do not move on while it fails.

Start with `truewire init [package] --template [bearer|hmac|jsonrpc|ws] --base-url [base URL]`. Pick the template nearest the API's auth and transport as docs/cores.md describes them, then adapt only src/[package]/core/ to the API: headers every call needs, the signing recipe, the envelope, the error mapping. Do not edit generated files.

Credentials are in the environment variables [NAMES]; pass them with `--new` and `new(...)`, never read them in the core, never write a value into the tree. Declare every transport-injected request field under `redacted` on the endpoints that carry it.

Prove the core with one `truewire capture` of the simplest public endpoint. Then record every endpoint you can, declare `unverified` with a reason for every one you cannot, and stop when `truewire check`, `truewire generate python`, `truewire examples --require-verified`, `truewire surface` and the tests against `truewire mock` all pass. Report which endpoints are verified, which are unverified and why, and every place the docs disagreed with a recording.
```

### Add these endpoints to an existing project

```text
Add the following endpoints to the Truewire project in [path]: [list of endpoints, with a docs link each].

Load the truewire-spec and truewire-implement skills from .agents/skills. The project's core in src/[package]/core/ already reaches the API; do not change it unless one of these endpoints needs a transport behavior it lacks, and say so if it does.

For each endpoint: write spec/endpoints/[group]/[name]/endpoint.json following docs/spec/authoring.md (titled schemas, enums for closed sets, real timestamp formats, `envelope.payload` where the core unwraps, `pagination` where the API pages), record an example with `truewire capture [group.name] --request '{...}'` using the credentials in [NAMES], or declare `unverified` with a reason from the closed set, and add a test against `truewire mock`.

Stop when `truewire check`, `truewire generate python`, `truewire examples --require-verified`, `truewire surface`, `truewire standards` and the tests all pass. Do not touch endpoints outside the list. Report the new methods and their verification status.
```

### Record and verify everything you can

```text
Bring the Truewire project in [path] to full verified coverage.

Run `truewire examples --require-verified --verbose` and take the endpoints it lists under `No example files:`. For each one, call it with `truewire capture [group.name] --request '{...}'` using the credentials in [NAMES] and parameters taken from the docs or from an existing recording of a related endpoint; scrub secrets in responses with `--scrub`. Where a capture fails, fix whichever side is wrong: the spec when the recording is right and the schema is not, the core when the request never reached the API correctly. An endpoint you cannot call gets an `unverified` block with a reason from the closed set and a detail that says what you tried and what the API answered.

Never call an endpoint that moves money or changes account state; declare it `unverified` with reason `unsafe`. Never write a credential into the tree.

Stop when `truewire check`, `truewire examples --require-verified`, `truewire standards` and the tests against `truewire mock` pass. Report every endpoint by status, and every disagreement between the docs and the wire you added to an endpoint's `notes`.
```
