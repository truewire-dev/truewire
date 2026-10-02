"""Build a language's protobuf stubs from the project's `spec/proto/` tree (ADR 0017).

`truewire protos <language>` is the one step between the spec and the stubs a generated
gRPC endpoint imports. It reads the tree (`truewire.grpc.proto`), refuses a tree that
cannot describe an endpoint's messages, writes the stripped tree to a scratch directory
and runs `buf generate` over it with the language's standard plugin:

- `typescript`: `protoc-gen-es` (protobuf-es v2, `target=ts`, `.js` import extensions),
  into `<[typescript].src>/<package>/protos/`, one `<name>_pb.ts` per `.proto`.
- `go`: `protoc-gen-go`, in buf's managed mode with `go_package_prefix` set to the
  generated package's import path plus `/protos`, into `<[go] package dir>/protos/`, one
  Go package per proto directory.
- `rust`: `protoc-gen-prost` (prost 0.14), into `<[rust] package dir>/protos/`: one
  `<proto package>.rs` per package, `descriptors.binpb` (the tree's
  `FileDescriptorSet`, from `buf build`, which the fake gRPC server of `truewire-testing`
  reads recordings with), and `mod.rs` nesting a module per package segment around each
  `include!`, the way `prost-build` itself names them, and exporting the set as
  `FILE_DESCRIPTOR_SET`. The crate depends on `prost` (and `prost-types` when a message
  uses a well-known type); no `build.rs` and no `protoc` are needed to compile it.

A build owns the stub files under that directory (`*_pb.ts`, `*.pb.go`; for Rust every
`.rs` file and `descriptors.binpb`) and nothing else, so a generated file beside them (the
Go backend's `protos/protos.go`, ADR 0016) survives it; `check` compares the stub files only.
Tools are found on the environment (`TRUEWIRE_BUF`, `TRUEWIRE_PROTOC_GEN_ES`,
`TRUEWIRE_PROTOC_GEN_GO`, `TRUEWIRE_PROTOC_GEN_PROST`), then in `node_modules/.bin` above
the project (buf and protoc-gen-es ship as npm packages), then on `PATH` (and
`$GOPATH/bin` for protoc-gen-go, `~/.cargo/bin` for protoc-gen-prost).
"""
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from truewire.project import Project

from .proto import ProtoTree

LANGUAGES = ('typescript', 'go', 'rust')
PROTOS_DIR = 'protos'
RUST_DESCRIPTORS = 'descriptors.binpb'
RUST_MOD = 'mod.rs'


class StubError(RuntimeError):
  """The stubs cannot be built: a missing tool, a broken tree, or a failed `buf generate`."""


@dataclass(frozen=True)
class StubTarget:
  language: str
  output: Path
  """The directory the stubs are written to."""

  @property
  def suffix(self) -> str:
    """The file name ending every stub this language's plugin writes."""
    return {'typescript': '_pb.ts', 'go': '.pb.go', 'rust': '.rs'}[self.language]

  def owned(self) -> set[str]:
    """The stub files present under `output`, relative to it."""
    if not self.output.is_dir():
      return set()
    found = {
      path.relative_to(self.output).as_posix()
      for path in self.output.rglob(f'*{self.suffix}') if path.is_file()
    }
    if self.language == 'rust' and (self.output / RUST_DESCRIPTORS).is_file():
      found.add(RUST_DESCRIPTORS)
    return found
  go_prefix: str | None = None
  """The Go import path `protos/` is reached by, for `go`."""


def grpc_endpoints(project: Project) -> list[tuple[str, object]]:
  """(function, spec) of every gRPC endpoint that is not declared absent."""
  from truewire.spec import GrpcEndpointSpec, endpoint_records

  out = []
  for record in endpoint_records(project):
    endpoint = record.endpoint
    if isinstance(endpoint.spec, GrpcEndpointSpec) and not (endpoint.surface is not None and endpoint.surface.kind == 'absent'):
      out.append((endpoint.resolved_function(record.path, project.spec_dir), endpoint.spec))
  return sorted(out, key=lambda item: item[0])


