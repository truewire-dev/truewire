"""`truewire init`: scaffold a new project directory."""
import re
from pathlib import Path

import typer

from truewire.project import PROJECT_FILE

CORE_TEMPLATE = '''"""Hand-written core for the {package} client: transport, auth, envelope and errors.

Every generated endpoint class subclasses `Endpoint` and calls `self.request(...)`; this is
the one place that knows how to reach the upstream API. Adapt `Transport` (base URL,
headers, signing, envelope unwrapping, error mapping) to your API; the generated code
never changes when you do.
"""
from dataclasses import dataclass, field
from types import UnionType
from typing_extensions import Any, NotRequired, Self, TypedDict, TypeVar, cast

from truewire_core.exceptions import ApiError
from truewire_core.http import HttpClient
from truewire_core.validation import validator

from .types import (  # noqa: F401  re-exported for generated code
  DateIso, TimestampIso, TimestampMicros, TimestampMillis, TimestampNanos, TimestampSeconds,
)

T = TypeVar('T')


class Meta(TypedDict):
  """Per-endpoint facts declared in `endpoint.json`'s `meta`, matching `[cores.default].meta`."""
  public: NotRequired[bool]
  """Whether the call needs no credentials."""


@dataclass(kw_only=True)
class Transport:
  """The shared HTTP transport: base URL plus whatever auth the API needs."""
  base_url: str
  http: HttpClient = field(default_factory=HttpClient)
  api_key: str | None = None
  validate: bool = True

  def headers(self, *, public: bool) -> dict[str, str]:
    """Headers for one call. Add signing here."""
    if public or self.api_key is None:
      return {{}}
    return {{'Authorization': f'Bearer {{self.api_key}}'}}

  async def send(self, method: str, path: str, *, params: dict[str, Any], body: bytes | None, public: bool) -> bytes:
    """Send one request; raise `ApiError` on a non-2xx status."""
    filled = path
    for name, value in list(params.items()):
      if f'{{{{{{name}}}}}}' in filled:
        filled = filled.replace(f'{{{{{{name}}}}}}', str(value))
        params.pop(name)
    response = await self.http.request(
      method, self.base_url.rstrip('/') + '/' + filled.lstrip('/'),
      params=params or None, content=body, headers=self.headers(public=public),
    )
    if response.status_code >= 400:
      raise ApiError(f'{{method}} {{filled}}: HTTP {{response.status_code}}: {{response.text[:200]}}')
    return response.content


@dataclass(kw_only=True)
class ClientBase:
  """Root client: owns the transport every endpoint shares."""
  client: Transport

  @classmethod
  def new(cls, *, base_url: str = '{base_url}', api_key: str | None = None, validate: bool = True) -> Self:
    """Create a client against `base_url`."""
    return cls(client=Transport(base_url=base_url, api_key=api_key, validate=validate))

  async def __aenter__(self) -> Self:
    return self

  async def __aexit__(self, exc_type, exc_value, traceback):
    await self.client.http.__aexit__(exc_type, exc_value, traceback)


@dataclass(kw_only=True, frozen=True)
class Endpoint:
  """Base for every generated endpoint class: one shared transport."""
  client: Transport

  async def request(
    self, request: Any = None, *, method: str, path: str,
    validate: bool | None = None,
    request_type: type[Any] | UnionType | None = None,
    response_type: type[T] | UnionType | None = None,
    meta: Meta = {{}},
  ) -> T:
    """Send one request and validate the reply against `response_type`."""
    params = {{k: v for k, v in dict(request or {{}}).items() if v is not None}}
    body = None
    if method.upper() in ('POST', 'PUT', 'PATCH') and request_type is not None and request is not None:
      body = validator(cast(type, request_type)).dump(request)
      params = {{}}
    raw = await self.client.send(method, path, params=params, body=body, public=bool(meta.get('public')))
    if response_type is None:
      return None  # type: ignore[return-value]
    check = self.client.validate if validate is None else validate
    if check:
      return validator(cast(type, response_type)).json(raw)
    import json
    return json.loads(raw)
'''


