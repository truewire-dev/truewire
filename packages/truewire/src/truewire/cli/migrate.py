"""`truewire migrate`: rewrite a spec written under the previous form of authoring rule 6.

Before ADR 0010 a response schema described the value the core returned after unwrapping,
and `envelope.payload` told `truewire check` where in the recorded frame to find it. Now
the schema describes the wire frame and `envelope.payload` selects the returned value
inside it. This command wraps each old-form schema into the frame its recordings show, and
nothing else: an endpoint already in wire shape is left untouched, so a second run changes
nothing.

Where the frame comes from, in order: the endpoint's own recordings; the endpoint `--template`
names; the recordings of every endpoint sharing its core and its `envelope.payload` path.

`--rename-schema OLD=NEW` renames a shared schema (its id, its title and every `$ref` to it)
before anything else runs. It exists for authoring rule 18: a router group whose class name
equals a shared schema's is refused rather than renamed by the generators, because both names
are public surface, so the author picks the new name and this command applies it everywhere.
"""
import json
from copy import deepcopy
from pathlib import Path
from typing_extensions import Any

import typer

from truewire.project import Project, spec_dir as project_spec_dir
from truewire.spec import (
  EndpointRecord, endpoint_records, load_shared_schemas, path_segments, select_schema,
)
from truewire.spec.router import load_router

from .common import PATH_OPTION, PROJECT_OPTION, resolve_spec_scope

WIRE_FIELD = 'Wire envelope field; see the core.'
"""Description every inferred wrapper property carries; nothing generates from them."""

ASSUMED_ITEMS: dict[str, Any] = {'type': 'string'}
"""Element schema written for an array every recording shows empty. No evidence backs it;
the command names each such field, and the first recording that disagrees fails
`truewire check`, which is how every other schema gets corrected too."""

SOURCES = {
  'recordings': 'from their own recordings',
  'template': 'from --template',
  'core': 'from recorded endpoints of the same core',
}
"""Where a written frame came from, as the summary line reports it."""


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


class Validity:
  """Whether a recorded value validates against a schema, `$ref`s resolved against the
  project's shared schemas the way `truewire check` resolves them."""

  def __init__(self, shared: dict[str, Any]):
    self.defs: dict[str, Any] = {}
    for key, value in shared.items():
      node = self.defs
      parts = key.split('/')
      for part in parts[:-1]:
        node = node.setdefault(part, {})
      node[parts[-1]] = _rewrite_refs(value)

  def valid(self, schema: dict[str, Any], instance: Any) -> bool:
    from jsonschema import Draft202012Validator
    from referencing.exceptions import Unresolvable

    root = deepcopy(_rewrite_refs(schema))
    root['$schema'] = 'https://json-schema.org/draft/2020-12/schema'
    root['$defs'] = self.defs
    try:
      return Draft202012Validator(root).is_valid(instance)
    except Unresolvable:
      return False

  def still_unwrapped(self, schema: dict[str, Any], keys: list[str], frames: list[Any]) -> bool:
    """Whether recordings show `schema` describing the unwrapped value even though the
    payload path resolves inside it (an old schema with a same-named property of its own,
    `{"result": {"result": [...]}}`): no recorded frame validates against it whole, and the
    value at the path of every frame carrying one does."""
    carrying = [frame for frame in frames if _present(frame, keys)]
    if not carrying:
      return False
    return (
      not any(self.valid(schema, frame) for frame in carrying)
      and all(self.valid(schema, _at(frame, keys)) for frame in carrying)
    )


def _rewrite_refs(obj: Any) -> Any:
  """A spec `$ref` (a shared schema id) as a pointer into `$defs`."""
  if isinstance(obj, dict):
    return {
      key: (f'#/$defs/{value}' if key == '$ref' and isinstance(value, str) and not value.startswith('#/')
            else _rewrite_refs(value))
      for key, value in obj.items()
    }
  if isinstance(obj, list):
    return [_rewrite_refs(value) for value in obj]
  return obj


