"""
Exercise the shared mock server (`truewire.mock`) against synthetic fixtures.

This suite used to load real client specs by name -- mexc, dydx and others -- and assert
against those clients' recorded examples, endpoint function names and paths. That coupled
a shared-library suite to the contents of individual clients: a client rebuild that changed
which examples existed, or renamed a function, broke tests that were never about that
client. It happened three times in two days, most recently when dydx was rebuilt as a
REST-only first slice and its websocket examples -- the ones four tests here quietly
depended on -- disappeared with it.

Every fixture below lives at `fixtures/mock_server/` and is owned by this suite alone.
`truewire.mock` accepts an explicit `root=` alongside the usual client name (see
`load_http_examples`, `load_ws_examples` and the constructors and servers built on top
of them), the same way `truewire.cli`'s `spec`/`test` commands already accept a `--path`
override alongside `client`. No client rebuild can touch this suite's fixtures, and no
change here can touch a client's.

One test moved the other way. `test_mexc_protobuf_fixture_decodes_with_client_proto` was
never about the mock server: it decoded a recorded frame with mexc's own generated
`PushDataV3ApiWrapper`, so its subject is mexc's protobuf definitions, not this module.
It now lives in `clients/mexc/test/test_streams.py`.
"""

import asyncio
import base64
import json
from pathlib import Path

import pytest
import websockets

from truewire.mock import (
  AMBIGUOUS_PARAM_STATUS,
  EXPECTED_PARAM_STATUS,
  AmbiguousRequestMatch,
  AmbiguousSubscriptionMatch,
  MockRegistry,
  UnexpectedRequestParameters,
  UnexpectedSubscriptionParameters,
  WsMockRegistry,
  load_ws_examples,
  query_matches,
  running_mock_servers,
  running_server,
  running_ws_server,
)
from truewire.spec import load_ws_binary_frames


CLIENT = 'synthetic-fixture'
"""Label passed to `truewire.mock` constructors; not a real client under `clients/`."""

ROOT = Path(__file__).resolve().parent / 'fixtures' / 'mock_server'
"""Spec root this suite owns. See the module docstring."""

PROTOBUF_ENDPOINT = ROOT / 'spec' / 'endpoints' / 'ws' / 'protobuf_feed'
"""Endpoint directory backing the protobuf sidecar fixture."""

PROTOBUF_FRAME = b'synthetic-protobuf-frame\x00\x01\x02'
"""Raw bytes encoded in `protobuf_feed/examples/doc.messages.protobuf.json`."""

VENUE_ROOT = Path(__file__).resolve().parent / 'fixtures' / 'mock_server_venue'
"""Spec root whose examples record the venue's own subscribe frames under `payload`.

Kept apart from `ROOT` so the two websocket dialects stay separable: the candidate lists
in `ROOT`'s `unexpected_parameters` assertions would otherwise pick up every payload
example in the root as well.
"""

VENUE_PROTOBUF_FRAME = b'synthetic-venue-frame\x00\x01\x02'
"""Raw bytes encoded in `venue_ticks_pb/examples/doc.messages.protobuf.json`."""

ENVELOPE_CLIENT = 'synthetic-fixture-enveloped'
"""Label passed to `truewire.mock` constructors; not a real client under `clients/`."""

ENVELOPE_ROOT = Path(__file__).resolve().parent / 'fixtures' / 'mock_server_enveloped'
"""Spec root whose endpoints declare their own `envelope` field. See the module docstring."""

COMMAND_DIALECT_CLIENT = 'synthetic-fixture-command-dialect'
"""Label passed to `truewire.mock` constructors; not a real client under `clients/`."""

COMMAND_DIALECT_ROOT = (
  Path(__file__).resolve().parent / 'fixtures' / 'mock_server_command_dialect'
)
"""Spec root with one `kind: 'rpc'` WS example whose own recorded payload has no `method`
field -- a bit2me-style `{'event': 'add-order', ...}` command. Regression fixture for
`match_rpc` false-matching a frame that lacks `method` against an example that also lacks
it, purely because both compare equal to `None`. See the module docstring.
"""

CHANNEL_DIALECT_CLIENT = 'synthetic-fixture-channel-dialect'
"""Label passed to `truewire.mock` constructors; not a real client under `clients/`."""

CHANNEL_DIALECT_ROOT = (
  Path(__file__).resolve().parent / 'fixtures' / 'mock_server_channel_dialect'
)
"""Spec root with two `kind: 'stream'` endpoints (`streams.ticker`, `streams.level1`)
declaring `envelope.channel` -- kucoin's real shape: subscribe frames name their channel
under `topic`, not the mock's generic `{type, channel}` shape, and no example records the
frame verbatim under `payload`, only `parameters`. Regression fixture for `match_subscribe`
routing on `envelope.channel`, compared against the parameter-interpolated endpoint channel,
instead of finding zero candidates and reporting `unknown_subscription` for every subscribe.
"""

SUBSCRIBE_CONFLICT_CLIENT = 'synthetic-fixture-subscribe-conflict'
"""Label passed to `truewire.mock` constructors; not a real client under `clients/`."""

SUBSCRIBE_CONFLICT_ROOT = (
  Path(__file__).resolve().parent / 'fixtures' / 'mock_server_subscribe_conflict'
)
"""Spec root with two declared-channel (`envelope.channel`) endpoints on the same channel --
one recording no `payload` (channel-identity-only), one recording the exact venue frame.
Regression fixture for the uniqueness rule Marcel confirmed: a channel-identity-only example
matching alongside a payload-specific sibling on the same channel is `AmbiguousSubscriptionMatch`,
not 'the specific one wins'.
"""

RPC_DIALECT_CLIENT = 'synthetic-fixture-rpc-dialect'
"""Label passed to `truewire.mock` constructors; not a real client under `clients/`."""

RPC_DIALECT_ROOT = (
  Path(__file__).resolve().parent / 'fixtures' / 'mock_server_rpc_dialect'
)
"""Spec root exercising `match_rpc`'s uniqueness rewrite: a genuine zero-arg command
(`ws/ping`, no recorded `payload` at all) and a named-object command whose venue injects a
monotonic `nonce` into every call (`ws/get_stuff`, `redacted: ['nonce']`, two examples
differing only in that nonce).
"""

RPC_SUBSCRIBE_CONFLICT_CLIENT = 'synthetic-fixture-rpc-subscribe-conflict'
"""Label passed to `truewire.mock` constructors; not a real client under `clients/`."""

