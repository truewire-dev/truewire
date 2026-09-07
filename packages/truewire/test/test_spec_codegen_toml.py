from pathlib import Path
import pytest
from truewire.spec.codegen_toml import CoreConfig, PythonCoreConfig, load_codegen_toml


def _write(root: Path, text: str) -> None:
  """Write `codegen/config.toml` under `root`, creating the `codegen/` directory."""
  (root / 'truewire.toml').write_text(text)


def test_loads_python_section(tmp_path: Path):
  """Every `[python.cores.<name>]` entry is the identical `{base, children?}` table --
  root position included, no separate `client_base` field (design §5c/§6)."""
  _write(tmp_path, '''
[python]
name = "MEXC"

[python.cores.default]
base = "typed_mexc.core:ClientBase"

[python.cores.spot]
base = "typed_mexc.spot.core:SpotEndpoint"

[python.cores.futures]
base = "typed_mexc.futures.core:FuturesEndpoint"
'''.strip())
  config = load_codegen_toml(tmp_path)
  assert config.python is not None
  assert config.python.name == 'MEXC'
  assert config.python.cores == {
    'default': PythonCoreConfig(base='typed_mexc.core:ClientBase'),
    'spot': PythonCoreConfig(base='typed_mexc.spot.core:SpotEndpoint'),
    'futures': PythonCoreConfig(base='typed_mexc.futures.core:FuturesEndpoint'),
  }


def test_loads_children_mapping(tmp_path: Path):
  """A base composing more than one distinctly-based child declares `children` (design
  §5c) -- attribute name -> field name on `self` to forward when composing it."""
  _write(tmp_path, '''
[python]
name = "MEXC"

[python.cores.default]
base = "typed_mexc.core:ClientBase"

[python.cores.spot]
base = "typed_mexc.spot.core:SpotBase"
children = { rest = "rest_client", streams = "streams_client" }
'''.strip())
  config = load_codegen_toml(tmp_path)
  assert config.python is not None
  spot = config.python.cores['spot']
  assert spot.base == 'typed_mexc.spot.core:SpotBase'
  assert spot.children == {'rest': 'rest_client', 'streams': 'streams_client'}
  assert config.python.cores['default'].children is None


def test_missing_file_raises(tmp_path: Path):
  with pytest.raises(FileNotFoundError):
    load_codegen_toml(tmp_path)


def test_missing_cores_table_rejected(tmp_path: Path):
  _write(tmp_path, '[python]\nname = "X"\n')
  with pytest.raises(ValueError):
    load_codegen_toml(tmp_path)


def test_bare_string_core_entry_rejected(tmp_path: Path):
  """The old bare-string `[python.cores]` shape (`default = "module:Class"`) is no
  longer valid -- every entry is the uniform `{base, children?}` table now (design §5c:
  "One shape, not two"), so a bare string is a validation error, not silently accepted."""
  _write(tmp_path, '''
[python]
name = "X"

[python.cores]
default = "x.core:Endpoint"
'''.strip())
  with pytest.raises(ValueError):
    load_codegen_toml(tmp_path)


def test_loads_top_level_cores_meta_schema(tmp_path: Path):
  """A top-level `[cores.<name>]` entry declares `meta`'s shape as a real JSON Schema
  (design §2/§6) -- language-neutral, sibling to `[python]`, keyed by the identical
  symbolic core name `[python.cores]` itself resolves."""
  _write(tmp_path, '''
[cores.default]
# no meta -- most cores need none

[cores.exchange]
meta = { type = "object", properties = { scheme = { type = "string", enum = ["l1", "user_signed"] }, action = { type = "string", enum = ["ordinary", "batched"] } }, required = ["scheme"] }

[python]
name = "MEXC"

[python.cores.default]
base = "typed_mexc.core:ClientBase"

[python.cores.exchange]
base = "typed_mexc.exchange.core:ExchangeEndpoint"
'''.strip())
  config = load_codegen_toml(tmp_path)
  assert config.cores is not None
  assert config.cores['default'] == CoreConfig(meta=None)
  assert config.cores['exchange'] == CoreConfig(meta={
    'type': 'object',
    'properties': {
      'scheme': {'type': 'string', 'enum': ['l1', 'user_signed']},
      'action': {'type': 'string', 'enum': ['ordinary', 'batched']},
    },
    'required': ['scheme'],
  })


def test_no_cores_section_at_all_is_valid(tmp_path: Path):
  """Most of the fleet declares no `[cores.*]` at all -- `cores` stays `None`, not an
  error, matching every already-migrated client until one actually needs a `meta`
  schema."""
  _write(tmp_path, '''
[python]
name = "X"

[python.cores.default]
base = "x.core:Endpoint"
'''.strip())
  config = load_codegen_toml(tmp_path)
  assert config.cores is None


def test_cores_entry_extra_key_rejected(tmp_path: Path):
  """`CoreConfig` is `extra='forbid'`, same as every other model in this module -- a
  typo'd or unrecognized key under `[cores.<name>]` fails loudly rather than being
  silently ignored."""
  _write(tmp_path, '''
[cores.default]
meta = { type = "object" }
extra_field = true

[python]
name = "X"

[python.cores.default]
base = "x.core:Endpoint"
'''.strip())
  with pytest.raises(ValueError):
    load_codegen_toml(tmp_path)


def test_client_base_field_no_longer_accepted(tmp_path: Path):
  """`client_base` was a separate `[python]` field before design §5c retired the
  root/ordinary-node distinction -- the root's own base is just another
  `[python.cores.<name>]` entry now, so this field is rejected (`extra='forbid')`."""
  _write(tmp_path, '''
[python]
name = "X"
client_base = "x.core:ClientBase"

[python.cores.default]
base = "x.core:Endpoint"
'''.strip())
  with pytest.raises(ValueError):
    load_codegen_toml(tmp_path)
