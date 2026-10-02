"""`dispatch.rs`: every generated method reachable by its dotted function path.

Python reaches a method by `getattr` and TypeScript by indexing the client; Rust has no
reflection, so the backend writes the lookup instead -- one `match` arm per endpoint, on the
root struct:

```rust
impl Kraken {
    pub async fn call(&self, function: &str, request: Value, options: CallOptions) -> Result<Value>;
    pub async fn call_raw(&self, function: &str, request: Value, options: CallOptions) -> Result<Value>;
    pub async fn subscribe(&self, function: &str, parameters: Value, options: CallOptions) -> Result<Stream<Value>>;
    pub async fn subscribe_raw(&self, function: &str, parameters: Value, options: CallOptions) -> Result<Stream<Value>>;
}
```

`call` decodes the wire request into the endpoint's `Request`, calls the typed method and
dumps what it returned back to a wire value; `call_raw` calls the `_raw` twin. The pair is
what a replay test compares (the typed value must dump back to exactly the body the wire
sent), and what anything driving a client from data -- a CLI, an MCP server, a recorded
session -- calls without knowing the types. An unknown function is a `LogicError`; an
endpoint returning nothing answers `null`.

The module is private and every method `#[doc(hidden)]`: they stay callable on the root
(a replay test in the crate's `tests/`, a CLI or MCP server depending on it), but a
caller browsing the docs sees the typed methods only. A cargo feature would hide them as
well, but every crate and test that calls them would then have to turn it on, and the
generator does not own `Cargo.toml`. Each doc's example function is one of this plan's
own.
"""
from truewire.plan.model import PackagePlan

from .endpoint import EndpointModule
from .names import snake_ident, string
from .printer import BANNER, Imports, Writer
from .types import CORE

DISPATCH_FILE = 'dispatch.rs'


