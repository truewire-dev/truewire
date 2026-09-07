"""`truewire migrate`: rewrite a spec written under the previous form of authoring rule 6.

Before ADR 0010 a response schema described the value the core returned after unwrapping,
and `envelope.payload` told `truewire check` where in the recorded frame to find it. Now
the schema describes the wire frame and `envelope.payload` selects the returned value
inside it. This command wraps each old-form schema into the frame its own recordings
show, and nothing else: an endpoint whose path already resolves inside its schema is left
untouched, so a second run changes nothing.
"""
import json
from pathlib import Path
from typing_extensions import Any

import typer

from truewire.project import Project, spec_dir as project_spec_dir
from truewire.spec import (
  EndpointRecord, endpoint_records, load_shared_schemas, path_segments, select_schema,
)

from .common import PATH_OPTION, PROJECT_OPTION, resolve_spec_scope

WIRE_FIELD = 'Wire envelope field; see the core.'
"""Description every inferred wrapper property carries; nothing generates from them."""

ASSUMED_ITEMS: dict[str, Any] = {'type': 'string'}
"""Element schema written for an array every recording shows empty. No evidence backs it;
the command names each such field, and the first recording that disagrees fails
`truewire check`, which is how every other schema gets corrected too."""


class Refused(Exception):
  """One endpoint this command will not rewrite, with the reason."""


def pascal_case(name: str) -> str:
  """`asset_pairs` -> `AssetPairs`; an already-PascalCase word survives."""
  return ''.join(part[:1].upper() + part[1:] for part in name.replace('-', '_').split('_') if part)


def recorded_frames(examples_dir: Path) -> list[Any]:
  """Every recorded wire frame of one rpc endpoint: `*.response.json` payloads (http) and
  `*.reply.json` frames (ws), a `null` reply skipped (a no-reply command records one)."""
  if not examples_dir.is_dir():
    return []
  frames: list[Any] = []
  for path in sorted(examples_dir.glob('*.response.json')):
    example = json.loads(path.read_text())
    if isinstance(example, dict) and 'payload' in example:
      frames.append(example['payload'])
  for path in sorted(examples_dir.glob('*.reply.json')):
    reply = json.loads(path.read_text())
    if reply is not None:
      frames.append(reply)
  return frames


def infer_schema(values: list[Any], *, assumed: list[str], field: str) -> dict[str, Any]:
  """A JSON Schema for the recorded values of one wrapper field, from their JSON types.

  A scalar gets its type; an object becomes an untitled map (rule 1: a map needs no
  title, and nothing reads its keys); an array's element schema is inferred from every
  element seen across every recording, `ASSUMED_ITEMS` when none was. Values of more than
  one shape become an `anyOf`.
  """
  seen: dict[str, dict[str, Any]] = {}

  def add(schema: dict[str, Any]):
    seen.setdefault(json.dumps(schema, sort_keys=True), schema)

  elements: list[Any] = []
  has_array = False
  for value in values:
    if isinstance(value, bool):
      add({'type': 'boolean'})
    elif isinstance(value, int):
      add({'type': 'integer'})
    elif isinstance(value, float):
      add({'type': 'number'})
    elif isinstance(value, str):
      add({'type': 'string'})
    elif value is None:
      add({'type': 'null'})
    elif isinstance(value, dict):
      add({'type': 'object', 'additionalProperties': True})
    elif isinstance(value, list):
      has_array = True
      elements.extend(value)
  if has_array:
    if elements:
      items = infer_schema(elements, assumed=assumed, field=f'{field}[]')
    else:
      items = dict(ASSUMED_ITEMS)
      assumed.append(field)
    add({'type': 'array', 'items': items})
  schemas = list(seen.values())
  return schemas[0] if len(schemas) == 1 else {'anyOf': schemas}


