"""Generator.endpoint() dispatches kind: 'grpc' to grpc_endpoint(), and grpc_endpoint()
itself renders the direct, mechanically-derived stub call design §9 describes (Task 17).

`grpc_endpoint`'s field-level type resolution comes from actually importing and
introspecting a real (here, synthetic) generated proto package -- there is no
`openapi: Operation`/JSON Schema to derive it from, unlike `rpc_endpoint`/
`stream_endpoint` (`GrpcEndpointSpec.request`/`.response` are plain FQCN strings). The
fixture package at `test/fixtures/grpc_proto_client/` is hand-written to the same shape
a real betterproto2-generated one has (a `QueryStub` class, message dataclasses under
`<package>.protos.<dotted-proto-package>`) without depending on `betterproto2`/
`grpclib` at all -- see its own module docstrings for what each fixture module exercises.
"""
import ast
from pathlib import Path

import pytest

from truewire.project import resolve
from truewire.codegen.python import Generator
from truewire.spec import Endpoint

FIXTURE_ROOT = Path(__file__).parent / 'fixtures' / 'grpc_proto_client'


def _grpc_endpoint(**overrides):
  data = {
    'function': 'chain.bank.balance',
    'spec': {
      'kind': 'grpc',
      'service': 'cosmos.bank.v1beta1.Query',
      'rpc': 'Balance',
      'request': 'cosmos.bank.v1beta1.QueryBalanceRequest',
      'response': 'cosmos.bank.v1beta1.QueryBalanceResponse',
      'proto': 'cosmos/bank/v1beta1/query.proto',
      'description': 'Query one denom balance.',
    },
  }
  data.update(overrides)
  return Endpoint.model_validate(data)


def _generator() -> Generator:
  """A `Generator` wired the way the CLI wires one, against the synthetic proto fixture
  -- only `client_root` matters here (`grpc_endpoint` needs no `codegen/config.toml`/
  `router.json`-resolved core, unlike `rpc_endpoint`/`stream_endpoint`: its base class
  is always `truewire_core.grpc.GrpcEndpoint`)."""
  generator = Generator()
  generator.project = resolve(FIXTURE_ROOT)
  return generator


def test_base_grpc_endpoint_not_implemented():
  generator = Generator()
  with pytest.raises(NotImplementedError):
    generator.grpc_endpoint(_grpc_endpoint(), {}, class_name='Balance', method_name='balance')


def test_endpoint_dispatches_grpc_to_grpc_endpoint():
  class Recording(Generator):
    def grpc_endpoint(self, endpoint, references, *, class_name, method_name):
      return f'GRPC:{class_name}.{method_name}'

  generator = Recording()
  result = generator.endpoint(
    _grpc_endpoint(), {}, class_name='Balance', method_name='balance',
  )
  assert result == 'GRPC:Balance.balance'


def test_grpc_endpoint_renders_direct_stub_call_all_fields_required():
  """`cosmos.bank.v1beta1.Query/Balance` -- both `QueryBalanceRequest` fields
  (`address`, `denom`) are plain `str`, neither declared `optional_scalars`, so both are
  required generated parameters passed straight through unconditionally. Matches the
  real, hand-written `typed_dydx.chain.modules.bank.balance` module exactly (mechanical
  derivation, not a special case): `QueryStub(self.channel).balance(
  QueryBalanceRequest(address=address, denom=denom))`.
  """
  generator = _generator()
  code = generator.grpc_endpoint(
    _grpc_endpoint(), {}, class_name='Balance', method_name='balance',
  )
  ast.parse(code)  # must be valid Python, not just non-crashing generation

  assert 'from truewire_core.grpc import GrpcEndpoint, wrap_exceptions' in code
  # Stub + both message types resolve to one real import from the fixture's own
  # generated-proto module (`merge_imports` dedupes them onto one `from ... import`
  # line-group, wrapped since it's past 80 chars).
  assert 'from grpc_proto_client.protos.cosmos.bank.v1beta1 import (' in code
  assert 'QueryBalanceRequest,' in code
  assert 'QueryBalanceResponse,' in code
  assert 'QueryStub' in code
  assert 'class Balance(GrpcEndpoint):' in code
  assert '@wrap_exceptions' in code
  assert 'async def balance(' in code
  # Neither field is optional_scalars-declared, so both stay required kwargs and only
  # the first (`address`) can ever go positional-or-keyword -- but its type (`str`)
  # isn't unique among the request's own fields (`denom` is also `str`), so
  # `_flat_request_kwargs` pushes both to keyword-only.
  assert 'self, *,' in code
  assert 'address: str, denom: str,' in code
  assert '-> QueryBalanceResponse:' in code
  assert (
    'return await QueryStub(self.channel).balance('
    'QueryBalanceRequest(address=address, denom=denom))'
  ) in code


