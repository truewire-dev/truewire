"""A subscription belongs to one connection, even between reads or during cleanup."""
import asyncio
import json

import pytest
import websockets

from truewire_core.exceptions import NetworkError
from truewire_core.ws.streams import Streams
from truewire_core.ws.streams_rpc import StreamsRpc


class SubscriptionProtocol:
  async def request_subscription(self, channel, params=None):
    await (await self.ws).send(channel)

  async def request_unsubscription(self, channel, params=None):
    await (await self.ws).send('unsubscribe:' + channel)

  def parse_msg(self, msg):
    return {'kind': 'subscription', **json.loads(msg)}


class StreamSocket(SubscriptionProtocol, Streams):
  pass


class RpcStreamSocket(SubscriptionProtocol, StreamsRpc):
  async def rpc_send(self, id, req):
    raise NotImplementedError


@pytest.mark.asyncio
@pytest.mark.parametrize('socket_type', [StreamSocket, RpcStreamSocket])
@pytest.mark.parametrize('reopen', [False, True])
async def test_dropped_subscription_never_reads_or_unsubscribes_on_a_new_connection(socket_type, reopen):
  connections = []
  drop = asyncio.Event()

  async def serve(ws):
    connections.append(ws)
    if len(connections) > 1:
      await ws.wait_closed()
      return
    channel = await ws.recv()
    await ws.send(json.dumps({'channel': channel, 'notification': 'snapshot'}))
    await drop.wait()
    await ws.close()

  async with websockets.serve(serve, '127.0.0.1', 0) as server:
    port = server.sockets[0].getsockname()[1]
    async with socket_type(url=f'ws://127.0.0.1:{port}') as socket:
      stream = await socket.subscribe('book').__aenter__()
      frames = stream.__aiter__()
      assert await asyncio.wait_for(anext(frames), 1) == 'snapshot'
      original = await socket.ctx
      drop.set()
      await original.ws.wait_closed()
      await asyncio.gather(original.listener, return_exceptions=True)
      if reopen:
        await socket.open()  # Independent new work may reconnect; the old stream may not.
      before = asyncio.all_tasks()
      with pytest.raises(NetworkError):
        await asyncio.wait_for(anext(frames), 1)
      assert await asyncio.wait_for(stream.unsubscribe(), 1) is None
      assert not socket.subscriptions
      assert len(connections) == (2 if reopen else 1)
      assert not (asyncio.all_tasks() - before), 'stream read leaked its queue task'


