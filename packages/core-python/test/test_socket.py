"""
Pin `Socket`'s lazy connection contract.

`docs/production_guidelines.md`'s "Core And Instantiation" promises that entering a
client opens nothing, first use of a transport opens exactly that transport, and exiting
closes exactly what was opened -- closing nothing when nothing was. `Socket.__aenter__`
used to call `await self.open()` unconditionally, and `Socket.__aexit__` closed via
`await self.ctx`, whose property falls through to `open()` -- so exiting a socket that
was never entered-and-used dialled out just to hang up. Both are fixed by making
`__aenter__` a no-op and `__aexit__` a guard on `ctx_future.done()`.

These tests run against a `Socket` subclass whose `force_open` is a call-recorder, never a
real `websockets.connect`. The regression test for the finding above uses a subclass whose
`force_open` raises, so any code path that still reaches it fails loudly instead of quietly
opening a socket.
"""
from dataclasses import dataclass, field
from typing_extensions import cast
import asyncio
import gc
import pytest
import websockets

from truewire_core.exceptions import NetworkError
from truewire_core.ws.socket import Socket, Context

class FakeConnection:
  """Stand-in for `websockets.ClientConnection`: enough surface for `Socket` to treat it as open and closeable."""
  def __init__(self):
    self.state = websockets.State.OPEN
    self.exit_calls: list[tuple] = []

  async def __aexit__(self, exc_type, exc_value, traceback):
    self.exit_calls.append((exc_type, exc_value, traceback))

async def _hang():
  """Background task body that only ever ends via cancellation, standing in for the real listener/pinger tasks."""
  await asyncio.Event().wait()

@dataclass
class RecordingSocket(Socket):
  """`Socket` whose transport is a call-recorder instead of a real connection."""
  open_calls: int = field(default=0, init=False)
  last_connection: FakeConnection | None = field(default=None, init=False)

  def on_msg(self, msg: str | bytes):
    """Unused: no test here drives real message dispatch."""

  async def force_open(self) -> Context:
    """Record the call and hand back a fake connection instead of dialling out."""
    self.open_calls += 1
    self.last_connection = FakeConnection()
    return Context(
      ws=cast(websockets.ClientConnection, self.last_connection),
      listener=asyncio.create_task(_hang()),
      pinger=asyncio.create_task(_hang()),
    )

@dataclass
class RaisingSocket(Socket):
  """`Socket` whose `force_open` raises, to prove a code path never dials out."""

  def on_msg(self, msg: str | bytes):
    """Unused: this socket never reaches message dispatch."""

  async def force_open(self) -> Context:
    """Fail loudly if anything ever tries to open this socket."""
    raise AssertionError('force_open must not be called')

@pytest.mark.asyncio
async def test_aenter_opens_nothing():
  """Entering a socket must not connect."""
  socket = RecordingSocket(url='wss://example.invalid')
  async with socket as opened:
    assert opened is socket
    assert socket.open_calls == 0
    assert not socket.ctx_future.done()

@pytest.mark.asyncio
async def test_aexit_without_use_connects_nothing():
  """Regression test: exiting a socket that was never used must not open one just to close it.

  Before the fix, `__aexit__` read `await self.ctx`, whose property falls through to
  `open()`. Using a `force_open` that raises turns "it dialled out" into a hard failure
  instead of a silent one.
  """
  async with RaisingSocket(url='wss://example.invalid'):
    pass

@pytest.mark.asyncio
async def test_use_opens_the_socket():
  """First use of the socket opens it exactly once."""
  socket = RecordingSocket(url='wss://example.invalid')
  async with socket:
    ctx = await socket.open()
  assert socket.open_calls == 1
  assert ctx.ws is socket.last_connection

@pytest.mark.asyncio
async def test_two_touches_open_once():
  """Two uses of the socket within one `async with` open the transport once, not twice."""
  socket = RecordingSocket(url='wss://example.invalid')
  async with socket:
    await socket.open()
    await socket.open()
  assert socket.open_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('cancelled', [False, True])
async def test_failed_open_releases_all_waiters_and_allows_retry(cancelled):
  started = asyncio.Event()
  finish = asyncio.Event()
  error = NetworkError('connect failed')

  class FailingOnceSocket(RecordingSocket):
    async def force_open(self):
      if not started.is_set():
        started.set()
        await finish.wait()
        raise error
      return await super().force_open()

  socket = FailingOnceSocket(url='wss://example.invalid')
  owner = asyncio.create_task(socket.open())
  await started.wait()
  waiters = [asyncio.create_task(socket.open()) for _ in range(2)]
  await asyncio.sleep(0)  # Both waiters reach the in-flight attempt's future.
  calls = [owner, *waiters]
  try:
    if cancelled:
      owner.cancel()
    else:
      finish.set()
    done, pending = await asyncio.wait(calls, timeout=1)
    assert not pending, 'a failed connect left callers waiting'
    assert len(done) == 3
    for call in calls:
      if cancelled and call is owner:
        assert call.cancelled()
      elif cancelled:
        assert isinstance(call.exception(), NetworkError)
      else:
        assert call.exception() is error
    assert socket._ctx_future is None
    assert not socket.open_lock.locked()
    async with socket:
      contexts = await asyncio.gather(socket.open(), socket.open())
      assert contexts[0] is contexts[1]
      assert socket.open_calls == 1
  finally:
    for call in calls:
      call.cancel()
    await asyncio.gather(*calls, return_exceptions=True)


