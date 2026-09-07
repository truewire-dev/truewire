"""Synthetic `cosmos.base.query.v1beta1` module -- just the shared `PageRequest` shape a
real dYdX-style pagination message field references, for `grpc_endpoint` tests only."""
from dataclasses import dataclass


@dataclass
class PageRequest:
  """Synthetic pagination request, referenced by `QueryProposalsRequest.pagination`."""
  key: bytes = b''
