"""Synthetic `cosmos.gov.v1` module -- shaped like dYdX's real `Proposals` endpoint, for
`grpc_endpoint` tests only.

Exercises the two "optional" cases design §9/Task 7 distinguish: `voter` is a scalar
field whose absence proto3 can't express natively, so it's declared `optional_scalars`
on the fixture endpoint and needs a zero-value fallback (`''`) when the caller passes
`None`; `pagination` is a message-typed field, natively nullable in betterproto2
(`PageRequest | None`), so it needs no `optional_scalars` declaration at all and passes
straight through.
"""
from dataclasses import dataclass
from enum import IntEnum

from grpc_proto_client.protos.cosmos.base.query.v1beta1 import PageRequest


class ProposalStatus(IntEnum):
  """Synthetic enum -- zero value first, per real proto3 convention."""
  UNSPECIFIED = 0
  PASSED = 1


class QueryStub:
  """Synthetic gRPC stub, shaped like a real betterproto2 `ServiceStub`."""

  def __init__(self, channel):
    """Store the channel a real stub would issue calls over."""
    self.channel = channel

  async def proposals(self, message: 'QueryProposalsRequest') -> 'QueryProposalsResponse':
    """Synthetic unary call -- never actually invoked by these tests."""
    raise NotImplementedError


@dataclass
class QueryProposalsRequest:
  """Synthetic request message: list proposals, optionally filtered by voter."""
  voter: str = ''
  pagination: PageRequest | None = None


@dataclass
class QueryProposalsResponse:
  """Synthetic response message."""
  proposals: list[str] | None = None
