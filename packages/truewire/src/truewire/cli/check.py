import json
from copy import deepcopy
from pathlib import Path
from typing_extensions import Any

import typer

from truewire.project import Project, spec_dir as project_spec_dir
from truewire.spec import (
  StreamEnvelopeSpec,
  endpoint_records,
  endpoint_specs,
  envelope_spec,
  load_shared_schemas,
  load_ws_binary_frames,
  read_dotted_path,
  response_example_payload,
)

from .common import PATH_OPTION, PROJECT_OPTION, resolve_spec_scope
from truewire.spec.authoring import (
  RULE_HEADINGS, WARNING_RULES, audit, check_meta, check_mixed_leaf_router,
  check_router_names, check_schema_cycles, severity,
)

max_samples = 5
"""Offending locations shown per rule before the remainder collapses into a count."""


def report_authoring(
  root: Path | Project, client: str, *, verbose: bool, scope: Path | None = None
) -> int:
  """Audit every endpoint of a project against `docs/spec/authoring.md`.

  Findings are grouped by rule and sampled, so a project carrying four figures of debt
  reports as a ranked list of rules rather than as thousands of lines. Only
  `error`-severity violations fail gate 1 — see `truewire.spec.authoring.severity` and
  `WARNING_RULES` — but the summary line always states both counts, so a `warning` total
  cannot grow unnoticed just because it never turns the gate red.

  Also runs `check_mixed_leaf_router`, `check_meta`, `check_schema_cycles` and
  `check_router_names` once each, over the whole project
  (`root`, never `scope` -- a project-root-level check, like `check_router_core`/
  `check_schemas_no_shadowing` beside them in `truewire.spec.authoring`, has no
  per-endpoint scope to restrict to) -- unlike those two, both are wired in here so their
  own findings actually surface in `truewire check`'s own output, `check_mixed_leaf_
  router`'s `warning`-severity rollout rule (rule 16 / S30) the same way `ws-verb`/
  `title-empty-object` do, and `check_meta`'s `meta-schema`/`meta-collision` (design
  §2/§6) as `error`-severity findings, same as every other checkable-fact rule.
  `check_router_names` (rule 18) is the one of them that reads `truewire.toml`'s own
  backend sections: it runs here with no `language`, so the gate judges the declared
  client name of every backend the project declares at once, where
  `truewire generate <language>`'s own refusal narrows it to the one being generated.

  Args:
    root: Project (or project root).
    client: Project name, used in the summary line.
    verbose: Show every offending location instead of a bounded sample.
    scope: Subdivision under `<spec>/endpoints` to restrict the audit to.

  Returns:
    Number of error-severity violations found. Warnings are reported but never counted
    here, so they never fail the gate on their own.
  """
  spec_root = project_spec_dir(root)
  records = endpoint_records(root, scope=scope)
  grouped: dict[str, list[str]] = {}
  dirty: set[str] = set()
  errors = 0
  warnings = 0
  paginated = sum(1 for record in records if record.endpoint.pagination is not None)
  for record in records:
    # `endpoint.function` is `None` for a fully mechanized endpoint (dropped,
    # derived from directory position) -- `resolved_function` falls back to deriving it
    # from `record.path`'s own position under `spec/endpoints/` the same way
    # `codegen/layout.py`'s `output_function` already does, so a migrated project's
    # findings still name the real endpoint instead of printing a bare `None`.
    function = record.endpoint.resolved_function(record.path, spec_root)
    for violation in audit(record.endpoint):
      if severity(violation) == 'error':
        errors += 1
        dirty.add(function)
      else:
        warnings += 1
      grouped.setdefault(violation['rule'], []).append(
        f'{function}  {violation["location"]}: {violation["message"]}'
      )
  for violation in [
    *check_mixed_leaf_router(root), *check_meta(root), *check_schema_cycles(root),
    *check_router_names(root),
  ]:
    if severity(violation) == 'error':
      errors += 1
      dirty.add(violation['location'])
    else:
      warnings += 1
    grouped.setdefault(violation['rule'], []).append(
      f'{violation["location"]}: {violation["message"]}'
    )

  typer.echo('Spec authoring:')
  typer.echo(f'  pagination  {paginated}')
  typer.echo(f'  violations {errors}')
  typer.echo(f'  warnings   {warnings}')
  if not errors and not warnings:
    return 0

  typer.echo()
  typer.echo('Authoring findings:')
  for rule, heading in RULE_HEADINGS.items():
    lines = grouped.get(rule)
    if not lines:
      continue
    label = 'warning' if rule in WARNING_RULES else 'violation'
    is_warning = label == 'warning'
    typer.echo(
      f'  {len(lines):>4}  {heading} [{label}{"" if len(lines) == 1 else "s"}]',
      err=not is_warning,
    )
    shown = lines if verbose else lines[:max_samples]
    for line in shown:
      typer.echo(f'        {line}', err=True)
    if len(lines) > len(shown):
      typer.echo(
        f'        ... {len(lines) - len(shown)} more, rerun with --verbose', err=True
      )
  if errors:
    typer.echo(
      f'{errors} authoring violation{"" if errors == 1 else "s"} across '
      f'{len(dirty)}/{len(records)} endpoints of {client}; see '
      f'`docs/spec/authoring.md`.',
      err=True,
    )
  return errors