def tree_problems(project: Project, tree: ProtoTree) -> list[str]:
  """What stops the tree describing a declared gRPC endpoint: a service, method or message
  it does not declare, or a declaration inside an endpoint's messages that names a type the
  tree does not hold (and the stub build would drop). A dropped declaration no endpoint
  reaches is not a problem: service files routinely import far more than a client calls."""
  unresolved = tree.unresolved()
  problems: list[str] = []
  for function, spec in grpc_endpoints(project):
    service = tree.services.get(spec.service)  # type: ignore[attr-defined]
    if service is None:
      problems.append(f'{function}: service {spec.service} is declared by no file under spec/proto/')  # type: ignore[attr-defined]
      continue
    if spec.rpc not in service.methods:  # type: ignore[attr-defined]
      problems.append(f'{function}: {spec.service} has no rpc {spec.rpc}')  # type: ignore[attr-defined]
    reached: set[str] = set()
    for message in (spec.request, spec.response):  # type: ignore[attr-defined]
      if message not in tree.messages:
        problems.append(f'{function}: message {message} is declared by no file under spec/proto/')
        continue
      reached |= tree.closure(message)
    for file, declaration, missing in unresolved:
      owner = declaration.rsplit('.', 1)[0] if '/' not in declaration else None
      if declaration == f'{spec.service}/{spec.rpc}' or owner in reached:  # type: ignore[attr-defined]
        problems.append(f'{function}: {declaration} ({file}) names {missing}, which no file under spec/proto/ declares')
  return problems


def target(project: Project, language: str) -> StubTarget:
  if language == 'typescript':
    if project.typescript is None:
      raise StubError('truewire.toml declares no [typescript] section')
    return StubTarget(language, project.typescript_package_dir / PROTOS_DIR)
  if language == 'go':
    if project.go is None:
      raise StubError('truewire.toml declares no [go] section')
    from truewire.codegen.go import import_path
    from truewire.plan.model import PackagePlan

    prefix = import_path(PackagePlan(name=project.name, root_class=''), project)
    return StubTarget(language, project.go_package_dir / PROTOS_DIR, go_prefix=f'{prefix}/{PROTOS_DIR}')
  if language == 'rust':
    if project.rust is None:
      raise StubError('truewire.toml declares no [rust] section')
    return StubTarget(language, project.rust_package_dir / PROTOS_DIR)
  raise StubError(f'no protobuf stubs for {language!r}; one of: {", ".join(LANGUAGES)}')


def _node_bin(project: Project, name: str) -> str | None:
  for directory in [project.root, *project.root.parents]:
    candidate = directory / 'node_modules' / '.bin' / name
    if candidate.is_file():
      return str(candidate)
  return None


def find_tool(project: Project, name: str) -> str:
  """The executable `name` (`buf`, `protoc-gen-es`, `protoc-gen-go`), or `StubError` saying how to install it."""
  variable = f'TRUEWIRE_{name.upper().replace("-", "_")}'
  if os.environ.get(variable):
    return os.environ[variable]
  if name in ('buf', 'protoc-gen-es'):
    found = _node_bin(project, name)
    if found is not None:
      return found
  found = shutil.which(name)
  if found is not None:
    return found
  if name == 'protoc-gen-go':
    go = shutil.which('go')
    gopath = subprocess.run([go, 'env', 'GOPATH'], capture_output=True, text=True).stdout.strip() if go else ''
    for directory in [os.environ.get('GOBIN', ''), os.path.join(gopath, 'bin') if gopath else '', str(Path.home() / 'go' / 'bin')]:
      candidate = Path(directory) / name if directory else None
      if candidate is not None and candidate.is_file():
        return str(candidate)
  if name == 'protoc-gen-prost':
    cargo_home = Path(os.environ.get('CARGO_HOME') or Path.home() / '.cargo')
    candidate = cargo_home / 'bin' / name
    if candidate.is_file():
      return str(candidate)
  install = {
    'buf': 'npm install --save-dev @bufbuild/buf',
    'protoc-gen-es': 'npm install --save-dev @bufbuild/protoc-gen-es',
    'protoc-gen-go': 'go install google.golang.org/protobuf/cmd/protoc-gen-go@latest',
    'protoc-gen-prost': 'cargo install protoc-gen-prost --version 0.5.0 --locked',
  }[name]
  raise StubError(f'{name} not found (set {variable}, or `{install}`)')


PLUGINS = {'typescript': 'protoc-gen-es', 'go': 'protoc-gen-go', 'rust': 'protoc-gen-prost'}


def _template(stub: StubTarget, plugin: str) -> str:
  if stub.language == 'rust':
    return (
      'version: v2\n'
      'plugins:\n'
      f'  - local: {plugin}\n'
      '    out: out\n'
      '    opt: [flat_output_dir]\n'
    )
  if stub.language == 'typescript':
    return (
      'version: v2\n'
      'plugins:\n'
      f'  - local: {plugin}\n'
      '    out: out\n'
      '    opt: [target=ts, import_extension=js]\n'
    )
  return (
    'version: v2\n'
    'managed:\n'
    '  enabled: true\n'
    '  override:\n'
    '    - file_option: go_package_prefix\n'
    f'      value: {stub.go_prefix}\n'
    'plugins:\n'
    f'  - local: {plugin}\n'
    '    out: out\n'
    '    opt: [paths=source_relative]\n'
  )


