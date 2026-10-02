"""
Pin `Rpc.rpc_request` and `StreamsRpc.rpc_request` cleanup of the reply table.

Both used to store `replies[id]` and delete it only after a successful reply. A call
cancelled by the caller (an `asyncio.wait_for` timeout), a send that raised, or a
connection that dropped left the future in `replies` for the life of the connection,
and a late reply for that id then resolved a future nobody awaited. The entry is now
removed in a `finally`, and `on_msg` drops a reply whose id has no waiter.

The sockets here never dial out: `force_open` hands back a fake connection whose
listener task the test can fail, standing in for a dropped connection.
"""
from dataclasses import dataclass, field
from typing_extensions import Any, cast
import asyncio
import json
import pytest
import websockets

from truewire_core.ws.socket import Context
from truewire_core.ws.rpc import Rpc, Response
from truewire_core.ws.streams_rpc import StreamsRpc, Message

class FakeConnection:
  """Stand-in for `websockets.ClientConnection`: open, and closeable."""
  def __init__(self):
    self.state = websockets.State.OPEN

  async def __aexit__(self, exc_type, exc_value, traceback):
    pass

@dataclass
class FakeTransport:
  """The fake connection's background tasks, and the requests written to it."""
  sent: list[tuple[int, Any]] = field(default_factory=list)
  send_error: Exception | None = None
  drop: asyncio.Event = field(default_factory=asyncio.Event)

  def context(self) -> Context:
    async def listener():
      await self.drop.wait()
      raise ConnectionError('connection dropped')
    async def pinger():
      await asyncio.Event().wait()
    return Context(
      ws=cast(websockets.ClientConnection, FakeConnection()),
      listener=asyncio.create_task(listener()),
      pinger=asyncio.create_task(pinger()),
    )

  async def send(self, id: int, req: Any):
    if self.send_error is not None:
      raise self.send_error
    self.sent.append((id, req))

@dataclass
class FakeRpc(Rpc[str, str]):
  transport: FakeTransport = field(default_factory=FakeTransport)

  async def force_open(self) -> Context:
    return self.transport.context()

  async def rpc_send(self, id: int, req: str):
    await self.transport.send(id, req)

  def parse_response(self, msg: str | bytes) -> Response[str] | None:
    return json.loads(msg)

@dataclass
class FakeStreamsRpc(StreamsRpc[str, str]):
  transport: FakeTransport = field(default_factory=FakeTransport)

  async def force_open(self) -> Context:
    return self.transport.context()

  async def rpc_send(self, id: int, req: str, /):
    await self.transport.send(id, req)

  async def request_subscription(self, channel: str, params=None):
    raise NotImplementedError

  async def request_unsubscription(self, channel: str, params=None):
    raise NotImplementedError

  def parse_msg(self, msg: str | bytes, /) -> Message[str, Any] | None:
    res = json.loads(msg)
    return {'kind': 'response', 'id': res['id'], 'response': res['reply']}

def reply(id: int, value: str) -> str:
  return json.dumps({'id': id, 'reply': value})

async def in_flight(socket: FakeRpc | FakeStreamsRpc) -> asyncio.Task[str]:
  """Start one request and return once it has been written and is waiting for its reply."""
  task = asyncio.create_task(socket.rpc_request('ping'))
  while not socket.transport.sent:
    await asyncio.sleep(0)
  await asyncio.sleep(0)
  assert list(socket.replies) == [0]
  return task

SOCKETS = [FakeRpc, FakeStreamsRpc]

@pytest.mark.asyncio
@pytest.mark.parametrize('Socket', SOCKETS)
async def test_rpc_cancelled_request_leaves_no_pending_reply(Socket):
  """A caller's timeout cancels the request; its id leaves the table, and a late reply is dropped."""
  async with Socket(url='wss://example.invalid') as socket:
    with pytest.raises(asyncio.TimeoutError):
      await asyncio.wait_for(socket.rpc_request('ping'), timeout=0.01)
    assert socket.transport.sent == [(0, 'ping')]
    assert socket.replies == {}
    socket.on_msg(reply(0, 'late'))
    assert socket.replies == {}

@pytest.mark.asyncio
@pytest.mark.parametrize('Socket', SOCKETS)
async def test_rpc_cancelled_request_leaves_no_task_behind(Socket):
  """Cancelling the request also ends the task `wait` spawned to await the reply."""
  async with Socket(url='wss://example.invalid') as socket:
    task = await in_flight(socket)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
      await task
    await asyncio.sleep(0)
    assert socket.replies == {}
    ctx = await socket.ctx
    assert asyncio.all_tasks() == {asyncio.current_task(), ctx.listener, ctx.pinger}

@pytest.mark.asyncio
@pytest.mark.parametrize('Socket', SOCKETS)
async def test_rpc_failed_send_leaves_no_pending_reply(Socket):
  """A send that raises propagates, and its id leaves the table."""
  async with Socket(url='wss://example.invalid') as socket:
    socket.transport.send_error = ConnectionError('send failed')
    with pytest.raises(ConnectionError, match='send failed'):
      await socket.rpc_request('ping')
    assert socket.replies == {}

@pytest.mark.asyncio
@pytest.mark.parametrize('Socket', SOCKETS)
async def test_rpc_dropped_connection_leaves_no_pending_reply(Socket):
  """A connection that drops mid-request fails it, its id leaves the table, and a late reply is dropped."""
  async with Socket(url='wss://example.invalid') as socket:
    task = await in_flight(socket)
    socket.transport.drop.set()
    with pytest.raises(ConnectionError, match='connection dropped'):
      await task
    assert socket.replies == {}
    socket.on_msg(reply(0, 'late'))
    assert socket.replies == {}

@pytest.mark.asyncio
@pytest.mark.parametrize('Socket', SOCKETS)
async def test_rpc_answered_request_returns_the_reply_and_leaves_no_pending_reply(Socket):
  """The ordinary path: the reply for the id resolves the request, and the table is empty after."""
  async with Socket(url='wss://example.invalid') as socket:
    task = await in_flight(socket)
    socket.on_msg(reply(0, 'pong'))
    assert await task == 'pong'
    assert socket.replies == {}
    socket.on_msg(reply(0, 'duplicate'))
    socket.on_msg(reply(7, 'unknown id'))
    assert socket.replies == {}
