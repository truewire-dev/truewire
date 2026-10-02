"""A dependency-free reader for the `.proto` tree under a project's `spec/proto/`.

gRPC endpoints name their wire types by fully-qualified proto name (`GrpcEndpointSpec`); the
`.proto` files are the only source of truth for what those types hold. Two things need to
read them without a protobuf compiler installed:

- The plan (`truewire plan`), which resolves an endpoint's request fields and the fields a
  declared pagination walks (`pagination.key`, `pagination.next_key`, `total`) so every
  backend renders the same decisions (ADR 0017).
- The stub build (`truewire protos`), which hands the tree to `buf` after removing what the
  tree cannot resolve: imports of files that are not part of it (Cosmos's `gogoproto`,
  `cosmos_proto`, `amino`, `google/api`) and the custom options (`[(gogoproto.nullable) =
  false]`, `option (google.api.http) = {...}`) that use them. Custom options never change
  the binary encoding, so the stripped tree describes the same wire.

The reader covers the proto2/proto3 grammar the Cosmos and dYdX trees use: packages,
imports, file/message/field/enum/service options, nested messages and enums, `oneof`,
`map<K, V>`, `repeated`/`optional`/`required` labels, `reserved`/`extensions`, `extend`
blocks (skipped) and unary or streaming `rpc`s.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

from typing_extensions import Iterable, Literal

SCALARS = frozenset((
  'double', 'float', 'int32', 'int64', 'uint32', 'uint64', 'sint32', 'sint64', 'fixed32',
  'fixed64', 'sfixed32', 'sfixed64', 'bool', 'string', 'bytes',
))

WELL_KNOWN_PREFIX = 'google/protobuf/'
"""Imports every protobuf toolchain ships itself: kept even when the tree lacks them."""

_TOKEN = re.compile(
  r'(?P<space>\s+)'
  r'|(?P<line>//[^\n]*)'
  r'|(?P<block>/\*.*?\*/)'
  r'|(?P<string>"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\')'
  r'|(?P<number>[-+]?(?:0[xX][0-9a-fA-F]+|\d+\.?\d*(?:[eE][-+]?\d+)?|\.\d+(?:[eE][-+]?\d+)?))'
  r'|(?P<ident>\.?[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)'
  r'|(?P<symbol>[{}\[\]()<>;=,:/.\-+])',
  re.DOTALL,
)


class ProtoError(ValueError):
  """A `.proto` file the reader cannot parse, or a name it cannot resolve."""


@dataclass(frozen=True)
class Token:
  kind: str
  text: str
  start: int
  end: int


def tokenize(text: str, *, file: str = '<proto>') -> list[Token]:
  out: list[Token] = []
  position = 0
  while position < len(text):
    match = _TOKEN.match(text, position)
    if match is None:
      line = text.count('\n', 0, position) + 1
      raise ProtoError(f'{file}:{line}: unexpected character {text[position]!r}')
    kind = match.lastgroup or ''
    if kind not in ('space', 'line', 'block'):
      out.append(Token(kind, match.group(), match.start(), match.end()))
    position = match.end()
  return out


@dataclass
class ProtoField:
  name: str
  number: int
  type: str
  """A scalar name, or the type name as written (resolved by `ProtoTree.resolve`)."""
  label: Literal['repeated', 'optional', 'required'] | None = None
  map_key: str | None = None
  """The key scalar, for `map<K, V>` (then `type` is `V` and the field is repeated)."""
  oneof: str | None = None
  json_name: str | None = None
  """An explicit `json_name` option."""
  span: tuple[int, int] = (0, 0)
  """The declaration's character span in its file."""


@dataclass
class ProtoMessage:
  full_name: str
  file: str
  fields: list[ProtoField] = field(default_factory=list)
  oneofs: dict[str, tuple[int, int]] = field(default_factory=dict)
  """Each `oneof` group's character span, by name."""

  def field(self, name: str) -> ProtoField | None:
    return next((f for f in self.fields if f.name == name), None)


@dataclass
class ProtoEnum:
  full_name: str
  file: str
  values: dict[str, int] = field(default_factory=dict)


@dataclass
class ProtoMethod:
  name: str
  input: str
  output: str
  client_streaming: bool = False
  server_streaming: bool = False
  span: tuple[int, int] = (0, 0)