TYPES_TEMPLATE = '''"""Wire timestamp shapes the generated code refers to by name.

Each spec `format` (`epoch-seconds`, `epoch-millis`, `epoch-micros`, `epoch-nanos`,
`date-time`, `date`) renders to one of these: a real `datetime`/`date` that parses from
the wire form and serializes back to it.
"""
from datetime import date, datetime, timezone
from typing_extensions import Annotated

from pydantic import BeforeValidator, PlainSerializer

from truewire_core.times import DateConverter, EpochConverter, IsoConverter

timestamp_seconds = EpochConverter.seconds(tz=timezone.utc)
timestamp_millis = EpochConverter.milliseconds(tz=timezone.utc)
timestamp_micros = EpochConverter.microseconds(tz=timezone.utc)
timestamp_nanos = EpochConverter.nanoseconds(tz=timezone.utc)
timestamp_iso = IsoConverter()
date_iso = DateConverter()

TimestampSeconds = Annotated[
  datetime, BeforeValidator(timestamp_seconds.parse), PlainSerializer(timestamp_seconds.dump, when_used='json'),
]
"""An `epoch-seconds` field."""
TimestampMillis = Annotated[
  datetime, BeforeValidator(timestamp_millis.parse), PlainSerializer(timestamp_millis.dump, when_used='json'),
]
"""An `epoch-millis` field."""
TimestampMicros = Annotated[
  datetime, BeforeValidator(timestamp_micros.parse), PlainSerializer(timestamp_micros.dump, when_used='json'),
]
"""An `epoch-micros` field."""
TimestampNanos = Annotated[
  datetime, BeforeValidator(timestamp_nanos.parse), PlainSerializer(timestamp_nanos.dump, when_used='json'),
]
"""An `epoch-nanos` field."""
TimestampIso = Annotated[
  datetime, BeforeValidator(timestamp_iso.parse), PlainSerializer(timestamp_iso.dump, when_used='json'),
]
"""A `date-time` (RFC 3339) field."""
DateIso = Annotated[
  date, BeforeValidator(date_iso.parse), PlainSerializer(date_iso.dump, when_used='json'),
]
"""A `date` (RFC 3339 full-date) field."""
'''

PYPROJECT_TEMPLATE = '''[project]
name = "{package}"
version = "0.1.0"
description = "Typed client generated with Truewire."
requires-python = ">=3.11"
dependencies = ["truewire-core>=0.1.0"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-data]
{package} = ["py.typed"]
'''
"""The project's own packaging file, so `pip install -e .` makes the generated client importable."""


def init(
  name: str = typer.Argument(..., help='Project name; also the directory and Python package name.'),
  directory: Path | None = typer.Option(None, '--dir', help='Where to create the project; defaults to ./<name>.'),
  base_url: str = typer.Option('https://api.example.com', '--base-url', help='Upstream base URL baked into the core template.'),
):
  """Create a new Truewire project: `truewire.toml`, `pyproject.toml`, an empty `spec/`, and a core skeleton.

  Follow with `truewire import openapi <doc>` to seed the spec, or author
  `spec/endpoints/<group>/<name>/endpoint.json` by hand, then `truewire check` and
  `truewire generate python`.
  """
  if not re.fullmatch(r'[a-z][a-z0-9_]*', name):
    typer.echo('name must be a lowercase identifier: letters, digits, underscores', err=True)
    raise typer.Exit(code=1)
  root = (directory or Path(name)).resolve()
  if (root / PROJECT_FILE).exists():
    typer.echo(f'{root / PROJECT_FILE} already exists', err=True)
    raise typer.Exit(code=1)
  class_name = ''.join(part.capitalize() for part in name.split('_'))
  (root / 'spec' / 'endpoints').mkdir(parents=True, exist_ok=True)
  (root / 'src' / name / 'core').mkdir(parents=True, exist_ok=True)
  (root / PROJECT_FILE).write_text(f'''[project]
name = "{name}"

[spec]
dir = "spec"

[cores.default]
meta = {{ type = "object", properties = {{ public = {{ type = "boolean" }} }}, additionalProperties = false }}

[python]
package = "{name}"
src = "src"
name = "{class_name}"

[python.cores.root]
base = "{name}.core:ClientBase"

[python.cores.default]
base = "{name}.core:Endpoint"
''')
  (root / 'spec' / 'endpoints' / 'router.json').write_text(
    '{\n  "description": "' + class_name + ' API.",\n  "upstream": "' + base_url + '",\n  "core": "root"\n}\n'
  )
  (root / 'src' / name / '__init__.py').write_text(f'from .main import {class_name}\n')
  (root / 'src' / name / 'py.typed').write_text('')
  (root / 'src' / name / 'main.py').write_text(
    f'"""Placeholder; overwritten by `truewire generate python`."""\nfrom .core import ClientBase\n\n\nclass {class_name}(ClientBase):\n  """Generated root client (placeholder)."""\n'
  )
  (root / 'src' / name / 'core' / '__init__.py').write_text(
    CORE_TEMPLATE.format(package=name, base_url=base_url)
  )
  (root / 'src' / name / 'core' / 'types.py').write_text(TYPES_TEMPLATE)
  if not (root / 'pyproject.toml').exists():
    (root / 'pyproject.toml').write_text(PYPROJECT_TEMPLATE.format(package=name))
  (root / '.gitignore').write_text('.truewire/\n__pycache__/\n.venv/\n')
  typer.echo(f'Created {root}')
  typer.echo('Next: `truewire import openapi <document>` or write spec/endpoints/**/endpoint.json, then `truewire check` and `truewire generate python`.')
