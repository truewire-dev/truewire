"""
OpenAPI 3.0/3.1 -> spec-tree importer (prototype for `truewire import openapi`).

Reads one OpenAPI document and writes the directory-per-endpoint spec tree
`truewire.spec` already loads: `spec/endpoints/<group>/<operation>/endpoint.json`,
`router.json` per grouping, `spec/schemas.json` for component schemas shared by two or
more places, and `examples/<id>.{request,response}.json` pairs from the document's own
response examples. Everything it writes is checked against `docs/spec/authoring.md`
(rules 0, 1, 2, 3, 5, 7, 8, 9, 14, 16) by `validate_tree`, using the real loaders and
the real `spec test` checks -- an importer that writes what the checks refuse is no
importer.

Written against `truewire` so that `sed 's/truewire/truewire/g'` ports it: only
`truewire.spec`, `truewire.spec.authoring` and `truewire.generation` are imported.
"""
import json
import keyword
import re
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing_extensions import Any, Literal

import yaml
from truewire.generation.schema import LocalResolver, Schema
from truewire.generation.types import FlattenAllOf, RemoveOneOf
from jsonschema import Draft202012Validator

from truewire.spec import Endpoint, ExampleRequest, ExampleResponse, load_router
from truewire.spec.authoring import (
  Violation, audit, check_meta, check_mixed_leaf_router, check_router_core, severity,
)
from truewire.spec.repo import endpoint_records, http_examples, load_shared_schemas

FunctionStyle = Literal['tags', 'path']
"""How operations map onto directories: one group per first tag, or nested path segments."""

HTTP_METHODS = ('get', 'put', 'post', 'delete', 'options', 'head', 'patch', 'trace')
"""Path-item keys that are operations, in OpenAPI's own order."""

SCHEMA_REF_PREFIX = '#/components/schemas/'
COMPONENT_REF = re.compile(r'^#/components/(schemas|parameters|responses|requestBodies|examples)/([^/]+)$')
"""The only `$ref` targets this importer resolves: components of the document itself."""

OPENAPI_ONLY_KEYS = frozenset({'example', 'examples', 'nullable', 'xml', 'externalDocs', 'discriminator'})
"""Keys OpenAPI adds on top of JSON Schema that the spec model never reads; stripped from
every schema (`nullable` after it has been folded into the type, below)."""

NUMERIC_BOUND_KEYS = ('minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum', 'multipleOf')
"""Bounds the `Schema` model types as `float`; an integral value is written back as `int`."""

KEY_ORDER = ('$ref', 'title', 'description', 'deprecated', 'type', 'format', 'enum', 'default')
"""Leading key order for written schemas, for readability; the rest keep their own order,
with `required`/`additionalProperties` last."""

PAGINATION_NAMES = frozenset({
  'page', 'pagesize', 'limit', 'offset', 'cursor', 'next', 'before', 'after', 'since',
  'start', 'end',
})
"""Parameter names (folded through `fold_name`) that usually mean pagination -- a hint only;
rule 8 says pagination is declared, never inferred."""

IMPORTED_EXAMPLES_NOTE = 'examples imported from the OpenAPI document, not captured live'
NOT_CAPTURED_DETAIL = 'imported from OpenAPI with no example of its own; not yet captured live'
"""`unverified.detail` for an endpoint the document gave no example for."""


class OpenApiImportError(Exception):
  """A document this importer cannot represent: external `$ref`s, no upstream URL, ..."""


@dataclass
class ImportReport:
  """What one `import_openapi` run did, printable as a short summary."""
  operations: int = 0
  """Operations found in the document."""
  written: int = 0
  """Endpoints written."""
  skipped: list[tuple[str, str]] = field(default_factory=list)
  """`(operation, reason)` for every operation not written."""
  schemas_shared: list[str] = field(default_factory=list)
  """Component schema names written to `spec/schemas.json`."""
  examples: int = 0
  """Request/response example pairs written."""
  warnings: list[str] = field(default_factory=list)
  """Everything a human should look at: dropped parameters, missing descriptions, nesting."""
  endpoints: list[Path] = field(default_factory=list)
  """Every `endpoint.json` written, relative to `out`."""

  def summary(self) -> str:
    """Render the report as readable text."""
    lines = [
      f'operations      {self.operations}',
      f'written         {self.written}',
      f'skipped         {len(self.skipped)}',
      *(f'  - {name}: {reason}' for name, reason in self.skipped),
      f'shared schemas  {len(self.schemas_shared)}'
      + (f' ({", ".join(self.schemas_shared)})' if self.schemas_shared else ''),
      f'examples        {self.examples}',
      f'warnings        {len(self.warnings)}',
      *(f'  - {warning}' for warning in self.warnings),
    ]
    return '\n'.join(lines)

  def __str__(self) -> str:
    return self.summary()


@dataclass
class ValidationResult:
  """Outcome of `validate_tree`: what the real loaders and checks say about a written tree."""
  endpoints: int = 0
  examples: int = 0
  errors: list[str] = field(default_factory=list)
  warnings: list[str] = field(default_factory=list)

  @property
  def ok(self) -> bool:
    """Whether the tree passes with zero errors (warnings never fail it)."""
    return not self.errors


