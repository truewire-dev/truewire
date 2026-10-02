# ADR 0018: A request field minted per call is ignored by a located path under `match.ignore`

- Status: accepted
- Date: 2026-09-15

## Context

ADR 0007 gave the mock `redacted`: flat key names stripped from a request before it is compared with a recording. Flat names were chosen because the comparison already knows which substructure it is looking at, and that covered every case at the time: a signed query string (`signature`, `timestamp`, `recvWindow`), a body key, a named-object JSON-RPC `params` entry.

It does not cover a WebSocket login. Bitget's is `{"op": "login", "args": [{"apiKey", "passphrase", "timestamp", "sign"}]}`: the per-connection `timestamp` and `sign` live inside an object inside a positional array. `redacted` never reaches into a positional `params`, because a bare list element has no name. A recording necessarily holds a stale signature, so no recorded login could ever match. Every private channel that logs in first could not replay: 9 Python cases were strict `xfail` in `clients/bitget`, and the TypeScript and Go ports skipped the same channels. The same shape (a signature nested in a frame, not at its top level) is common among exchange WebSocket logins and signed JSON-RPC bodies.

Stretching `redacted` to accept paths was considered and rejected. A flat name like `ids[]` or `a.b` is a legitimate query-parameter name, so one string could mean either a name or a path. Stripping a name at every depth was also rejected: it cannot tell a volatile `sign` from a same-named field that should still be compared.

A per-example `volatile` list was rejected too. Which fields a transport mints is a property of the endpoint's signing, not of one recording, just as `redacted` and `envelope` are declared per endpoint.

## Decision

An endpoint declares `match: {ignore: [<path>, ...]}`, a sibling of `redacted` and `envelope`. Each entry is a path in the response-path grammar (dotted keys and bracket indices, `[-1]` for the last element; no `$` root, wildcard or slice), rooted at the whole request value the mock compares: the parsed WebSocket frame, or the parsed HTTP JSON body. The addressed field is dropped from both the real request and the recording before any comparison, including selector and `params` extraction: a dict key is removed, and a list element is blanked to `null` so the other paths' indices keep naming the elements they were written for (Bybit's `{"op": "auth", "args": [apiKey, expires, signature]}` ignores `args[1]` and `args[2]`). A path that does not resolve on one side is skipped, so a keyed field need not be recorded at all. `redacted` then applies as before.

`match` is a block rather than a bare list so a later matching rule has a place to go without another top-level key.

A query item or header is flat, so `redacted` remains the declaration for those. The mock never compares headers.

As with `redacted`, `match.ignore` only widens what the mock accepts. It is never read when an example is replayed through the real client, and uniqueness checking (ADR 0007) runs after it: two recordings that differ only in an ignored field are ambiguous.

## Consequences

A client whose login or signed body nests its volatile fields declares their paths on that endpoint and needs no change to the mock or to any language's replay helpers. `@truewire/testing`, `twtest` and `truewire-testing` all run `truewire mock` as a child process and pick the rule up unchanged. Private stream replays that previously had to be skipped can run.

A frame with no JSON-RPC `method` must also declare its selector (`envelope.selector`, `envelope.params`) for `match_rpc` to consider it at all. `match.ignore` does not remove that need.

Two endpoints recording the same login frame on one mock (Bitget's classic and UTA logins share a construction and a mock URL) would tie. The recording goes on one of them, and the other is answered from it.

A field whose position varies (`args[*]`) cannot be named. No wildcard is admitted, for the same reason the response-path grammar refuses one; it waits for a real case.
