"""Generic pytest coverage replaying a project's recorded examples through the real client.

See `docs/testing.md`. Each factory below returns one pytest test function, parametrized
over every recorded example for a project, that the project's own `test/` module assigns
to a module-level `test_*` name — reusing the project's existing `conftest.py` fixtures
rather than standardizing client construction, which stays project-specific.
"""

import inspect
from pathlib import Path

import pytest
from typing_extensions import Any

from .examples import (
  client_identifier,
  coerce_example_call,
  coerce_ws_example_call,
  resolve_endpoint_function,
  run_example_request,
)
from .project import Project, resolve
from .spec import (
  ExampleRequest,
  HttpExample,
  WsExample,
  client_http_examples,
  client_ws_examples,
)


def build_http_replay_test(
  root: Path | Project,
  *,
  client_fixture: str = 'client',
  resolve_root: str | None = None,
) -> Any:
  """Build a pytest test replaying every recorded HTTP example for the project.

  Assign the return value to a module-level `test_*` name in the project's own test file.

  Args:
    root: Project (or project root) whose recorded examples are replayed.
    client_fixture: Name of the fixture yielding a client already pointed at a running
      mock server, with `validate=True`.
    resolve_root: Attribute to descend into on the fixture value before resolving
      `endpoint.function` against it, for a client whose top-level object nests HTTP
      endpoints under a sub-router (e.g. `client.http.market...`) rather than
      exposing `endpoint.function`'s own first segment directly (e.g.
      `client.data...`). The fixture value itself is still what the `async with` block
      manages.

  An endpoint with no generated method yet — a spec added since the project's package was
  last regenerated, per `truewire surface` — is skipped rather than failed; that gap
  is `truewire surface`'s gate to report, not this one's.
  """
  project = resolve(root)
  root = project.root
  spec = project.spec_dir
  examples = client_http_examples(project)
  ids = [
    f'{example.endpoint.resolved_function(example.endpoint_path, spec)}'
    f'[{example.example_id}]'
    for example in examples
  ]

  @pytest.mark.asyncio
  @pytest.mark.parametrize('example', examples, ids=ids)
  async def test_http_examples_replay(
    example: HttpExample, request: pytest.FixtureRequest
  ):
    client = request.getfixturevalue(client_fixture)
    async with client:
      target = getattr(client, resolve_root) if resolve_root else client
      try:
        await run_example_request(
          target, example.endpoint, example.request, client_root=project,
          endpoint_path=example.endpoint_path,
        )
      except AttributeError as e:
        function = example.endpoint.resolved_function(example.endpoint_path, spec)
        pytest.skip(f'{function} has no generated method yet: {e}')

  return test_http_examples_replay


def build_ws_replay_test(
  root: Path | Project,
  *,
  client_fixture: str = 'client',
  resolve_root: str | None = None,
) -> Any:
  """Build a pytest test replaying every recorded WebSocket `kind: 'stream'` example for
  `client_name`: subscribe, read one recorded push (when the example captured any),
  unsubscribe.

  Assign the return value to a module-level `test_*` name in the project's own test file,
  mirroring `build_http_replay_test`. See `docs/testing.md`'s "Generic Replay" section.

  Args:
    root: Project (or project root) whose recorded examples are replayed.
    client_fixture: Name of the fixture yielding a client already pointed at a running
      mock WebSocket server, with `validate=True`.
    resolve_root: Attribute to descend into on the fixture value before resolving
      `endpoint.function` against it — see `build_http_replay_test`'s own parameter for
      the general reasoning. Most WS-carrying projects' `endpoint.function` already
      includes its own stream namespace (`streams.spot_margin.ticker`, reachable
      straight off `client.streams...`), so this is usually left `None`.

  Scoped to `endpoint.spec.kind == 'stream'`. `client_ws_examples` also loads
  `kind: 'rpc'` examples for a project whose RPC calls happen to travel over WebSocket
  — a single request/reply, not a subscribe/push/unsubscribe lifecycle, so they need
  their own binder (`build_ws_rpc_replay_test`) and aren't attempted here.

  An endpoint with no generated method yet is skipped rather than failed, the same way
  `build_http_replay_test` skips on `AttributeError` — a `Streams.futures` that is still
  a bare, unwired `StreamEndpoint` resolves no method to call for any of its examples yet.

  An example with no recorded push (`messages` and `message_frames` both empty —
  `WsExample.has_messages`) is skipped rather than failed: nothing arrives to pull with
  `anext`, and this builder's whole assertion *is* that pull's own `validate=True`
  pydantic validation succeeding — see `docs/testing.md`'s "Current non-goals" (no
  field-value assertions here, ever; that's what a project's own handwritten
  `test/test_streams.py` is for). This is common for a genuinely `unverified` endpoint
  that only captured a subscribe ack. A `message_frames`-only example (protobuf) is not
  special-cased beyond that: nothing here decodes protobuf, but a protobuf example is
  expected to also carry decoded `messages` as a human-readable sidecar, so `has_messages`
  is already true for those — revisit if an example ever records `message_frames` alone.
  """
  project = resolve(root)
  root = project.root
  spec = project.spec_dir
  identifier = client_identifier(project)
  examples = [
    example for example in client_ws_examples(project) if example.endpoint.spec.kind == 'stream'
  ]
  # `resolved_function` (not the bare `.function` field), mirroring `build_http_replay_test`'s
  # identical treatment just above -- `function` is unset once a project migrates onto the
  # directory-derived request/response shape, and the bare field silently
  # collapsed every example's id to the literal string `'None[...]'` before this fix.
  ids = [
    f'{example.endpoint.resolved_function(example.endpoint_path, spec)}'
    f'[{example.example_id}]'
    for example in examples
  ]

  @pytest.mark.asyncio
  @pytest.mark.parametrize('example', examples, ids=ids)
  async def test_ws_examples_replay(example: WsExample, request: pytest.FixtureRequest):
    function = example.endpoint.resolved_function(example.endpoint_path, spec)
    if not example.has_messages:
      pytest.skip(
        f'{function}[{example.example_id}]: no recorded push -- nothing '
        "to replay past the subscribe step (see build_ws_replay_test's own docstring)."
      )

    client = request.getfixturevalue(client_fixture)
    async with client:
      target = getattr(client, resolve_root) if resolve_root else client
      try:
        fn = resolve_endpoint_function(
          target, example.endpoint,
          endpoint_path=example.endpoint_path, spec_root=spec,
        )
        args, kwargs = coerce_ws_example_call(fn, example.parameters, identifier=identifier)
        manager = fn(*args, **kwargs)
      except AttributeError as e:
        pytest.skip(f'{function} has no generated method yet: {e}')

      async with manager as stream:
        await anext(stream.__aiter__())

  return test_ws_examples_replay


