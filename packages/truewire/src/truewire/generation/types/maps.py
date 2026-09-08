from typing_extensions import TypeVar, Generic, Iterable, Callable, Mapping
from dataclasses import dataclass, field
from abc import ABC, abstractmethod

from truewire.generation.schema import (
  Schema, Reference, SchemaResolver, SchemaCycleError,
)

A = TypeVar('A')
B = TypeVar('B')
S = TypeVar('S', bound=Reference|Schema)

def unzip(xs: Iterable[tuple[A, B]]) -> tuple[list[A], list[B]]:
  out_a: list[A] = []
  out_b: list[B] = []
  for a, b in xs:
    out_a.append(a)
    out_b.append(b)
  return out_a, out_b

class MapReduce(ABC, Generic[A]):
  """Map-reduce operation for schemas.
  
  - `map`: `(x, path) -> (x', a)`
  - `reduce`: `[a] -> a`
  - `zero`: `() -> a`

  Algorithm overview:

  ```text
  for each i:
    x'[i], A[i] <- map(x[i])
  a <- reduce(A, zero())
  ```
  """
  @abstractmethod
  def map(self, schema: S, path: tuple[str, ...]) -> tuple[S, A]:
    ...

  @abstractmethod
  def reduce(self, xs: list[A]) -> A:
    ...

  @abstractmethod
  def zero(self) -> A:
    ...

  def __call__(
    self, schema: S, path: tuple[str, ...] = ()
  ) -> tuple[S, A]:
    if isinstance(schema, Reference):
      return self.map(schema, path)

    allOf = self._array(schema.allOf, path + ('allOf',))
    anyOf = self._array(schema.anyOf, path + ('anyOf',))
    not_ = self._optional(schema.not_, path + ('not',))
    items = self._optional(schema.items, path + ('item',))
    prefixItems = self._array(schema.prefixItems, path + ('prefix',))
    properties = self._record(schema.properties, path)
    additionalProperties = self._optional_or_bool(schema.additionalProperties, path + ('additional',))
    
    mid = schema.model_copy()
    mid.properties = properties[0]
    mid.allOf = allOf[0]
    mid.anyOf = anyOf[0]
    mid.not_ = not_[0]
    mid.items = items[0]
    mid.prefixItems = prefixItems[0]
    mid.additionalProperties = additionalProperties[0]

    out, val = self.map(mid, path)

    out_val = self.reduce([
      properties[1],
      allOf[1],
      anyOf[1],
      not_[1],
      items[1],
      prefixItems[1],
      additionalProperties[1],
      val,
    ])

    return out, out_val

  def _array(self, xs: list[Reference|Schema], path: tuple[str, ...]) -> tuple[list[Reference|Schema], A]:
    out, vals = unzip([self(s, path + (str(i),)) for i, s in enumerate(xs)])
    return out, self.reduce(vals)

  def _optional(self, x: Reference|Schema | None, path: tuple[str, ...]) -> tuple[Reference|Schema | None, A]:
    if x is None:
      return None, self.zero()
    else:
      return self(x, path)
  
  def _optional_or_bool(self, x: Reference|Schema | bool | None, path: tuple[str, ...]) -> tuple[Reference|Schema | bool | None, A]:
    if x is None or isinstance(x, bool):
      return x, self.zero()
    else:
      return self(x, path)

  def _record(self, properties: dict[str, Reference|Schema] | None, path: tuple[str, ...]) -> tuple[dict[str, Reference|Schema]|None, A]:
    if properties is None:
      return None, self.zero()
    else:
      schemas: dict[str, Reference|Schema] = {}
      vals: list[A] = []
      for k, s in properties.items():
        s2, defs2 = self(s, path + (k,))
        schemas[k] = s2
        vals.append(defs2)
      return schemas, self.reduce(vals)

@dataclass
class PathMapImpl(MapReduce[None]):
  fn: Callable[[Reference|Schema, tuple[str, ...]], Reference|Schema]

  def zero(self) -> None:
    return None

  def reduce(self, xs: list[None]) -> None:
    return None

  def map(self, schema: Reference|Schema, path: tuple[str, ...]) -> tuple[Reference|Schema, None]:
    return self.fn(schema, path), None

def path_map(
  schema: S,
  fn: Callable[[Reference|Schema, tuple[str, ...]], Reference|Schema],
  *, path: tuple[str, ...] = ()
) -> S:
  mapper = PathMapImpl(fn)
  out, _ = mapper(schema, path)
  return out # type: ignore
  

