"""The upstream inventory's shared shape check for `check` and S1 coverage."""
import json
from importlib.resources import files
from pathlib import Path
from typing_extensions import Any

from jsonschema import Draft202012Validator, FormatChecker


def load_inventory(spec_dir: Path) -> tuple[Any, list[str]]:
  """Read an optional inventory, returning its data and every shape error.

  Absence is allowed by `check`; S1 separately requires the file and human approval.
  Keep readable data even when invalid so S1 can also report every unknown endpoint.
  """
  path = spec_dir / 'inventory.json'
  label = f'{spec_dir.name}/inventory.json'
  try:
    data = json.loads(path.read_text())
  except FileNotFoundError:
    return None, []
  except (OSError, ValueError) as exc:
    return None, [f'{label}: {exc}']
  schema = json.loads(files('truewire').joinpath('resources/inventory.schema.json').read_text())
  validator = Draft202012Validator(schema, format_checker=FormatChecker())
  errors = []
  for error in validator.iter_errors(data):
    location = ''.join(f'[{part}]' if isinstance(part, int) else f'.{part}' for part in error.path)
    message = error.message
    if error.validator == 'not' and error.validator_value == {'required': ['endpoint', 'excluded']}:
      message = 'entry must not have both endpoint and excluded'
    errors.append(f'{label}{location}: {message}')
  entries = data.get('endpoints') if isinstance(data, dict) else None
  if isinstance(entries, list):
    seen: dict[tuple[str, str], int] = {}
    for index, entry in enumerate(entries):
      if not isinstance(entry, dict):
        continue
      method, endpoint_path = entry.get('method'), entry.get('path')
      if not isinstance(method, str) or not isinstance(endpoint_path, str):
        continue  # The schema already reports missing or wrongly typed identifiers.
      key = (method.upper(), endpoint_path)
      if key in seen:
        errors.append(f'{label}.endpoints[{index}]: duplicates endpoints[{seen[key]}] {key[0]} {endpoint_path}')
      else:
        seen[key] = index
  return data, errors
