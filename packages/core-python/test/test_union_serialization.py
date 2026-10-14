"""Dump dispatch must not revalidate inputs or swallow serializer failures."""
from datetime import datetime, timezone
import json

from pydantic import AfterValidator, BeforeValidator, Field, PlainSerializer, TypeAdapter
from pydantic_core import PydanticSerializationError
import pytest
from typing_extensions import Annotated, Literal, NotRequired

from truewire_core.types import TimestampSeconds, TimestampMillis
from truewire_core.validation import TypedDict, validator


class Basic(TypedDict):
  id: int


class Timed(TypedDict):
  id: int
  time: TimestampSeconds


def test_validation_and_coercion_are_unchanged_and_dump_does_not_validate():
  calls = []

  def parse(value):
    calls.append(value)
    return value

  class Counted(TypedDict):
    id: Annotated[int, BeforeValidator(parse)]
    time: TimestampSeconds

  union = Basic | Counted
  payload = {'id': '7', 'time': 1767225600, 'future': {'time': 'unvalidated'}}
  expected = TypeAdapter(union).validate_python(payload)
  calls.clear()
  adapter = validator(union)
  value = adapter.python(payload)
  assert value == expected
  assert calls == ['7']
  calls.clear()
  assert json.loads(adapter.dump(value)) == {'id': 7, 'time': 1767225600, 'future': {'time': 'unvalidated'}}
  assert calls == []
  assert value['time'] == datetime(2026, 1, 1, tzinfo=timezone.utc)
  # A direct request is already in Python form: dumping must not run parse again.
  adapter.dump(value)
  assert calls == []


def test_non_idempotent_validator_is_not_reapplied():
  def increment(value):
    return value + 1

  class Counted(TypedDict):
    id: Annotated[int, AfterValidator(increment)]
    time: TimestampSeconds

  adapter = validator(Basic | Counted)
  value = adapter.python({'id': 1, 'time': 1767225600})
  assert value['id'] == 2
  assert json.loads(adapter.dump(value)) == {'id': 2, 'time': 1767225600}


def test_invalid_python_fields_still_warn():
  with pytest.warns(UserWarning, match='PydanticSerializationUnexpectedValue'):
    assert json.loads(validator(Basic | Timed).dump({'id': 'invalid'})) == {'id': 'invalid'}


def test_selected_serializer_failure_is_not_silently_downgraded_to_basic():
  def fail(value):
    raise ValueError('declared conversion failed')

  class Broken(TypedDict):
    id: int
    value: Annotated[int, PlainSerializer(fail)]

  with pytest.raises(PydanticSerializationError, match='declared conversion failed'):
    validator(Basic | Broken).dump({'id': 1, 'value': 2})


def test_explicit_union_serializer_is_preserved():
  union = Annotated[Basic | Timed, PlainSerializer(lambda _: {'custom': True})]
  assert json.loads(validator(union).dump({'id': 1, 'time': datetime.now(timezone.utc)})) == {'custom': True}


def test_tagged_union_keeps_its_discriminator_and_nested_dispatch():
  class Seconds(TypedDict):
    kind: Literal['seconds']
    time: TimestampSeconds
    nested: Basic | Timed

  class Millis(TypedDict):
    kind: Literal['millis']
    time: TimestampMillis
    nested: Basic | Timed

  tagged = Annotated[Seconds | Millis, Field(discriminator='kind')]
  adapter = validator(tagged)
  for kind, stamp in [('seconds', 1767225600), ('millis', 1767225600000)]:
    wire = {'kind': kind, 'time': stamp, 'nested': {'id': 1, 'time': 1767225600, 'extra': [False]}, 'extra': True}
    value = adapter.json(json.dumps(wire))
    assert json.loads(adapter.dump(value)) == wire


def test_nested_present_fields_break_outer_tie():
  class BasicEnvelope(TypedDict):
    value: Basic

  class TimedEnvelope(TypedDict):
    value: Timed

  wire = {'value': {'id': 1, 'time': 1767225600, 'extra': None}, 'extra': False}
  adapter = validator(BasicEnvelope | TimedEnvelope)
  assert json.loads(adapter.dump(adapter.json(json.dumps(wire)))) == wire