def test_grpc_endpoint_optional_scalar_falls_back_to_zero_value():
  """`cosmos.gov.v1.Query/Proposals` -- `voter` is declared `optional_scalars` (Task 7):
  a scalar field proto3 gives no native way to mark absent, so the generated parameter
  defaults to `None` and the constructor argument falls back to the type's own zero
  value (`''`) when the caller passes `None` -- matching dYdX's own real
  `voter=voter if voter is not None else ''` shape exactly. `pagination` is a
  message-typed field, already nullable in its own live type (`PageRequest | None`) with
  no `optional_scalars` declaration at all, and passes straight through unconditionally.
  """
  endpoint = _grpc_endpoint(spec={
    'kind': 'grpc',
    'service': 'cosmos.gov.v1.Query',
    'rpc': 'Proposals',
    'request': 'cosmos.gov.v1.QueryProposalsRequest',
    'response': 'cosmos.gov.v1.QueryProposalsResponse',
    'proto': 'cosmos/gov/v1/query.proto',
    'description': 'Query all governance proposals, optionally filtered by voter.',
    'optional_scalars': ['voter'],
  })
  generator = _generator()
  code = generator.grpc_endpoint(
    endpoint, {}, class_name='Proposals', method_name='proposals',
  )
  ast.parse(code)

  assert 'class Proposals(GrpcEndpoint):' in code
  assert '@wrap_exceptions' in code
  assert 'async def proposals(' in code
  assert (
    'from grpc_proto_client.protos.cosmos.base.query.v1beta1 import PageRequest'
  ) in code
  assert 'from grpc_proto_client.protos.cosmos.gov.v1 import (' in code
  assert 'QueryProposalsRequest,' in code
  assert 'QueryProposalsResponse,' in code
  assert 'QueryStub' in code
  # `voter` (optional_scalars, unique-typed `str`, and the first declared field) can go
  # positional-or-keyword; `pagination` (message-typed, naturally optional) is
  # keyword-only, per `_flat_request_kwargs`'s "only the very first field" rule.
  assert 'self, voter: str | None = None, *,' in code
  assert 'pagination: PageRequest | None = None,' in code
  assert "voter=(voter if voter is not None else '')" in code
  assert 'pagination=pagination' in code
  assert (
    "return await QueryStub(self.channel).proposals(QueryProposalsRequest("
    "voter=(voter if voter is not None else ''), pagination=pagination))"
  ) in code


def test_grpc_endpoint_rejects_non_unary_streaming():
  """`streaming` other than `'unary'` has no codegen path today (`GrpcEndpointSpec`'s
  own docstring) -- must fail loud, not silently mis-generate a unary-shaped call for a
  streaming RPC."""
  endpoint = _grpc_endpoint(spec={
    'kind': 'grpc',
    'service': 'cosmos.bank.v1beta1.Query',
    'rpc': 'Balance',
    'streaming': 'server',
    'request': 'cosmos.bank.v1beta1.QueryBalanceRequest',
    'response': 'cosmos.bank.v1beta1.QueryBalanceResponse',
    'proto': 'cosmos/bank/v1beta1/query.proto',
    'description': 'Query one denom balance.',
  })
  generator = _generator()
  with pytest.raises(NotImplementedError, match='streaming'):
    generator.grpc_endpoint(endpoint, {}, class_name='Balance', method_name='balance')


def test_grpc_endpoint_rejects_unknown_optional_scalar():
  """`optional_scalars` naming a field the real message doesn't have is a spec/proto
  mismatch -- `grpc_endpoint` must catch it rather than silently generating a method
  that never references the bogus name."""
  endpoint = _grpc_endpoint(spec={
    'kind': 'grpc',
    'service': 'cosmos.bank.v1beta1.Query',
    'rpc': 'Balance',
    'request': 'cosmos.bank.v1beta1.QueryBalanceRequest',
    'response': 'cosmos.bank.v1beta1.QueryBalanceResponse',
    'proto': 'cosmos/bank/v1beta1/query.proto',
    'description': 'Query one denom balance.',
    'optional_scalars': ['nonexistent_field'],
  })
  generator = _generator()
  with pytest.raises(ValueError, match='nonexistent_field'):
    generator.grpc_endpoint(endpoint, {}, class_name='Balance', method_name='balance')