async def dropped(socket):
  ctx = socket.ctx_future.result()
  await ctx.ws.wait_closed()
  await asyncio.gather(ctx.listener, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('socket_type', [StreamSocket, RpcStreamSocket])
async def test_unsubscribe_after_a_drop_frees_the_channel(socket_type):
  connections = []

  async def serve(ws):
    connections.append(ws)
    channel = await ws.recv()
    await ws.send(json.dumps({'channel': channel, 'notification': f'snapshot{len(connections)}'}))
    if len(connections) == 1:
      await ws.close()
    else:
      await ws.wait_closed()

  async with websockets.serve(serve, '127.0.0.1', 0) as server:
    port = server.sockets[0].getsockname()[1]
    async with socket_type(url=f'ws://127.0.0.1:{port}') as socket:
      async with socket.subscribe('book') as stream:
        assert await asyncio.wait_for(anext(stream.__aiter__()), 1) == 'snapshot1'
        await dropped(socket)
      assert not socket.subscriptions
      async with socket.subscribe('book') as again:
        assert await asyncio.wait_for(anext(again.__aiter__()), 1) == 'snapshot2'
      assert len(connections) == 2


class FlakyUnsubscribe:
  failures = 1

  async def request_unsubscription(self, channel, params=None):
    if self.failures:
      self.failures -= 1
      raise NetworkError('unsubscribe failed')
    await super().request_unsubscription(channel, params)


@pytest.mark.asyncio
@pytest.mark.parametrize('socket_type', [StreamSocket, RpcStreamSocket])
async def test_failed_unsubscribe_on_a_live_connection_keeps_the_subscription(socket_type):
  received = []
  unsubscribed = asyncio.Event()

  async def serve(ws):
    channel = await ws.recv()
    await ws.send(json.dumps({'channel': channel, 'notification': 'before'}))
    received.append(await ws.recv())
    unsubscribed.set()
    await ws.wait_closed()

  flaky_type = type('Flaky' + socket_type.__name__, (FlakyUnsubscribe, socket_type), {})
  async with websockets.serve(serve, '127.0.0.1', 0) as server:
    port = server.sockets[0].getsockname()[1]
    async with flaky_type(url=f'ws://127.0.0.1:{port}') as socket:
      stream = await socket.subscribe('book')
      frames = stream.__aiter__()
      with pytest.raises(NetworkError):
        await stream.unsubscribe()
      assert 'book' in socket.subscriptions
      assert await asyncio.wait_for(anext(frames), 1) == 'before'
      await asyncio.wait_for(stream.unsubscribe(), 1)
      # unsubscribe returns once the frame is sent; wait for the server to read it.
      await asyncio.wait_for(unsubscribed.wait(), 1)
      assert received == ['unsubscribe:book']
      assert not socket.subscriptions
      assert [n async for n in frames] == []


@pytest.mark.asyncio
@pytest.mark.parametrize('socket_type', [StreamSocket, RpcStreamSocket])
async def test_stale_stream_ends_and_keeps_a_newer_subscription(socket_type):
  """A is unsubscribed, its connection drops, B subscribes to the same channel anew:
  reading A ends cleanly and does not take B's queue."""
  connections = []
  drop = asyncio.Event()

  async def serve(ws):
    connections.append(ws)
    channel = await ws.recv()
    if len(connections) == 1:
      await ws.recv()  # unsubscribe
      await drop.wait()
      await ws.close()
      return
    await ws.send(json.dumps({'channel': channel, 'notification': 'b1'}))
    await ws.wait_closed()

  async with websockets.serve(serve, '127.0.0.1', 0) as server:
    port = server.sockets[0].getsockname()[1]
    async with socket_type(url=f'ws://127.0.0.1:{port}') as socket:
      a = await socket.subscribe('book')
      await a.unsubscribe()
      ctx = socket.ctx_future.result()
      drop.set()
      await ctx.ws.wait_closed()
      await asyncio.gather(ctx.listener, return_exceptions=True)
      b = await socket.subscribe('book')
      assert [n async for n in a] == []
      assert await asyncio.wait_for(anext(b.__aiter__()), 1) == 'b1'


@pytest.mark.asyncio
@pytest.mark.parametrize('socket_type', [StreamSocket, RpcStreamSocket])
async def test_notifications_received_before_a_drop_are_delivered_first(socket_type):
  """Three notifications arrive, then the server closes. A reader that gets to the stream
  after the close receives all three, then NetworkError."""
  async def serve(ws):
    channel = await ws.recv()
    for n in 'abc':
      await ws.send(json.dumps({'channel': channel, 'notification': n}))
    await ws.close()

  async with websockets.serve(serve, '127.0.0.1', 0) as server:
    port = server.sockets[0].getsockname()[1]
    async with socket_type(url=f'ws://127.0.0.1:{port}') as socket:
      stream = await socket.subscribe('book')
      await dropped(socket)
      got = []
      with pytest.raises(NetworkError):
        async for n in stream:
          got.append(n)
      assert got == ['a', 'b', 'c']


@pytest.mark.asyncio
@pytest.mark.parametrize('socket_type', [StreamSocket, RpcStreamSocket])
async def test_a_dead_listener_on_an_open_connection_gets_a_fresh_connection(socket_type):
  """A frame the parser cannot read kills the listener while the TCP connection stays
  OPEN. The stream on it fails with the listener's error; new work reconnects."""
  connections = []

  async def serve(ws):
    connections.append(ws)
    channel = await ws.recv()
    if len(connections) == 1:
      await ws.send('not json')
    else:
      await ws.send(json.dumps({'channel': channel, 'notification': 'fresh'}))
    await ws.wait_closed()

  async with websockets.serve(serve, '127.0.0.1', 0) as server:
    port = server.sockets[0].getsockname()[1]
    async with socket_type(url=f'ws://127.0.0.1:{port}') as socket:
      first = await socket.subscribe('book')
      with pytest.raises(json.JSONDecodeError):
        await asyncio.wait_for(anext(first.__aiter__()), 1)
      assert await first.unsubscribe() is None
      second = await asyncio.wait_for(socket.subscribe('trades'), 1)
      assert await asyncio.wait_for(anext(second.__aiter__()), 1) == 'fresh'
      assert len(connections) == 2
