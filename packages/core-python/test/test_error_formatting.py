"""PR #129 regression: diagnostics must not replace the public exception."""

import pydantic
import pytest

from truewire_core.exceptions import ValidationError
from truewire_core.validation import validator


class Unprintable:
    def __str__(self):
        raise RuntimeError("cannot stringify this value")


class UnprintableInt(int):
    def __str__(self):
        raise RuntimeError("cannot stringify this integer")


@pytest.mark.parametrize("case", ["large_integer", "unprintable_object", "unprintable_integer"])
def test_formatting_preserves_validation_error(case):
    if case == "large_integer":
        value = 10**5000
    elif case == "unprintable_integer":
        value = UnprintableInt(1)
    else:
        value = Unprintable()

    with pytest.raises(ValidationError) as caught:
        validator(str).python(value)

    assert isinstance(caught.value.__cause__, pydantic.ValidationError)
    kind = 'integer' if case == 'large_integer' else 'object'
    assert str(caught.value) == f'ValidationError(Input should be a valid string, got {kind} at /)'
    assert caught.value.__cause__.errors()[0]['input'] is value


@pytest.mark.parametrize('value', [42, -42, 1.5])
def test_builtin_numbers_keep_their_value_in_the_message(value):
    with pytest.raises(ValidationError) as caught:
        validator(str).python(value)
    assert str(caught.value) == f'ValidationError(Input should be a valid string, got {value} at /)'