RPC_SUBSCRIBE_CONFLICT_ROOT = (
  Path(__file__).resolve().parent / 'fixtures' / 'mock_server_rpc_subscribe_conflict'
)
"""Spec root, deribit-shaped: a generic multiplexed subscribe RPC (`ws/subscribe`, `kind:
'rpc'`, method `acme/subscribe`, one recorded example subscribing to `ticker.ACME`) alongside
a separate declared-channel stream endpoint (`ws/book`, `kind: 'stream'`, `envelope.channel`)
reached through that same RPC method for a *different* channel (`book.ACME`). Regression
fixture for the server loop's `match_rpc`/`match_subscribe` precedence: a live subscribe for
`book.ACME` is recognized by `match_rpc` (method matches) but mismatches its one example's
params, and must fall through to `match_subscribe`'s declared-channel routing rather than the
handler raising `UnexpectedSubscriptionParameters` immediately.
"""

PUSH_CLIENT = 'synthetic-fixture-push'
"""Label passed to `truewire.mock` constructors; not a real client under `clients/`."""

PUSH_ROOT = Path(__file__).resolve().parent / 'fixtures' / 'mock_server_push'
"""Spec root exercising `docs/spec/authoring.md` rule 11's `push` declaration: `ws/connect_push`
(`push: {trigger: 'connect'}`, no subscribe frame at all -- binance's private user-data
streams) and `ws/after_rpc_push` (`push: {trigger: 'after_rpc', method: 'login'}`, gated on
the sibling `ws/login` zero-arg RPC example -- mexc's `futures.streams.user.*` shape).
"""

WS_RECV_TIMEOUT = 5
"""Seconds to wait for a frame the mock is expected to send."""


async def recv_frame(ws: websockets.ClientConnection):
  """Receive one frame, failing on timeout rather than blocking the suite.

  A mock that answers a subscribe with a single `error` frame sends one frame where a
  reply plus a message are expected, so an unguarded second `recv()` hangs forever
  instead of reporting which frame never arrived.
  """
  async with asyncio.timeout(WS_RECV_TIMEOUT):
    return await ws.recv()


NO_FRAME_TIMEOUT = 0.2
"""Seconds to wait, at most, when asserting no further frame arrives -- short because a
local mock server that has nothing left to send answers immediately, and a genuine bug
(a stray replayed frame) also arrives immediately, so this only pads for scheduling."""


async def assert_no_frame(ws: websockets.ClientConnection):
  """Assert the connection sends nothing further within `NO_FRAME_TIMEOUT`.

  A timeout is the pass condition: a mock that wrongly keeps a subscription active (or
  hijacks an unsubscribe into the subscribe branch) sends one more replayed message here
  instead of nothing.
  """
  with pytest.raises(asyncio.TimeoutError):
    async with asyncio.timeout(NO_FRAME_TIMEOUT):
      await ws.recv()


def test_protobuf_fixture_is_encoded_as_declared():
  """Guard the fixture itself: a hand-edited base64 blob is easy to get wrong quietly."""
  path = PROTOBUF_ENDPOINT / 'examples' / 'doc.messages.protobuf.json'
  frames = json.loads(path.read_text())
  assert base64.b64decode(frames[0]['data']) == PROTOBUF_FRAME


def test_http_registry_matches_rest_example():
  """A REST body is matched by comparing the full JSON body, not select fields."""
  registry = MockRegistry(root=ROOT)

  match = registry.match(
    'POST',
    '/orders',
    [],
    {
      'side': 'sell',
      'symbol': 'ACME/USD',
      'price': '100.00',
      'amount': '0.5',
      'clientOrderId': 'synthetic-rest-1',
    },
  )

  assert match is not None
  assert match.endpoint_function == 'orders.create'
  assert match.example_id == 'open'


def test_http_registry_matches_path_parameter_example():
  """A path parameter is read out of the route, not the query string or body."""
  registry = MockRegistry(root=ROOT)

  match = registry.match('GET', '/widgets/w1', [], None)

  assert match is not None
  assert match.endpoint_function == 'widgets.get'
  assert match.example_id == 'quiet'


def test_http_registry_matches_query_parameter_example():
  """Two examples on the same route are told apart by their query string alone."""
  registry = MockRegistry(root=ROOT)

  match = registry.match('GET', '/widgets/w1', [('verbose', 'true')], None)

  assert match is not None
  assert match.endpoint_function == 'widgets.get'
  assert match.example_id == 'verbose'


def test_http_registry_ignores_a_credential_carried_in_the_query_string():
  """A venue authenticating on a query parameter still matches its recorded examples.

  The example cannot record the credential, so every replayed request carries one query
  item more than the example did and matching used to fail on the count alone. The
  endpoint's own `auth` block names the parameter; the matcher skips it.
  """
  registry = MockRegistry(root=ROOT)

  match = registry.match(
    'GET', '/keyed-widgets', [('id', 'k1'), ('apikey', 'secret')], None
  )

  assert match is not None
  assert match.endpoint_function == 'widgets.keyed'
  assert match.example_id == 'default'


def test_http_registry_ignores_only_the_declared_credential_parameter():
  """Skipping the credential is not a licence to ignore every unrecorded query item."""
  registry = MockRegistry(root=ROOT)

  with pytest.raises(UnexpectedRequestParameters):
    registry.match('GET', '/keyed-widgets', [('id', 'k1'), ('cursor', 'p2')], None)


def test_http_registry_ignores_a_redacted_key_thats_also_a_genuine_query_parameter():
  """A `redacted` key that's also legitimately recorded in `parameters` still matches a
  live request carrying a different value for it -- `docs/spec/spec.md`'s Declared
  Redaction section explicitly permits this ("a redacted key that's also a genuine
  bindable parameter still needs its own normal `parameters`/`payload` entry").

  Regression for the bug `ef422ccc` introduced: it stripped `redacted_names` from the
  actual query only, never from `example.expected_query` -- for a key recorded on both
  sides (this fixture's `token`, unlike `keyed-widgets`' `apikey`, which the example never
  records at all), the actual side would drop it while the expected side always kept it,
  so the two sides could never compare equal and the example became permanently
  unmatchable. Passing a token that differs from the recorded placeholder proves the key
  is genuinely ignored on both sides, not just excluded from one.
  """
  registry = MockRegistry(root=ROOT)

  match = registry.match(
    'GET',
    '/redacted-widgets',
    [('id', 'r1'), ('token', 'a-genuinely-live-token-value')],
    None,
  )

  assert match is not None
  assert match.endpoint_function == 'widgets.redacted_query'
  assert match.example_id == 'default'


def test_http_registry_matches_single_positional_rpc_example():
  """A JSON-RPC method with one positional parameter is not itself a list.

  The matcher wraps a non-list recorded payload in a single-element list before
  comparing it to the request's `params` array; this is the case that exercises
  that wrap.
  """
  registry = MockRegistry(root=ROOT)

  match = registry.match(
    'POST',
    '/',
    [],
    {
      'jsonrpc': '2.0',
      'id': 1,
      'method': 'acme_getBalance',
      'params': ['0xACME0000000000000000000000000000000001'],
    },
  )

  assert match is not None
  assert match.endpoint_function == 'acme.get_balance'
  assert match.rpc_method == 'acme_getBalance'


