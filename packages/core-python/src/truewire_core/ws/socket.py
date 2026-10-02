from typing_extensions import Awaitable, TypeVar
from abc import ABC, abstractmethod
import asyncio
import contextlib
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import timedelta
import logging
import websockets

from truewire_core.exceptions import NetworkError

T = TypeVar('T')

logger = logging.getLogger(__name__)

class ClosedByOwner(NetworkError):
  """The socket's owner left `async with` while this connect was in flight."""

@dataclass
class Context:
  ws: websockets.ClientConnection
  listener: asyncio.Task
  pinger: asyncio.Task
  closed: bool = False
  """Set once `Socket.close` has started closing this connection."""

  @property
  def alive(self) -> bool:
    """Whether this connection can still carry work: open and served."""
    return (
      not self.closed and self.ws.state == websockets.State.OPEN
      and not self.listener.done() and not self.pinger.done()
    )

  def failure(self) -> BaseException:
    """Why this connection can no longer carry work: the listener's or pinger's own
    exception when one of them failed, otherwise a closed connection."""
    for task in (self.listener, self.pinger):
      if task.done() and not task.cancelled() and (exc := task.exception()) is not None:
        return exc
    return NetworkError('WebSocket connection closed')

@dataclass
class Socket(ABC):
  """Base WebSocket client.
  
  ### Features
  - Connection handling
  - Optional periodic pinging
  - Message listener
  - Error propagation via `.wait(...)`

  ### Requires implementing
  - `on_msg`: handle incoming messages.
  - `ping`: if you the server requires some custom pinging mechanism

  ### Concurrency Contract
  1. Connection: single owner via `async with`, also supports lazy no-owner use
  2. Requests: many concurrent `wait()` calls OK

  ### Error Propagation

  The websocket connection could fail without you knowing.
  To avoid that happening, you can ensure they are propagated by using `.wait(...)`.

  **Example**:

  ```python
  future = asyncio.Future()

  class MySocket(Socket):
    def on_msg(self, msg: str | bytes):
      future.set_result(msg)

  async with MySocket('wss://example.com') as ws:
    result = await ws.wait(future)
  ```

  If you awaited directly and an exception happened, you would wait forever.
  
  This way, if the connection fails, a `NetworkError` will be raised. An exception the
  listener itself raised (say `on_msg` could not parse a frame) is raised as it is.
  """
  url: str
  timeout: timedelta = field(kw_only=True, default=timedelta(seconds=10))
  ping_interval: timedelta = field(kw_only=True, default=timedelta(hours=24))
  proxy: str | None = field(kw_only=True, default=None)
  """HTTP(S) proxy URL the connection goes through, sent `CONNECT` (packages clause P18).
  Left `None` or `''`, websockets reads the environment: `WSS_PROXY`/`WS_PROXY`, then
  `SOCKS_PROXY`, then `HTTPS_PROXY`, then (for `ws://` only) `HTTP_PROXY`; `NO_PROXY` is
  honoured and `ALL_PROXY` is not read. With none of those set, on macOS and Windows it
  falls back to the system proxy settings, bypass list included; httpx reads the same
  system proxies but not that bypass list, so a host the system exempts can be reached
  directly over WebSocket and through the proxy over HTTP. An explicit one ignores the
  environment and the system settings. A `socks5://` URL needs `python-socks`."""
  _ctx_future: 'asyncio.Future[Context] | None' = field(default=None, init=False, repr=False)
  """Backing store for `ctx_future`, `None` until something first reaches for it."""
  _bound_ctx: ContextVar[Context | None] = field(
    default_factory=lambda: ContextVar('socket_context', default=None), init=False, repr=False,
  )
  open_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
  close_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
  _exits: int = field(default=0, init=False, repr=False)
  """How many times an owner has left `async with`: a connect that sees it change while
  in flight closes what it opened (typed-core 0.9.2's `exits`)."""

  @property
  def ctx_future(self) -> 'asyncio.Future[Context]':
    """Future holding the connection once one is opened, created on first access.

    Deliberately not a `default_factory`: `asyncio.Future()` binds the running loop as it
    is constructed, so a socket built outside a loop -- a client constructed in a
    synchronous test fixture, or at module scope -- raised `RuntimeError` before anything
    had a chance to connect. A client that owns a socket it may never use has to be
    constructible anywhere; the loop is needed when the socket opens, not when it is built.
    """
    if self._ctx_future is None:
      self._ctx_future = asyncio.Future()
    return self._ctx_future

  @abstractmethod
  def on_msg(self, msg: str | bytes):
    ...

  async def ping(self, ws: websockets.ClientConnection):
    """Ping the server.

    If implemented, this is called periodically by the pinger task.

    Args:
      ws: The live connection, handed directly rather than resolved via `self.ws` —
        `pinger` runs from the moment `force_open` creates it, before `self.ctx` has
        anything to resolve to.
    """
    raise NotImplementedError

  async def pinger(self, ws: websockets.ClientConnection):
    while True:
      await asyncio.sleep(self.ping_interval.total_seconds())
      try:
        await self.ping(ws)
      except NotImplementedError:
        ...

  @property
  async def ctx(self) -> Context:
    """The current connection context, opening one first if none exists yet.

    Reading this property is itself a connect-on-demand: it is right for any caller that
    wants to *use* the socket, but wrong for `__aexit__`, which must be able to close
    without ever opening. Inside `wait`, it resolves only that request's connection
    and raises `NetworkError` if it has closed.
    """
    if (ctx := self._bound_ctx.get()) is not None:
      if not ctx.alive:
        raise ctx.failure()
      return ctx
    return await self.open()

  @property
  async def ws(self) -> websockets.ClientConnection:
    return (await self.ctx).ws

  async def __aenter__(self):
    """Take ownership without connecting; the socket opens lazily on first use."""
    return self

  async def __aexit__(self, exc_type, exc_value, traceback):
    """Close the connection if one was opened; do nothing when none ever was.

    Deliberately not `await self.ctx` -- that property falls through to `open()`, so a
    client that only ever used the other transport would dial out here just to hang up.
    """
    self._exits += 1
    if self._ctx_future is not None and self._ctx_future.done():
      await self.close(self._ctx_future.result(), exc_type, exc_value, traceback)
  
  async def force_open(self):
    """Connect, then hand the live connection straight to `listener`/`pinger`.

    Deliberately not `self.listener()`/`self.pinger()` resolving `ws` themselves via
    `self.ws` — that property goes through `self.ctx` -> `self.open()`, which is *this
    call*, still in progress. A subclass override that needs to send-and-await a reply
    during its own `force_open` (an auth handshake, say) would otherwise deadlock: the
    listener can't start delivering until `self.ctx_future` resolves, and it only
    resolves once `force_open` returns — which it can't, waiting on a reply only the
    listener can deliver. Passing `ws` as a parameter here means `listener`/`pinger`
    never need `self.ctx` at all, so there's nothing left for them to wait on.
    """
    async def connect():
      try:
        return await websockets.connect(
          self.url, open_timeout=self.timeout.total_seconds(),
          proxy=self.proxy or True,
        )
      except (websockets.exceptions.WebSocketException, OSError, asyncio.TimeoutError) as e:
        raise NetworkError(f'Failed to connect to {self.url}') from e

    ws = await connect()
    logger.info('Connected!')
    return Context(
      ws=ws,
      listener=asyncio.create_task(self.listener(ws)),
      pinger=asyncio.create_task(self.pinger(ws)),
    )
  
  async def open(self):
    return await self._open(self._exits)

  async def _open(self, exits: int, *, retried: bool = False, replaced: bool = False) -> Context:
    if self.open_lock.locked() or self.ctx_future.done():
      try:
        ctx = await asyncio.shield(self.ctx_future)
      except ClosedByOwner:
        # Joined a connect that started before an exit this call did not see: try once more.
        if retried or self._exits != exits:
          raise
        return await self._open(exits, retried=True, replaced=replaced)
      if ctx.alive:
        return ctx
      await self.close(ctx)
      if self._exits != exits:
        raise ClosedByOwner('The socket was closed by its owner')
      if replaced:
        # The connection this call waited for died too: report it, as its connector would.
        raise ctx.failure()
      return await self._open(exits, retried=retried, replaced=True)

    async with self.open_lock:
      logger.info('Connecting...')
      attempt = self.ctx_future
      try:
        ctx = await self.force_open()
      except BaseException as exc:
        # Release every caller waiting on this attempt, including on cancellation.
        # Retrieve the exception even when no other caller is waiting on the future.
        if not attempt.done():
          attempt.set_exception(exc if isinstance(exc, Exception) else NetworkError('Connecting was cancelled'))
          attempt.exception()
        self._ctx_future = None
        raise
      if self._exits == exits:
        attempt.set_result(ctx)
        return ctx
      self._ctx_future = None
      error = ClosedByOwner('The socket was closed by its owner while connecting')
      attempt.set_exception(error)
      attempt.exception()
    # Outside the lock: a later call connects anew instead of waiting for this handshake.
    await self.force_close(ctx)
    raise error

  async def force_close(self, ctx: Context, exc_type=None, exc_value=None, traceback=None):
    ctx.listener.cancel()
    ctx.pinger.cancel()
    await ctx.ws.__aexit__(exc_type, exc_value, traceback)

  async def close(self, ctx: Context, exc_type=None, exc_value=None, traceback=None):
    """Close `ctx` once; closing one connection never skips closing another."""
    if ctx.closed:
      return
    ctx.closed = True
    future = self._ctx_future
    if future is not None and future.done() and not future.cancelled() \
        and future.exception() is None and future.result() is ctx:
      self._ctx_future = None
    try:
      await self.force_close(ctx, exc_type, exc_value, traceback)
    except asyncio.CancelledError:
      # Detached already, so nothing else will close it: drop the TCP connection.
      ctx.ws.transport.abort()
      raise

  async def listener(self, ws: websockets.ClientConnection):
    while True:
      try:
        msg = await ws.recv()
        logger.debug('Received: %s', msg)
        self.on_msg(msg)
      except websockets.exceptions.WebSocketException as e:
        logger.error('Error receiving message: %s', e)
        raise NetworkError('Error receiving message') from e

  async def wait(self, fut: Awaitable[T], *, ctx: Context | None = None) -> T:
    """Wait for a future to complete, propagating any exceptions in the background tasks.

    A completed request wins over connection shutdown. Otherwise a cancelled or
    normally finished background task means the connection closed. Caller cancellation
    still propagates, after cancelling and awaiting the inner request. Nested reads of
    `self.ctx` in that request stay bound to this connection, including cleanup: they
    never reconnect after it closes. The binding is local to this socket and task.

    Args:
      fut: Future to wait for.
      ctx: Connection context to race against, if already held. Defaults to resolving
        `self.ctx` -- the only exception is a `force_open` override waiting on its own
        bootstrap reply, where `self.ctx` isn't resolved yet (it resolves through this
        same `force_open` call, still in progress) but the override already has the
        `Context` it just built, from `ctx = await super().force_open()`.
    """
    if ctx is None:
      ctx = await self.ctx
    async def coro():
      token = self._bound_ctx.set(ctx)
      try:
        return await fut
      finally:
        self._bound_ctx.reset(token)
    task = asyncio.create_task(coro())
    try:
      done, _ = await asyncio.wait([task, ctx.listener, ctx.pinger], return_when='FIRST_COMPLETED')
      if task.done():
        return task.result()
      background = ctx.listener if ctx.listener in done else ctx.pinger
      if not background.cancelled() and (exc := background.exception()) is not None:
        raise exc
      raise NetworkError('WebSocket connection closed')
    finally:
      task.cancel()
      with contextlib.suppress(asyncio.CancelledError):
        await task
