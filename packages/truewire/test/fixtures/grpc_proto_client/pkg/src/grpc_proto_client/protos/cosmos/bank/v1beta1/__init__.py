"""Synthetic `cosmos.bank.v1beta1` module -- shaped like a real betterproto2-generated
one (`QueryStub` plus request/response dataclasses), for `grpc_endpoint` tests only.

Exercises the "every field required" case: `QueryBalanceRequest.address`/`.denom` are
both plain `str`, neither declared `optional_scalars`, so `grpc_endpoint` must pass both
straight through unconditionally.
"""
from dataclasses import dataclass


class QueryStub:
  """Synthetic gRPC stub, shaped like a real betterproto2 `ServiceStub`."""

  def __init__(self, channel):
    """Store the channel a real stub would issue calls over."""
    self.channel = channel

  async def balance(self, message: 'QueryBalanceRequest') -> 'QueryBalanceResponse':
    """Synthetic unary call -- never actually invoked by these tests."""
    raise NotImplementedError


@dataclass
class QueryBalanceRequest:
  """Synthetic request message: one denom balance for one account address."""
  address: str = ''
  denom: str = ''


@dataclass
class QueryBalanceResponse:
  """Synthetic response message."""
  balance: str = ''