def test_http_registry_matches_nary_positional_rpc_example():
  """A JSON-RPC method with several positional parameters compares the whole array."""
  registry = MockRegistry(root=ROOT)

  match = registry.match(
    'POST',
    '/',
    [],
    {
      'jsonrpc': '2.0',
      'id': 1,
      'method': 'acme_getBlockByNumber',
      'params': ['0x10', True],
    },
  )

  assert match is not None
  assert match.endpoint_function == 'acme.get_block_by_number'
  assert match.rpc_method == 'acme_getBlockByNumber'


def test_http_registry_matches_new_shape_flat_object_rpc_example():
  """A new-shape (`request`/`response`, no `openapi`) JSON-RPC operation with a flat,
  multi-property `request` object -- the default JSON-RPC wire shape (`core` wraps the
  whole dict in a one-element `params` array) -- must match too, not just the legacy
  `openapi`-shaped case the two tests above already cover.

  Regression test for a real bug (common/lib commit 111f62df4, found running alchemy's
  own generic example replay: 11 of 70 examples 422'd with `unexpected_parameters`, all
  JSON-RPC): `_mock_http_example`'s new-shape branch unconditionally set `expected_body =
  None` for a flat-object request, on the assumption every new-shape request splits
  cleanly into query-string vs body the way a REST operation does -- but a JSON-RPC
  operation has no query string at all, so no JSON-RPC endpoint with a flat `request`
  object could ever match here; only a titled-`anyOf` or parameterless RPC request
  happened to reach the one branch that did set `expected_body`. `rpc/get_flat_
  allowance/` (`fixtures/mock_server/`) is this exact shape.
  """
  registry = MockRegistry(root=ROOT)

  match = registry.match(
    'POST',
    '/',
    [],
    {
      'jsonrpc': '2.0',
      'id': 1,
      'method': 'acme_getFlatAllowance',
      'params': [
        {
          'contract': '0xACME0000000000000000000000000000000002',
          'owner': '0xACME0000000000000000000000000000000003',
        }
      ],
    },
  )

  assert match is not None
  assert match.endpoint_function == 'acme.get_flat_allowance'
  assert match.rpc_method == 'acme_getFlatAllowance'


def test_http_registry_raises_for_unexpected_rest_parameters():
  """A body that matches no recorded example raises rather than 404ing silently."""
  registry = MockRegistry(root=ROOT)

  with pytest.raises(UnexpectedRequestParameters) as excinfo:
    registry.match(
      'POST',
      '/orders',
      [],
      {
        'side': 'sell',
        'symbol': 'ACME/USD',
        'price': '100.00',
        'amount': '999',
        'clientOrderId': 'synthetic-rest-1',
      },
    )

  assert excinfo.value.method == 'POST'
  assert excinfo.value.path == '/orders'
  assert excinfo.value.candidates[0].function == 'orders.create'


def test_http_registry_raises_ambiguous_match_when_redaction_collapses_two_examples():
  """Two recorded examples that only differ in a `redacted` field are ambiguous, not a match.

  `orders.create_signed` declares `redacted: ['nonce']` and records two examples with an
  identical `orderId` and no `nonce` at all (kraken's own convention -- a monotonic value
  with no fixed answer to capture). Before the body-first branch redacted the actual side,
  neither example matched a live body carrying a real nonce at all (`UnexpectedRequestParameters`);
  once redacted, both match equally, which is the new failure mode this exercises.
  """
  registry = MockRegistry(root=ROOT)

  with pytest.raises(AmbiguousRequestMatch) as excinfo:
    registry.match(
      'POST', '/signed-orders', [], {'orderId': 'dup-order', 'nonce': 'live-nonce-1'}
    )

  assert excinfo.value.method == 'POST'
  assert excinfo.value.path == '/signed-orders'
  assert len(excinfo.value.candidates) == 2
  assert {candidate.example_id for candidate in excinfo.value.candidates} == {'a', 'b'}


def test_http_registry_matches_a_redacted_key_thats_also_recorded_in_the_body():
  """A `redacted` key legitimately recorded in the body too is still ignored on both sides.

  `orders.create_signed`'s third example (`c`) records `nonce` in its own `payload` at
  whatever value was live during capture -- `docs/spec/spec.md`'s Declared Redaction section
  permits this explicitly. A real request carrying a *different* nonce must still match:
  stripping only the actual side (the body-first branch's original behavior) would force an
  exact compare on a value `redacted` says is never fixed, making this example permanently
  unmatchable the moment its own capture happened to include the key.
  """
  registry = MockRegistry(root=ROOT)

  match = registry.match(
    'POST',
    '/signed-orders',
    [],
    {'orderId': 'unique-order', 'nonce': 'a-completely-different-live-nonce'},
  )

  assert match is not None
  assert match.example_id == 'c'


def test_http_registry_matches_a_list_valued_body_field_pooled_from_parameters():
  """A list-valued `query`-role parameter recorded via `parameters` only (`widgets.list`
  declares no `requestBody` and no query-string parameter, so the pooling branch is what
  handles it) must be compared through the same wire-shaped expansion as the query side,
  not Python's own `repr` of the list -- `str(['w1', 'w2'])` can never equal what
  `_normalize_query` produces for the same value, which used to make this example
  permanently unmatchable the moment its one field held more than one item.
  """
  registry = MockRegistry(root=ROOT)

  match = registry.match('POST', '/list-widgets', [], {'ids': ['w1', 'w2']})

  assert match is not None
  assert match.endpoint_function == 'widgets.list'
  assert match.example_id == 'default'


def test_ws_registry_matches_subscription_example():
  """A subscribe message is matched against the recorded subscription parameters."""
  registry = WsMockRegistry(root=ROOT)

  match = registry.match_subscribe(
    {
      'type': 'subscribe',
      'channel': 'stream_trades',
      'id': 'ACME-1',
    }
  )

  assert match is not None
  assert match.example.function == 'streams.trades'
  assert match.example.example_id == 'w1'


def test_ws_loader_loads_protobuf_sidecar_frames():
  """The protobuf sidecar loader decodes base64 frames and wires them onto the example.

  This is the loader-level counterpart to the mexc test that decodes a real frame
  with a generated proto class -- that one is about mexc's protobuf definitions,
  this one is about `load_ws_binary_frames`/`load_ws_examples` picking the sidecar
  file up at all, which does not need a real venue to demonstrate.
  """
  path = PROTOBUF_ENDPOINT / 'examples' / 'doc.messages.protobuf.json'

  frames = load_ws_binary_frames(path)
  examples = [
    example
    for example in load_ws_examples(root=ROOT)
    if example.function == 'streams.ticks_protobuf'
  ]

  assert frames == [PROTOBUF_FRAME]
  assert examples[0].message_frames == frames


