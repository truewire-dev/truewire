"""
Pins `validator`'s contract: pydantic-backed, raising `truewire_core.exceptions.ValidationError`
(never pydantic's own) on mismatch, tolerating undocumented fields on the base `TypedDict`,
caching one adapter per type, and dumping back to JSON bytes.
"""
import pytest

from truewire_core.exceptions import ValidationError
from truewire_core.validation import TypedDict, validator


class Order(TypedDict):
  """A minimal wire shape, on the tolerant base."""
  id: str
  amount: str


class TestValidate:
  def test_json_bytes(self):
    assert validator(Order)(b'{"id": "ord_1", "amount": "10.5"}') == {'id': 'ord_1', 'amount': '10.5'}

  def test_json_str(self):
    assert validator(Order)('{"id": "ord_1", "amount": "10.5"}') == {'id': 'ord_1', 'amount': '10.5'}

  def test_python_value(self):
    assert validator(Order)({'id': 'ord_1', 'amount': '10.5'}) == {'id': 'ord_1', 'amount': '10.5'}

  def test_extra_field_is_kept_not_rejected(self):
    """The base `TypedDict` is `extra='allow'`: an undocumented field survives validation."""
    order = validator(Order)(b'{"id": "ord_1", "amount": "10.5", "extra": true}')
    assert order == {'id': 'ord_1', 'amount': '10.5', 'extra': True}

  def test_missing_field_raises_our_validation_error(self):
    with pytest.raises(ValidationError):
      validator(Order)(b'{"id": "ord_1"}')

  def test_wrong_type_raises_our_validation_error(self):
    with pytest.raises(ValidationError):
      validator(Order).python({'id': 1, 'amount': '10.5'})

  def test_invalid_json_raises_our_validation_error(self):
    with pytest.raises(ValidationError):
      validator(Order).json(b'{not json')

  def test_validation_error_is_chained_from_pydantic(self):
    """The pydantic error is kept as `__cause__`, so a caller who wants the detail has it."""
    import pydantic
    with pytest.raises(ValidationError) as info:
      validator(Order)(b'{}')
    assert isinstance(info.value.__cause__, pydantic.ValidationError)


class TestDump:
  def test_dump_is_json_bytes(self):
    out = validator(Order).dump({'id': 'ord_1', 'amount': '10.5'})
    assert isinstance(out, bytes)
    assert out == b'{"id":"ord_1","amount":"10.5"}'

  def test_dump_keeps_extra_fields(self):
    out = validator(Order).dump({'id': 'ord_1', 'amount': '10.5', 'extra': True})  # type: ignore[typeddict-unknown-key]
    assert out == b'{"id":"ord_1","amount":"10.5","extra":true}'

  def test_round_trips(self):
    v = validator(Order)
    order = {'id': 'ord_1', 'amount': '10.5'}
    assert v(v.dump(order)) == order


class TestAdapterCache:
  def test_same_type_shares_one_adapter(self):
    assert validator(Order).adapter is validator(Order).adapter

  def test_list_of_type_validates(self):
    rows = validator(list[Order])(b'[{"id": "a", "amount": "1"}, {"id": "b", "amount": "2"}]')
    assert [r['id'] for r in rows] == ['a', 'b']
