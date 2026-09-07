"""
Exercise `truewire.mock`'s HTTP request matching against new-shape (`request`/`response`,
design §10) endpoints -- the mock-serving half of Task 22.

`_mock_http_example` used to require `endpoint.openapi` and build its matcher data from
`openapi.parameters`/`requestBody` alone, which raised on a new-shape endpoint (no
`Operation` at all). It now derives the same matcher data from a new-shape endpoint's
single `request` JSON Schema via `truewire.spec.request.split_request_by_location`
(Task 21), and `_request_match`'s query/body pooling -- previously gated on
`expected_body is None`, true only for a legacy endpoint recording no `payload` -- now
fires unconditionally for every new-shape endpoint, since Step 3 never sets
`expected_body` for one with flat `request` properties.

Fixtures live at `fixtures/mock_server_new_shape/`, owned by this suite alone (same
isolation rationale as `test_mock.py`'s own module docstring): `get_orderbook` is a flat
`request` schema (`symbol`/`limit`), exercising the pooled query/body match: ADR 0011's
"role, not wire location" rule, now unconditional. `create_order` is a discriminated
`anyOf` `request` (no flat top-level `properties`), exercising the fallback branch that
mirrors the legacy `requestBody`+`anyOf` treatment -- the whole recorded value becomes
`expected_body`, with no per-field query/body pooling.

`get_orderbook_request_field` (Task 23, design §11) is `get_orderbook`'s schema again, but
its example records both `request` and `parameters` -- deliberately set to *different*
values (`request`: BTCUSDT/10, `parameters`: ETHUSDT/5) -- so matching against `parameters`'s
value proves nothing on its own; only a request matching `request`'s value should match,
proving `_mock_http_example` genuinely prefers `request` over a legacy field set alongside
it, not merely that `request` works when it's the only field present. `get_orderbook`'s own
still-`parameters`-only example continues to match exactly as before.

`fixtures/mock_server_legacy_request_field/` (Task 23 fix-up) covers the other branch: a
legacy `openapi`-shaped endpoint (`endpoint.openapi` set, same dual-shape path
`test_mock.py`'s fixtures exercise) whose `request_body` read used to try only `payload`,
bypassing `request` entirely even when both were recorded on the same example. Its
`create_order` endpoint declares a discriminated-union (`anyOf`) `requestBody` specifically
so no flat body property gets pooled into `expected_query` (`_flat_body_parameter_names`
returns nothing for an `anyOf` schema) -- isolating the `request`-vs-`payload` precedence
in `request_body`'s own read from query/body pooling entirely. Its `limit` example sets
`request` to a limit order and `payload` to a market order (a different variant), so only a
real body matching `request`'s value should match.
"""

from pathlib import Path

import pytest

from truewire.mock import MockRegistry, UnexpectedRequestParameters, load_http_examples


ROOT = Path(__file__).resolve().parent / 'fixtures' / 'mock_server_new_shape'
"""Spec root this suite owns. See the module docstring."""

LEGACY_ROOT = Path(__file__).resolve().parent / 'fixtures' / 'mock_server_legacy_request_field'
"""Spec root for the legacy `openapi`-branch precedence fix-up. See the module docstring."""


def test_new_shape_endpoint_matches_via_pooled_query_and_body():
  """A new-shape endpoint's recorded `parameters` match a real request carrying them
  either as query-string items or as a JSON body -- ADR 0011's "role, not wire location"
  rule, now unconditional rather than gated on `expected_body is None`."""
  examples = load_http_examples(root=ROOT)
  assert examples, 'expected the new-shape fixture to load at least one example'

  registry = MockRegistry(root=ROOT)
  # `query_items` arrives pre-sorted from the real HTTP handler (`MockRequestHandler._handle`
  # sorts `parse_qsl(...)` before calling `.match`) -- `query_matches` does a positional,
  # not a keyed, comparison, so a direct `.match()` call has to honor that same invariant.
  match_query_variant = registry.match(
    'GET', '/v1/orderbook', sorted([('symbol', 'BTCUSDT'), ('limit', '10')]), None
  )
  match_body_variant = registry.match(
    'GET', '/v1/orderbook', [], {'symbol': 'BTCUSDT', 'limit': 10}
  )
  assert match_query_variant is not None
  assert match_body_variant is not None
  assert match_query_variant.endpoint_function == 'market.orderbook'
  assert match_body_variant.endpoint_function == 'market.orderbook'


def test_new_shape_endpoint_partial_pooling_still_matches():
  """The pooling is over the union of query and body items, not an all-or-nothing choice
  of one channel -- `symbol` on the query string and `limit` in the body, together, still
  match the one recorded example."""
  registry = MockRegistry(root=ROOT)
  match = registry.match(
    'GET', '/v1/orderbook', [('symbol', 'BTCUSDT')], {'limit': 10}
  )
  assert match is not None
  assert match.endpoint_function == 'market.orderbook'


