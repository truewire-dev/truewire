"""
The mock's WebSocket replies for two recorded shapes the hyperliquid ports hit.

- A dual-transport rpc whose HTTP recording answered with the body `null`. Its WS example is
  synthesized from that recording, so `null` is a real reply and the mock sends it, while a
  native `null` `.reply.json` still means the API sends nothing (`docs/spec/authoring.md`
  rule 11).
- A stream whose subscribe frame names a different channel (`userEvents`) from the one its
  pushes arrive on (`spec.channel`, `user`), declared by `envelope.subscribe_channel`.

Fixtures live in `fixtures/mock_server_ws_replies/`, owned by this suite alone.
"""

import asyncio
import json
from pathlib import Path

import pytest
import websockets
from pydantic import ValidationError

from truewire.mock import WsMockRegistry, load_ws_examples, running_mock_servers, running_ws_server
from truewire.spec import StreamEnvelopeSpec


ROOT = Path(__file__).resolve().parent / 'fixtures' / 'mock_server_ws_replies'
WS_RECV_TIMEOUT = 5
NO_FRAME_TIMEOUT = 0.2


async def recv_frame(ws: websockets.ClientConnection):
  async with asyncio.timeout(WS_RECV_TIMEOUT):
    return await ws.recv()


async def assert_no_frame(ws: websockets.ClientConnection):
  with pytest.raises(asyncio.TimeoutError):
    async with asyncio.timeout(NO_FRAME_TIMEOUT):
      await ws.recv()


def _examples() -> dict[tuple[str, str], object]:
  return {(example.function, example.example_id): example for example in load_ws_examples(ROOT)}


def test_only_examples_built_from_an_http_recording_are_marked_synthesized():
  examples = _examples()
  assert examples[('info.lookup', 'missing')].synthesized  # type: ignore[attr-defined]
  assert examples[('info.lookup', 'missing')].reply is None  # type: ignore[attr-defined]
  assert not examples[('ws.fire', 'doc')].synthesized  # type: ignore[attr-defined]
  assert not examples[('streams.user_events', 'doc')].synthesized  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_ws_mock_sends_a_null_reply_synthesized_from_a_null_http_body():
  with running_ws_server(root=ROOT) as server:
    async with websockets.connect(server.url) as ws:
      await ws.send(json.dumps({'jsonrpc': '2.0', 'id': 7, 'method': 'lookup', 'params': {'key': 'missing'}}))
      missing = await recv_frame(ws)
      await ws.send(json.dumps({'jsonrpc': '2.0', 'id': 8, 'method': 'lookup', 'params': {'key': 'found'}}))
      found = await recv_frame(ws)

  assert missing == 'null'
  assert json.loads(found) == {'value': 1}


@pytest.mark.asyncio
async def test_ws_mock_still_sends_nothing_for_a_native_null_reply():
  with running_ws_server(root=ROOT) as server:
    async with websockets.connect(server.url) as ws:
      await ws.send(json.dumps({'method': 'fire'}))
      await assert_no_frame(ws)


def test_http_mock_serves_the_null_body_as_before():
  import httpx

  with running_mock_servers(root=ROOT) as servers:
    response = httpx.post(f'{servers.http_base_url}/', json={'method': 'lookup', 'params': {'key': 'missing'}})
  assert response.status_code == 200
  assert response.json() is None


def test_subscribe_channel_routes_the_frame_to_its_endpoint():
  registry = WsMockRegistry(ROOT)
  frame = {'method': 'subscribe', 'subscription': {'type': 'userEvents', 'user': '0xabc'}}
  assert [example.function for example in registry.declared_channel_candidates(frame)] == ['streams.user_events']
  push_channel = {'method': 'subscribe', 'subscription': {'type': 'user', 'user': '0xabc'}}
  assert registry.declared_channel_candidates(push_channel) == []
  book = {'method': 'subscribe', 'subscription': {'type': 'l2Book', 'coin': 'ACME'}}
  assert [example.function for example in registry.declared_channel_candidates(book)] == ['streams.l2_book']


@pytest.mark.asyncio
async def test_ws_mock_acks_and_pushes_a_stream_subscribed_under_its_subscribe_channel():
  subscription = {'type': 'userEvents', 'user': '0xabc'}
  with running_ws_server(root=ROOT) as server:
    async with websockets.connect(server.url) as ws:
      await ws.send(json.dumps({'method': 'subscribe', 'subscription': subscription}))
      ack = json.loads(await recv_frame(ws))
      push = json.loads(await recv_frame(ws))
      await ws.send(json.dumps({'method': 'unsubscribe', 'subscription': subscription}))
      unsubscribed = json.loads(await recv_frame(ws))
      await assert_no_frame(ws)

  assert ack == {'channel': 'subscriptionResponse', 'data': {'method': 'subscribe', 'subscription': subscription}}
  assert push == {'channel': 'user', 'data': {'fills': []}}
  assert unsubscribed == ack


@pytest.mark.asyncio
async def test_ws_mock_does_not_match_a_frame_naming_the_push_channel():
  with running_ws_server(root=ROOT) as server:
    async with websockets.connect(server.url) as ws:
      await ws.send(json.dumps({'method': 'subscribe', 'subscription': {'type': 'user', 'user': '0xabc'}}))
      reply = json.loads(await recv_frame(ws))

  assert reply['type'] == 'error'


def test_subscribe_channel_is_a_stream_envelope_field_only():
  envelope = StreamEnvelopeSpec.model_validate(
    {'payload': 'data', 'channel': 'subscription.type', 'subscribe_channel': 'userEvents'}
  )
  assert envelope.subscribe_channel == 'userEvents'
  assert StreamEnvelopeSpec.model_validate({'payload': 'data'}).subscribe_channel is None
  from truewire.spec import RpcEnvelopeSpec

  with pytest.raises(ValidationError):
    RpcEnvelopeSpec.model_validate({'payload': 'data', 'subscribe_channel': 'userEvents'})
