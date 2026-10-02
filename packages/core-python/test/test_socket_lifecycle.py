"""`Socket.open`/`close` under a dead connection, a slow close handshake and owner exits.

A raw WebSocket server on 127.0.0.1 controls each connection: delay the handshake, send a
frame the parser cannot read, drop without a close frame, or answer the client's close
frame late. From the TRU-451 and TRU-452 reviews of PR #47.
"""
import asyncio
import base64
import contextlib
import hashlib
import json
import time

import pytest

from truewire_core.exceptions import NetworkError
from truewire_core.ws.socket import ClosedByOwner, Socket

GUID = b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'


class RawServer:
  """One plan per accepted connection, in order. A plan is a dict of:
  handshake_delay (s), send (text frames right after the handshake), drop (close TCP
  after sending), close_delay (s before answering the client's close frame)."""

  def __init__(self, *plans):
    self.plans = list(plans)
    self.accepted = 0
    self.open = 0

  async def __aenter__(self):
    self.server = await asyncio.start_server(self.handle, '127.0.0.1', 0)
    self.url = f'ws://127.0.0.1:{self.server.sockets[0].getsockname()[1]}'
    return self

  async def __aexit__(self, *exc):
    self.server.close()

  async def handle(self, reader, writer):
    plan = self.plans[self.accepted] if self.accepted < len(self.plans) else {}
    self.accepted += 1
    self.open += 1
    try:
      head = await reader.readuntil(b'\r\n\r\n')
      key = next(
        line.split(b':', 1)[1].strip() for line in head.split(b'\r\n')
        if line.lower().startswith(b'sec-websocket-key:')
      )
      accept = base64.b64encode(hashlib.sha1(key + GUID).digest())
      await asyncio.sleep(plan.get('handshake_delay', 0))
      writer.write(
        b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
        b'Sec-WebSocket-Accept: ' + accept + b'\r\n\r\n'
      )
      for text in plan.get('send', []):
        data = text.encode()
        writer.write(bytes([0x81, len(data)]) + data)
      await writer.drain()
      if plan.get('drop'):
        return
      while True:
        b0, b1 = await reader.readexactly(2)
        length = b1 & 0x7F
        if length == 126:
          length = int.from_bytes(await reader.readexactly(2), 'big')
        elif length == 127:
          length = int.from_bytes(await reader.readexactly(8), 'big')
        await reader.readexactly(4 + length)
        if b0 & 0x0F == 0x8:
          await asyncio.sleep(plan.get('close_delay', 0))
          writer.write(bytes([0x88, 2]) + (1000).to_bytes(2, 'big'))
          await writer.drain()
          return
    except (asyncio.IncompleteReadError, ConnectionError):
      pass
    finally:
      self.open -= 1
      writer.close()


class JsonSocket(Socket):
  """A client whose parser raises on a frame it cannot read, as generated clients do."""

  def on_msg(self, msg):
    json.loads(msg)


async def current(socket):
  return await socket.ctx


async def gone(ctx):
  await ctx.ws.wait_closed()
  await asyncio.gather(ctx.listener, return_exceptions=True)


@pytest.mark.asyncio
async def test_open_while_another_close_holds_the_lock():
  """No owner. Connection A's listener dies on an unreadable frame (the TCP connection
  stays open) and the server is slow to answer A's close frame. Caller X replaces A:
  it closes A, holding the socket's close lock for the handshake. Caller Y connects B
  meanwhile, and the server drops B. Caller Z then asks for a connection.

  typed-core 0.9.2 closes B (closing is per connection) and connects C. At 14b82573,
  `close(B)` returns at once because the lock is held, so `open()` finds B again and
  recurses without ever awaiting: RecursionError."""
  async with RawServer(
    {'send': ['not json'], 'close_delay': 1.0}, {'drop': True}, {},
  ) as server:
    socket = JsonSocket(url=server.url)
    a = await current(socket)
    await asyncio.gather(a.listener, return_exceptions=True)
    assert isinstance(a.listener.exception(), json.JSONDecodeError)
    x = asyncio.create_task(current(socket))  # closes A: a 1 s close handshake
    await asyncio.sleep(0.2)
    b = await current(socket)  # Y
    await gone(b)
    try:
      c = await asyncio.wait_for(current(socket), 5)  # Z
      assert c.alive and c is not b
    finally:
      await asyncio.gather(x, return_exceptions=True)
      await socket.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_a_call_started_after_the_exit_does_not_get_closed_by_owner():
  """The owner leaves while its first connect is in flight (a slow handshake). A no-owner
  call made after that exit joins the connect in flight.

  typed-core 0.9.2: the caller that started before the exit gets ClosedByOwner; the one
  that started after it retries once on a fresh connection. At 14b82573 both get
  ClosedByOwner."""
  async with RawServer({'handshake_delay': 0.5}, {}) as server:
    socket = JsonSocket(url=server.url)
    await socket.__aenter__()
    before = asyncio.create_task(current(socket))
    await asyncio.sleep(0.1)
    await socket.__aexit__(None, None, None)
    after = asyncio.create_task(current(socket))  # a later call, no owner
    results = await asyncio.gather(before, after, return_exceptions=True)
    try:
      assert isinstance(results[0], ClosedByOwner)
      assert not isinstance(results[1], BaseException), repr(results[1])
      assert results[1].alive
    finally:
      await socket.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_owner_reenters_and_its_first_call_connects():
  """Same as above, but the owner itself re-enters `async with` and makes a call."""
  async with RawServer({'handshake_delay': 0.5}, {}) as server:
    socket = JsonSocket(url=server.url)
    async with socket:
      before = asyncio.create_task(current(socket))
      await asyncio.sleep(0.1)
    async with socket:
      again = await asyncio.gather(current(socket), return_exceptions=True)
      assert not isinstance(again[0], BaseException), repr(again[0])
    assert isinstance((await asyncio.gather(before, return_exceptions=True))[0], ClosedByOwner)