def test_absent_optional_fields_do_not_count():
  class Optional(TypedDict):
    id: int
    a: NotRequired[int]
    b: NotRequired[int]
    c: NotRequired[int]

  adapter = validator(Optional | Timed)
  wire = {'id': 1, 'time': 1767225600, 'extra': True}
  assert json.loads(adapter.dump(adapter.json(json.dumps(wire)))) == wire


def test_unknown_values_are_not_used_to_select_a_member():
  class Uninspectable(dict):
    def items(self):
      raise AssertionError('selection inspected an unknown value')

  from truewire_core._serialization import _Members
  schema = TypeAdapter(Basic | Timed).core_schema
  match = _Members(schema['choices'], {})
  assert match({'id': 1, 'time': datetime.now(timezone.utc), 'extra': Uninspectable()}) == 1


def test_circular_data_still_fails():
  value = {'id': 1}
  value['extra'] = value
  with pytest.raises(PydanticSerializationError, match='Circular reference'):
    validator(Basic | Timed).dump(value)


def test_exact_python_type_breaks_equal_field_count_tie():
  class FloatId(TypedDict):
    id: float
    time: TimestampMillis

  adapter = validator(FloatId | Timed)
  value = {'id': 1, 'time': datetime(1970, 1, 1, 0, 16, 40, tzinfo=timezone.utc)}
  validated = adapter.python(value)
  assert type(validated['id']) is int
  assert json.loads(adapter.dump(validated)) == {'id': 1, 'time': 1000}
  assert json.loads(adapter.dump(value)) == {'id': 1, 'time': 1000}


def test_float_member_still_accepts_an_integer_when_no_exact_member_matches():
  class FloatValue(TypedDict):
    id: int
    value: float
    time: TimestampSeconds

  value = {'id': 1, 'value': 2, 'time': datetime(2026, 1, 1, tzinfo=timezone.utc)}
  assert json.loads(validator(Basic | FloatValue).dump(value)) == {
    'id': 1, 'value': 2.0, 'time': 1767225600,
  }


def test_any_field_is_less_exact_than_a_declared_timestamp():
  from typing_extensions import Any

  class Broad(TypedDict):
    id: int
    time: Any

  value = {'id': 1, 'time': datetime(2026, 1, 1, tzinfo=timezone.utc)}
  assert json.loads(validator(Broad | Timed).dump(value)) == {'id': 1, 'time': 1767225600}


@pytest.mark.parametrize('scalar, value', [
  (int, type('Integer', (int,), {})(7)),
  (float, type('Number', (float,), {})(7)),
  (str, type('Text', (str,), {})('seven')),
  (datetime, type('Instant', (datetime,), {})(2026, 1, 1, tzinfo=timezone.utc)),
])
def test_scalar_subclasses_preserve_other_field_serializers(scalar, value):
  class Detail(TypedDict):
    id: int
    value: scalar
    time: TimestampSeconds

  request = {'id': 1, 'value': value, 'time': datetime(2026, 1, 1, tzinfo=timezone.utc)}
  assert validator(Basic | Detail).dump(request) == validator(Detail).dump(request)


def test_date_subtypes_use_the_declared_date_serializer():
  from datetime import date
  from truewire_core.types import DateIso

  class Day(date):
    pass

  class Detail(TypedDict):
    id: int
    day: DateIso
    time: TimestampSeconds

  for day in (Day(2026, 1, 1), datetime(2026, 1, 1, tzinfo=timezone.utc)):
    request = {'id': 1, 'day': day, 'time': datetime(2026, 1, 1, tzinfo=timezone.utc)}
    assert validator(Basic | Detail).dump(request) == validator(Detail).dump(request)


def test_integer_subclass_outranks_numeric_compatibility():
  from http import HTTPStatus

  class FloatId(TypedDict):
    id: float
    time: TimestampMillis

  value = {'id': HTTPStatus.OK, 'time': datetime(2026, 1, 1, tzinfo=timezone.utc)}
  assert json.loads(validator(FloatId | Timed).dump(value)) == {'id': 200, 'time': 1767225600}
