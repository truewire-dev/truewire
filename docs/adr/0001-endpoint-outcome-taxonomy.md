# ADR 0001: Every documented endpoint resolves to verified, unverified with a reason, or excluded

- Status: accepted
- Date: 2026-08-05 (carried over 2026-09-06)

## Context

An API's documented surface is never uniformly buildable. Some endpoints can be called and a real response recorded. Some cannot be called from a given build: the credential is the wrong tier, the account has no state that would exercise the endpoint, a no-writes rule forbids the call. Some should not be built at all: the endpoint is deprecated upstream, or it speaks a binary protocol the client does not.

Early builds conflated the second and third cases. An endpoint the credential could not reach was marked excluded, the same as one upstream had actually removed. That silently drops a real, documented, callable endpoint from the client. A `place_order` that does not exist because nobody had a funded account handy is not a safer client, only a less useful one, and nothing about "excluded" distinguishes it from "removed in 2023."

## Decision

Every endpoint an API documents resolves to exactly one of three outcomes:

| Outcome | When | What it produces |
| --- | --- | --- |
| **verified** | the call was made and a real response recorded | spec, examples, generated method, docs |
| **unverified** | the endpoint cannot be called from here | spec, generated method, docs, and an `unverified: {reason, detail}` declaration on the endpoint |
| **excluded** | there is nothing to build | nothing |

**Unverified is the default** for anything uncallable, never a fallback to excluded. A credential that cannot reach a surface, an account state the build cannot create, a program the account is not enrolled in: all of those get spec'd from the documentation, ship a generated method, and declare why no example exists. `reason` is a closed set (`missing_credentials`, `program_enrollment`, `unsafe`, `requires_state`, `runtime_error`); `detail` is prose.

**Exclusion requires that there is nothing to build.** Three reasons only: upstream deprecated it or never documented it; it is a binary surface the client does not speak; it is effectful under an explicit no-writes constraint for this build. "The credential can't reach it" is not an exclusion reason. It is unverified.

`truewire examples --require-verified` enforces the taxonomy: every endpoint has a paired example or an `unverified` declaration, and a declaration left in place after an example was recorded fails the check as stale.

## Consequences

Every endpoint is accounted for in exactly one bucket, so a client's surface cannot quietly shrink to whatever the build happened to have credentials for. Reporting has to state the denominator, "18 of 147 endpoints verified," because a bare count no longer distinguishes a small API from a mostly-unverified one.

The taxonomy is a contract about the client's shape, independent of the process that builds it. It holds whether a human writes the spec by hand, a coding agent drives it, or an OpenAPI import seeds it.

Left open: nothing checks that every endpoint the upstream documents actually landed in one of the three buckets. That is still a discipline the author holds, not an automated invariant.