@pytest.mark.asyncio
async def test_a_replacement_connect_after_the_owner_left_does_not_outlive_it():
  """Inside `async with`, a call finds the connection dead (its listener died on an
  unreadable frame) and closes it to connect anew; the owner leaves during that close
  handshake. Nothing may be left open once both have finished.

  typed-core 0.9.2 raises ClosedByOwner for that call. At 14b82573 `open()` recurses
  with a fresh exit count, connects B after the owner left, and B stays open."""
  async with RawServer({'send': ['not json'], 'close_delay': 0.5}, {}) as server:
    socket = JsonSocket(url=server.url)
    await socket.__aenter__()
    a = await current(socket)
    await asyncio.gather(a.listener, return_exceptions=True)
    call = asyncio.create_task(current(socket))
    await asyncio.sleep(0.1)
    await socket.__aexit__(None, None, None)
    result, = await asyncio.gather(call, return_exceptions=True)
    await asyncio.sleep(0.1)
    leaked = server.open
    connected_after_exit = not isinstance(result, BaseException) and result.alive
    if not isinstance(result, BaseException):
      await socket.close(result)
    assert not connected_after_exit, 'a live connection outlives the owner'
    assert isinstance(result, NetworkError), repr(result)
    assert leaked == 0


@pytest.mark.asyncio
async def test_owner_exit_during_connect_does_not_hold_later_calls_for_the_close_handshake():
  """The owner leaves during a connect; the server answers close frames after 2 s. A
  no-owner call made once the connect has completed should not wait for that handshake.

  typed-core 0.9.2 fails the attempt, then closes outside the lock: the later call
  connects at once. At 14b82573 the close runs under the open lock, before the attempt
  fails, so the later call waits 2 s and then gets ClosedByOwner too."""
  async with RawServer({'handshake_delay': 0.3, 'close_delay': 2.0}, {}) as server:
    socket = JsonSocket(url=server.url)
    await socket.__aenter__()
    before = asyncio.create_task(current(socket))
    await asyncio.sleep(0.1)
    await socket.__aexit__(None, None, None)
    await asyncio.sleep(0.4)  # the connect has completed; its close handshake is running
    start = time.monotonic()
    later, = await asyncio.gather(current(socket), return_exceptions=True)
    elapsed = time.monotonic() - start
    await asyncio.gather(before, return_exceptions=True)
    if not isinstance(later, BaseException):
      await socket.close(later)
    assert not isinstance(later, BaseException), f'{later!r} after {elapsed:.1f} s'
    assert elapsed < 1


@pytest.mark.asyncio
async def test_a_joiner_of_dead_connects_does_not_recurse_forever():
  """The server accepts every handshake and drops the connection at once (maintenance,
  a rate limit). Caller B retries in a loop; caller A makes one call. A joins B's
  connect each time, finds it dead, closes it and recurses; B wins the next connect.
  A should get the connection's failure; before TRU-452 (and 0.9.2) this raised RecursionError
  after ~1000 connects."""
  async with RawServer(*[{'drop': True}] * 5000) as server:
    socket = JsonSocket(url=server.url)
    stop = False

    async def retrier():
      while not stop:
        with contextlib.suppress(Exception):
          await current(socket)
        await asyncio.sleep(0)

    b = asyncio.create_task(retrier())
    await asyncio.sleep(0.05)
    try:
      before = server.accepted  # B's own connects so far, not A's
      try:
        ctx = await asyncio.wait_for(current(socket), 20)
      except NetworkError:
        pass  # the connection this call joined failed: fine
      else:
        assert ctx is not None
      during = server.accepted - before
      assert during < 50, f'{during} connects during one call'
    finally:
      stop = True
      b.cancel()
      await asyncio.gather(b, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_close_cancelled_mid_handshake_aborts_the_connection():
  """Connection A's listener dies on an unreadable frame; the server never answers A's
  close frame. A caller replacing A is cancelled (say by asyncio.timeout) during that
  handshake. A is already detached, so nothing will ever close it again.
  typed-core 0.9.2 aborts the TCP connection on that cancellation; before TRU-452 it stayed
  CLOSING past websockets' 10 s close_timeout."""
  async with RawServer({'send': ['not json'], 'close_delay': 60}, {}) as server:
    socket = JsonSocket(url=server.url)
    a = await current(socket)
    await asyncio.gather(a.listener, return_exceptions=True)
    assert isinstance(a.listener.exception(), json.JSONDecodeError)
    replacing = asyncio.create_task(current(socket))
    await asyncio.sleep(0.2)
    replacing.cancel()
    await asyncio.gather(replacing, return_exceptions=True)
    try:
      await asyncio.wait_for(a.ws.wait_closed(), 1)
    finally:
      await socket.__aexit__(None, None, None)