def test_ws_registry_raises_for_unexpected_subscription_parameters():
  """A subscribe message for a known channel but an unrecorded id raises."""
  registry = WsMockRegistry(root=ROOT)

  with pytest.raises(UnexpectedSubscriptionParameters) as excinfo:
    registry.match_subscribe(
      {
        'type': 'subscribe',
        'channel': 'stream_trades',
        'id': 'NOPE-1',
      }
    )

  assert excinfo.value.message['channel'] == 'stream_trades'
  assert excinfo.value.candidates[0].function == 'streams.trades'


def test_ws_registry_raises_ambiguous_match_for_a_channel_identity_sibling():
  """A channel-identity-only example does not win over a matching payload-specific sibling.

  `SUBSCRIBE_CONFLICT_ROOT` declares two endpoints on the same `envelope.channel`-routed
  channel: `orderbook_identity` records no `payload` at all, `orderbook_payload` records the
  exact venue frame sent below. Both are legitimate candidates once a live frame matches
  `orderbook_payload`'s recorded payload -- Marcel confirmed this is `AmbiguousSubscriptionMatch`,
  not 'the specific one wins', since silently preferring either would hide that the fixture
  (or a real spec) has two examples claiming the same wire call.
  """
  registry = WsMockRegistry(root=SUBSCRIBE_CONFLICT_ROOT)

  with pytest.raises(AmbiguousSubscriptionMatch) as excinfo:
    registry.match_subscribe(
      {
        'id': '9',
        'type': 'subscribe',
        'topic': 'orderbook',
        'privateChannel': False,
        'response': True,
      }
    )

  assert len(excinfo.value.candidates) == 2
  assert {candidate.function for candidate in excinfo.value.candidates} == {
    'streams.orderbook_identity',
    'streams.orderbook_payload',
  }


@pytest.mark.asyncio
async def test_combined_mock_servers_replay_http_and_ws():
  """Subscribe, receive a reply and a streamed update, then unsubscribe cleanly.

  This is the regression the suite exists to catch: a websocket handler bug once
  misrouted `unsubscribe` to the subscribe branch, and only a full subscribe/
  unsubscribe round trip like this one notices.
  """
  with running_mock_servers(root=ROOT) as servers:
    http_host, http_port = servers.http_server.server_address
    assert f'http://{http_host}:{http_port}' == servers.http_base_url
    assert servers.ws_server is not None

    async with websockets.connect(servers.ws_server.url) as ws:
      await ws.send(
        json.dumps(
          {
            'type': 'subscribe',
            'channel': 'stream_trades',
            'id': 'ACME-1',
          }
        )
      )
      reply = json.loads(await ws.recv())
      update = json.loads(await ws.recv())
      await ws.send(
        json.dumps(
          {
            'type': 'unsubscribe',
            'channel': 'stream_trades',
            'id': 'ACME-1',
          }
        )
      )
      unsubscribed = json.loads(await ws.recv())

  assert reply['type'] == 'subscribed'
  assert update['type'] == 'channel_data'
  assert unsubscribed['type'] == 'unsubscribed'


@pytest.mark.asyncio
async def test_ws_mock_uses_a_recorded_unsubscribe_reply_for_a_fallback_dialect_example():
  """A fallback-dialect (no `envelope.channel`) example with a recorded `unsubscribe_reply`
  gets that exact captured frame back on unsubscribe -- not its own subscribe `reply`, and
  not the generic synthesized `{'type': 'unsubscribed', 'connection_id', 'message_id', ...}`
  shape `_ws_unsubscribe_message` falls back to otherwise (exercised by
  `test_combined_mock_servers_replay_http_and_ws` above, on the sibling `ACME-1` example
  which records no `unsubscribe_reply`).

  This is the tier coinbase needed: its real unsubscribe ack reuses the exact envelope its
  subscribe ack uses, and coinbase declares no `envelope.channel` anywhere, so it could
  never reach the declared-channel reply-reuse tier either -- only a dedicated recorded
  `unsubscribe_reply` produces the right frame for that dialect. `ws/trades/examples/w2.*`
  is the fixture: same channel and endpoint as `ACME-1`, a different subscription id, its
  own recorded `reply` and a distinct recorded `unsubscribe_reply`.
  """
  with running_ws_server(root=ROOT) as server:
    async with websockets.connect(server.url) as ws:
      await ws.send(
        json.dumps({'type': 'subscribe', 'channel': 'stream_trades', 'id': 'ACME-2'})
      )
      reply = json.loads(await ws.recv())
      await ws.send(
        json.dumps({'type': 'unsubscribe', 'channel': 'stream_trades', 'id': 'ACME-2'})
      )
      unsubscribed = json.loads(await ws.recv())

  assert reply == {'type': 'subscribed', 'channel': 'stream_trades', 'id': 'ACME-2'}
  assert unsubscribed == {
    'ack': 'unsubscribed',
    'channel': 'stream_trades',
    'id': 'ACME-2',
  }
  assert unsubscribed != reply
  assert 'type' not in unsubscribed


@pytest.mark.asyncio
async def test_ws_mock_routes_an_unsubscribe_for_a_payload_only_dialect():
  """A venue whose subscribe/unsubscribe frames carry neither a top-level `type` key nor
  a `channel` key the mock's generic dialect understands -- bitget's `{"op": "subscribe",
  "args": [{"channel": ..., ...}]}` shape, `w3`'s fixture -- still gets a real unsubscribe
  ack, not a crash.

  Before `_match_active_subscription` learned to derive an unsubscribe frame from a
  recorded subscribe `payload` (swapping the one `"subscribe"` string for `"unsubscribe"`),
  an incoming `{"op": "unsubscribe", ...}` frame matched no `type`/`channel` signal, fell
  through to `match_subscribe`, found no *subscribe* example whose `op` also read
  `"unsubscribe"`, and raised `UnexpectedSubscriptionParameters` -- an unhandled exception
  inside the connection handler, which `websockets` reports as a 1011 (internal error)
  close, not a JSON error frame a real client's `parse_msg` could ever see.
  """
  with running_ws_server(root=ROOT) as server:
    async with websockets.connect(server.url) as ws:
      await ws.send(
        json.dumps({'op': 'subscribe', 'args': [{'channel': 'stream_trades', 'id': 'ACME-3'}]})
      )
      reply = json.loads(await ws.recv())
      await ws.send(
        json.dumps({'op': 'unsubscribe', 'args': [{'channel': 'stream_trades', 'id': 'ACME-3'}]})
      )
      unsubscribed = json.loads(await ws.recv())

  assert reply == {'event': 'subscribe', 'arg': {'channel': 'stream_trades', 'id': 'ACME-3'}}
  assert unsubscribed == {
    'event': 'unsubscribe',
    'arg': {'channel': 'stream_trades', 'id': 'ACME-3'},
  }