def build_ws_rpc_replay_test(
  root: Path | Project,
  *,
  client_fixture: str = 'client',
  resolve_root: str | None = None,
) -> Any:
  """Build a pytest test replaying every recorded `kind: 'rpc'` WebSocket example for
  `client_name` -- a single request/reply call, never a `kind: 'stream'` subscribe/push
  lifecycle (`build_ws_replay_test`'s own scope).

  Assign the return value to a module-level `test_*` name in the project's own test file,
  mirroring `build_http_replay_test`/`build_ws_replay_test`. See `docs/testing.md`'s
  "Generic Replay" section.

  Args:
    root: Project (or project root) whose recorded examples are replayed.
    client_fixture: Name of the fixture yielding a client already pointed at a running
      WS mock server, with `validate=True`.
    resolve_root: Attribute to descend into on the fixture value before resolving
      `endpoint.function` against it — see `build_http_replay_test`'s own parameter for
      the general reasoning.

  `resolve_endpoint_function`/`coerce_example_call` (`truewire.examples`) are already
  transport-agnostic — both just walk `endpoint.function` and bind a call by inspecting
  the resolved callable's signature — so this reuses them directly rather than
  reimplementing binding. The one adaptation a WS-RPC example needs: a
  `WsParametersExample` only carries `parameters`/`payload` (no `args`/`kwargs`, no
  auto-fill), so its recorded `parameters` dict is wrapped in a real `ExampleRequest`
  first, giving it the exact same binding path an HTTP example already gets.

  An endpoint with no generated method yet is skipped rather than failed, the same way
  `build_http_replay_test`/`build_ws_replay_test` skip on `AttributeError`.

  Originally built locally in one project's own test suite and promoted here, unchanged
  in behavior, once a second project needed the same generic WS-RPC replay coverage. See
  standard S19.
  """
  project = resolve(root)
  root = project.root
  spec = project.spec_dir
  identifier = client_identifier(project)

  # `resolved_function` (not the bare `.function` field), mirroring `build_http_replay_test`'s
  # identical treatment above -- see `build_ws_replay_test`'s identical comment for why.
  def function_of(example: WsExample) -> str:
    return example.endpoint.resolved_function(example.endpoint_path, spec)

  examples = sorted(
    (example for example in client_ws_examples(project) if example.endpoint.spec.kind == 'rpc'),
    key=lambda example: (function_of(example), example.example_id),
  )
  ids = [f'{function_of(example)}[{example.example_id}]' for example in examples]

  @pytest.mark.asyncio
  @pytest.mark.parametrize('example', examples, ids=ids)
  async def test_ws_rpc_examples_replay(example: WsExample, request: pytest.FixtureRequest):
    client = request.getfixturevalue(client_fixture)
    async with client:
      target = getattr(client, resolve_root) if resolve_root else client
      try:
        fn = resolve_endpoint_function(
          target, example.endpoint,
          endpoint_path=example.endpoint_path, spec_root=spec,
        )
        wrapped = ExampleRequest(parameters=example.parameters.parameters)
        args, kwargs = coerce_example_call(fn, wrapped, identifier=identifier)
      except AttributeError as e:
        pytest.skip(f'{function_of(example)} has no generated method yet: {e}')
      # A genuine multi-transport rpc method takes a real `transport` keyword rather
      # than being reached
      # through a separate WS-bound object -- this replay's whole point is exercising
      # the WS side specifically, so it has to force that choice explicitly. Left alone,
      # `fn`'s own default (`spec.transports[0]`, generally `'http'`) would silently
      # send the call over the fixture's HTTP transport instead, the exact opposite of
      # what a WS-RPC replay is meant to prove.
      if 'transport' in inspect.signature(fn).parameters:
        kwargs['transport'] = 'ws'
      await fn(*args, **kwargs)

  return test_ws_rpc_examples_replay
