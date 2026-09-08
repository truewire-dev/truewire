from typing_extensions import Mapping, Sequence, TypeVar, Protocol
from dataclasses import dataclass
from pydantic import TypeAdapter
from . import Reference, Schema, Parameter, Response, RequestBody

T = TypeVar('T')

class ResolutionError(Exception):
  def __init__(self, ref: str, *args: object):
    self.ref = ref
    super().__init__(*args)

  def __str__(self):
    return f'ResolutionError(ref="{self.ref}")'

  __repr__ = __str__

class SchemaCycleError(ResolutionError):
  """A reference cycle no amount of inlining can break.

  Every schema on the cycle renders *inline* -- it has no `properties`, so it becomes an
  expression (`list[...]`, `A | B`) substituted at each use site rather than a class with
  a name. Expanding one therefore expands the next, forever: `Tree = string | Tree[]`
  is `string | list[string | list[...]]` all the way down, and no finite Python type
  expression states it. A cycle that passes through at least one record is a different
  matter and is supported -- the record has a name, so the reference to it renders as a
  forward reference (`docs/spec/authoring.md` rule 17).
  """

  def __init__(self, cycle: 'Sequence[str]'):
    self.cycle = tuple(cycle)
    """The schema ids on the cycle, in the order they were expanded, closing back on
    `cycle[0]`."""
    super().__init__(self.cycle[0] if self.cycle else '')

  def __str__(self):
    return f'SchemaCycleError(cycle="{" -> ".join((*self.cycle, self.cycle[0]))}")'

  __repr__ = __str__

class SchemaResolver(Protocol):
  def __call__(self, schema: Reference|Schema, /) -> Schema | None:
    """Resolve a schema from a reference or string. Returning `None` indicates the schema shall be treated as an external reference."""

  def ref(self, ref: str) -> Schema | None:
    """Resolve a reference from a string."""
    return self(Reference(ref=ref))

@dataclass
class LocalResolver(SchemaResolver):
  """Look one reference up in a flat mapping of schemas.

  Exactly one hop, and no cycle detection: `schemas` maps an id to a `Schema`, never to
  another `Reference` (`TypeGenerator` splits top-level references out before building
  this), so there is no chain here to loop on. A reference cycle in a spec runs through a
  schema's *properties*, which this never reads -- it is created, and caught, where the
  properties are walked, in `truewire.generation.types.InlineSchemas`.
  """
  schemas: Mapping[str, Schema]
  raise_on_unknown: bool = False
  """Whether to raise an error if an unknown reference is encountered (or return `None`)."""

  def __call__(self, schema: Reference|Schema) -> Schema | None:
    s = Reference(ref=schema) if isinstance(schema, str) else schema
    if not isinstance(s, Reference):
      return s
    if s.ref in self.schemas:
      return self.schemas[s.ref]
    if self.raise_on_unknown:
      raise ResolutionError(f'Unknown reference: {s.ref}')
    return None

class Resolver(Protocol):
  def schema(self, ref: Reference|Schema, /) -> Schema | None:
    if not isinstance(ref, Reference):
      return ref

  def parameter(self, ref: Reference|Parameter) -> Parameter:
    if isinstance(ref, Reference):
      raise ResolutionError('EmptyResolver does not support references')
    return ref

  def response(self, ref: Reference|Response) -> Response:
    if isinstance(ref, Reference):
      raise ResolutionError('EmptyResolver does not support references')
    return ref

  def body(self, ref: Reference|RequestBody) -> RequestBody:
    if isinstance(ref, Reference):
      raise ResolutionError('EmptyResolver does not support references')
    return ref

class EmptyResolver(Resolver):
  ...

@dataclass
class OpenApiResolver(Resolver):
  """Resolves references from an OpenAPI schema."""
  openapi: Mapping

  Error = ResolutionError

  def __call__(self, ref: Reference|T, type: type[T]) -> T:
    visited = set[str]()
    adapter = TypeAdapter(Reference|type)
    
    def rec(ref: Reference|T) -> T:
      if isinstance(ref, Reference):
        if ref.ref in visited:
          raise ResolutionError(f'Circular reference detected: {ref.ref}')
        visited.add(ref.ref)
        if (s := self.openapi.get(ref.ref)) is not None:
          val = adapter.validate_python(s)
        else:
          path = ref.ref.lstrip('#/').split('/')
          out = self.openapi
          for p in path:
            if p in out:
              out = out[p]
            else:
              raise ResolutionError(ref.ref)
          val = adapter.validate_python(out)
        return rec(val)
      else:
        return ref
    
    s = Reference(ref=ref) if isinstance(ref, str) else ref
    return rec(s)

  def schema(self, ref: Reference|Schema, /) -> Schema | None:
    return self(ref, Schema)

  def parameter(self, ref: Reference|Parameter):
    return self(ref, Parameter)

  def response(self, ref: Reference|Response):
    return self(ref, Response)

  def body(self, ref: Reference|RequestBody):
    return self(ref, RequestBody)