def check(
  project: str | None = PROJECT_OPTION,
  path: str | None = PATH_OPTION,
  verbose: bool = typer.Option(False, '--verbose', '-v'),
):
  """Lint the spec and replay every recorded example against its endpoint's response schema.

  For each `endpoint.json` in scope, validates its `*.response.json` (rpc over http) or
  `*.reply.json`/`*.messages.json` (rpc over ws, or stream) examples against the endpoint's
  response schema, `$ref`s into `spec/schemas.json` resolved first. An rpc recording is
  the wire body and validates whole; a declared `envelope.payload` selects the returned
  value inside the schema, it extracts nothing here (ADR 0010). An endpoint with no
  recorded examples is skipped, not failed — that gap is `truewire examples`'s to
  report. Also runs the `docs/spec/authoring.md` audit over the same scope.

  Args:
    project: Project directory holding `truewire.toml`; defaults to the nearest one.
    path: Project root, or one subdivision of its `spec/endpoints`, to scope the run to.
    verbose: List every example file as it is checked, and every authoring-audit
      violation instead of a bounded sample.
  """
  from jsonschema import Draft202012Validator
  from jsonschema.exceptions import ValidationError
  from referencing.exceptions import Unresolvable

  max_errors_per_file = 20

  class TestError(Exception):
    pass

  def rewrite_refs(obj: Any) -> Any:
    if isinstance(obj, dict):
      out: dict[str, Any] = {}
      for key, value in obj.items():
        if key == '$ref' and isinstance(value, str) and not value.startswith('#/'):
          out[key] = f'#/$defs/{value}'
        else:
          out[key] = rewrite_refs(value)
      return out
    if isinstance(obj, list):
      return [rewrite_refs(value) for value in obj]
    return obj

  def response_json_schema(response: dict[str, Any]) -> dict[str, Any] | None:
    content = response.get('content')
    if not isinstance(content, dict):
      return None
    media = content.get('application/json')
    if not isinstance(media, dict):
      return None
    schema = media.get('schema')
    if not isinstance(schema, dict):
      return None
    return schema

  def has_protobuf_content(response: dict[str, Any]) -> bool:
    """Return whether the response declares Protobuf wire content."""
    content = response.get('content')
    return isinstance(content, dict) and 'application/x-protobuf' in content

  def dotted_path_present(value: Any, path: str) -> bool:
    """Whether every segment of a dotted path actually exists in `value`.

    Only a stream's frames are still read through a path here (ADR 0010 left stream
    schemas describing the extracted message). `read_dotted_path` returns `None` both
    when a path segment is genuinely absent and when it is present with a `null` value --
    a schema is free to declare a nullable payload, so `extracted is None` alone can't
    tell "not found" apart from "found, and null." `path == ''` (the whole-frame
    sentinel, `PayloadPath`) is always present -- there is no segment to look up.
    """
    if path == '':
      return True
    for part in path.split('.'):
      if not isinstance(value, dict) or part not in value:
        return False
      value = value[part]
    return True

  def schema_errors(validator: Draft202012Validator, instance: Any) -> list[ValidationError]:
    """Every schema violation for `instance`, in path order.

    A `$ref` that resolves to nothing (a typo, or a shared schema that was renamed) is
    reported as one violation naming the reference, instead of escaping as a traceback.
    """
    try:
      return sorted(validator.iter_errors(instance), key=lambda err: list(err.path))
    except Unresolvable as exc:
      return [ValidationError(f'unresolvable $ref: {exc}')]

  def validator_for(
    schema: dict[str, Any], shared_schemas: dict[str, Any]
  ) -> Draft202012Validator:
    defs: dict[str, Any] = {}

    def assign_def(key: str, value: Any):
      parts = key.split('/')
      node = defs
      for part in parts[:-1]:
        node = node.setdefault(part, {})
      node[parts[-1]] = rewrite_refs(value)

    root = deepcopy(rewrite_refs(schema))
    root.setdefault('$schema', 'https://json-schema.org/draft/2020-12/schema')
    for key, value in shared_schemas.items():
      assign_def(key, value)
    root['$defs'] = defs
    return Draft202012Validator(root)

  def http_examples(endpoint_path: Path) -> list[Path]:
    examples_dir = endpoint_path.parent / 'examples'
    if not examples_dir.is_dir():
      return []
    return sorted(examples_dir.glob('*.response.json'))

  def ws_examples(endpoint_path: Path) -> tuple[list[Path], list[Path]]:
    examples_dir = endpoint_path.parent / 'examples'
    if not examples_dir.is_dir():
      return [], []
    replies = sorted(examples_dir.glob('*.reply.json'))
    messages = sorted(examples_dir.glob('*.messages.json'))
    messages.extend(sorted(examples_dir.glob('*.message.json')))
    messages.extend(sorted(examples_dir.glob('*.messages.protobuf.json')))
    return replies, messages

  def validate_http_example(
    example_path: Path,
    endpoint_data: dict[str, Any],
    shared_schemas: dict[str, Any],
  ) -> list[str]:
    example = response_example_payload(example_path)
    spec = endpoint_data['spec']
    status = str(example['status'])
    # The recorded body, validated whole: the response schema describes the wire frame
    # and a declared `envelope.payload` only selects the returned value inside it (ADR
    # 0010) -- nothing to extract before validating.
    payload = example['payload']

    # A migrated endpoint carries `request`/`response` directly -- one schema
    # for the whole 2xx reply, no per-status `openapi.responses` map to look a status code
    # up in at all. Mirrors the identical dual-shape branch `truewire.spec.authoring`'s
    # own checks already take (`spec.request if spec.request is not None else
    # spec.parameters`) -- not a new mechanism, just this checker catching up to it.
    new_shape = spec.get('request') is not None or spec.get('response') is not None
    if new_shape:
      schema = spec.get('response')
      if schema is None:
        return [f'{example_path}: endpoint declares no `response` schema']
    else:
      responses = spec['openapi'].get('responses', {})
      if status not in responses:
        return [f'{example_path}: status {status} not present in endpoint responses']

      schema = response_json_schema(responses[status])
      if schema is None:
        return [f'{example_path}: response {status} has no application/json schema']

    validator = validator_for(schema, shared_schemas)
    errors = schema_errors(validator, payload)
    return format_validation_errors(example_path, errors)

  def validate_ws_reply_example(
    example_path: Path,
    endpoint_data: dict[str, Any],
    shared_schemas: dict[str, Any],
  ) -> list[str]:
    spec = endpoint_data['spec']
    # A migrated endpoint carries `request`/`response` (rpc) or
    # `parameters`/`payload` (stream) directly, no per-status `openapi.responses` map --
    # mirrors `validate_http_example`'s identical dual-shape branch. The `rpc`-kind
    # branch below matters for a WS-transported rpc endpoint, whose reply genuinely
    # needs validating against a real `response` schema.
    new_shape = (
      spec.get('request') is not None or spec.get('response') is not None
      or spec.get('parameters') is not None or spec.get('payload') is not None
    )
    if new_shape:
      if spec['kind'] == 'stream':
        # A migrated `stream` endpoint's subscribe acknowledgement has no schema of its
        # own any more (`StreamEndpointSpec` carries `request`/`parameters`/`payload`
        # only, replacing the legacy `openapi.responses.reply` slot) --
        # nothing left to validate a recorded `.reply.json` ack frame against.
        return []
      schema = spec.get('response')
      if schema is None:
        # docs/spec/authoring.md rule 11's "no-reply RPC" case (an `authenticate`
        # command that is never answered, say): a genuinely no-reply rpc
        # command (`spec.response: null`) records its own reply example as the literal
        # JSON `null`, matching `truewire.mock`'s own `after_rpc` dispatch
        # (`example.reply is None` -- "sends nothing back on the wire"), not a spec-
        # authoring gap. Any other recorded payload on a response-less endpoint still
        # errors -- there is nothing declared for it to validate against.
        payload = json.loads(example_path.read_text())
        if payload is None:
          return []
        return [f'{example_path}: endpoint declares no `response` schema']
    else:
      responses = spec['openapi'].get('responses', {})
      if 'reply' not in responses:
        return [f'{example_path}: endpoint has no `reply` response schema']
      schema = response_json_schema(responses['reply'])
      if schema is None:
        return [f'{example_path}: `reply` response has no application/json schema']
    payload = json.loads(example_path.read_text())

    envelope_data = endpoint_data.get('envelope')
    # An rpc reply validates whole: its `response` schema describes the wire frame and
    # `envelope.payload` selects the returned value inside it (ADR 0010). A stream's ack
    # still extracts -- ADR 0010 leaves stream schemas as they were.
    if envelope_data is not None and spec['kind'] == 'stream':
      envelope = envelope_spec(envelope_data, spec_kind='stream')
      # A stream ack is a different frame from the pushed message the same envelope's
      # `payload` describes -- a JSON-RPC ack is a whole reply (`result`), never
      # `payload`'s `params.data` -- so `reply_payload` overrides it here when declared,
      # matching an API whose ack genuinely needs its own extraction path.
      reply_payload = (
        envelope.reply_payload
        if isinstance(envelope, StreamEnvelopeSpec) and envelope.reply_payload is not None
        else envelope.payload
      )
      if not dotted_path_present(payload, reply_payload):
        return [
          f'{example_path}: envelope declares reply payload={reply_payload!r}, not found in the recorded raw payload'
        ]
      payload = read_dotted_path(payload, reply_payload)

    validator = validator_for(schema, shared_schemas)
    errors = schema_errors(validator, payload)
    return format_validation_errors(example_path, errors)

  def validate_ws_messages_example(
    example_path: Path,
    endpoint_data: dict[str, Any],
    shared_schemas: dict[str, Any],
  ) -> list[str]:
    spec = endpoint_data['spec']
    # See `validate_ws_reply_example`'s identical dual-shape gate, just above -- same
    # reasoning, `payload` rather than `response`.
    new_shape = (
      spec.get('request') is not None or spec.get('response') is not None
      or spec.get('parameters') is not None or spec.get('payload') is not None
    )
    if new_shape:
      schema = spec.get('payload')
      if schema is None:
        return [f'{example_path}: endpoint declares no `payload` schema']
      if example_path.name.endswith('.messages.protobuf.json'):
        try:
          load_ws_binary_frames(example_path)
        except Exception as exc:
          return [f'{example_path}: invalid protobuf frame fixture: {exc}']
        return []
    else:
      responses = spec['openapi'].get('responses', {})
      if 'message' not in responses:
        return [f'{example_path}: endpoint has no `message` response schema']
      if example_path.name.endswith('.messages.protobuf.json'):
        if not has_protobuf_content(responses['message']):
          return [
            f'{example_path}: `message` response has no application/x-protobuf schema'
          ]
        try:
          load_ws_binary_frames(example_path)
        except Exception as exc:
          return [f'{example_path}: invalid protobuf frame fixture: {exc}']
        return []

      schema = response_json_schema(responses['message'])
      if schema is None:
        return [f'{example_path}: `message` response has no application/json schema']

    payload = json.loads(example_path.read_text())
    messages = payload if isinstance(payload, list) else [payload]

    envelope_data = endpoint_data.get('envelope')
    envelope = (
      envelope_spec(envelope_data, spec_kind=endpoint_data['spec']['kind'])
      if envelope_data is not None
      else None
    )

    validator = validator_for(schema, shared_schemas)
    out: list[str] = []
    for index, message in enumerate(messages):
      if envelope is not None:
        # A stream that declares `envelope` stores each message as the complete raw
        # push frame (`docs/spec/spec.md`'s "Declared Envelope Extraction" section),
        # mirroring an `rpc` endpoint's `response.json` -- `payload` is extracted here
        # before validating, the same way `validate_ws_reply_example` does for `reply_payload`.
        if not dotted_path_present(message, envelope.payload):
          out.append(
            f'{example_path}: [{index}] envelope declares payload={envelope.payload!r}, '
            'not found in the recorded raw message'
          )
          continue
        message = read_dotted_path(message, envelope.payload)
      errors = schema_errors(validator, message)
      out.extend(format_validation_errors(example_path, errors, prefix=f'[{index}]'))
    return out

  def format_validation_error(
    path: Path, err: ValidationError, *, prefix: str = ''
  ) -> str:
    location = ''.join(
      f'[{part}]' if isinstance(part, int) else f'.{part}' for part in err.path
    )
    if location.startswith('.'):
      location = location[1:]
    if prefix:
      location = prefix + (f'.{location}' if location else '')
    return f'{path}: {location or "<root>"}: {err.message}'

  def format_validation_errors(
    path: Path,
    errors: list[ValidationError],
    *,
    prefix: str = '',
  ) -> list[str]:
    if len(errors) <= max_errors_per_file:
      return [format_validation_error(path, err, prefix=prefix) for err in errors]
    out = [
      format_validation_error(path, err, prefix=prefix)
      for err in errors[:max_errors_per_file]
    ]
    out.append(
      f'{path}: {prefix or "<root>"}: ... {len(errors) - max_errors_per_file} more errors omitted'
    )
    return out

  loaded, scoped = resolve_spec_scope(project, path)
  client = loaded.name
  try:
    root = loaded
    endpoints_root = scoped.endpoints_root
    shared_schemas = load_shared_schemas(root)

    endpoint_paths = endpoint_specs(root, scope=scoped.scope)
    if not endpoint_paths:
      # Same rule as `truewire examples`: a gate must never report success having examined
      # nothing.
      raise TestError(
        f'{scoped.scope}: no endpoint specs found, so nothing was checked'
      )
    endpoint_data = [
      (endpoint_path, json.loads(endpoint_path.read_text()))
      for endpoint_path in endpoint_paths
    ]
    kinds = {
      'rpc': sum(
        1 for _, data in endpoint_data if data.get('spec', {}).get('kind') == 'rpc'
      ),
      'stream': sum(
        1 for _, data in endpoint_data if data.get('spec', {}).get('kind') == 'stream'
      ),
      'grpc': sum(
        1 for _, data in endpoint_data if data.get('spec', {}).get('kind') == 'grpc'
      ),
    }
    typer.echo(f'Project: {client}')
    if scoped.is_subdivision:
      typer.echo(f'Scope: {scoped.scope.relative_to(endpoints_root)} (subdivision)')
    kind_breakdown = f'rpc={kinds["rpc"]}, stream={kinds["stream"]}'
    if kinds['grpc']:
      kind_breakdown += f', grpc={kinds["grpc"]}'
    typer.echo(f'Endpoints: {len(endpoint_paths)} ({kind_breakdown})')
    checked_endpoints = 0
    checked_examples = 0
    failures: list[str] = []

    for endpoint_path, data in endpoint_data:
      spec = data.get('spec', {})
      kind = spec.get('kind')
      transports = spec.get('transports', [])

      if kind == 'rpc':
        if 'http' in transports:
          examples = http_examples(endpoint_path)
          if examples:
            checked_endpoints += 1
            for example_path in examples:
              checked_examples += 1
              if verbose:
                typer.echo(
                  f'[{client}] rpc/http {endpoint_path.parent.relative_to(endpoints_root)} :: {example_path.name}'
                )
              failures.extend(validate_http_example(example_path, data, shared_schemas))

        if 'ws' in transports:
          replies, messages = ws_examples(endpoint_path)
          if replies or messages:
            checked_endpoints += 1
            for example_path in replies:
              checked_examples += 1
              if verbose:
                typer.echo(
                  f'[{client}] rpc/ws reply {endpoint_path.parent.relative_to(endpoints_root)} :: {example_path.name}'
                )
              failures.extend(
                validate_ws_reply_example(example_path, data, shared_schemas)
              )
            for example_path in messages:
              checked_examples += 1
              if verbose:
                typer.echo(
                  f'[{client}] rpc/ws message {endpoint_path.parent.relative_to(endpoints_root)} :: {example_path.name}'
                )
              failures.extend(
                validate_ws_messages_example(example_path, data, shared_schemas)
              )

      elif kind == 'stream':
        replies, messages = ws_examples(endpoint_path)
        if not replies and not messages:
          continue
        checked_endpoints += 1
        for example_path in replies:
          checked_examples += 1
          if verbose:
            typer.echo(
              f'[{client}] stream reply {endpoint_path.parent.relative_to(endpoints_root)} :: {example_path.name}'
            )
          failures.extend(validate_ws_reply_example(example_path, data, shared_schemas))
        for example_path in messages:
          checked_examples += 1
          if verbose:
            typer.echo(
              f'[{client}] stream message {endpoint_path.parent.relative_to(endpoints_root)} :: {example_path.name}'
            )
          failures.extend(
            validate_ws_messages_example(example_path, data, shared_schemas)
          )

    typer.echo()
    typer.echo('Example validation:')
    typer.echo(f'  endpoints  {checked_endpoints}/{len(endpoint_paths)}')
    typer.echo(f'  files      {checked_examples}')
    typer.echo(f'  errors     {len(failures)}')

    if failures:
      typer.echo()
      typer.echo('Validation errors:', err=True)
      for failure in failures:
        typer.echo(f'  {failure}', err=True)

    typer.echo()
    violations = report_authoring(root, client, verbose=verbose, scope=scoped.scope)
    typer.echo()
    typer.echo(f'Result: {"FAILED" if failures or violations else "OK"}')
    if failures or violations:
      raise typer.Exit(code=1)
  except TestError as exc:
    typer.echo(str(exc), err=True)
    raise typer.Exit(code=1)
