"""Tests for `GrpcEndpointSpec` and `Endpoint`'s grpc branch."""

import pytest
from pydantic import ValidationError

from truewire.spec import Endpoint, GrpcEndpointSpec


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
      'description': 'Query one denom balance for an account address.',
    },
  }
  data.update(overrides)
  return Endpoint.model_validate(data)


def test_grpc_endpoint_parses():
  endpoint = _grpc_endpoint()
  assert isinstance(endpoint.spec, GrpcEndpointSpec)
  assert endpoint.spec.streaming == 'unary'
  assert endpoint.spec.service == 'cosmos.bank.v1beta1.Query'


def test_grpc_endpoint_openapi_is_none():
  endpoint = _grpc_endpoint()
  assert endpoint.openapi is None
  assert endpoint.path is None
  assert endpoint.method is None
  assert endpoint.channel is None
  assert endpoint.transports == []


def test_grpc_endpoint_rejects_envelope():
  with pytest.raises(ValidationError):
    _grpc_endpoint(envelope={'payload': 'result'})


def test_grpc_endpoint_rejects_extra_field():
  with pytest.raises(ValidationError):
    Endpoint.model_validate({
      'function': 'chain.bank.balance',
      'spec': {
        'kind': 'grpc',
        'service': 'cosmos.bank.v1beta1.Query',
        'rpc': 'Balance',
        'request': 'cosmos.bank.v1beta1.QueryBalanceRequest',
        'response': 'cosmos.bank.v1beta1.QueryBalanceResponse',
        'proto': 'cosmos/bank/v1beta1/query.proto',
        'description': 'Query one denom balance.',
        'path': '/should/not/exist',
      },
    })


def test_grpc_endpoint_spec_accepts_optional_scalars():
  spec = GrpcEndpointSpec.model_validate({
    'service': 'cosmos.gov.v1.Query', 'rpc': 'Proposals',
    'request': 'cosmos.gov.v1.QueryProposalsRequest',
    'response': 'cosmos.gov.v1.QueryProposalsResponse',
    'proto': 'cosmos/gov/v1/query.proto', 'description': 'List proposals.',
    'optional_scalars': ['proposal_status', 'voter', 'depositor'],
  })
  assert spec.optional_scalars == ['proposal_status', 'voter', 'depositor']


def test_grpc_endpoint_spec_optional_scalars_defaults_empty():
  spec = GrpcEndpointSpec.model_validate({
    'service': 'x.Query', 'rpc': 'Y', 'request': 'x.R', 'response': 'x.S',
    'proto': 'x.proto', 'description': 'Y.',
  })
  assert spec.optional_scalars == []
