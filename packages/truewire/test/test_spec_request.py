"""Exercise `truewire.spec.request.split_request_by_location`."""

from truewire.spec.request import split_request_by_location


def test_split_request_by_location():
  """A `request` schema property matching a `{placeholder}` in the template is a path param."""
  locations = split_request_by_location(
    '/v1/orderbook/{symbol}',
    {'properties': {'symbol': {'type': 'string'}, 'limit': {'type': 'integer'}}},
  )
  assert locations.path_params == frozenset({'symbol'})
  assert locations.rest == frozenset({'limit'})


def test_split_request_by_location_no_schema():
  """A `None` request schema yields no path params and no rest -- nothing to split."""
  locations = split_request_by_location('/v1/ping', None)
  assert locations.path_params == frozenset()
  assert locations.rest == frozenset()