def render_dispatch(
  plan: PackagePlan, *, root_name: str, endpoints: dict[str, EndpointModule],
) -> str | None:
  """`dispatch.rs` for the reachable `endpoints`, or `None` when there are none."""
  if not endpoints:
    return None
  imports = Imports()
  imports.add(CORE, 'CallOptions')
  imports.add(CORE, 'Error')
  imports.add(CORE, 'Result')
  imports.add('crate', root_name)
  by_kind: dict[str, list[tuple[str, EndpointModule]]] = {'rpc': [], 'stream': []}
  for function in sorted(endpoints):
    module = endpoints[function]
    by_kind.setdefault(module.kind, []).append((function, module))
  deprecated = {
    endpoint.function for endpoint in plan.endpoints if endpoint.deprecated and endpoint.function in endpoints
  }

  body = Writer(1)
  first = True

  def method(name: str, subject: str, returns: str, items: list[tuple[str, EndpointModule]], raw: bool, doc: str):
    nonlocal first
    if not first:
      body.blank()
    first = False
    body.doc(doc)
    body.line('#[doc(hidden)]')
    if any(function in deprecated for function, _ in items):
      body.line('#[allow(deprecated)]')
    body.signature(
      f'pub async fn {name}',
      ['&self', 'function: &str', f'{subject}: serde_json::Value', 'options: CallOptions'],
      f' -> Result<{returns}> {{',
    )
    with body.indented():
      with body.block('match function {'):
        for function, module in items:
          _arm(body, function, module, subject=subject, raw=raw, imports=imports)
        kind = 'stream' if subject == 'parameters' else 'rpc'
        body.line(f'_ => Err(Error::logic(format!("no {kind} endpoint {{function}}"))),')
    body.line('}')

  rpc = by_kind.get('rpc', [])
  stream = by_kind.get('stream', [])
  grpc = by_kind.get('grpc', [])
  if rpc or stream:
    imports.module(f'{CORE}::serde_json')
  if rpc:
    method(
      'call', 'request', 'serde_json::Value', rpc, raw=False,
      doc=f'Call the `rpc` endpoint `function` names (`{rpc[0][0]}`) with a wire '
          'request: decoded into its `Request`, sent, and the typed response dumped back to a wire value.',
    )
    method(
      'call_raw', 'request', 'serde_json::Value', rpc, raw=True,
      doc='`call` through the `_raw` twin: the wire body as it came.',
    )
  if stream:
    imports.add(CORE, 'Stream')
    method(
      'subscribe', 'parameters', 'Stream<serde_json::Value>', stream, raw=False,
      doc=f'Subscribe to the `stream` endpoint `function` names (`{stream[0][0]}`) with wire parameters; every '
          'pushed message is validated, then dumped back to a wire value.',
    )
    method(
      'subscribe_raw', 'parameters', 'Stream<serde_json::Value>', stream, raw=True,
      doc='`subscribe` through the `_raw` twin: the frames as they came.',
    )

  if grpc:
    imports.add('prost', 'Message')
    imports.add('crate::grpc_codec', 'decode_message')
    if not first:
      body.blank()
    first = False
    body.doc(
      f'Call the gRPC endpoint `function` names (`{grpc[0][0]}`) with an encoded request message: '
      'decoded into its `Request`, sent, and the response message encoded back.'
    )
    body.line('#[doc(hidden)]')
    if any(function in deprecated for function, _ in grpc):
      body.line('#[allow(deprecated)]')
    body.signature(
      'pub async fn call_grpc', ['&self', 'function: &str', 'request: &[u8]', 'options: CallOptions'],
      ' -> Result<Vec<u8>> {',
    )
    with body.indented():
      with body.block('match function {'):
        for function, module in grpc:
          fields = [f'.{snake_ident(segment, fallback="router")}' for segment in function.split('.')[:-1]]
          with body.block(f'{string(function)} => {{'):
            body.chain(
              'let response = ', 'self', [*fields, f'.{module.main}(decode_message(request)?, options)', '.await?'], ';',
            )
            body.line('Ok(response.encode_to_vec())')
        body.line('_ => Err(Error::logic(format!("no grpc endpoint {function}"))),')
    body.line('}')

  w = Writer()
  w.line(BANNER)
  w.line('//!')
  w.doc(
    'Every generated method reachable by its dotted function path: the lookup a replay test, '
    'a CLI or anything else driving the client from data uses instead of reflection.',
    inner=True,
  )
  w.blank()
  for line in imports.render():
    w.line(line)
  w.blank()
  w.line(f'impl {root_name} {{')
  for line in body.render().rstrip('\n').split('\n'):
    w.line(line) if line else w.blank()
  w.line('}')
  return w.render()


def _arm(w: Writer, function: str, module: EndpointModule, *, subject: str, raw: bool, imports: Imports) -> None:
  """One `"a.b.c" => { ... }` arm."""
  segments = function.split('.')
  fields = [f'.{snake_ident(segment, fallback="router")}' for segment in segments[:-1]]
  target = module.raw if raw and module.raw is not None else module.main
  target = module.aliases.get(target, target)
  args = ', '.join([subject, 'options'] if module.takes_request else ['options'])
  with w.block(f'{string(function)} => {{'):
    if module.takes_request:
      imports.add(CORE, 'decode')
      w.line(f'let {subject} = decode({subject})?;')
    else:
      w.line(f'let _ = {subject};')
    is_stream = module.kind == 'stream'
    if raw and module.raw is not None:
      w.chain('', 'self', [*fields, f'.{target}({args})', '.await'])
      return
    if is_stream:
      imports.add(CORE, 'dump')
      w.chain('let stream = ', 'self', [*fields, f'.{target}({args})', '.await?'], ';')
      if module.typed_reply:
        w.chain('', 'stream', ['.map(|message| dump(&message))', '.map_reply(|reply| dump(&reply))'])
      else:
        w.line('Ok(stream.map(|message| dump(&message)))')
    elif module.raw is None or module.unit_response:
      # Nothing to dump: binding a `()` response would trip `clippy::let_unit_value`.
      w.chain('', 'self', [*fields, f'.{target}({args})', '.await?'], ';')
      w.line('Ok(serde_json::Value::Null)')
    else:
      imports.add(CORE, 'dump')
      w.chain('let response = ', 'self', [*fields, f'.{target}({args})', '.await?'], ';')
      w.line('dump(&response)')


__all__ = ['DISPATCH_FILE', 'render_dispatch']