@dataclass
class ProtoService:
  full_name: str
  file: str
  methods: dict[str, ProtoMethod] = field(default_factory=dict)


@dataclass
class ProtoFile:
  path: str
  """Relative to the tree root, POSIX separators (`cosmos/bank/v1beta1/query.proto`)."""
  package: str = ''
  imports: list[str] = field(default_factory=list)
  messages: list[ProtoMessage] = field(default_factory=list)
  enums: list[ProtoEnum] = field(default_factory=list)
  services: list[ProtoService] = field(default_factory=list)
  edits: list[tuple[int, int, str]] = field(default_factory=list)
  """(start, end, replacement) spans `strip` applies: every custom option, and every import
  the tree does not hold."""
  text: str = ''


class _Parser:
  def __init__(self, text: str, path: str):
    self.file = ProtoFile(path=path, text=text)
    self.tokens = tokenize(text, file=path)
    self.index = 0
    self.text = text

  # -- cursor -------------------------------------------------------------------------

  def peek(self, offset: int = 0) -> Token | None:
    i = self.index + offset
    return self.tokens[i] if i < len(self.tokens) else None

  def next(self) -> Token:
    token = self.peek()
    if token is None:
      raise self.error('unexpected end of file')
    self.index += 1
    return token

  def error(self, message: str) -> ProtoError:
    token = self.peek()
    line = self.text.count('\n', 0, token.start) + 1 if token is not None else self.text.count('\n') + 1
    return ProtoError(f'{self.file.path}:{line}: {message}')

  def expect(self, text: str) -> Token:
    token = self.next()
    if token.text != text:
      self.index -= 1
      raise self.error(f'expected {text!r}, found {token.text!r}')
    return token

  def accept(self, text: str) -> bool:
    token = self.peek()
    if token is not None and token.text == text:
      self.index += 1
      return True
    return False

  def ident(self) -> str:
    token = self.next()
    if token.kind != 'ident':
      self.index -= 1
      raise self.error(f'expected a name, found {token.text!r}')
    return token.text

  def skip_block(self):
    """Skip a balanced `{ ... }` whose `{` is the next token."""
    self.expect('{')
    depth = 1
    while depth:
      token = self.next()
      if token.text == '{':
        depth += 1
      elif token.text == '}':
        depth -= 1

  def skip_statement(self):
    """Skip to the `;` ending this statement, over any nested `{}`/`[]`."""
    depth = 0
    while True:
      token = self.next()
      if token.text in '{[(':
        depth += 1
      elif token.text in '}])':
        depth -= 1
      elif token.text == ';' and depth == 0:
        return

  # -- options ------------------------------------------------------------------------

  def option_name(self) -> tuple[str, bool]:
    """An option's name and whether it is a custom (extension) option."""
    parts: list[str] = []
    custom = False
    while True:
      if self.accept('('):
        parts.append(f'({self.ident()})')
        self.expect(')')
        custom = custom or not parts[1:]
      else:
        parts.append(self.ident())
      token = self.peek()
      if token is not None and token.kind == 'ident' and token.text.startswith('.'):
        parts.append(self.next().text)
        break
      if not self.accept('.'):
        break
    return ''.join(parts), custom

  def option_value(self) -> str:
    token = self.peek()
    if token is not None and token.text == '{':
      start = token.start
      self.skip_block()
      return self.text[start:self.tokens[self.index - 1].end]
    token = self.next()
    if token.kind == 'string':
      value = token.text
      while (following := self.peek()) is not None and following.kind == 'string':
        value += self.next().text
      return value
    return token.text

  def option_statement(self, start: Token):
    """`option name = value;`, `option` already consumed as `start`."""
    _, custom = self.option_name()
    self.expect('=')
    self.option_value()
    end = self.expect(';')
    if custom:
      self.file.edits.append((start.start, end.end, ''))

  def field_options(self) -> dict[str, str]:
    """`[ a = 1, (b) = c ]` when present: the built-in options by name, custom ones stripped."""
    opening = self.peek()
    if opening is None or opening.text != '[':
      return {}
    self.next()
    kept: list[str] = []
    builtin: dict[str, str] = {}
    stripped = False
    while True:
      entry_start = self.peek()
      name, custom = self.option_name()
      self.expect('=')
      value = self.option_value()
      assert entry_start is not None
      if custom:
        stripped = True
      else:
        builtin[name] = value
        kept.append(self.text[entry_start.start:self.tokens[self.index - 1].end])
      if not self.accept(','):
        break
    closing = self.expect(']')
    if stripped:
      replacement = f'[{", ".join(kept)}]' if kept else ''
      self.file.edits.append((opening.start, closing.end, replacement))
    return builtin

  # -- top level ----------------------------------------------------------------------

  def parse(self) -> ProtoFile:
    while (token := self.peek()) is not None:
      text = token.text
      if text == ';':
        self.next()
      elif text in ('syntax', 'edition'):
        self.skip_statement()
      elif text == 'package':
        self.next()
        self.file.package = self.ident()
        self.expect(';')
      elif text == 'import':
        self.next()
        if (modifier := self.peek()) is not None and modifier.text in ('public', 'weak'):
          self.next()
        path = self.next()
        if path.kind != 'string':
          raise self.error('expected an import path')
        end = self.expect(';')
        self.file.imports.append(path.text[1:-1])
        self.import_spans.append((token.start, end.end, path.text[1:-1]))
      elif text == 'option':
        self.option_statement(self.next())
      elif text == 'message':
        self.next()
        self.message(self.file.package)
      elif text == 'enum':
        self.next()
        self.enum(self.file.package)
      elif text == 'service':
        self.next()
        self.service()
      elif text == 'extend':
        self.next()
        self.ident()
        self.skip_block()
      else:
        raise self.error(f'unexpected {text!r}')
    return self.file

  import_spans: list[tuple[int, int, str]]

  def qualified(self, scope: str, name: str) -> str:
    return f'{scope}.{name}' if scope else name

  def message(self, scope: str):
    name = self.ident()
    full = self.qualified(scope, name)
    message = ProtoMessage(full_name=full, file=self.file.path)
    self.file.messages.append(message)
    self.expect('{')
    self.message_body(message, full, oneof=None)

  def message_body(self, message: ProtoMessage, scope: str, *, oneof: str | None):
    while not self.accept('}'):
      token = self.peek()
      if token is None:
        raise self.error('unterminated message')
      text = token.text
      if text == ';':
        self.next()
      elif text == 'option':
        self.option_statement(self.next())
      elif text == 'message' and oneof is None and self._declares():
        self.next()
        self.message(scope)
      elif text == 'enum' and self._declares():
        self.next()
        self.enum(scope)
      elif text == 'oneof' and self._declares():
        self.next()
        group = self.ident()
        self.expect('{')
        self.message_body(message, scope, oneof=group)
        message.oneofs[group] = (token.start, self.tokens[self.index - 1].end)
      elif text in ('reserved', 'extensions') and not self._is_field():
        self.next()
        self.skip_statement()
      elif text == 'extend' and self._declares():
        self.next()
        self.ident()
        self.skip_block()
      else:
        self.field(message, oneof)

  def _declares(self) -> bool:
    """Whether the keyword at the cursor opens a declaration (`message Foo {`), rather than
    naming a field type (`message foo = 1;` is a field of a type called `message`)."""
    second = self.peek(1)
    third = self.peek(2)
    return second is not None and second.kind == 'ident' and third is not None and third.text == '{'

  def _is_field(self) -> bool:
    second = self.peek(1)
    third = self.peek(2)
    return second is not None and second.kind == 'ident' and third is not None and third.text == '='

  def field(self, message: ProtoMessage, oneof: str | None):
    label = None
    token = self.peek()
    assert token is not None
    begin = token.start
    if token is not None and token.text in ('repeated', 'optional', 'required') and not self._is_field():
      label = self.next().text
    map_key = None
    if (token := self.peek()) is not None and token.text == 'map' and (after := self.peek(1)) is not None and after.text == '<':
      self.next()
      self.expect('<')
      map_key = self.ident()
      self.expect(',')
      type_name = self.ident()
      self.expect('>')
      label = 'repeated'
    else:
      type_name = self.ident()
    name = self.ident()
    self.expect('=')
    number = self.next()
    if number.kind != 'number':
      raise self.error(f'expected a field number for {name!r}')
    options = self.field_options()
    end = self.expect(';')
    json_name = options.get('json_name')
    message.fields.append(ProtoField(
      name=name, number=int(number.text, 0), type=type_name, label=label, map_key=map_key,  # type: ignore[arg-type]
      oneof=oneof, json_name=json_name[1:-1] if json_name else None, span=(begin, end.end),
    ))

  def enum(self, scope: str):
    name = self.ident()
    enum = ProtoEnum(full_name=self.qualified(scope, name), file=self.file.path)
    self.file.enums.append(enum)
    self.expect('{')
    while not self.accept('}'):
      token = self.next()
      if token.text == ';':
        continue
      if token.text == 'option':
        self.option_statement(token)
      elif token.text == 'reserved':
        self.skip_statement()
      else:
        self.expect('=')
        value = self.next()
        self.field_options()
        self.expect(';')
        enum.values[token.text] = int(value.text, 0)

  def service(self):
    name = self.ident()
    service = ProtoService(full_name=self.qualified(self.file.package, name), file=self.file.path)
    self.file.services.append(service)
    self.expect('{')
    while not self.accept('}'):
      token = self.next()
      if token.text == ';':
        continue
      if token.text == 'option':
        self.option_statement(token)
        continue
      if token.text != 'rpc':
        raise self.error(f'unexpected {token.text!r} in service {name}')
      begin = token.start
      rpc = self.ident()
      self.expect('(')
      client_streaming = self.accept('stream') if self._stream_keyword() else False
      input_type = self.ident()
      self.expect(')')
      self.expect('returns')
      self.expect('(')
      server_streaming = self.accept('stream') if self._stream_keyword() else False
      output_type = self.ident()
      self.expect(')')
      if (opening := self.peek()) is not None and opening.text == '{':
        self.next()
        while not self.accept('}'):
          inner = self.next()
          if inner.text == 'option':
            self.option_statement(inner)
          elif inner.text != ';':
            raise self.error(f'unexpected {inner.text!r} in rpc {rpc}')
      else:
        self.expect(';')
      end = self.tokens[self.index - 1].end
      service.methods[rpc] = ProtoMethod(
        rpc, input_type, output_type, client_streaming, server_streaming, span=(begin, end),
      )

  def _stream_keyword(self) -> bool:
    token = self.peek()
    following = self.peek(1)
    return token is not None and token.text == 'stream' and following is not None and following.kind == 'ident'


