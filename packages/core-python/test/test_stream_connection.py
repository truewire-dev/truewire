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
async def test_queue_task_creation_failure_preserves_error_and_frees_channel(socket_type, monkeypatch):
  async def serve(ws):
    await ws.wait_closed()

  async with websockets.serve(serve, '127.0.0.1', 0) as server:
    port = server.sockets[0].getsockname()[1]
    async with socket_type(url=f'ws://127.0.0.1:{port}') as socket:
      stream = await socket.subscribe('book')
      failure = RuntimeError('queue task creation failed')
      create_task = asyncio.create_task

      def reject_queue_task(coro, **kwargs):
        if coro.cr_code is asyncio.Queue.get.__code__:
          coro.close()
          raise failure
        return create_task(coro, **kwargs)

      with monkeypatch.context() as patch:
        patch.setattr(asyncio, 'create_task', reject_queue_task)
        with pytest.raises(RuntimeError) as raised:
          await anext(stream.__aiter__())
      assert raised.value is failure
      assert not socket.subscriptions
      async with socket.subscribe('book'):
        assert 'book' in socket.subscriptions


@pytest.mark.asyncio
@pytest.mark.parametrize('socket_type', [StreamSocket, RpcStreamSocket])
@pytest.mark.parametrize('ending', ['cancel', 'unsubscribe'])
async def test_pending_stream_read_cleans_up_queue_task(socket_type, ending, monkeypatch):
  async def serve(ws):
    await ws.wait_closed()

  async with websockets.serve(serve, '127.0.0.1', 0) as server:
    port = server.sockets[0].getsockname()[1]
    async with socket_type(url=f'ws://127.0.0.1:{port}') as socket:
      stream = await socket.subscribe('book')
      queue = socket.subscriptions['book']
      get = queue.get
      reading = asyncio.Event()
      queue_tasks = []

      async def observed_get():
        queue_tasks.append(asyncio.current_task())
        reading.set()
        return await get()

      monkeypatch.setattr(queue, 'get', observed_get)
      reader = asyncio.create_task(anext(stream.__aiter__()))
      try:
        await asyncio.wait_for(reading.wait(), 1)
        if ending == 'cancel':
          reader.cancel()
          with pytest.raises(asyncio.CancelledError):
            await reader
        else:
          await asyncio.wait_for(stream.unsubscribe(), 1)
          with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(reader, 1)
        assert len(queue_tasks) == 1
        assert queue_tasks[0].cancelled()
        assert not socket.subscriptions
        await stream.unsubscribe()
        async with socket.subscribe('book'):
          assert 'book' in socket.subscriptions
      finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


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


class GatedSubscribe:
  """Holds every subscribe frame until `release` is set, and records an abandoned one."""
  reached: asyncio.Event
  release: asyncio.Event
  abandoned: asyncio.Event

  async def request_subscription(self, channel, params=None):
    self.reached.set()
    try:
      await self.release.wait()
    except asyncio.CancelledError:
      self.abandoned.set()
      raise
    await super().request_subscription(channel, params)


@pytest.mark.asyncio
@pytest.mark.parametrize('socket_type', [StreamSocket, RpcStreamSocket])
async def test_a_timed_out_subscribe_never_sends_later(socket_type):
  """A subscribe the caller gave up on must stop with it (TRU-343).

  `Socket.wait` ran the request in a task it never cancelled, so a subscribe that timed out
  under `asyncio.wait_for` was still sent and acked later, with its queue already gone.
  """
  received = []
  unsubscribed = asyncio.Event()

  async def serve(ws):
    async for frame in ws:
      received.append(frame)
      if frame.startswith('unsubscribe:'):
        unsubscribed.set()
      else:
        await ws.send(json.dumps({'channel': frame, 'notification': 'snapshot'}))

  gated_type = type('Gated' + socket_type.__name__, (GatedSubscribe, socket_type), {
    'reached': asyncio.Event(), 'release': asyncio.Event(), 'abandoned': asyncio.Event(),
  })
  async with websockets.serve(serve, '127.0.0.1', 0) as server:
    port = server.sockets[0].getsockname()[1]
    async with gated_type(url=f'ws://127.0.0.1:{port}') as socket:
      async def subscribe():
        return await socket.subscribe('trades')

      call = asyncio.create_task(subscribe())
      await socket.reached.wait()
      with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(call, timeout=0)
      assert socket.abandoned.is_set(), 'the timed-out subscribe is still running'
      assert not socket.subscriptions
      # Anything the abandoned request would still send goes out before this subscribe's
      # frames on the same connection, so the server's log at the unsubscribe is complete.
      socket.release.set()
      stream = await asyncio.wait_for(socket.subscribe('trades'), 1)
      assert await asyncio.wait_for(anext(stream.__aiter__()), 1) == 'snapshot'
      await asyncio.wait_for(stream.unsubscribe(), 1)
      await asyncio.wait_for(unsubscribed.wait(), 1)
      assert received == ['trades', 'unsubscribe:trades']