@pytest.mark.asyncio
async def test_ws_mock_returns_error_payload_for_unexpected_parameters():
  """Subscribing with an unrecorded id gets an `error` frame, not a silent drop."""
  with running_mock_servers(root=ROOT) as servers:
    assert servers.ws_server is not None

    async with websockets.connect(servers.ws_server.url) as ws:
      await ws.send(
        json.dumps(
          {
            'type': 'subscribe',
            'channel': 'stream_trades',
            'id': 'NOPE-1',
          }
        )
      )
      reply = json.loads(await ws.recv())

  assert reply['type'] == 'error'
  assert reply['message'] == 'unexpected_parameters'
  assert reply['details']['candidates'][0]['channel'] == 'stream_trades'


@pytest.mark.asyncio
async def test_ws_mock_replays_protobuf_binary_frames_verbatim():
  """A subscription backed by a protobuf sidecar gets the raw frame bytes, undecoded."""
  with running_ws_server(root=ROOT) as server:
    async with websockets.connect(server.url) as ws:
      await ws.send(json.dumps({'type': 'subscribe', 'channel': 'stream_ticks_pb'}))
      reply = json.loads(await ws.recv())
      frame = await ws.recv()

  assert reply['type'] == 'subscribed'
  assert isinstance(frame, bytes)
  assert frame == PROTOBUF_FRAME


@pytest.mark.asyncio
async def test_ws_mock_matches_a_venue_dialect_subscribe_frame():
  """A subscribe frame recorded under `payload` is matched as-is, without a `type` field.

  Examples may record the venue's own frame instead of parameters alone, and real venues
  do not send the mock's synthetic `{'type': 'subscribe', ...}` shape -- mexc futures
  sends `{'method': 'sub.depth', 'param': {...}}`. Dispatching on `type` alone dropped
  every such frame into the `unsupported_message_type` branch, and no fixture here used
  the venue dialect, so the whole shape went uncovered.
  """
  with running_ws_server(root=VENUE_ROOT) as server:
    async with websockets.connect(server.url) as ws:
      await ws.send(
        json.dumps({'method': 'sub.depth', 'param': {'symbol': 'ACME_USDT'}})
      )
      reply = json.loads(await recv_frame(ws))
      update = json.loads(await recv_frame(ws))

  assert reply['channel'] == 'rs.sub.depth'
  assert update['channel'] == 'push.depth'
  assert update['data']['version'] == 42


@pytest.mark.asyncio
async def test_ws_mock_replays_binary_frames_for_a_venue_dialect_subscribe():
  """The venue dialect reaches the binary replay path too, not just the JSON one.

  This is the mexc spot shape: a JSON subscribe frame, a JSON reply, then protobuf
  pushes. When the subscribe frame is not matched the mock answers with a text error,
  and a client decoding the push as protobuf fails on `str` instead of `bytes`.
  """
  with running_ws_server(root=VENUE_ROOT) as server:
    async with websockets.connect(server.url) as ws:
      await ws.send(
        json.dumps(
          {
            'method': 'SUBSCRIPTION',
            'params': ['acme@public.ticks.pb@ACMEUSDT'],
          }
        )
      )
      reply = json.loads(await recv_frame(ws))
      frame = await recv_frame(ws)

  assert reply['code'] == 0
  assert isinstance(frame, bytes)
  assert frame == VENUE_PROTOBUF_FRAME


@pytest.mark.asyncio
async def test_ws_mock_reports_unexpected_parameters_for_a_venue_dialect_frame():
  """An unrecorded venue frame still gets the diagnostic `error`, not a silent drop."""
  with running_ws_server(root=VENUE_ROOT) as server:
    async with websockets.connect(server.url) as ws:
      await ws.send(
        json.dumps({'method': 'sub.depth', 'param': {'symbol': 'NOPE_USDT'}})
      )
      reply = json.loads(await recv_frame(ws))

  assert reply['type'] == 'error'
  assert reply['message'] == 'unexpected_parameters'
  assert reply['details']['request']['param']['symbol'] == 'NOPE_USDT'


@pytest.mark.asyncio
async def test_ws_mock_matches_a_declared_channel_subscribe_frame_with_no_recorded_payload():
  """A subscribe frame is routed by `envelope.channel` alone when no example records `payload`.

  Regression for the kucoin bug: real subscribe frames are `{id, type, topic,
  privateChannel, response}`, with no `channel` key for the old `message.get('channel')`
  routing to key off, and no example recorded a raw `payload` either. Both endpoints in
  `CHANNEL_DIALECT_ROOT` declare `envelope.channel = 'topic'`; sending the real venue frame
  for `streams.ticker`'s channel must reach *that* endpoint's messages, not `streams.level1`'s
  or `unknown_subscription`. The mock also synthesizes the ack neither example records, and
  replays the stored message verbatim -- `doc.messages.json` stores the complete raw wire
  frame per entry (`docs/spec/spec.md`'s "Declared Envelope Extraction" section), the same
  as an `rpc` endpoint's `response.json`, so there is nothing left to reconstruct.
  """
  with running_ws_server(root=CHANNEL_DIALECT_ROOT) as server:
    async with websockets.connect(server.url) as ws:
      await ws.send(
        json.dumps(
          {
            'id': '2',
            'type': 'subscribe',
            'topic': '/market/ticker:ACME-USDT',
            'privateChannel': False,
            'response': True,
          }
        )
      )
      ack = json.loads(await recv_frame(ws))
      update = json.loads(await recv_frame(ws))

  assert ack == {'type': 'ack', 'id': '2'}
  assert update == {
    'type': 'message',
    'topic': '/market/ticker:ACME-USDT',
    'subject': '',
    'data': {'price': '100.5', 'size': '1'},
  }


@pytest.mark.asyncio
async def test_ws_mock_declared_channel_routing_disambiguates_between_endpoints():
  """Channel-identity routing picks the endpoint whose interpolated channel matches, not
  just any declared-channel example -- the whole value of a real phase-1 route."""
  with running_ws_server(root=CHANNEL_DIALECT_ROOT) as server:
    async with websockets.connect(server.url) as ws:
      await ws.send(
        json.dumps(
          {
            'id': '3',
            'type': 'subscribe',
            'topic': '/market/level1:ACME-USDT',
            'privateChannel': False,
            'response': True,
          }
        )
      )
      ack = json.loads(await recv_frame(ws))
      update = json.loads(await recv_frame(ws))

  assert ack == {'type': 'ack', 'id': '3'}
  assert update == {
    'type': 'message',
    'topic': '/market/level1:ACME-USDT',
    'subject': '',
    'data': {'bid': '100.4', 'ask': '100.6'},
  }


