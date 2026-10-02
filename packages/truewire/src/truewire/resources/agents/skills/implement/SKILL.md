---
name: truewire-implement
description: Record real examples for every endpoint of a Truewire project, generate the client and make its tests pass against the mock server, until `truewire examples --require-verified`, `truewire surface` and the test suite are green. Use after `truewire-core`.
---

# Implement: recordings, client, tests

## Goal

Every endpoint verified by a recording or declared `unverified` with a reason, a generated
client that type-checks, and a `test/` directory whose tests replay every recording through
the real client against `truewire mock`, with no network and no credentials.

## Steps

1. **Record, one endpoint at a time.** For each endpoint the inventory marked `record:
   yes`: `truewire capture <group.name> --request '{...}' --id <id> -d "<what this
   shows>" [--new api_key=$KEY] [--scrub <secret field>]`. The request is API-named
   parameters as JSON; the command calls through your client, writes
   `examples/<id>.request.json` and `.response.json`, and runs `check` on the pair. The
   `--id` is also the test id (`group.name[id]`); ids are unique per endpoint.
   - **A paginated endpoint gets a real walk, with the page index explicit on every
     request, `"page": 1` included.** The generated `_paged` walk sends the index on its
     first call, and the mock matches the whole request; a `page1` recorded without
     `page` answers 422 inside the walk test. Record `page1`, `page2`, ... until the
     declared terminator fires (a short page, an empty page, an absent cursor). Pin the
     walk to a fixed start where the API allows it (`sha`, `since`, an id) so re-recording
     yields the same pages. Probe with a small `per_page` on a small collection.
   - A non-2xx answer is not recorded. Fix the request, the core or the spec, then retry.
     If the endpoint truly cannot be called from here, declare `unverified` with the
     reason and what was tried.
   - When a recording contradicts the schema, `check` fails on the pair: change the
     schema to match the wire and note the disagreement with the docs in `notes`.
   - WebSocket streams are recorded by hand today: subscribe with the core's socket
     client, save the frames as `<id>.parameters.json` / `<id>.messages.json` under the
     stream's `examples/`; `examples/kraken` in the toolchain repository shows every file
     shape.
2. **Generate.** `truewire generate python`; pyright (configured in the core skill) must
   report zero errors. `truewire generate python --check` afterwards proves spec and
   package agree.
3. **Tests.** pytest with `pytest-asyncio` (mark async tests `@pytest.mark.asyncio`, or
   set `asyncio_mode = auto`). `test/conftest.py`:
   ```python
   from pathlib import Path
   import pytest
   from <pkg> import <Name>                      # the class truewire init named
   from truewire.mock import running_mock_servers

   @pytest.fixture
   def mock_servers():
     with running_mock_servers(Path(__file__).resolve().parents[1]) as servers:
       yield servers                             # servers.http_base_url, servers.ws_server

   @pytest.fixture
   def client(mock_servers):
     return <Name>.new(base_url=mock_servers.http_base_url, validate=True)
   ```
   `test/test_examples_replay.py`: `test_examples_replay =
   truewire.testing.build_http_replay_test(Path(__file__).resolve().parents[1])` (and
   `build_ws_replay_test` for streams). One test per `_paged` walk, using the
   `PaginatedResponse` contract: `async for page in walk` yields one page's rows as a
   `Sequence` and **skips empty pages**; `walk.pages()` includes them; `await walk`
   returns every row flattened as a `Sequence`, not a `list`. Assert the page lengths the
   recordings show (`[2, 1]` for a short-page end; `[2, 2]` for an empty-page end, with
   three requests made).
   `PYTHONPATH=src pytest -q` must pass. A 422 from the mock means the client sent a
   request no recording matches (usually a missing `page`); a 409 means two recordings
   match one request: collapse them.
4. **Gates.** `truewire examples --require-verified` (every endpoint has a pair or a
   reason; read the `Coverage (paired examples)` line, the `Public`/`Authed` split is
   informational), `truewire surface` (every spec produces a callable), `truewire check`.

## Done when

- `truewire check`, `truewire examples --require-verified` and `truewire surface` exit 0.
- `PYTHONPATH=src pytest -q` passes with the mock, pyright reports 0 errors,
  `truewire generate python --check` reports no drift.
- `spec/inventory.md`'s `record` column matches reality: every `yes` has examples, every
  reason appears as an `unverified` declaration.

## Do not

- Do not write an example by hand from the docs. If it was not recorded, it is not an
  example; the OpenAPI importer's document examples are the only exception, and they are
  labelled as such in `notes`.
- Do not edit generated files; change the spec or the core and regenerate.
- Do not commit a recording that contains a credential; `--scrub` it, and grep the
  recordings for the API's token prefixes from the inventory's `## Transport`.
