"""The public core preserves the runtime's number-epoch aliases and converters."""

import pytest
from truewire_core import types as runtime_types

from kraken import core
from kraken.core import types


@pytest.mark.parametrize('unit', ['Seconds', 'Millis', 'Micros', 'Nanos'])
def test_number_epoch_reexports(unit: str):
  for name in (f'Timestamp{unit}Float', f'timestamp_{unit.lower()}_float'):
    assert getattr(types, name) is getattr(runtime_types, name)
    assert name in core.__all__
    assert getattr(core, name) is getattr(runtime_types, name)