@pytest.mark.asyncio
async def test_ws_mock_acks_an_unsubscribe_for_a_declared_channel_example():
  """Unsubscribe gets the same synthesized `{type: 'ack'}` shape as subscribe -- kucoin's
  protocol has no separate 'unsubscribed' concept, and a client whose `parse_msg` only
  recognizes `ack`/`error` for a frame with no `topic` would otherwise wait forever on the
  mock's own generic `{'type': 'unsubscribed', ...}` reply. The active-subscription lookup
  also has to key off the parameter-interpolated channel, not the raw uninterpolated
  template `_ws_example_key` stored it under before this fix.
  """
  with running_ws_server(root=CHANNEL_DIALECT_ROOT) as server:
    async with websockets.connect(server.url) as ws:
      await ws.send(
        json.dumps(
          {
            'id': '5',
            'type': 'subscribe',
            'topic': '/market/ticker:ACME-USDT',
            'privateChannel': False,
            'response': True,
          }
        )
      )
      await recv_frame(ws)  # ack
      await recv_frame(ws)  # message
      await ws.send(
        json.dumps(
          {
            'id': '6',
            'type': 'unsubscribe',
            'topic': '/market/ticker:ACME-USDT',
            'privateChannel': False,
            'response': True,
          }
        )
      )
      reply = json.loads(await recv_frame(ws))

  assert reply == {'type': 'ack', 'id': '6'}


@pytest.mark.asyncio
async def test_ws_mock_unsubscribe_does_not_replay_a_stray_message_for_a_declared_channel():
  """A declared-channel unsubscribe stops the stream -- it does not get hijacked back into
  the subscribe branch and replay one more message.

  Regression for the bug `ef422ccc` introduced: the server loop's declared-channel
  precedence skip (checking `registry.declared_channel_candidates(message)` before ever
  trying `match_rpc`) is channel-identity-only, so it cannot tell a subscribe attempt from
  an unsubscribe one apart -- an unsubscribe frame for a channel with a declared-channel
  home got routed into the subscribe-handling branch too, which re-registered the
  subscription and replayed its recorded message again instead of tearing it down. After
  the unsubscribe ack, no further frame should arrive.
  """
  with running_ws_server(root=CHANNEL_DIALECT_ROOT) as server:
    async with websockets.connect(server.url) as ws:
      await ws.send(
        json.dumps(
          {
            'id': '7',
            'type': 'subscribe',
            'topic': '/market/ticker:ACME-USDT',
            'privateChannel': False,
            'response': True,
          }
        )
      )
      await recv_frame(ws)  # ack
      await recv_frame(ws)  # message
      await ws.send(
        json.dumps(
          {
            'id': '8',
            'type': 'unsubscribe',
            'topic': '/market/ticker:ACME-USDT',
            'privateChannel': False,
            'response': True,
          }
        )
      )
      reply = json.loads(await recv_frame(ws))
      assert reply == {'type': 'ack', 'id': '8'}

      await assert_no_frame(ws)


@pytest.mark.asyncio
async def test_ws_mock_reports_unknown_subscription_for_an_unrecorded_channel():
  """A topic matching neither declared channel is `unknown_subscription`, not a false match."""
  with running_ws_server(root=CHANNEL_DIALECT_ROOT) as server:
    async with websockets.connect(server.url) as ws:
      await ws.send(
        json.dumps(
          {
            'id': '4',
            'type': 'subscribe',
            'topic': '/market/ticker:NOPE-USDT',
            'privateChannel': False,
            'response': True,
          }
        )
      )
      reply = json.loads(await recv_frame(ws))

  assert reply['type'] == 'error'
  assert reply['message'] == 'unknown_subscription'


@pytest.mark.asyncio
async def test_ws_mock_matches_json_rpc_call_by_method_and_params_ignoring_id():
  """A JSON-RPC call over WS matches by declared method+params; correlate threads the caller's own id."""
  with running_ws_server(root=ENVELOPE_ROOT) as server:
    async with websockets.connect(server.url) as ws:
      await ws.send(
        json.dumps(
          {
            'jsonrpc': '2.0',
            'id': 99,
            'method': 'acme_getBalance',
            'params': ['0xACME0000000000000000000000000000000001'],
          }
        )
      )
      reply = json.loads(await ws.recv())

  assert reply == {'jsonrpc': '2.0', 'id': 99, 'result': '0x1bc16d674ec80000'}


@pytest.mark.asyncio
async def test_ws_mock_rpc_match_does_not_interfere_with_subscribe_dialect():
  """A client with no WS RPC examples (VENUE_ROOT) is unaffected -- the RPC matcher is a no-op."""
  with running_ws_server(root=VENUE_ROOT) as server:
    async with websockets.connect(server.url) as ws:
      await ws.send(
        json.dumps({'method': 'sub.depth', 'param': {'symbol': 'ACME_USDT'}})
      )
      reply = json.loads(await recv_frame(ws))

  assert reply['channel'] == 'rs.sub.depth'


@pytest.mark.asyncio
async def test_ws_mock_rpc_match_does_not_false_match_a_frame_with_no_method():
  """A `kind: 'rpc'` example whose own payload has no `method` (COMMAND_DIALECT_ROOT's
  bit2me-style `{'event': 'add-order', ...}`) must not make `match_rpc` treat every
  incoming frame that also lacks `method` as a match.

  The defect: `_json_rpc_call_matches` compares `actual.get('method')` against
  `expected.get('method')`, and `None == None`. Before `match_rpc` checked the example's
  own payload for a genuine `method` field equal to its declared `rpc_method`, any
  `method`-less frame -- including an ordinary `unsubscribe` -- matched this example and
  got served the `add-order` reply instead of falling through to the normal
  subscribe/unsubscribe dispatch. Sending an `unsubscribe` for a channel that was never
  subscribed proves the fall-through: it reaches the unsubscribe branch and reports
  `unknown_subscription`, not the RPC example's reply.
  """
  with running_ws_server(root=COMMAND_DIALECT_ROOT) as server:
    async with websockets.connect(server.url) as ws:
      await ws.send(json.dumps({'type': 'unsubscribe', 'channel': 'add-order'}))
      reply = json.loads(await recv_frame(ws))

  assert reply['type'] == 'error'
  assert reply['message'] == 'unknown_subscription'