def wrap(
  payload_schema: dict[str, Any], keys: list[str], frames: list[dict[str, Any]], *,
  title: str, assumed: list[str],
) -> dict[str, Any]:
  """The wire-frame schema around `payload_schema`, `keys` deep, from recorded frames.

  Properties follow the recorded key order; every key but the payload's is inferred and
  described as a wire field; `required` holds the keys every frame carries plus the
  payload key. A deeper path nests one titled object per segment.
  """
  key, rest = keys[0], keys[1:]
  order: list[str] = []
  for frame in frames:
    for name in frame:
      if name not in order:
        order.append(name)
  if key not in order:
    order.append(key)
  properties: dict[str, Any] = {}
  for name in order:
    if name == key:
      if rest:
        inner = [frame[key] for frame in frames if isinstance(frame.get(key), dict)]
        properties[name] = wrap(
          payload_schema, rest, inner, title=f'{title}{pascal_case(key)}', assumed=assumed,
        )
      else:
        properties[name] = payload_schema
        if not payload_schema.get('description') and '$ref' not in payload_schema:
          properties[name] = {**payload_schema, 'description': 'The value the generated method returns.'}
      continue
    values = [frame[name] for frame in frames if name in frame]
    properties[name] = {**infer_schema(values, assumed=assumed, field=name), 'description': WIRE_FIELD}
  always = [name for name in order if name == key or all(name in frame for frame in frames)]
  return {
    'title': title,
    'type': 'object',
    'description': f'Wire frame; the generated method returns `{key}`.',
    'required': always,
    'properties': properties,
  }


def frame_title(data: dict[str, Any], endpoint_dir: Path) -> str:
  """`<ResponseTitle>Frame`, falling back to the request's title (its `Request` suffix
  dropped) and then to the endpoint directory's name, for a map response with no title."""
  spec = data['spec']
  response = spec.get('response') or {}
  base = response.get('title') if isinstance(response, dict) else None
  if not base:
    request = spec.get('request') or {}
    request_title = request.get('title') if isinstance(request, dict) else None
    if isinstance(request_title, str) and request_title:
      base = request_title[:-len('Request')] if request_title.endswith('Request') else request_title
  if not base:
    base = pascal_case(endpoint_dir.name)
  return f'{base}Frame'


def migrate_endpoint(
  record: EndpointRecord, *, shared: dict[str, Any], template_frames: list[Any] | None,
  assumed: list[str],
) -> tuple[str, str] | None:
  """Rewrite one endpoint's response schema into wire shape, or leave it alone.

  Returns:
    `(title, keys)` of the frame written, or `None` when nothing needed doing.

  Raises:
    Refused: With the reason, when the frame cannot be derived honestly.
  """
  data = json.loads(record.path.read_text())
  envelope = data.get('envelope')
  spec = data.get('spec') or {}
  if not isinstance(envelope, dict) or spec.get('kind') != 'rpc':
    return None
  path = envelope.get('payload')
  if not isinstance(path, str) or path == '':
    return None
  if spec.get('response') is None:
    if 'openapi' in spec:
      raise Refused('still openapi-shaped; move it to `request`/`response` first')
    return None
  response = spec['response']
  try:
    select_schema(response, path, shared=shared)
    return None
  except LookupError:
    pass
  segments = path_segments(path)
  if any(kind == 'index' for kind, _ in segments):
    raise Refused(f'`envelope.payload` {path!r} carries a bracket index; wrap this schema by hand')
  keys = [str(key) for _, key in segments]
  frames = recorded_frames(record.path.parent / 'examples')
  if frames:
    if not all(isinstance(frame, dict) for frame in frames):
      raise Refused('a recorded frame is not a JSON object, so there is no envelope to derive')
    if not any(_present(frame, keys) for frame in frames):
      raise Refused(f'no recorded frame carries `{path}`, so the declared envelope does not match the wire')
  elif template_frames:
    frames = [frame for frame in template_frames if isinstance(frame, dict)]
  else:
    raise Refused(
      'no recording to derive the frame from; record one with `truewire capture`, or pass '
      '`--template <function>` naming a recorded endpoint whose frame stands in'
    )
  title = frame_title(data, record.path.parent)
  spec['response'] = wrap(response, keys, frames, title=title, assumed=assumed)
  record.path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n')
  return title, ', '.join(spec['response']['properties'])