def parse_file(text: str, path: str) -> tuple[ProtoFile, list[tuple[int, int, str]]]:
  """One file's declarations, and the spans of its `import` statements (for `strip`)."""
  parser = _Parser(text, path)
  parser.import_spans = []
  return parser.parse(), parser.import_spans


def json_name(name: str) -> str:
  """protoc's default `json_name`: underscores dropped, the letter after each capitalised."""
  out: list[str] = []
  upper = False
  for char in name:
    if char == '_':
      upper = True
    elif upper:
      out.append(char.upper())
      upper = False
    else:
      out.append(char)
  return ''.join(out)


class ProtoTree:
  """Every `.proto` file under one root, with a symbol table over their declarations."""

  def __init__(self, root: Path):
    self.root = root
    self.files: dict[str, ProtoFile] = {}
    self._imports: dict[str, list[tuple[int, int, str]]] = {}
    self.messages: dict[str, ProtoMessage] = {}
    self.enums: dict[str, ProtoEnum] = {}
    self.services: dict[str, ProtoService] = {}
    if root.is_dir():
      for path in sorted(root.rglob('*.proto')):
        relative = path.relative_to(root).as_posix()
        parsed, imports = parse_file(path.read_text(encoding='utf-8'), relative)
        self.files[relative] = parsed
        self._imports[relative] = imports
        for message in parsed.messages:
          self.messages[message.full_name] = message
        for enum in parsed.enums:
          self.enums[enum.full_name] = enum
        for service in parsed.services:
          self.services[service.full_name] = service

  def resolve(self, name: str, scope: str) -> tuple[Literal['scalar', 'message', 'enum', 'external'], str]:
    """A type name as written inside `scope` (a message's or package's full name), resolved
    by protobuf's scoping rule: innermost scope outward, a leading `.` being absolute.

    `external` is a name the tree does not declare (a well-known type such as
    `google.protobuf.Timestamp`, whose file every toolchain ships).
    """
    if name in SCALARS:
      return 'scalar', name
    if name.startswith('.'):
      candidates = [name[1:]]
    else:
      parts = scope.split('.') if scope else []
      candidates = ['.'.join([*parts[:i], name]) for i in range(len(parts), -1, -1)]
    for candidate in candidates:
      if candidate in self.messages:
        return 'message', candidate
      if candidate in self.enums:
        return 'enum', candidate
    return 'external', name.lstrip('.')

  def type_file(self, full_name: str) -> str | None:
    declared = self.messages.get(full_name) or self.enums.get(full_name)
    return declared.file if declared is not None else None

  def _external(self, name: str, scope: str) -> str | None:
    """`name` when it resolves to nothing the tree or a well-known file declares."""
    kind, resolved = self.resolve(name, scope)
    if kind != 'external' or resolved.startswith('google.protobuf.'):
      return None
    return resolved

  def unresolved(self) -> list[tuple[str, str, str]]:
    """Every declaration naming a type the tree does not hold, as (file, declaration,
    missing type): a message field (`pkg.Message.field`) or an rpc (`pkg.Service/Rpc`).
    `stripped()` drops each one, so a spec whose tree was cut short still builds."""
    out: list[tuple[str, str, str]] = []
    for message in self.messages.values():
      for f in message.fields:
        missing = self._external(f.type, message.full_name)
        if missing is not None:
          out.append((message.file, f'{message.full_name}.{f.name}', missing))
    for service in self.services.values():
      scope = service.full_name.rsplit('.', 1)[0] if '.' in service.full_name else ''
      for method in service.methods.values():
        for name in (method.input, method.output):
          missing = self._external(name, scope)
          if missing is not None:
            out.append((service.file, f'{service.full_name}/{method.name}', missing))
            break
    return sorted(out)

  def closure(self, full_name: str) -> set[str]:
    """Every message reachable from `full_name` through its fields, itself included."""
    seen: set[str] = set()
    pending = [full_name]
    while pending:
      name = pending.pop()
      if name in seen or name not in self.messages:
        continue
      seen.add(name)
      for f in self.messages[name].fields:
        kind, resolved = self.resolve(f.type, name)
        if kind == 'message':
          pending.append(resolved)
    return seen

  def stripped(self) -> dict[str, str]:
    """Every file's text with custom options, unresolvable imports and the declarations
    `unresolved()` lists removed, by path."""
    dropped: dict[str, list[tuple[int, int]]] = {}
    for message in self.messages.values():
      gone = [f for f in message.fields if self._external(f.type, message.full_name) is not None]
      for group, span in message.oneofs.items():
        members = [f for f in message.fields if f.oneof == group]
        if members and all(f in gone for f in members):
          # A `oneof` left with no member does not compile: drop the group itself.
          gone = [f for f in gone if f.oneof != group]
          dropped.setdefault(message.file, []).append(span)
      for f in gone:
        dropped.setdefault(message.file, []).append(f.span)
    for service in self.services.values():
      scope = service.full_name.rsplit('.', 1)[0] if '.' in service.full_name else ''
      for method in service.methods.values():
        if any(self._external(name, scope) is not None for name in (method.input, method.output)):
          dropped.setdefault(service.file, []).append(method.span)
    out: dict[str, str] = {}
    for path, parsed in self.files.items():
      spans = dropped.get(path, [])
      edits = [
        edit for edit in parsed.edits
        if not any(start <= edit[0] < end for start, end in spans)
      ]
      edits.extend((start, end, '') for start, end in spans)
      for start, end, target in self._imports[path]:
        if target not in self.files and not target.startswith(WELL_KNOWN_PREFIX):
          edits.append((start, end, ''))
      text = parsed.text
      for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
      out[path] = text
    return out

  def write_stripped(self, target: Path, paths: Iterable[str] | None = None):
    """Write `stripped()` under `target`, mirroring the tree."""
    for path, text in self.stripped().items():
      if paths is not None and path not in paths:
        continue
      destination = target / path
      destination.parent.mkdir(parents=True, exist_ok=True)
      destination.write_text(text, encoding='utf-8')


__all__ = [
  'ProtoEnum', 'ProtoError', 'ProtoField', 'ProtoFile', 'ProtoMessage', 'ProtoMethod',
  'ProtoService', 'ProtoTree', 'SCALARS', 'json_name', 'parse_file', 'tokenize',
]