@pytest.mark.asyncio
async def test_ws_mock_declared_channel_routing_wins_over_a_mismatched_rpc_candidate():
  """A generic subscribe RPC method recognized-but-mismatched by `match_rpc` must still fall
  through to `match_subscribe`'s declared-channel routing, rather than the server loop
  surfacing `match_rpc`'s raise immediately -- the deribit `public/subscribe` regression.

  `RPC_SUBSCRIBE_CONFLICT_ROOT`'s `acme/subscribe` RPC command has exactly one recorded
  example (`ticker.ACME`), so any other channel -- like `book.ACME`, served by a separate
  `kind: 'stream'` endpoint on the same method name -- is a `match_rpc` candidate whose
  params don't match. Before the server-loop fix, that raised `UnexpectedSubscriptionParameters`
  and the message never reached `match_subscribe`, where the stream endpoint's own
  declared-channel routing would have served it correctly.
  """
  with running_ws_server(
    root=RPC_SUBSCRIBE_CONFLICT_ROOT
  ) as server:
    async with websockets.connect(server.url) as ws:
      await ws.send(
        json.dumps(
          {
            'jsonrpc': '2.0',
            'id': 1,
            'method': 'acme/subscribe',
            'params': {'channels': ['book.ACME']},
          }
        )
      )
      reply = json.loads(await recv_frame(ws))
      message = json.loads(await recv_frame(ws))

  assert reply == {'jsonrpc': '2.0', 'id': 1, 'result': ['book.ACME']}
  assert message['params']['channel'] == 'book.ACME'
  assert message['params']['data'] == {'symbol': 'ACME', 'bid': 100.4}


@pytest.mark.asyncio
async def test_ws_mock_prefers_a_recorded_unsubscribe_reply_over_declared_channel_reply_reuse():
  """Tier ordering: a declared-channel example that records its own `unsubscribe_reply`
  uses that captured frame, not the reply-reuse fallback (`example.reply`, re-correlated)
  that tier would otherwise fall back to for a declared-channel example whose own reply is
  recorded (`RPC_SUBSCRIBE_CONFLICT_ROOT`'s `book` endpoint, per `docs/spec/spec.md`'s
  Deribit-style envelope).

  `book`'s `01` example now records both `01.reply.json` (the subscribe ack,
  `result: ['book.ACME']`) and `01.unsubscribe_reply.json` (`result: 'closed'`) -- if the
  new tier were not consulted first, unsubscribing would get the old reply-reuse shape
  instead, which is what this asserts against.

  Deribit's real dialect carries no `type` key at all, so re-sending the identical subscribe
  frame for an already-active channel is itself how an unsubscribe is recognized here (see
  the `_handle` comment on `active`-membership); this reuses that same mechanism.
  """
  with running_ws_server(
    root=RPC_SUBSCRIBE_CONFLICT_ROOT
  ) as server:
    async with websockets.connect(server.url) as ws:
      subscribe = {
        'jsonrpc': '2.0',
        'id': 1,
        'method': 'acme/subscribe',
        'params': {'channels': ['book.ACME']},
      }
      await ws.send(json.dumps(subscribe))
      reply = json.loads(await recv_frame(ws))
      await recv_frame(ws)  # message
      await ws.send(json.dumps({**subscribe, 'id': 2}))
      unsubscribed = json.loads(await recv_frame(ws))

  assert reply == {'jsonrpc': '2.0', 'id': 1, 'result': ['book.ACME']}
  assert unsubscribed == {'jsonrpc': '2.0', 'id': 2, 'result': 'closed'}
  assert unsubscribed != {'jsonrpc': '2.0', 'id': 2, 'result': ['book.ACME']}


@pytest.mark.asyncio
async def test_ws_mock_pushes_a_connect_triggered_example_with_no_frame_sent():
  """`docs/spec/authoring.md` rule 11's `connect` trigger: a `push: {trigger: 'connect'}`
  example is pushed the instant the connection is accepted, with no frame ever sent by the
  client -- binance's private user-data streams (listenKey in the URL, no outgoing frame at
  all) are this shape. `ws/connect_push` never declares `envelope.channel`, a `payload`, or
  anything else `match_subscribe`/`match_rpc` could match against, so the only way this
  message can arrive is the `connect`-trigger push path itself.
  """
  with running_ws_server(root=PUSH_ROOT) as server:
    async with websockets.connect(server.url) as ws:
      pushed = json.loads(await recv_frame(ws))

  assert pushed == {'channel': 'connect.push', 'data': {'greeting': 'pushed-on-connect'}}


@pytest.mark.asyncio
async def test_ws_mock_pushes_an_after_rpc_triggered_example_once_the_gating_reply_is_sent():
  """`docs/spec/authoring.md` rule 11's `after_rpc` trigger: `ws/after_rpc_push` declares
  `push: {trigger: 'after_rpc', method: 'login'}` and has no subscribe frame of its own --
  mexc's `futures.streams.user.*` shape (a normal `login` request/reply, then auto-push).
  Sending the gating `login` RPC call gets its own reply first, then the push-only example's
  declared message arrives unprompted, with no further frame sent by the client.

  `PUSH_ROOT` also declares a `connect`-triggered example (`ws/connect_push`), so every
  connection gets that push immediately, before this test ever sends anything -- consumed
  first here so the assertions below are about the `after_rpc` push specifically.
  """
  with running_ws_server(root=PUSH_ROOT) as server:
    async with websockets.connect(server.url) as ws:
      on_connect = json.loads(await recv_frame(ws))
      await ws.send(json.dumps({'method': 'login'}))
      reply = json.loads(await recv_frame(ws))
      pushed = json.loads(await recv_frame(ws))

  assert on_connect == {'channel': 'connect.push', 'data': {'greeting': 'pushed-on-connect'}}
  assert reply == {'method': 'login', 'result': 'ok'}
  assert pushed == {'channel': 'personal.orders', 'data': {'orderId': '1', 'status': 'NEW'}}


def test_ws_registry_match_rpc_matches_a_genuine_zero_arg_command():
  """A `kind: 'rpc'` example with no recorded `payload` at all still matches its bare call.

  `RPC_DIALECT_ROOT`'s `ws/ping` records only empty `parameters`, no `payload` -- the shape a
  genuine zero-arg command actually has. Rekeying candidate identification off the incoming
  message's own `method` (rather than reading it out of the example's own, absent, payload)
  is what lets this become a candidate at all; the synthesized `{'method': 'ping'}` expected
  frame is what lets a real `{'method': 'ping'}` message structurally match it.
  """
  registry = WsMockRegistry(root=RPC_DIALECT_ROOT)

  match = registry.match_rpc({'method': 'ping'})

  assert match is not None
  assert match.example.function == 'system.ws_ping'


