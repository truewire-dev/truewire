"""
Pins `validator`'s contract: pydantic-backed, raising `truewire_core.exceptions.ValidationError`
(never pydantic's own) on mismatch, tolerating undocumented fields on the base `TypedDict`,
caching one adapter per type, and dumping back to JSON bytes.
"""
import pydantic
import pytest
from typing_extensions import Literal

from truewire_core.exceptions import Error, ValidationError
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
    with pytest.raises(ValidationError) as info:
      validator(Order)(b'{}')
    assert isinstance(info.value.__cause__, pydantic.ValidationError)


class Phase(TypedDict):
  """A closed enum field, as generated for a `{"enum": [...]}` schema."""
  phase: Literal['in_progress', 'completed']


class Status(TypedDict):
  """`Phase` nested under an array, so a failure's path has three segments."""
  upcoming_maintenance: list[Phase]


class TestErrorText:
  """`str(err)` names the offending location and the reason, as the TS, Go and Rust errors do,
  so `log.error('%s', err)` is enough to see what failed."""

  def test_closed_literal_names_field_and_value(self):
    with pytest.raises(ValidationError) as info:
      validator(Phase)({'phase': 'reminder_1h'})
    text = str(info.value)
    assert text == (
      "ValidationError(Input should be 'in_progress' or 'completed', got \"reminder_1h\" at /phase)"
    )
    assert 'phase' in text and 'reminder_1h' in text

  def test_closed_literal_from_json(self):
    with pytest.raises(ValidationError) as info:
      validator(Phase)(b'{"phase": "reminder_1h"}')
    assert str(info.value).endswith(', got "reminder_1h" at /phase)')

  def test_nested_path_is_a_json_pointer(self):
    body = b'{"upcoming_maintenance": [{"phase": "completed"}, {"phase": "reminder_1h"}]}'
    with pytest.raises(ValidationError) as info:
      validator(Status)(body)
    assert str(info.value).endswith('got "reminder_1h" at /upcoming_maintenance/1/phase)')

  def test_missing_field_names_no_value(self):
    with pytest.raises(ValidationError) as info:
      validator(Status)({'upcoming_maintenance': [{}]})
    assert str(info.value) == 'ValidationError(Field required at /upcoming_maintenance/0/phase)'

  def test_container_is_described_by_kind(self):
    with pytest.raises(ValidationError) as info:
      validator(Order)({'id': {'nested': 'secret-ish'}, 'amount': '1'})
    assert str(info.value) == 'ValidationError(Input should be a valid string, got object at /id)'

  def test_long_value_is_cut(self):
    with pytest.raises(ValidationError) as info:
      validator(Phase)({'phase': 'x' * 100})
    assert f'got "{"x" * 40}…" at /phase' in str(info.value)
    assert 'x' * 41 not in str(info.value)

  def test_further_failures_are_counted(self):
    with pytest.raises(ValidationError) as info:
      validator(Order)({})
    assert str(info.value) == 'ValidationError(Field required at /id (and 1 more))'

  def test_invalid_json_quotes_no_document(self):
    with pytest.raises(ValidationError) as info:
      validator(Order).json(b'{"id": "ord_1", not json')
    text = str(info.value)
    assert text.startswith('ValidationError(Invalid JSON: ') and text.endswith(' at /)')
    assert 'ord_1' not in text

  def test_pointer_escapes_tilde_and_slash(self):
    with pytest.raises(ValidationError) as info:
      validator(dict[str, int])({'a/b~c': 'x'})
    assert str(info.value).endswith(' at /a~1b~0c)')

  def test_type_hierarchy_args_and_cause_are_kept(self):
    with pytest.raises(ValidationError) as info:
      validator(Phase)({'phase': 'reminder_1h'})
    err = info.value
    assert type(err) is ValidationError and isinstance(err, Error)
    assert err.args == ("Input should be 'in_progress' or 'completed', got \"reminder_1h\" at /phase",)
    assert isinstance(err.__cause__, pydantic.ValidationError)
    assert err.__cause__.errors(include_url=False)[0]['loc'] == ('phase',)


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
