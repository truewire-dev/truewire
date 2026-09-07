"""
Pin `coerce_example_call`'s handling of API-named example parameters.

`ExampleRequest.parameters` is keyed by the API name (`docs/spec/spec.md`#example-parameters-are-
api-named), while a generated method's signature uses the Python identifier codegen derived
from it (`docs/spec/authoring.md`). `coerce_example_call` bridges that gap so a
`parameters`-shaped example can drive the real client the same way a hand-authored
`args`/`kwargs` example already does.
"""

from pathlib import Path
from typing_extensions import Literal

from truewire.generation.python.util import safe_identifier

from truewire.examples import client_identifier, coerce_example_call
from truewire.spec import ExampleRequest


def fn(
  *,
  category: str,
  base_coin: str | None = None,
  from_: Literal['spot', 'margin'] | None = None,
  type_: Literal['LIMIT', 'MARKET'] | None = None,
):
  pass


def test_a_camel_case_api_name_binds_to_its_snake_case_identifier():
  request = ExampleRequest(parameters={'category': 'option', 'baseCoin': 'BTC'})
  _, kwargs = coerce_example_call(fn, request)
  assert kwargs == {'category': 'option', 'base_coin': 'BTC'}


def test_a_reserved_keyword_api_name_binds_to_its_suffixed_identifier():
  request = ExampleRequest(parameters={'category': 'option', 'from': 'spot'})
  _, kwargs = coerce_example_call(fn, request)
  assert kwargs == {'category': 'option', 'from_': 'spot'}


def test_a_name_already_matching_the_signature_passes_through_unchanged():
  request = ExampleRequest(kwargs={'category': 'option', 'base_coin': 'BTC'})
  _, kwargs = coerce_example_call(fn, request)
  assert kwargs == {'category': 'option', 'base_coin': 'BTC'}


def test_a_name_with_no_matching_identifier_still_raises():
  request = ExampleRequest(parameters={'category': 'option', 'quoteCoin': 'USDT'})
  try:
    coerce_example_call(fn, request)
  except TypeError as e:
    assert 'quoteCoin' in str(e)
  else:
    raise AssertionError('expected a TypeError for an unresolvable parameter name')


def test_coerce_example_call_honors_a_given_identifier_rule_over_the_default():
  """A client-specific override (e.g. mexc's) must win over codegen's default rule."""
  request = ExampleRequest(parameters={'category': 'option', 'type': 'LIMIT'})
  _, kwargs = coerce_example_call(fn, request, identifier=lambda name: f'{name}_')
  assert kwargs == {'category': 'option', 'type_': 'LIMIT'}


def test_client_identifier_falls_back_to_the_default_rule_without_a_backend(
  tmp_path: Path,
):
  assert client_identifier(tmp_path) is safe_identifier


def test_client_identifier_uses_the_backends_own_override(tmp_path: Path):
  (tmp_path / 'truewire.toml').write_text(
    '[python]\nbackend = "backend.py"\n[python.cores.default]\nbase = "x.core:Endpoint"\n'
  )
  (tmp_path / 'backend.py').write_text(
    'class FakeGenerator:\n'
    '  def identifier(self, name):\n'
    '    return name.upper()\n'
    'generator = FakeGenerator()\n'
  )
  identifier = client_identifier(tmp_path)
  assert identifier is not safe_identifier
  assert identifier('baseCoin') == 'BASECOIN'


def batch_cancel(*, order_ids: list[str]):
  pass


def add(*, body: dict):
  pass


def two_required(*, a: str, b: str):
  pass


def test_a_flat_parameter_payload_spreads_onto_its_matching_parameter():
  """coinbase's `batch_cancel`: the payload's one key already names the real parameter,
  so it binds directly rather than nesting a second time."""
  request = ExampleRequest(payload={'order_ids': ['abc']})
  _, kwargs = coerce_example_call(batch_cancel, request)
  assert kwargs == {'order_ids': ['abc']}


def test_a_whole_body_payload_that_matches_no_parameter_binds_to_the_sole_unfilled_one():
  """kucoin's `spot.orders_hf.add`: none of the payload's keys name a real parameter, so
  the legacy whole-blob-to-sole-parameter behavior still applies unchanged."""
  request = ExampleRequest(payload={'clientOid': 'x', 'symbol': 'BTC-USDT'})
  _, kwargs = coerce_example_call(add, request)
  assert kwargs == {'body': {'clientOid': 'x', 'symbol': 'BTC-USDT'}}


def test_a_partially_matching_payload_does_not_partially_decompose():
  """A payload where only one of several keys names a real parameter is ambiguous
  evidence, not a decomposition -- it must not spread that one key in isolation. Here
  it also fails the whole-blob fallback (more than one required parameter is still
  unfilled), so nothing binds at all and the call would raise `TypeError` downstream."""
  request = ExampleRequest(payload={'a': 'x', 'unmatched': 'y'})
  _, kwargs = coerce_example_call(two_required, request)
  assert kwargs == {}


def test_an_empty_dict_payload_keeps_the_whole_blob_behavior():
  """An empty-dict payload has no keys to decompose, so it falls straight to the
  whole-blob-to-sole-parameter branch, same as before this fix."""
  request = ExampleRequest(payload={})
  _, kwargs = coerce_example_call(add, request)
  assert kwargs == {'body': {}}


def test_example_request_accepts_single_request_value():
  """`ExampleRequest.request` (design §11, `docs/TODO.md` T7 item 4) holds one value
  matching the endpoint's `request` schema, replacing the `parameters`/`payload` split."""
  example = ExampleRequest.model_validate({'request': {'symbol': 'BTCUSDT', 'limit': 10}})
  assert example.request == {'symbol': 'BTCUSDT', 'limit': 10}
