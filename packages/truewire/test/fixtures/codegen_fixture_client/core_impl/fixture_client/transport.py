"""Trivial in-memory transport backing the fixture client's hand-written `core/`.

Stands in for `truewire_core.http.HttpClient`/`truewire_core.ws.socket.Socket` -- the real
transport primitives every production client wraps (`docs/production_standards.md`
S10) -- so `test_codegen_generator_e2e.py` can construct a real client and drive its
generated call sites end to end without opening a socket or a network connection. A
canned reply is registered per `(method, path)` pair for RPC calls, and a canned
message list per channel for subscriptions; every call is also recorded in `calls`/
`subscriptions` so a test can assert on what the generated code actually sent.
"""

from typing_extensions import Any, AsyncIterator, NotRequired, TypedDict
from dataclasses import dataclass, field


class RecordedCall(TypedDict):
  """One RPC call the transport observed, for assertions in tests."""

  method: str
  path: str
  body: NotRequired[bytes | None]
  signed: bool


@dataclass
class InMemoryTransport:
  """Records every call/subscription and replies from a small, hand-seeded table.

  Args:
    api_key: Credential a caller configured, or `None` for a public-only client --
      mirrors a real transport's own credential wiring (S9), just enough for `core`
      to refuse a `signed` call with none configured.
  """

  api_key: str | None = None
  responses: dict[tuple[str, str], bytes] = field(default_factory=dict)
  channels: dict[str, list[bytes]] = field(default_factory=dict)
  calls: list[RecordedCall] = field(default_factory=list)
  subscriptions: list[str] = field(default_factory=list)

  def seed_response(self, method: str, path: str, body: bytes):
    """Register the canned reply for one `(method, path)` pair."""
    self.responses[(method, path)] = body

  def seed_channel(self, channel: str, messages: list[bytes]):
    """Register the canned push messages for one resolved channel."""
    self.channels[channel] = messages

  async def send(
    self,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    signed: bool = False,
  ) -> bytes:
    """Perform one RPC call, raising if it needs credentials this transport lacks.

    Raises:
      LookupError: No response was seeded for this `(method, path)` pair.
      PermissionError: `signed` is set and no `api_key` was configured.
    """
    if signed and self.api_key is None:
      raise PermissionError(f'{method} {path} needs credentials; none configured')
    self.calls.append(RecordedCall(method=method, path=path, body=body, signed=signed))
    key = (method, path)
    if key not in self.responses:
      raise LookupError(f'no canned response seeded for {method} {path}')
    return self.responses[key]

  async def iter_channel(self, channel: str) -> AsyncIterator[bytes]:
    """Yield every canned message seeded for `channel`, then stop."""
    self.subscriptions.append(channel)
    for message in self.channels.get(channel, []):
      yield message


def fill_template(template: str, values: dict[str, Any]) -> str:
  """Substitute every `{name}` placeholder in `template` from `values`.

  Shared by `RpcEndpoint.request`'s `path` and `.subscribe`'s `channel` -- design §7/§8
  give both the identical placeholder-substitution rule, resolved by `core`, never by
  generated code (`docs/spec/authoring.md`'s "location markers are eliminated, not
  redeclared").
  """
  result = template
  for name, value in values.items():
    result = result.replace(f'{{{name}}}', str(value))
  return result