def test_ws_registry_match_rpc_raises_unexpected_when_params_dont_match():
  """A recognized method with no matching candidate raises, rather than returning `None`.

  This is the kraken `ping`-hang bug: before this rewrite, a method-recognized-but-params-
  differ call silently returned `None` from `match_rpc`, leaving a caller's WS RPC call
  waiting forever for a reply that would never come. `RPC_DIALECT_ROOT`'s `ws/get_stuff`
  recognizes `acme_getStuff`, but neither recorded example's `symbol` is `'NOPE'`.
  """
  registry = WsMockRegistry(root=RPC_DIALECT_ROOT)

  with pytest.raises(UnexpectedSubscriptionParameters) as excinfo:
    registry.match_rpc(
      {'method': 'acme_getStuff', 'params': {'symbol': 'NOPE', 'nonce': 'live-nonce'}}
    )

  assert len(excinfo.value.candidates) == 2


def test_ws_registry_match_rpc_raises_ambiguous_when_redaction_collapses_two_examples():
  """Two examples sharing a method, differing only in a `redacted` param, are ambiguous.

  `ws/get_stuff` declares `redacted: ['nonce']` and records two examples (`a`, `b`) with the
  same `symbol` but different `nonce`. A live call for that `symbol` matches both once
  `nonce` is stripped from both sides.
  """
  registry = WsMockRegistry(root=RPC_DIALECT_ROOT)

  with pytest.raises(AmbiguousSubscriptionMatch) as excinfo:
    registry.match_rpc(
      {'method': 'acme_getStuff', 'params': {'symbol': 'ACME', 'nonce': 'live-nonce'}}
    )

  assert len(excinfo.value.candidates) == 2


def test_http_mock_returns_422_details():
  """The 422 body names the route's real candidates so a caller can see what differed."""
  with running_mock_servers(root=ROOT) as servers:
    import httpx

    response = httpx.post(
      f'{servers.http_base_url}/orders',
      json={
        'side': 'sell',
        'symbol': 'ACME/USD',
        'price': '100.00',
        'amount': '999',
        'clientOrderId': 'synthetic-rest-1',
      },
    )

  assert response.status_code == EXPECTED_PARAM_STATUS
  payload = response.json()
  assert payload['error'] == 'unexpected_parameters'
  assert payload['candidates'][0]['function'] == 'orders.create'
  candidate = next(c for c in payload['candidates'] if c['example_id'] == 'open')
  assert candidate['expected_request']['payload']['amount'] == '0.5'


def test_http_mock_returns_409_details_for_an_ambiguous_match():
  """A request that genuinely matches two recorded examples gets 409, not a silent pick.

  Mirrors `test_http_mock_returns_422_details` for the new ambiguous-match status --
  `orders.create_signed`'s two examples (see
  `test_http_registry_raises_ambiguous_match_when_redaction_collapses_two_examples`) both
  match once the live `nonce` is redacted from the request.
  """
  with running_mock_servers(root=ROOT) as servers:
    import httpx

    response = httpx.post(
      f'{servers.http_base_url}/signed-orders',
      json={'orderId': 'dup-order', 'nonce': 'live-nonce-2'},
    )

  assert response.status_code == AMBIGUOUS_PARAM_STATUS
  payload = response.json()
  assert payload['error'] == 'ambiguous_parameters'
  assert payload['method'] == 'POST'
  assert payload['path'] == '/signed-orders'
  assert {c['example_id'] for c in payload['candidates']} == {'a', 'b'}


def test_http_mock_replays_raw_payload_verbatim_without_an_envelope():
  """With no endpoint-declared `envelope`, the recorded payload is replayed exactly as stored."""
  import httpx

  with running_server(root=ROOT) as server:
    host, port = server.server_address
    response = httpx.get(f'http://{host}:{port}/widgets/w1')

  body = response.json()
  assert 'retCode' not in body
  assert body == {'name': 'Widget One', 'stock': 4}


def test_http_mock_serves_raw_payload_and_threads_correlate_for_a_declared_envelope():
  """An endpoint-declared `envelope` threads the request's own correlation value into the raw reply."""
  import httpx

  with running_server(root=ENVELOPE_ROOT) as server:
    host, port = server.server_address
    response = httpx.get(f'http://{host}:{port}/widgets/w1')

  body = response.json()
  assert body['retCode'] == 0
  assert body['result'] == {'name': 'Widget One', 'stock': 4}


class TestQueryMatching:
  """A recorded example and a typed client may render the same number differently."""

  def test_identical_items_match(self):
    assert query_matches([('a', 'x'), ('b', 'y')], [('a', 'x'), ('b', 'y')])

  def test_a_float_rendering_matches_an_integer_recording(self):
    """The defect: a `number` parameter recorded as `10` is replayed as `10.0`.

    `coerce_example_call` validates the example value against the generated annotation, so
    a `float` parameter arrives as `10.0` on the wire. Matching by text made every such
    example unreplayable — Deribit's `trading.margins` records `amount: 10` and `price:
    1000` against two `number` parameters.
    """
    assert query_matches([('amount', '10.0')], [('amount', '10')])

  def test_different_numbers_still_differ(self):
    assert not query_matches([('amount', '10.5')], [('amount', '10')])

  def test_a_string_that_looks_numeric_still_matches_exactly(self):
    assert query_matches([('id', '007')], [('id', '007')])

  def test_a_non_numeric_difference_is_a_mismatch(self):
    assert not query_matches([('id', 'w1')], [('id', 'w2')])

  def test_a_missing_item_is_a_mismatch(self):
    assert not query_matches([('a', 'x')], [('a', 'x'), ('b', 'y')])

  def test_a_renamed_key_is_a_mismatch(self):
    assert not query_matches([('a', 'x')], [('b', 'x')])


def test_http_server_answers_a_browser():
  """A generated client running in a browser can reach the mock.

  Without `Access-Control-Allow-Origin` the browser discards the response before the
  client sees it, and without an `OPTIONS` handler the preflight fails before the request
  is even sent -- so a browser client could not be tested against the mock at all. Both
  were true until a real Chromium was pointed at it.
  """
  import httpx

  with running_server(root=ROOT) as server:
    host, port = server.server_address
    origin = {'Origin': 'http://localhost:5173'}
    preflight = httpx.options(f'http://{host}:{port}/widgets/w1', headers=origin)
    response = httpx.get(f'http://{host}:{port}/widgets/w1', headers=origin)

  assert preflight.status_code == 204
  assert preflight.headers['access-control-allow-origin'] == '*'
  assert 'GET' in preflight.headers['access-control-allow-methods']
  assert response.headers['access-control-allow-origin'] == '*'