def load_document(path: Path) -> dict[str, Any]:
  """
  Load an OpenAPI document from JSON or YAML, by extension (`.json` is JSON, else YAML).

  Args:
    path: Document path.
  """
  text = Path(path).read_text()
  if path.suffix.lower() == '.json':
    doc = json.loads(text)
  else:
    doc = yaml.safe_load(text)
  if not isinstance(doc, dict) or 'openapi' not in doc:
    raise OpenApiImportError(f'{path}: not an OpenAPI document (no top-level `openapi` key)')
  return doc


def snake_case(name: str) -> str:
  """Fold any identifier-ish string to a valid, non-keyword `snake_case` Python identifier."""
  words = re.sub(r'[^A-Za-z0-9]+', ' ', re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', name)).split()
  out = '_'.join(word.lower() for word in words) or 'root'
  if out[0].isdigit():
    out = f'x_{out}'
  if keyword.iskeyword(out):
    out = f'{out}_'
  return out


def pascal_case(name: str) -> str:
  """Fold any identifier-ish string to `PascalCase`."""
  return ''.join(word[:1].upper() + word[1:] for word in snake_case(name).split('_'))


def fold_name(name: str) -> str:
  """Lowercase alphanumerics only, so `page_size` meets `pageSize`."""
  return re.sub(r'[^a-z0-9]', '', name.lower())


def order_keys(schema: dict[str, Any]) -> dict[str, Any]:
  """Reorder one schema's keys per `KEY_ORDER`, trailing `required`/`additionalProperties`."""
  trailing = ('required', 'additionalProperties')
  head = [k for k in KEY_ORDER if k in schema]
  middle = [k for k in schema if k not in KEY_ORDER and k not in trailing]
  tail = [k for k in trailing if k in schema]
  return {k: schema[k] for k in [*head, *middle, *tail]}


def is_ref(value: Any) -> bool:
  """Whether a node is a `$ref`."""
  return isinstance(value, dict) and isinstance(value.get('$ref'), str)


class Components:
  """The document's `components`, with `$ref` resolution and shared-schema bookkeeping."""

  def __init__(self, doc: dict[str, Any]):
    self.doc = doc
    components = doc.get('components') or {}
    self.schemas: dict[str, Any] = dict(components.get('schemas') or {})
    self.parameters: dict[str, Any] = dict(components.get('parameters') or {})
    self.responses: dict[str, Any] = dict(components.get('responses') or {})
    self.request_bodies: dict[str, Any] = dict(components.get('requestBodies') or {})
    self.examples: dict[str, Any] = dict(components.get('examples') or {})
    self.shared: set[str] = set()
    """Component schema names written to `schemas.json` rather than inlined."""

  def check_refs(self):
    """Raise on any `$ref` that does not point into this document's own components."""
    external: list[str] = []
    for ref in iter_refs(self.doc):
      if not COMPONENT_REF.match(ref):
        external.append(ref)
    if external:
      raise OpenApiImportError(
        'unsupported $ref targets (only `#/components/...` of the same document are resolved): '
        + ', '.join(sorted(set(external)))
      )

  def component(self, ref: str) -> tuple[str, Any]:
    """Return `(kind, value)` for a component `$ref`."""
    match = COMPONENT_REF.match(ref)
    if match is None:
      raise OpenApiImportError(f'unsupported $ref {ref!r}')
    kind, name = match.groups()
    table = {
      'schemas': self.schemas, 'parameters': self.parameters, 'responses': self.responses,
      'requestBodies': self.request_bodies, 'examples': self.examples,
    }[kind]
    if name not in table:
      raise OpenApiImportError(f'{ref}: no such component')
    return kind, table[name]

  def resolve_object(self, value: Any) -> Any:
    """Resolve a non-schema component `$ref` (parameter, response, body, example) in place."""
    while is_ref(value) and not value['$ref'].startswith(SCHEMA_REF_PREFIX):
      _kind, value = self.component(value['$ref'])
    return value

  def decide_shared(self, roots: list[Any]):
    """
    Decide which component schemas go to `schemas.json`: those referenced two or more
    times from the imported content (transitively through components), plus any schema
    on a reference cycle, which could never be inlined (rule 0).

    Args:
      roots: The schema-bearing fragments that will actually be imported (parameters,
        bodies, 2xx responses) -- refs from dropped 4xx responses do not count.
    """
    counts: dict[str, int] = {}
    edges: dict[str, set[str]] = {name: set() for name in self.schemas}
    for name, schema in self.schemas.items():
      for ref in iter_refs(schema):
        if ref.startswith(SCHEMA_REF_PREFIX):
          edges[name].add(ref[len(SCHEMA_REF_PREFIX):])
    seen: set[str] = set()
    stack: list[str] = []
    for root in roots:
      for ref in iter_refs(root):
        if ref.startswith(SCHEMA_REF_PREFIX):
          stack.append(ref[len(SCHEMA_REF_PREFIX):])
    while stack:
      name = stack.pop()
      counts[name] = counts.get(name, 0) + 1
      if name in seen:
        continue
      seen.add(name)
      stack.extend(edges.get(name, ()))
    cyclic = {name for name in seen if on_cycle(name, edges)}
    self.shared = {name for name, count in counts.items() if count >= 2} | cyclic


def on_cycle(start: str, edges: dict[str, set[str]]) -> bool:
  """Whether `start` can reach itself through `edges`."""
  stack = list(edges.get(start, ()))
  visited: set[str] = set()
  while stack:
    node = stack.pop()
    if node == start:
      return True
    if node in visited:
      continue
    visited.add(node)
    stack.extend(edges.get(node, ()))
  return False


def iter_refs(value: Any):
  """Yield every `$ref` string anywhere under `value`."""
  if isinstance(value, dict):
    for key, item in value.items():
      if key == '$ref' and isinstance(item, str):
        yield item
      else:
        yield from iter_refs(item)
  elif isinstance(value, list):
    for item in value:
      yield from iter_refs(item)


class SchemaNormalizer:
  """
  Turns an OpenAPI schema into the JSON Schema the spec model wants (rules 1, 2, 5).

  Steps, in order: resolve component `$ref`s (inline once-used components, keep shared
  ones as bare-id `$ref`s the spec's `schemas.json` convention uses); fold 3.0 `nullable`
  into the type; run `truewire.generation`'s own `RemoveOneOf`/`FlattenAllOf` through the
  `Schema` model; then strip OpenAPI-only keys, rewrite nullable objects as `anyOf`, turn
  `const` into a one-value `enum`, and title every object from its position.
  """

  def __init__(self, components: Components):
    self.components = components
    self.shared_models: dict[str, Schema] = {}
    self.warnings: list[str] = []

  def prepare(self):
    """Build the resolver the `allOf` flattener needs, over every shared component."""
    for name in sorted(self.components.shared):
      raw = self.resolve(deepcopy(self.components.schemas[name]), stack=(name,))
      self.shared_models[name] = Schema.model_validate(self.fold_nullable(raw))

  def normalize(self, schema: Any, *, title: str) -> dict[str, Any]:
    """
    Normalize one schema.

    Args:
      schema: Raw OpenAPI schema (may be a `$ref`).
      title: Name for this schema when it is an untitled object; nested objects derive
        theirs from it.
    """
    resolved = self.resolve(deepcopy(schema), stack=())
    if is_ref(resolved):
      return resolved
    folded = self.fold_nullable(resolved)
    model = Schema.model_validate(folded)
    resolver = LocalResolver(self.shared_models)
    for step in (RemoveOneOf(), FlattenAllOf(resolver)):
      model = step(model)
    dumped = model.model_dump(mode='json', by_alias=True, exclude_defaults=True)
    return self.finish(dumped, title=title)

  def shared_schema(self, name: str) -> dict[str, Any]:
    """Normalize one shared component for `schemas.json`, titled by its own name."""
    return self.normalize(self.components.schemas[name], title=name)

  def resolve(self, schema: Any, *, stack: tuple[str, ...]) -> Any:
    """Inline once-used component schemas; rewrite shared ones to bare-id `$ref`s."""
    if isinstance(schema, list):
      return [self.resolve(item, stack=stack) for item in schema]
    if not isinstance(schema, dict):
      return schema
    if is_ref(schema):
      ref = schema['$ref']
      if not ref.startswith(SCHEMA_REF_PREFIX):
        raise OpenApiImportError(f'{ref}: a schema position may only reference `#/components/schemas/`')
      name = ref[len(SCHEMA_REF_PREFIX):]
      if name in self.components.shared:
        return {'$ref': name}
      if name in stack:
        raise OpenApiImportError(f'{ref}: reference cycle through {" -> ".join(stack)}')
      _kind, target = self.components.component(ref)
      inlined = deepcopy(target)
      if isinstance(inlined, dict) and not is_ref(inlined):
        inlined.setdefault('title', name)
        extra = {k: v for k, v in schema.items() if k != '$ref'}
        inlined = {**inlined, **extra}
      return self.resolve(inlined, stack=(*stack, name))
    return {key: self.resolve(value, stack=stack) for key, value in schema.items()}

  def fold_nullable(self, schema: Any) -> Any:
    """Turn 3.0 `nullable: true` into a `null` type branch (list form; objects are
    rewritten to `anyOf` in `finish`)."""
    if isinstance(schema, list):
      return [self.fold_nullable(item) for item in schema]
    if not isinstance(schema, dict):
      return schema
    out = {key: self.fold_nullable(value) for key, value in schema.items() if key != 'nullable'}
    if schema.get('nullable') is True:
      declared = out.get('type')
      if isinstance(declared, str):
        out['type'] = [declared, 'null']
      elif isinstance(declared, list):
        out['type'] = [*declared, 'null'] if 'null' not in declared else declared
      elif not is_ref(out):
        keep = {k: out.pop(k) for k in ('description', 'title', 'default') if k in out}
        out = {**keep, 'anyOf': [out, {'type': 'null'}]}
    return out

  def finish(self, schema: Any, *, title: str) -> Any:
    """Post-pass over the dumped model: strip OpenAPI keys, nullable objects to `anyOf`,
    `const` to `enum`, sorted `required`, titles for every object (rule 1)."""
    if isinstance(schema, list):
      return [self.finish(item, title=f'{title}Item{index}') for index, item in enumerate(schema)]
    if not isinstance(schema, dict) or is_ref(schema):
      return schema
    if set(schema) == {'ref'}:
      # `truewire.generation.schema.Reference` dumps its `$ref` field under its Python name.
      return {'$ref': schema['ref']}
    out = {key: value for key, value in schema.items() if key not in OPENAPI_ONLY_KEYS}
    if 'const' in out and not out.get('enum'):
      out['enum'] = [out.pop('const')]
    if isinstance(out.get('required'), list):
      required = sorted(set(out['required']))
      if required:
        out['required'] = required
      else:
        del out['required']
    declared = out.get('type')
    is_object = declared == 'object' or (
      isinstance(declared, list) and 'object' in declared
    ) or (declared is None and 'properties' in out)
    if isinstance(declared, list) and 'null' in declared and is_object:
      inner = {k: v for k, v in out.items() if k not in ('description', 'default')}
      inner['type'] = [t for t in declared if t != 'null']
      if len(inner['type']) == 1:
        inner['type'] = inner['type'][0]
      keep = {k: out[k] for k in ('description', 'default') if k in out}
      return {**keep, 'anyOf': [self.finish(inner, title=title), {'type': 'null'}]}
    if is_object and 'title' not in out and (
      'properties' in out or not out.get('additionalProperties')
    ):
      out['title'] = title
    own = out.get('title') or title
    if isinstance(out.get('properties'), dict):
      out['properties'] = {
        name: self.finish(value, title=f'{own}{pascal_case(name)}')
        for name, value in out['properties'].items()
      }
    if isinstance(out.get('additionalProperties'), dict):
      out['additionalProperties'] = self.finish(out['additionalProperties'], title=f'{own}Value')
    if isinstance(out.get('items'), dict):
      out['items'] = self.finish(out['items'], title=f'{own}Item')
    if isinstance(out.get('prefixItems'), list):
      out['prefixItems'] = [
        self.finish(item, title=f'{own}Item{index}') for index, item in enumerate(out['prefixItems'])
      ]
    if isinstance(out.get('anyOf'), list):
      variants = out['anyOf']
      objects = [v for v in variants if isinstance(v, dict) and not is_ref(v) and v.get('type') != 'null']
      out['anyOf'] = [
        self.finish(v, title=own if len(objects) == 1 else f'{own}Option{index}')
        for index, v in enumerate(variants)
      ]
    for key in NUMERIC_BOUND_KEYS:
      value = out.get(key)
      if isinstance(value, float) and value.is_integer():
        out[key] = int(value)
    return order_keys(out)


@dataclass
class OperationContext:
  """One operation of the document, with path-level parameters already merged in."""
  method: str
  path: str
  operation: dict[str, Any]
  parameters: list[dict[str, Any]]
  name: str
  """Snake-case operation name (operationId, or `<method>_<path words>`)."""
  public: bool

  @property
  def label(self) -> str:
    """`GET /pets`, for reports."""
    return f'{self.method.upper()} {self.path}'


def operations(doc: dict[str, Any], components: Components) -> list[OperationContext]:
  """Every operation in `paths`, path-level parameters merged (operation-level wins)."""
  global_security = doc.get('security')
  out: list[OperationContext] = []
  for path, item in (doc.get('paths') or {}).items():
    item = components.resolve_object(item)
    shared = [components.resolve_object(p) for p in item.get('parameters') or []]
    for method in HTTP_METHODS:
      operation = item.get(method)
      if not isinstance(operation, dict):
        continue
      own = [components.resolve_object(p) for p in operation.get('parameters') or []]
      merged = {(p['name'], p['in']): p for p in shared}
      merged.update({(p['name'], p['in']): p for p in own})
      security = operation.get('security', global_security)
      public = not security
      operation_id = operation.get('operationId')
      name = snake_case(operation_id) if operation_id else f'{method}_{path_words(path)}'
      out.append(OperationContext(
        method=method, path=path, operation=operation, parameters=list(merged.values()),
        name=name, public=public,
      ))
  return out


def path_segments(path: str) -> list[tuple[str, str]]:
  """`(directory segment, original segment)` per path segment; `{x}` becomes `by_x`."""
  out: list[tuple[str, str]] = []
  for raw in path.strip('/').split('/'):
    if not raw:
      continue
    match = re.fullmatch(r'\{(.+)\}', raw)
    out.append((f'by_{snake_case(match.group(1))}' if match else snake_case(raw), raw))
  return out


def path_words(path: str) -> str:
  """`pets_by_pet_id` for `/pets/{petId}`: the operationId fallback."""
  return '_'.join(segment for segment, _raw in path_segments(path)) or 'root'


@dataclass
class Placement:
  """Where one operation lands under `spec/endpoints/`."""
  op: OperationContext
  directory: tuple[str, ...]
  nested: bool = False
  """Whether rule 16 forced the leaf one level deeper than the natural layout."""


def place_operations(
  ops: list[OperationContext], *, function_style: FunctionStyle,
) -> tuple[list[Placement], list[str]]:
  """
  Assign each operation a directory, nesting a leaf one deeper (`<name>/<method>`) when
  its directory would also be a grouping, or is claimed by another operation (rule 16).

  Returns the placements and the warnings explaining every nesting.
  """
  placements: list[Placement] = []
  for op in ops:
    if function_style == 'tags':
      tags = op.operation.get('tags') or []
      group = snake_case(tags[0]) if tags else 'default'
      directory: tuple[str, ...] = (group, op.name)
    else:
      directory = tuple(segment for segment, _raw in path_segments(op.path)) or ('root',)
    placements.append(Placement(op=op, directory=directory))
  warnings: list[str] = []
  changed = True
  while changed:
    changed = False
    directories = [p.directory for p in placements]
    for placement in placements:
      if placement.nested:
        continue
      d = placement.directory
      collides = any(
        other != placement and (other.directory == d or other.directory[:len(d)] == d)
        for other in placements
      ) or directories.count(d) > 1
      if collides:
        placement.directory = (*d, placement.op.method)
        placement.nested = True
        warnings.append(
          f'{placement.op.label}: nested as `{"/".join(placement.directory)}` -- '
          f'`{"/".join(d)}` is also a grouping (docs/spec/authoring.md rule 16)'
        )
        changed = True
  return placements, warnings


def json_media(content: Any) -> dict[str, Any] | None:
  """The `application/json` (or `*+json`) media object of a content map, if any."""
  if not isinstance(content, dict):
    return None
  if isinstance(content.get('application/json'), dict):
    return content['application/json']
  for media_type, media in content.items():
    if media_type.endswith('json') and isinstance(media, dict):
      return media
  return None


def success_response(op: OperationContext, components: Components) -> tuple[str, dict[str, Any]] | None:
  """The lowest 2xx response, as `(status, response)`; `2XX` wildcards sort last."""
  candidates: list[tuple[int, str, dict[str, Any]]] = []
  for status, response in (op.operation.get('responses') or {}).items():
    text = str(status)
    if not text.startswith('2'):
      continue
    order = int(text) if text.isdigit() else 299
    candidates.append((order, text, components.resolve_object(response)))
  if not candidates:
    return None
  _order, status, response = min(candidates, key=lambda item: item[0])
  return status, response


def media_examples(media: dict[str, Any] | None, components: Components) -> list[tuple[str, str | None, Any]]:
  """`(id, summary, value)` for every usable example on a media object, `examples` first."""
  if media is None:
    return []
  out: list[tuple[str, str | None, Any]] = []
  examples = media.get('examples')
  if isinstance(examples, dict):
    for name, example in examples.items():
      example = components.resolve_object(example)
      if not isinstance(example, dict) or 'value' not in example:
        continue
      out.append((snake_case(name), example.get('summary'), example['value']))
    return out
  if 'example' in media:
    return [('default', None, media['example'])]
  schema = media.get('schema')
  if isinstance(schema, dict) and 'example' in schema:
    return [('default', None, schema['example'])]
  return out


def parameter_example(parameter: dict[str, Any]) -> tuple[bool, Any]:
  """`(found, value)` for a parameter's own `example`/`examples`/schema `example`."""
  if 'example' in parameter:
    return True, parameter['example']
  examples = parameter.get('examples')
  if isinstance(examples, dict):
    for example in examples.values():
      if isinstance(example, dict) and 'value' in example:
        return True, example['value']
  schema = parameter.get('schema')
  if isinstance(schema, dict) and 'example' in schema:
    return True, schema['example']
  return False, None


@dataclass
class BuiltEndpoint:
  """One endpoint's file contents, ready to write."""
  endpoint: dict[str, Any]
  examples: list[tuple[str, dict[str, Any], dict[str, Any]]]
  """`(id, request file, response file)`."""


def build_endpoint(
  placement: Placement, *, components: Components, normalizer: SchemaNormalizer,
  report: ImportReport,
) -> BuiltEndpoint:
  """Build one `endpoint.json` (plus examples) from an operation."""
  op = placement.op
  operation = op.operation
  notes: list[str] = []
  op_pascal = pascal_case(op.name)
  properties: dict[str, Any] = {}
  required: list[str] = []
  request_example: dict[str, Any] = {}
  ordered = sorted(op.parameters, key=lambda p: 0 if p.get('in') == 'path' else 1)
  for parameter in ordered:
    location = parameter.get('in')
    name = parameter['name']
    if location in ('header', 'cookie'):
      notes.append(f'dropped {location} parameter `{name}`: the spec records only path and query parameters')
      report.warnings.append(f'{op.label}: dropped {location} parameter `{name}`')
      continue
    schema = normalizer.normalize(parameter.get('schema') or {}, title=f'{op_pascal}Request{pascal_case(name)}')
    if is_ref(schema):
      prop: dict[str, Any] = schema
    else:
      prop = dict(schema)
      if parameter.get('description'):
        prop['description'] = parameter['description']
      else:
        report.warnings.append(f'{op.label}: parameter `{name}` has no description (rule 7)')
      if parameter.get('deprecated'):
        prop['deprecated'] = True
      prop = order_keys(prop)
    properties[name] = prop
    if parameter.get('required') or location == 'path':
      required.append(name)
    found, value = parameter_example(parameter)
    if found:
      request_example[name] = value
  body = components.resolve_object(operation.get('requestBody'))
  if isinstance(body, dict):
    media = json_media(body.get('content'))
    if media is None:
      media_types = ', '.join((body.get('content') or {}).keys()) or 'none'
      notes.append(f'dropped request body: no JSON media type (found: {media_types})')
      report.warnings.append(f'{op.label}: request body has no JSON media type; dropped')
    else:
      body_schema = normalizer.normalize(media.get('schema') or {}, title=f'{op_pascal}Body')
      body_examples = media_examples(media, components)
      body_example = body_examples[0][2] if body_examples else None
      flat = (
        not is_ref(body_schema) and body_schema.get('type') == 'object'
        and isinstance(body_schema.get('properties'), dict) and not body_schema.get('anyOf')
      )
      if flat:
        for name, prop in body_schema['properties'].items():
          if name in properties:
            notes.append(f'request body property `{name}` collides with a parameter of the same name; the parameter was kept')
            report.warnings.append(f'{op.label}: body property `{name}` collides with a parameter')
            continue
          properties[name] = prop
        required.extend(n for n in body_schema.get('required') or [] if n not in required and n in properties)
        if isinstance(body_example, dict):
          request_example.update({k: v for k, v in body_example.items() if k in properties})
      else:
        prop = body_schema if is_ref(body_schema) else dict(body_schema)
        if not is_ref(prop) and body.get('description') and not prop.get('description'):
          prop['description'] = body['description']
        properties['body'] = order_keys(prop) if not is_ref(prop) else prop
        if body.get('required'):
          required.append('body')
        if body_example is not None:
          request_example['body'] = body_example
  request_schema: dict[str, Any] = {
    'title': f'{op_pascal}Request', 'type': 'object', 'properties': properties,
  }
  if required:
    request_schema['required'] = sorted(set(required))
  request_schema['additionalProperties'] = False
  for name, prop in properties.items():
    if not is_ref(prop) and not prop.get('description'):
      report.warnings.append(f'{op.label}: request property `{name}` has no description (rule 7)')
  hints = [name for name in properties if fold_name(name) in PAGINATION_NAMES]
  if hints:
    notes.append(
      f'parameters {", ".join(f"`{n}`" for n in hints)} look like pagination; declare a '
      f'`pagination` block (docs/spec/authoring.md rule 8) -- never inferred by the importer'
    )
  dropped = sorted(
    str(status) for status in (operation.get('responses') or {}) if not str(status).startswith('2')
  )
  if dropped:
    notes.append(f'dropped non-2xx responses {", ".join(dropped)}: errors belong to the client core (rule 0)')
  success = success_response(op, components)
  response_examples: list[tuple[str, str | None, Any]] = []
  status_code = 200
  if success is None:
    response_schema: dict[str, Any] = {'type': 'null'}
    notes.append('no 2xx response declared; response typed `null`')
    report.warnings.append(f'{op.label}: no 2xx response declared')
  else:
    status, response = success
    status_code = int(status) if status.isdigit() else 200
    media = json_media(response.get('content'))
    if media is None or media.get('schema') is None:
      response_schema = {'type': 'null'}
      notes.append(f'2xx response `{status}` has no JSON body; response typed `null`')
    else:
      response_schema = normalizer.normalize(media['schema'], title=f'{op_pascal}Response')
      if not is_ref(response_schema) and response.get('description') and not response_schema.get('description'):
        response_schema = order_keys({**response_schema, 'description': response['description']})
    response_examples = media_examples(media, components)
  examples: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
  for example_id, summary, value in response_examples:
    request = dict(request_example)
    if isinstance(value, dict):
      for name in request_schema.get('required') or []:
        if name not in request and name in value and name in placeholders(op.path):
          request[name] = value[name]
    examples.append((
      example_id,
      {'description': summary or 'Imported from OpenAPI', 'request': request},
      {'status': status_code, 'payload': value},
    ))
  if examples:
    notes.append(IMPORTED_EXAMPLES_NOTE)
  description = operation.get('description') or operation.get('summary')
  if not description:
    report.warnings.append(f'{op.label}: operation has no description (rule 7)')
  endpoint: dict[str, Any] = {}
  external = operation.get('externalDocs')
  if isinstance(external, dict) and external.get('url'):
    endpoint['docs'] = external['url']
  if operation.get('deprecated'):
    endpoint['deprecated'] = True
  endpoint['meta'] = {'public': True} if op.public else {}
  if not examples:
    endpoint['unverified'] = {'reason': 'not_captured', 'detail': NOT_CAPTURED_DETAIL}
  endpoint['spec'] = {
    'kind': 'rpc', 'transports': ['http'], 'method': op.method.upper(), 'path': op.path,
    'description': description, 'request': request_schema, 'response': response_schema,
  }
  endpoint['notes'] = notes
  return BuiltEndpoint(endpoint=endpoint, examples=examples)


def placeholders(path: str) -> set[str]:
  """`{name}` parameters of a path template."""
  return set(re.findall(r'\{([^{}]+)\}', path))


def root_router(doc: dict[str, Any], *, core: str, upstream: str | None) -> dict[str, Any]:
  """The root `router.json`: title + summary, upstream from externalDocs or servers."""
  info = doc.get('info') or {}
  description = info.get('title') or 'Imported API'
  extra = info.get('summary') or info.get('description')
  if extra:
    description = f'{description}. {extra.strip()}'
  url = upstream or (doc.get('externalDocs') or {}).get('url')
  if not url:
    servers = doc.get('servers') or []
    url = servers[0].get('url') if servers and isinstance(servers[0], dict) else None
  if not url:
    raise OpenApiImportError(
      'no upstream URL: the document has neither `externalDocs.url` nor `servers[0].url`; '
      'pass `upstream=` (rule 14: never invented)'
    )
  return {'description': description, 'upstream': url, 'core': core}


def group_router(
  directory: tuple[str, ...], *, doc: dict[str, Any], root: dict[str, Any],
  raw_segments: dict[tuple[str, ...], str], group_core: str | None,
) -> dict[str, Any]:
  """A grouping's `router.json`: the tag's description/externalDocs when the grouping is a
  tag, else derived from the path prefix; `core` only when `group_core` is given."""
  tags = {snake_case(tag['name']): tag for tag in doc.get('tags') or [] if isinstance(tag, dict) and tag.get('name')}
  tag = tags.get(directory[0]) if len(directory) == 1 else None
  if tag is not None:
    description = tag.get('description') or f'Operations tagged `{tag["name"]}`.'
    upstream = (tag.get('externalDocs') or {}).get('url') or root['upstream']
  else:
    prefix = raw_segments.get(directory)
    description = f'Operations under `{prefix}`.' if prefix else f'Operations grouped as `{"/".join(directory)}`.'
    upstream = root['upstream']
  out: dict[str, Any] = {'description': description, 'upstream': upstream}
  if group_core is not None and len(directory) == 1:
    out['core'] = group_core
  return out


def write_json(path: Path, value: Any):
  """Write pretty JSON with a trailing newline, creating parents."""
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')


def import_openapi(
  doc: dict[str, Any], *, out: Path, function_style: FunctionStyle = 'tags',
  core: str = 'default', group_core: str | None = None, upstream: str | None = None,
) -> ImportReport:
  """
  Import one OpenAPI document into a `spec/` tree under `out`.

  Args:
    doc: Decoded OpenAPI 3.0/3.1 document (see `load_document`).
    out: Directory that receives `spec/` (a project root).
    function_style: `tags` groups by first tag; `path` nests by path segment.
    core: `core` declared on the root `router.json`.
    group_core: `core` declared on every top-level grouping's `router.json`, when the root
      core is a lifecycle base distinct from the endpoints' own (the `Generator`'s
      root-vs-leaf split); `None` leaves groupings inheriting the root's.
    upstream: Override for the root upstream URL when the document declares none.
  """
  report = ImportReport()
  components = Components(doc)
  components.check_refs()
  ops = operations(doc, components)
  report.operations = len(ops)
  roots: list[Any] = []
  for op in ops:
    roots.extend(p.get('schema') for p in op.parameters if p.get('in') in ('path', 'query'))
    body = components.resolve_object(op.operation.get('requestBody'))
    if isinstance(body, dict):
      roots.append(body.get('content'))
    success = success_response(op, components)
    if success is not None:
      roots.append(success[1].get('content'))
  components.decide_shared([r for r in roots if r is not None])
  normalizer = SchemaNormalizer(components)
  normalizer.prepare()
  placements, nesting = place_operations(ops, function_style=function_style)
  report.warnings.extend(nesting)
  root = root_router(doc, core=core, upstream=upstream)
  endpoints_root = out / 'spec' / 'endpoints'
  endpoints_root.mkdir(parents=True, exist_ok=True)
  write_json(endpoints_root / 'router.json', root)
  raw_segments: dict[tuple[str, ...], str] = {}
  for placement in placements:
    segments = path_segments(placement.op.path)
    for depth in range(1, len(segments) + 1):
      raw_segments.setdefault(tuple(s for s, _r in segments[:depth]), '/' + '/'.join(r for _s, r in segments[:depth]))
  written_dirs: set[tuple[str, ...]] = set()
  for placement in placements:
    if placement.directory in written_dirs:
      report.skipped.append((placement.op.label, f'duplicate directory `{"/".join(placement.directory)}`'))
      continue
    try:
      built = build_endpoint(placement, components=components, normalizer=normalizer, report=report)
    except OpenApiImportError as exc:
      report.skipped.append((placement.op.label, str(exc)))
      continue
    directory = endpoints_root.joinpath(*placement.directory)
    write_json(directory / 'endpoint.json', built.endpoint)
    for example_id, request, response in built.examples:
      write_json(directory / 'examples' / f'{example_id}.request.json', request)
      write_json(directory / 'examples' / f'{example_id}.response.json', response)
      report.examples += 1
    written_dirs.add(placement.directory)
    report.written += 1
    report.endpoints.append((directory / 'endpoint.json').relative_to(out))
  groupings: set[tuple[str, ...]] = set()
  for directory in written_dirs:
    for depth in range(1, len(directory)):
      groupings.add(directory[:depth])
  for grouping in sorted(groupings):
    write_json(
      endpoints_root.joinpath(*grouping, 'router.json'),
      group_router(grouping, doc=doc, root=root, raw_segments=raw_segments, group_core=group_core),
    )
  if components.shared:
    shared = {name: normalizer.shared_schema(name) for name in sorted(components.shared)}
    write_json(out / 'spec' / 'schemas.json', shared)
    report.schemas_shared = sorted(components.shared)
  report.warnings.extend(normalizer.warnings)
  return report


def rewrite_refs(value: Any) -> Any:
  """Bare-id `$ref`s to `#/$defs/<id>`, the way `spec test` validates examples."""
  if isinstance(value, dict):
    return {
      key: (f'#/$defs/{item}' if key == '$ref' and isinstance(item, str) and not item.startswith('#/') else rewrite_refs(item))
      for key, item in value.items()
    }
  if isinstance(value, list):
    return [rewrite_refs(item) for item in value]
  return value


def example_validator(schema: dict[str, Any], shared: dict[str, Any]) -> Draft202012Validator:
  """A validator for one endpoint schema with the shared schemas mounted as `$defs`."""
  root = deepcopy(rewrite_refs(schema))
  root['$schema'] = 'https://json-schema.org/draft/2020-12/schema'
  root['$defs'] = {name: rewrite_refs(value) for name, value in shared.items()}
  return Draft202012Validator(root)


def validate_tree(root: Path) -> ValidationResult:
  """
  Load a written tree through the real loaders and run the checks `spec test` runs.

  Every `endpoint.json` is `Endpoint.model_validate`d via `truewire.spec.repo`, every
  `router.json` via `RouterDoc`, every example pair through `ExampleRequest`/
  `ExampleResponse` plus JSON Schema validation of the response payload (an error, as in
  `spec test`) and of the request value (a warning: `spec test` does not check it).
  Authoring violations are split by `severity`.

  Args:
    root: The directory `import_openapi` wrote `spec/` under.
  """
  result = ValidationResult()
  try:
    records = endpoint_records(root)
  except Exception as exc:
    result.errors.append(f'load: {exc}')
    return result
  result.endpoints = len(records)
  endpoints_root = root / 'spec' / 'endpoints'
  for directory in sorted({p for p in endpoints_root.rglob('*') if p.is_dir()} | {endpoints_root}):
    if directory.name == 'examples':
      continue
    try:
      load_router(directory)
    except Exception as exc:
      result.errors.append(f'{directory.relative_to(root)}/router.json: {exc}')
  shared = load_shared_schemas(root)
  spec_root = root / 'spec'
  for record in records:
    function = record.endpoint.resolved_function(record.path, spec_root)
    for violation in audit(record.endpoint):
      sink = result.errors if severity(violation) == 'error' else result.warnings
      sink.append(format_violation(function, violation))
    for example in http_examples(record.path, record.endpoint):
      result.examples += 1
      response = record.endpoint.response
      if response is None:
        result.errors.append(f'{function} :: {example.example_id}: endpoint declares no response schema')
        continue
      for error in example_validator(response, shared).iter_errors(example.response.payload):
        result.errors.append(f'{function} :: {example.example_id}.response: {error.message} at {list(error.path)}')
      request = record.endpoint.request
      if request is not None and example.request.request is not None:
        for error in example_validator(request, shared).iter_errors(example.request.request):
          result.warnings.append(f'{function} :: {example.example_id}.request: {error.message} at {list(error.path)}')
    examples_dir = record.path.parent / 'examples'
    if examples_dir.is_dir():
      for path in sorted(examples_dir.glob('*.request.json')):
        ExampleRequest.model_validate(json.loads(path.read_text()))
      for path in sorted(examples_dir.glob('*.response.json')):
        ExampleResponse.model_validate(json.loads(path.read_text()))
  for violation in [*check_mixed_leaf_router(root), *check_meta(root), *check_router_core(root)]:
    sink = result.errors if severity(violation) == 'error' else result.warnings
    sink.append(format_violation(violation['location'], violation))
  return result


def format_violation(where: str, violation: Violation) -> str:
  """One-line rendering of a violation, `<rule>` first so lists group by eye."""
  return f'[{violation["rule"]}] {where} {violation["location"]}: {violation["message"]}'