@pytest.mark.asyncio
async def test_failed_open_without_waiters_retrieves_future_exception():
  loop = asyncio.get_running_loop()
  previous_handler = loop.get_exception_handler()
  unhandled = []
  loop.set_exception_handler(lambda loop, context: unhandled.append(context))
  try:
    async with RaisingSocket(url='wss://example.invalid') as socket:
      with pytest.raises(AssertionError, match='force_open must not be called'):
        await socket.open()
      assert socket._ctx_future is None
    gc.collect()
    assert not unhandled
  finally:
    loop.set_exception_handler(previous_handler)

@pytest.mark.asyncio
async def test_aexit_closes_what_was_opened():
  """Exiting after use closes exactly the connection that was opened."""
  socket = RecordingSocket(url='wss://example.invalid')
  async with socket:
    await socket.open()
  connection = socket.last_connection
  assert connection is not None
  assert len(connection.exit_calls) == 1

@pytest.mark.asyncio
async def test_exception_in_body_still_closes_what_was_opened():
  """An exception raised inside the `async with` body must not skip closing an opened socket."""
  socket = RecordingSocket(url='wss://example.invalid')
  with pytest.raises(RuntimeError, match='boom'):
    async with socket:
      await socket.open()
      raise RuntimeError('boom')
  connection = socket.last_connection
  assert connection is not None
  assert len(connection.exit_calls) == 1
  exc_type, exc_value, _ = connection.exit_calls[0]
  assert exc_type is RuntimeError
  assert str(exc_value) == 'boom'

@pytest.mark.asyncio
async def test_reentering_after_exit_gets_a_working_connection():
  """A socket entered, used, exited and re-entered opens a fresh connection, not a consumed future."""
  socket = RecordingSocket(url='wss://example.invalid')
  async with socket:
    await socket.open()
  first_connection = socket.last_connection
  async with socket:
    ctx = await socket.open()
  assert socket.open_calls == 2
  assert first_connection is not None
  assert first_connection.exit_calls
  assert ctx.ws is socket.last_connection
  assert ctx.ws is not first_connection

def test_a_socket_is_constructible_outside_an_event_loop():
  """Building a socket must not need a running loop; connecting is what needs one.

  `ctx_future` used to be a `default_factory=asyncio.Future`, and `asyncio.Future()` binds
  the running loop as it is constructed. That was invisible while sockets were only ever
  built inside `asyncio.run`, and became load-bearing the moment a root client started
  owning sockets it may never use: constructing one in a synchronous test fixture raised
  `RuntimeError: There is no current event loop` before anything could connect.

  Deliberately a plain `def`, with no `pytest.mark.asyncio`, so no loop is running.
  """
  socket = RaisingSocket(url='wss://example.invalid')
  assert socket._ctx_future is None

@pytest.mark.asyncio
async def test_close_during_wait_raises_network_error():
  started = asyncio.Event()
  cleaned = asyncio.Event()

  async def request():
    started.set()
    try:
      await _hang()
    finally:
      await asyncio.sleep(0)
      cleaned.set()

  async with RecordingSocket(url='wss://example.invalid') as socket:
    call = asyncio.create_task(socket.wait(request()))
    await started.wait()
  with pytest.raises(NetworkError, match='WebSocket connection closed'):
    await call
  assert not call.cancelled()
  assert cleaned.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize('background', ['listener', 'pinger'])
@pytest.mark.parametrize('ending', ['cancelled', 'returned', 'failed'])
async def test_wait_background_exit(background, ending):
  error = NetworkError('transport failed')

  async def finish():
    if ending == 'failed':
      raise error

  async with RecordingSocket(url='wss://example.invalid') as socket:
    ctx = await socket.open()
    old = getattr(ctx, background)
    old.cancel()
    await asyncio.gather(old, return_exceptions=True)
    task = asyncio.create_task(finish())
    setattr(ctx, background, task)
    if ending == 'cancelled':
      task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    request = asyncio.Future()
    with pytest.raises(NetworkError) as exc:
      await socket.wait(request, ctx=ctx)
    if ending == 'failed':
      assert exc.value is error
    else:
      assert exc.value.args == ('WebSocket connection closed',)
    assert request.cancelled()