class PathMap(ABC):
  @abstractmethod
  def map(self, schema: Reference|Schema, path: tuple[str, ...]) -> Reference|Schema:
    ...

  def __call__(self, schema: S, path: tuple[str, ...] = ()) -> S:
    return path_map(schema, self.map, path=path)

class Map(ABC):
  @abstractmethod
  def map(self, schema: Reference|Schema) -> Reference|Schema:
    ...

  def __call__(self, schema: S) -> S:
    return path_map(schema, lambda s, p: self.map(s))

class MapSchema(Map):
  @abstractmethod
  def map_schema(self, schema: Schema) -> Schema:
    ...

  def map(self, schema: Reference|Schema) -> Reference|Schema:
    if isinstance(schema, Reference):
      return schema
    else:
      return self.map_schema(schema)

@dataclass
class Translate(Map):
  translate: Callable[[str], str]

  @classmethod
  def of(cls, translations: Mapping[str, str]):
    def translate(ref: str) -> str:
      return translations.get(ref, ref)
    return cls(translate)

  def map(self, schema: Reference|Schema) -> Reference|Schema:
    if isinstance(schema, Reference):
      schema = schema.model_copy()
      schema.ref = self.translate(schema.ref)
    return schema

  def all(self, schemas: Mapping[str, S]) -> Mapping[str, S]:
    return {self.translate(k): self(s) for k, s in schemas.items()}

class RemoveOneOf(MapSchema):
  """Convert oneOf's to anyOf's"""
  def map_schema(self, schema: Schema) -> Schema:
    if schema.oneOf:
      out = schema.model_copy()
      out.anyOf = out.oneOf
      out.oneOf = []
      return out
    else:
      return schema

@dataclass
class FlattenAllOf(MapSchema):
  """Flatten allOf.{properties, required} -> this.{properties, required}"""
  resolve: SchemaResolver

  def map_schema(self, schema: Schema) -> Schema:
    if schema.allOf:
      out = schema.model_copy()
      out.allOf = []
      properties = schema.properties or {}
      required = set(schema.required or [])
      for sub in schema.allOf:
        if (s := self.resolve(sub)) is None:
          raise ValueError('Found external reference in allOf')
        properties.update(s.properties or {})
        required.update(s.required or [])
      out.properties = properties
      out.required = list(required)
      out.type = 'object'
      return out
    else:
      return schema

@dataclass
class MergeAnyOf(MapSchema):
  """Move this.{properties, required} -> anyOf.each.{properties, required}"""
  resolve: SchemaResolver

  def map_schema(self, schema: Schema) -> Schema:
    if schema.anyOf and schema.properties:
      out = schema.model_copy()
      out.properties = None
      out.required = None
      out.anyOf = []
      for sub in schema.anyOf:
        if (s := self.resolve(sub)) is None:
          raise ValueError('Found external reference in anyOf')
        s = s.model_copy()
        if s.properties is not None:
          s.properties.update(schema.properties)
          s.required = list(set().union(schema.required or [], s.required or []))
        out.anyOf.append(s)
      return out
    else:
      return schema

@dataclass
class InlineSchemas(Map):
  """Inlines non-record schemas"""
  resolve: SchemaResolver
  nested: bool = True
  expanding: tuple[str, ...] = field(default=(), init=False, repr=False)
  """Ids currently being expanded, outermost first -- the chain `map` is partway down.

  A stack rather than a plain `visited` set: an id is on it only while its own expansion
  is unfinished, so the same schema inlined twice side by side is not mistaken for a
  cycle. It is the only cycle guard in the pipeline that can see a real one, because this
  is the only walk that follows a reference into the schema's *properties* and back out
  through another reference (`LocalResolver` resolves one hop and reads no properties at
  all).
  """

  def map(self, schema: Reference|Schema) -> Reference|Schema:
    if (s := self.resolve(schema)) is None:
      return schema
    if s.properties is not None or s.type == 'object' or s.allOf:
      # A record is never inlined, so its own fields never need resolving here. Deciding
      # this before recursing matters for a self-referential record -- a
      # `BasicOrder.children: BasicOrder[]` -- where resolving-then-recursing first would
      # walk back into this same reference forever; this reads only the schema `resolve`
      # already returned, no recursion, so a self-referential record cannot loop. It is
      # also what makes a cycle through a record legal: the chain below stops here, and
      # the reference survives into the type tree, where a backend renders it as a
      # forward reference.
      return schema
    if self.nested and isinstance(schema, Reference):
      if schema.ref in self.expanding:
        raise SchemaCycleError(
          self.expanding[self.expanding.index(schema.ref):]
        )
      self.expanding = (*self.expanding, schema.ref)
      try:
        s = self(s)
      finally:
        self.expanding = self.expanding[:-1]
    return s
    