def test_new_shape_discriminated_union_request_matches_whole_body():
  """A new-shape endpoint whose `request` is a top-level `anyOf` (no flat `properties`)
  has no individually named fields to pool -- the whole recorded `parameters` value is
  compared against the whole real body instead, mirroring the legacy path's own
  `requestBody`+`anyOf` treatment."""
  registry = MockRegistry(root=ROOT)
  match = registry.match('POST', '/v1/orders', [], {'price': '100.5'})
  assert match is not None
  assert match.endpoint_function == 'trading.create_order'

  # A body matching neither variant's recorded value is a real mismatch, not a silent
  # `None` -- there is exactly one route candidate and it fails structural comparison.
  with pytest.raises(UnexpectedRequestParameters):
    registry.match('POST', '/v1/orders', [], {'quantity': '2'})


def test_new_shape_endpoint_matches_via_single_request_field():
  """An example recording `request` (Task 23) matches a real request carrying `request`'s
  own value, even though `parameters` is also set on the same example (to a different
  value) -- `_mock_http_example` prefers `request` over `parameters` when both are set."""
  registry = MockRegistry(root=ROOT)
  match_query_variant = registry.match(
    'GET', '/v1/orderbook2', sorted([('symbol', 'BTCUSDT'), ('limit', '10')]), None
  )
  match_body_variant = registry.match(
    'GET', '/v1/orderbook2', [], {'symbol': 'BTCUSDT', 'limit': 10}
  )
  assert match_query_variant is not None
  assert match_body_variant is not None
  assert match_query_variant.endpoint_function == 'market.orderbook_request_field'
  assert match_body_variant.endpoint_function == 'market.orderbook_request_field'


def test_new_shape_endpoint_ignores_parameters_value_when_request_is_set():
  """The genuine precedence proof: `get_orderbook_request_field`'s example sets `request`
  to BTCUSDT/10 and `parameters` to a deliberately different ETHUSDT/5. A real request
  carrying `parameters`'s value must NOT match -- if `_mock_http_example` fell back to (or
  merged in) `parameters` instead of genuinely preferring `request`, this would wrongly
  match on `parameters`'s value, or on a mix of both. There is exactly one route candidate
  for this path, so a non-match surfaces as `UnexpectedRequestParameters`, not a silent
  `None`."""
  registry = MockRegistry(root=ROOT)
  with pytest.raises(UnexpectedRequestParameters):
    registry.match(
      'GET', '/v1/orderbook2', sorted([('symbol', 'ETHUSDT'), ('limit', '5')]), None
    )
  with pytest.raises(UnexpectedRequestParameters):
    registry.match('GET', '/v1/orderbook2', [], {'symbol': 'ETHUSDT', 'limit': 5})


def test_new_shape_endpoint_parameters_example_still_matches_unchanged():
  """`get_orderbook`'s pre-existing `parameters`-shaped example (no `request` set) keeps
  matching exactly as before -- the `request` preference added in Task 23 only takes
  effect when `request` is actually present on the recorded example."""
  registry = MockRegistry(root=ROOT)
  match = registry.match(
    'GET', '/v1/orderbook', sorted([('symbol', 'BTCUSDT'), ('limit', '10')]), None
  )
  assert match is not None
  assert match.endpoint_function == 'market.orderbook'


def test_legacy_openapi_endpoint_prefers_request_over_payload_in_body():
  """The legacy `openapi` branch's own `request_body` read used to try only `payload`,
  silently bypassing `request` even when both were recorded on the same example --
  contradicting `_mock_http_example`'s own docstring claim that `request` is preferred
  "wherever this function reads named request fields." `create_order`'s `limit` example
  sets `request` to a limit order and `payload` to a market order (a different variant): a
  real body matching `request`'s value must match."""
  registry = MockRegistry(root=LEGACY_ROOT)
  match = registry.match('POST', '/orders', [], {'type': 'limit', 'price': '100.00'})
  assert match is not None
  assert match.endpoint_function == 'orders.create'
  assert match.example_id == 'limit'


def test_legacy_openapi_endpoint_ignores_payload_value_when_request_is_set():
  """The other half of the same proof: a real body matching `payload`'s (unpreferred)
  value must NOT match. There is exactly one route candidate for this path, so a non-match
  surfaces as `UnexpectedRequestParameters`, not a silent `None`."""
  registry = MockRegistry(root=LEGACY_ROOT)
  with pytest.raises(UnexpectedRequestParameters):
    registry.match('POST', '/orders', [], {'type': 'market'})