@pytest.mark.asyncio
@pytest.mark.parametrize('background', ['listener', 'pinger'])
@pytest.mark.parametrize('failed', [False, True])
async def test_completed_request_wins_over_closed_connection(background, failed):
  error = ValueError('request failed')
  async with RecordingSocket(url='wss://example.invalid') as socket:
    ctx = await socket.open()
    task = getattr(ctx, background)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    request = asyncio.Future()
    if failed:
      request.set_exception(error)
      with pytest.raises(ValueError) as exc:
        await socket.wait(request, ctx=ctx)
      assert exc.value is error
    else:
      request.set_result('reply')
      assert await socket.wait(request, ctx=ctx) == 'reply'


@pytest.mark.asyncio
@pytest.mark.parametrize('timeout', [False, True])
async def test_wait_caller_cancellation_awaits_request_cleanup(timeout):
  started = asyncio.Event()
  cleaned = asyncio.Event()

  async def request():
    started.set()
    try:
      await _hang()
    finally:
      await asyncio.sleep(0)
      cleaned.set()

  async with RecordingSocket(url='wss://example.invalid') as socket:
    ctx = await socket.open()
    call = asyncio.create_task(socket.wait(request()))
    await started.wait()
    if timeout:
      with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(call, timeout=0)
    else:
      call.cancel()
      with pytest.raises(asyncio.CancelledError):
        await call
    assert cleaned.is_set()
    assert not ctx.listener.done()
    assert not ctx.pinger.done()


@pytest.mark.asyncio
async def test_cancelled_connect_waiter_leaves_owner_and_other_waiter_running():
  started, finish = asyncio.Event(), asyncio.Event()

  class GatedSocket(RecordingSocket):
    async def force_open(self):
      started.set()
      await finish.wait()
      return await super().force_open()

  async with GatedSocket(url='wss://example.invalid') as socket:
    owner = asyncio.create_task(socket.open())
    await started.wait()
    waiter = asyncio.create_task(socket.open())
    survivor = asyncio.create_task(socket.open())
    await asyncio.sleep(0)
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)
    finish.set()
    results = await asyncio.gather(owner, survivor, return_exceptions=True)
    assert all(isinstance(ctx, Context) for ctx in results), results
    assert results[0] is results[1] is await socket.open()
    assert socket.open_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('error_type', [ConnectionRefusedError, OSError, TimeoutError])
async def test_connect_transport_failures_are_network_errors(monkeypatch, error_type):
  error = error_type('synthetic connection failure')

  async def connect(*args, **kwargs):
    raise error

  monkeypatch.setattr(websockets, 'connect', connect)

  class RealSocket(Socket):
    def on_msg(self, msg):
      pass

  async with RealSocket(url='ws://example.invalid') as socket:
    with pytest.raises(NetworkError) as info:
      await socket.open()
    assert info.value.__cause__ is error


@pytest.mark.asyncio
async def test_wait_binds_nested_context_resolution_without_affecting_other_sockets():
  async with RecordingSocket(url='ws://example.invalid') as socket:
    async with RecordingSocket(url='ws://other.invalid') as other:
      original = await socket.open()
      await socket.close(original)
      replacement = await socket.open()

      async def request():
        assert await other.ctx is not original
        return await socket.ctx

      with pytest.raises(NetworkError):
        await socket.wait(request(), ctx=original)
      assert socket.open_calls == 2
      assert other.open_calls == 1
      assert await socket.ctx is replacement


@pytest.mark.asyncio
async def test_owner_exit_during_connect_closes_the_connection():
  """The owner leaves `async with` while the first connect is in flight. The connect closes
  what it opened and raises `ClosedByOwner`, instead of leaving a connection, listener and
  pinger with no owner."""
  from truewire_core.ws.socket import ClosedByOwner

  gate = asyncio.Event()
  started = asyncio.Event()
  opened: list[Context] = []

  @dataclass
  class Gated(Socket):
    def on_msg(self, msg):
      ...

    async def force_open(self) -> Context:
      started.set()
      await gate.wait()
      ctx = Context(
        ws=cast(websockets.ClientConnection, FakeConnection()),
        listener=asyncio.create_task(_hang()),
        pinger=asyncio.create_task(_hang()),
      )
      opened.append(ctx)
      return ctx

  socket = Gated(url='wss://example.invalid')
  async with socket:
    call = asyncio.create_task(socket.open())
    await started.wait()
  gate.set()
  with pytest.raises(ClosedByOwner):
    await call
  await asyncio.sleep(0)
  assert len(opened) == 1
  assert opened[0].listener.done() and opened[0].pinger.done()
  assert len(cast(FakeConnection, opened[0].ws).exit_calls) == 1
  assert isinstance(ClosedByOwner('x'), NetworkError)