def _present(frame: Any, keys: list[str]) -> bool:
  """Whether every segment exists in `frame`, a `null` leaf included."""
  for key in keys:
    if not isinstance(frame, dict) or key not in frame:
      return False
    frame = frame[key]
  return True


def migrate(
  project: str | None = PROJECT_OPTION,
  path: str | None = PATH_OPTION,
  template: str | None = typer.Option(
    None, '--template',
    help=(
      'Function name of a recorded endpoint whose frame stands in for an endpoint with no '
      'recording of its own (an `unverified` one sharing the API\'s envelope).'
    ),
  ),
):
  """Rewrite response schemas written for the unwrapped value into wire-frame schemas.

  For every rpc endpoint declaring `envelope.payload` whose response schema does not carry
  that path, wraps the schema into the frame its recordings show: a `<Title>Frame` object
  whose other properties are inferred from the recorded keys and described as wire fields,
  with the old schema under the payload key (ADR 0010). An endpoint whose path already
  resolves is left as it is, so the command is safe to rerun. An endpoint with no
  recording is refused rather than guessed, unless `--template` names one whose frame
  stands in.

  Args:
    project: Project directory holding `truewire.toml`; defaults to the nearest one.
    path: Project root, or one subdivision of its `spec/endpoints`, to scope the run to.
    template: Function name of a recorded endpoint supplying the frame for endpoints
      without recordings.
  """
  loaded, scoped = resolve_spec_scope(project, path)
  spec_root = project_spec_dir(loaded)
  shared = load_shared_schemas(loaded)
  records = endpoint_records(loaded, scope=scoped.scope)

  template_frames: list[Any] | None = None
  if template is not None:
    template_frames = _template_frames(loaded, spec_root, template)

  migrated: list[str] = []
  refused: list[str] = []
  unchanged = 0
  assumed: list[str] = []
  for record in records:
    function = record.endpoint.resolved_function(record.path, spec_root)
    try:
      result = migrate_endpoint(
        record, shared=shared, template_frames=template_frames, assumed=assumed,
      )
    except Refused as exc:
      refused.append(f'{function}: {exc}')
      continue
    if result is None:
      unchanged += 1
      continue
    title, keys = result
    migrated.append(function)
    typer.echo(f'migrated   {function}  {title} ({keys})')

  typer.echo()
  typer.echo(
    f'Migrated {len(migrated)} endpoint(s); {unchanged} left as they are (no `envelope.payload`, '
    f'or already in wire shape).'
  )
  if assumed:
    by_field: dict[str, int] = {}
    for field in assumed:
      by_field[field] = by_field.get(field, 0) + 1
    fields = ', '.join(f'`{field}` ({count})' for field, count in sorted(by_field.items()))
    typer.echo(
      f'Element type assumed `string` for arrays every recording shows empty: {fields}. '
      f'No recording evidences it; the first one that disagrees fails `truewire check`.'
    )
  if refused:
    typer.echo(f'Refused {len(refused)} endpoint(s):', err=True)
    for line in refused:
      typer.echo(f'  {line}', err=True)
    raise typer.Exit(code=1)


def _template_frames(loaded: Project, spec_root: Path, template: str) -> list[Any]:
  """Recorded frames of the endpoint `--template` names, anywhere in the project."""
  for record in endpoint_records(loaded):
    if record.endpoint.resolved_function(record.path, spec_root) == template:
      frames = recorded_frames(record.path.parent / 'examples')
      if not frames:
        typer.echo(f'--template {template}: that endpoint has no recording either', err=True)
        raise typer.Exit(code=1)
      return frames
  typer.echo(f'--template {template}: no such endpoint', err=True)
  raise typer.Exit(code=1)