def render(project: Project, language: str) -> tuple[StubTarget, dict[str, bytes]]:
  """Every stub file for `language`, by path relative to the target directory."""
  stub = target(project, language)
  tree = ProtoTree(project.spec_dir / 'proto')
  if not tree.files:
    raise StubError(f'no .proto files under {project.spec_dir / "proto"}')
  problems = tree_problems(project, tree)
  if problems:
    raise StubError('spec/proto/ cannot describe every gRPC endpoint:\n  ' + '\n  '.join(problems))
  buf = find_tool(project, 'buf')
  plugin = find_tool(project, PLUGINS[language])
  with tempfile.TemporaryDirectory(prefix='truewire-protos-') as scratch:
    work = Path(scratch)
    tree.write_stripped(work / 'proto')
    (work / 'buf.gen.yaml').write_text(_template(stub, plugin), encoding='utf-8')
    result = subprocess.run(
      [buf, 'generate', 'proto', '--template', 'buf.gen.yaml'], cwd=work, capture_output=True, text=True,
    )
    if result.returncode != 0:
      raise StubError(f'buf generate failed:\n{result.stderr or result.stdout}')
    out = work / 'out'
    if language == 'rust':
      result = subprocess.run(
        [buf, 'build', 'proto', '--as-file-descriptor-set', '--exclude-source-info', '-o', str(out / RUST_DESCRIPTORS)],
        cwd=work, capture_output=True, text=True,
      )
      if result.returncode != 0:
        raise StubError(f'buf build failed:\n{result.stderr or result.stdout}')
    files = {
      path.relative_to(out).as_posix(): path.read_bytes()
      for path in sorted(out.rglob('*')) if path.is_file()
    }
  if language == 'rust':
    files[RUST_MOD] = rust_mod([Path(name).name[:-3] for name in files if name.endswith('.rs')]).encode()
  return stub, files


def rust_mod(packages: list[str]) -> str:
  """`protos/mod.rs`: one module per package segment, as `prost-build` names them, around
  the `include!` of each package's file, and the descriptor set the tree compiles to."""
  from truewire.codegen.rust.prost_names import to_snake

  tree: dict = {}
  for package in sorted(packages):
    node = tree
    for segment in package.split('.'):
      node = node.setdefault(to_snake(segment), {})
    node.setdefault('', []).append(f'{package}.rs')
  lines = [
    '//! Generated by `truewire protos rust` from `spec/proto/` — do not edit by hand.',
    '//!',
    '//! The `prost` messages every gRPC endpoint of this crate sends and returns (ADR 0017).',
    '#![allow(clippy::all, clippy::pedantic, missing_docs, rustdoc::all)]',
  ]

  def emit(node: dict, depth: int):
    indent = '    ' * depth
    for include in node.get('', []):
      lines.append(f'{indent}include!("{include}");')
    for name in sorted(key for key in node if key):
      if lines[-1] != '' and not lines[-1].endswith('{'):
        lines.append('')
      lines.append(f'{indent}pub mod {name} {{')
      emit(node[name], depth + 1)
      lines.append(f'{indent}}}')

  emit(tree, 0)
  lines.extend((
    '',
    '/// The `google.protobuf.FileDescriptorSet` of the stripped tree, imports included.',
    f'pub const FILE_DESCRIPTOR_SET: &[u8] = include_bytes!("{RUST_DESCRIPTORS}");',
  ))
  return '\n'.join(lines) + '\n'


def write(stub: StubTarget, files: dict[str, bytes]):
  """Replace the stub files under the target directory with `files`."""
  for relative in stub.owned() - set(files):
    stale = stub.output / relative
    stale.unlink()
    parent = stale.parent
    while parent != stub.output and parent.is_dir() and not any(parent.iterdir()):
      parent.rmdir()
      parent = parent.parent
  for relative, content in files.items():
    destination = stub.output / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)


def differences(stub: StubTarget, files: dict[str, bytes]) -> list[str]:
  """Paths under the target that are missing, stale or not part of the build."""
  out: list[str] = []
  present = stub.owned()
  for relative, content in files.items():
    path = stub.output / relative
    if relative not in present:
      out.append(f'missing: {relative}')
    elif path.read_bytes() != content:
      out.append(f'stale: {relative}')
  out.extend(f'extra: {relative}' for relative in sorted(present - set(files)))
  return out


__all__ = [
  'LANGUAGES', 'PROTOS_DIR', 'StubError', 'StubTarget', 'differences', 'find_tool', 'grpc_endpoints',
  'render', 'target', 'tree_problems', 'write',
]