def payload_keys(data: dict[str, Any]) -> tuple[str, list[str]] | None:
  """`(path, keys)` of an rpc endpoint's non-empty, index-free `envelope.payload`."""
  envelope = data.get('envelope')
  spec = data.get('spec') or {}
  if not isinstance(envelope, dict) or spec.get('kind') != 'rpc':
    return None
  path = envelope.get('payload')
  if not isinstance(path, str) or path == '':
    return None
  segments = path_segments(path)
  if any(kind == 'index' for kind, _ in segments):
    return None
  return path, [str(key) for _, key in segments]


def migrate_endpoint(
  record: EndpointRecord, *, shared: dict[str, Any], template_frames: list[Any] | None,
  core: tuple[str | None, list[Any]] = (None, []), assumed: list[str],
  validity: Validity | None = None,
) -> tuple[str, str, str] | None:
  """Rewrite one endpoint's response schema into wire shape, or leave it alone.

  Args:
    core: The endpoint's core and the recorded frames of the endpoints sharing it and its
      payload path, used when neither its own recordings nor `template_frames` exist.

  Returns:
    `(title, keys, source)` of the frame written, `source` a `SOURCES` key; `None` when
    nothing needed doing.

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
  segments = path_segments(path)
  keys = [str(key) for _, key in segments]
  own = recorded_frames(record.path.parent / 'examples')
  try:
    select_schema(response, path, shared=shared)
    resolves = True
  except LookupError:
    resolves = False
  if any(kind == 'index' for kind, _ in segments):
    if resolves:
      return None
    raise Refused(f'`envelope.payload` {path!r} carries a bracket index; wrap this schema by hand')
  if resolves:
    dict_frames = [frame for frame in own if isinstance(frame, dict)]
    validity = validity or Validity(shared)
    if not dict_frames or not validity.still_unwrapped(response, keys, dict_frames):
      return None
  core_name, core_frames = core
  if own:
    if not all(isinstance(frame, dict) for frame in own):
      raise Refused('a recorded frame is not a JSON object, so there is no envelope to derive')
    if not any(_present(frame, keys) for frame in own):
      raise Refused(f'no recorded frame carries `{path}`, so the declared envelope does not match the wire')
    frames, source = own, 'recordings'
  elif template_frames:
    frames, source = [frame for frame in template_frames if isinstance(frame, dict)], 'template'
  elif core_frames:
    frames, source = core_frames, 'core'
  else:
    raise Refused(
      f'no recording to derive the frame from, and no recorded endpoint of core '
      f'`{core_name}` declares `envelope.payload` `{path}`; record one with `truewire capture`, '
      'or pass `--template <function>` naming a recorded endpoint whose frame stands in'
    )
  title = frame_title(data, record.path.parent)
  spec['response'] = wrap(response, keys, frames, title=title, assumed=assumed)
  _write_json(record.path, data)
  return title, ', '.join(spec['response']['properties']), source


def _present(frame: Any, keys: list[str]) -> bool:
  """Whether every segment exists in `frame`, a `null` leaf included."""
  for key in keys:
    if not isinstance(frame, dict) or key not in frame:
      return False
    frame = frame[key]
  return True


def _at(frame: Any, keys: list[str]) -> Any:
  """The value `keys` names in a frame `_present` accepted."""
  for key in keys:
    frame = frame[key]
  return frame


def _write_json(path: Path, data: Any):
  path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n')


def _replace_refs(node: Any, old: str, new: str) -> int:
  """Point every `$ref` naming `old` at `new`, in place; the number rewritten."""
  count = 0
  if isinstance(node, dict):
    for key, value in node.items():
      if key == '$ref' and value == old:
        node[key] = new
        count += 1
      else:
        count += _replace_refs(value, old, new)
  elif isinstance(node, list):
    for value in node:
      count += _replace_refs(value, old, new)
  return count


def rename_schema(spec_root: Path, old: str, new: str) -> tuple[int, int] | None:
  """Rename the shared schema `old` to `new`: its `schemas.json` key (order kept), its
  `title` when that was `old`, and every `$ref` in every `endpoint.json` and `schemas.json`.

  Returns:
    `(refs, files)` rewritten, or `None` when `old` is gone and `new` exists (already
    renamed).

  Raises:
    Refused: When `old` does not exist and `new` does not either, or both exist.
  """
  files = sorted({spec_root / 'schemas.json', *(spec_root / 'endpoints').rglob('schemas.json')})
  scopes = {path: json.loads(path.read_text()) for path in files if path.is_file()}
  holding = [path for path, schemas in scopes.items() if old in schemas]
  taken = [path for path, schemas in scopes.items() if new in schemas]
  if not holding:
    if taken:
      return None
    raise Refused(f'no shared schema is called `{old}`')
  if taken:
    raise Refused(f'a shared schema is already called `{new}`')
  documents = {**scopes, **{path: json.loads(path.read_text()) for path in sorted((spec_root / 'endpoints').rglob('endpoint.json'))}}
  for path in holding:
    schemas = documents[path]
    documents[path] = {(new if key == old else key): value for key, value in schemas.items()}
    if isinstance(documents[path][new], dict) and documents[path][new].get('title') == old:
      documents[path][new]['title'] = new
  refs = 0
  changed = set(holding)
  for path, document in documents.items():
    count = _replace_refs(document, old, new)
    if count:
      refs += count
      changed.add(path)
  for path in sorted(changed):
    _write_json(path, documents[path])
  return refs, len(changed)


def core_of(directory: Path, endpoints_root: Path, cache: dict[Path, str | None]) -> str | None:
  """The core the nearest `router.json` at or above `directory` declares."""
  if directory in cache:
    return cache[directory]
  doc = load_router(directory)
  if doc is not None and doc.core is not None:
    core = doc.core
  elif directory == endpoints_root or directory.parent == directory:
    core = None
  else:
    core = core_of(directory.parent, endpoints_root, cache)
  cache[directory] = core
  return core


def core_pools(
  records: list[EndpointRecord], endpoints_root: Path, cache: dict[Path, str | None],
) -> dict[tuple[str | None, str], list[Any]]:
  """`(core, payload path)` -> every recorded frame carrying that path, across the
  project, in endpoint order: the frames an unrecorded endpoint of that core stands on."""
  pools: dict[tuple[str | None, str], list[Any]] = {}
  for record in records:
    selected = payload_keys(json.loads(record.path.read_text()))
    if selected is None:
      continue
    path, keys = selected
    frames = [
      frame for frame in recorded_frames(record.path.parent / 'examples')
      if isinstance(frame, dict) and _present(frame, keys)
    ]
    if frames:
      core = core_of(record.path.parent, endpoints_root, cache)
      pools.setdefault((core, path), []).extend(frames)
  return pools


def migrate(
  project: str | None = PROJECT_OPTION,
  path: str | None = PATH_OPTION,
  template: str | None = typer.Option(
    None, '--template',
    help=(
      'Function name of a recorded endpoint whose frame stands in for an endpoint with no '
      'recording of its own (an `unverified` one sharing the API\'s envelope). Without it, '
      'the recordings of endpoints sharing the core and payload path stand in.'
    ),
  ),
  rename_schemas: list[str] = typer.Option(
    [], '--rename-schema',
    help=(
      '`OLD=NEW`: rename a shared schema (id, title, every `$ref`) before migrating, for a '
      'router group whose class name a shared schema already takes (authoring rule 18). Repeatable.'
    ),
  ),
  report: Path | None = typer.Option(
    None, '--report',
    help='Also write the outcome per endpoint (migrated with its frame source, refused with the reason) as JSON.',
  ),
):
  """Rewrite response schemas written for the unwrapped value into wire-frame schemas.

  For every rpc endpoint declaring `envelope.payload` whose response schema does not carry
  that path (or does, but its recordings show the schema is still the unwrapped value),
  wraps the schema into the frame the recordings show: a `<Title>Frame` object whose other
  properties are inferred from the recorded keys and described as wire fields, with the old
  schema under the payload key (ADR 0010). An endpoint already in wire shape is left as it
  is, so the command is safe to rerun. An endpoint with no recording takes its frame from
  `--template`, else from every recorded endpoint of the same core with the same payload
  path, and is refused when neither exists.

  Args:
    project: Project directory holding `truewire.toml`; defaults to the nearest one.
    path: Project root, or one subdivision of its `spec/endpoints`, to scope the run to.
    template: Function name of a recorded endpoint supplying the frame for endpoints
      without recordings.
    rename_schemas: `OLD=NEW` shared schema renames, applied first.
    report: JSON file to write the per-endpoint outcome to.
  """
  loaded, scoped = resolve_spec_scope(project, path)
  spec_root = project_spec_dir(loaded)
  endpoints_root = spec_root / 'endpoints'
  for rename in rename_schemas:
    old, sep, new = rename.partition('=')
    if not sep or not old or not new:
      typer.echo(f'--rename-schema {rename}: expected OLD=NEW', err=True)
      raise typer.Exit(code=1)
    try:
      renamed = rename_schema(spec_root, old, new)
    except Refused as exc:
      typer.echo(f'--rename-schema {rename}: {exc}', err=True)
      raise typer.Exit(code=1)
    if renamed is None:
      typer.echo(f'renamed    schema {old} -> {new} (already done)')
    else:
      typer.echo(f'renamed    schema {old} -> {new} ({renamed[0]} $ref(s), {renamed[1]} file(s))')
  shared = load_shared_schemas(loaded)
  records = sorted(endpoint_records(loaded, scope=scoped.scope), key=lambda record: record.path)
  cache: dict[Path, str | None] = {}
  pools = core_pools(sorted(endpoint_records(loaded), key=lambda record: record.path), endpoints_root, cache)
  validity = Validity(shared)

  template_frames: list[Any] | None = None
  if template is not None:
    template_frames = _template_frames(loaded, spec_root, template)

  migrated: list[dict[str, str]] = []
  refused: list[dict[str, str]] = []
  unchanged = 0
  assumed: list[str] = []
  for record in records:
    function = record.endpoint.resolved_function(record.path, spec_root)
    core_name = core_of(record.path.parent, endpoints_root, cache)
    selected = payload_keys(json.loads(record.path.read_text()))
    core_frames = pools.get((core_name, selected[0]), []) if selected is not None else []
    try:
      result = migrate_endpoint(
        record, shared=shared, template_frames=template_frames, core=(core_name, core_frames),
        assumed=assumed, validity=validity,
      )
    except Refused as exc:
      refused.append({'function': function, 'reason': str(exc)})
      continue
    if result is None:
      unchanged += 1
      continue
    title, keys, source = result
    migrated.append({'function': function, 'frame': title, 'source': source})
    note = '' if source == 'recordings' else f'  [frame {SOURCES[source]}]'
    typer.echo(f'migrated   {function}  {title} ({keys}){note}')

  typer.echo()
  counts = ', '.join(
    f'{sum(1 for entry in migrated if entry["source"] == source)} {label}'
    for source, label in SOURCES.items()
  )
  typer.echo(
    f'Migrated {len(migrated)} endpoint(s); {unchanged} left as they are (no `envelope.payload`, '
    f'or already in wire shape).'
  )
  if migrated:
    typer.echo(f'Frames: {counts}.')
  by_field: dict[str, int] = {}
  for field in assumed:
    by_field[field] = by_field.get(field, 0) + 1
  if by_field:
    fields = ', '.join(f'`{field}` ({count})' for field, count in sorted(by_field.items()))
    typer.echo(
      f'Element type assumed `string` for arrays every recording shows empty: {fields}. '
      f'No recording evidences it; the first one that disagrees fails `truewire check`.'
    )
  collisions = _schema_collisions(loaded)
  for message in collisions:
    typer.echo(f'collision  {message} Apply a new name with `--rename-schema OLD=NEW`.')
  if report is not None:
    report.write_text(json.dumps({
      'migrated': migrated, 'refused': refused, 'unchanged': unchanged,
      'assumed_string_items': dict(sorted(by_field.items())),
      'schema_collisions': collisions,
    }, indent=2) + '\n')
  if refused:
    typer.echo(f'Refused {len(refused)} endpoint(s):', err=True)
    for entry in refused:
      typer.echo(f'  {entry["function"]}: {entry["reason"]}', err=True)
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


def _schema_collisions(loaded: Project) -> list[str]:
  """Rule 18's router-group-versus-shared-schema findings, as `truewire check` words them."""
  from truewire.spec.authoring import check_router_names

  return [
    f'{violation["location"]}: {violation["message"]}' for violation in check_router_names(loaded)
    if 'shared schema' in violation['message']
  ]
