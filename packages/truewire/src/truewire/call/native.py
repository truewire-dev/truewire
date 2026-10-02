"""`truewire call typescript|rust`: the call made by the generated client in its own language.

The client is built the way the example cores build it, since `init` writes no TypeScript
or Rust constructor: `new <Root>(new Core(options))` with `Core` and `CoreOptions` from
`<package>/core/index.ts`, and `<Root>::new(core::CoreOptions { .. })` with
`CoreOptions: Default`. `[secrets]`, `--base-url`, `--ws-url` and `--new` set the fields
of `CoreOptions`, read from the core's source, under the same rules as Python's `new(...)`.

Arguments cannot be typed here from the method's signature, as Python's are, so each
`key=value` string becomes its wire value by the endpoint's argument schema first
(`wire_arguments`); the generated codec then decodes and validates it in the driver:
`resources/call.mjs` under Node, or a small binary crate calling `<Root>::call` and
`<Root>::subscribe` from the generated `dispatch.rs`, built under `.truewire/cache/call/rust/`.

A driver reads its job as JSON on stdin, never on its command line, where `ps` would show
the `[secrets]` values in `options` to every local user. It prints one JSON line per event
on stdout (`Event`), wire values only, so every language's output goes through the same
`json.dumps` in `truewire.cli.call`.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing_extensions import Any

from truewire.call import CallError, Target, argument_schema, option_kwargs
from truewire.project import Project
from truewire.spec import load_shared_schemas
from truewire.test import NoSuite, package_directory

NATIVE = ('typescript', 'rust')
"""Languages whose client runs in its own runtime, driven from here."""


@dataclass(frozen=True)
class Event:
  """One line a driver printed: `result`, `reply`, `message`, `unsubscribed` or `error`."""
  kind: str
  value: Any


def admits_string(schema: Any, shared: Mapping[str, Any], seen: frozenset[str] = frozenset()) -> bool | None:
  """Whether `schema` takes a JSON string: `True`, `False`, or `None` when it cannot say
  (no schema, an unresolvable `$ref`)."""
  if not isinstance(schema, dict):
    return None
  ref = schema.get('$ref')
  if isinstance(ref, str):
    if ref in seen or ref not in shared:
      return None
    return admits_string(shared[ref], shared, seen | {ref})
  if 'const' in schema:
    return isinstance(schema['const'], str)
  if isinstance(schema.get('enum'), list):
    return any(isinstance(value, str) for value in schema['enum'])
  kind = schema.get('type')
  if isinstance(kind, str):
    return kind == 'string'
  if isinstance(kind, list):
    return 'string' in kind
  branches = schema.get('anyOf') or schema.get('oneOf')
  if isinstance(branches, list):
    answers = [admits_string(branch, shared, seen) for branch in branches]
    return True if True in answers else None if None in answers else False
  return None


def wire_value(text: str, schema: Any, shared: Mapping[str, Any]) -> Any:
  """A `key=value` string as the wire value its parameter's schema expects: kept as a
  string when the schema takes one, else decoded as JSON (`42`, `true`, `["a"]`). A string
  that is not JSON stays a string, and the generated codec says what is wrong with it; so
  does `NaN` or `Infinity`, which JSON has no number for."""
  if admits_string(schema, shared) is True:
    return text
  try:
    return json.loads(text, parse_constant=refuse_constant)
  except ValueError:
    return text


def refuse_constant(name: str) -> Any:
  raise ValueError(f'{name} is not a JSON number')


def argument_properties(schema: Mapping[str, Any], shared: Mapping[str, Any]) -> dict[str, Any]:
  """Each argument's schema by name. A union request (`anyOf`/`oneOf` of objects, no
  `properties` of its own) gives each name the union of the schemas its branches declare
  for it, so a value any branch takes as a string stays one."""
  properties = schema.get('properties')
  if isinstance(properties, dict):
    return dict(properties)
  branches = schema.get('anyOf') or schema.get('oneOf')
  if not isinstance(branches, list):
    return {}
  found: dict[str, list[Any]] = {}
  for branch in branches:
    if isinstance(branch, dict) and isinstance(branch.get('$ref'), str):
      branch = shared.get(branch['$ref'])
    if isinstance(branch, dict) and isinstance(branch.get('properties'), dict):
      for name, property_schema in branch['properties'].items():
        found.setdefault(name, []).append(property_schema)
  return {name: schemas[0] if len(schemas) == 1 else {'anyOf': schemas} for name, schemas in found.items()}


def wire_arguments(target: Target, arguments: Mapping[str, str], project: Project) -> dict[str, Any]:
  """API-named `key=value` arguments as the wire request (a stream's parameters) that the
  generated `Request`/`Parameters` codec decodes."""
  shared = load_shared_schemas(project)
  properties = argument_properties(argument_schema(target.endpoint) or {}, shared)
  return {name: wire_value(text, properties.get(name), shared) for name, text in arguments.items()}


_TS_OPTIONS = re.compile(r'^export\s+(?:interface\s+CoreOptions\b[^{]*|type\s+CoreOptions\s*=\s*)\{$', re.MULTILINE)
_TS_FIELD = re.compile(r'^\s*(?:readonly\s+)?([A-Za-z_$][\w$]*)\??\s*:\s*(.+?)\s*[;,]?\s*$')
_RUST_OPTIONS = re.compile(r'^pub\s+struct\s+CoreOptions\s*\{$', re.MULTILINE)
_RUST_FIELD = re.compile(r'^\s*pub\s+(?:r#)?(\w+)\s*:\s*(.+?)\s*,?\s*$')


def root_name(project: Project, language: str) -> str:
  """The generated root client's name in `language`: `[<language>].name`.

  Raises:
    CallError: The project declares no package in `language`.
  """
  section = getattr(project.config, language)
  if section is None:
    raise CallError(f'{project.root / "truewire.toml"} declares no [{language}] package')
  return section.name


def core_source(project: Project, language: str) -> Path:
  """The core module that defines `CoreOptions`."""
  if language == 'typescript':
    return project.typescript_package_dir / 'core' / 'index.ts'
  package = project.rust_package_dir
  return next((path for path in (package / 'core' / 'mod.rs', package / 'core.rs') if path.is_file()), package / 'core' / 'mod.rs')


def core_options(project: Project, language: str) -> dict[str, str]:
  """The fields `CoreOptions` declares in the core's source, each with its type as
  written: TypeScript's `export interface CoreOptions`, Rust's `pub struct CoreOptions`
  (its `pub` fields).

  Raises:
    CallError: The core defines no `CoreOptions` there.
  """
  path = core_source(project, language)
  convention = (
    f'new {root_name(project, language)}(new Core(options: CoreOptions))' if language == 'typescript'
    else f'{root_name(project, language)}::new(core::CoreOptions {{ .. }})'
  )
  text = path.read_text() if path.is_file() else ''
  found = (_TS_OPTIONS if language == 'typescript' else _RUST_OPTIONS).search(text)
  if found is None:
    raise CallError(f'{path}: no CoreOptions; call {language} builds the client as {convention}')
  fields: dict[str, str] = {}
  depth = 1
  for line in text[found.end():].splitlines():
    stripped = line.strip()
    if depth == 1 and not stripped.startswith(('//', '/*', '*', '#')):
      match = (_TS_FIELD if language == 'typescript' else _RUST_FIELD).match(line)
      if match:
        fields[match.group(1)] = match.group(2)
    depth += line.count('{') - line.count('}')
    if depth <= 0:
      break
  return fields


def snake_case(name: str) -> str:
  """`apiKey` -> `api_key`; a snake_case name is unchanged."""
  return re.sub(r'(?<=[a-z0-9])([A-Z])', r'_\1', name).lower()


def native_options(
  project: Project, language: str, *, secrets: Mapping[str, str], secret_map: Mapping[str, str] | None = None,
  base_url: str | None = None, ws_url: str | None = None, new: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
  """`CoreOptions` as JSON: each secret under the field its name ends with, then
  `--base-url`/`--ws-url`, then `--new`, under `truewire.call.option_kwargs`'s rules.

  Keywords are matched in snake_case, so `PETSTORE_API_KEY` finds TypeScript's `apiKey`,
  and `--new` and `--secret` take either spelling.

  Raises:
    CallError: As `option_kwargs`, or a `--new` key that names no field.
  """
  fields = core_options(project, language)
  spelled = {snake_case(name): name for name in fields}
  strings = [snake_case(name) for name, kind in fields.items() if re.search(r'\bstring\b|\bString\b|&str\b', kind)]
  owner = f'CoreOptions ({core_source(project, language)})'
  new = dict(new or {})
  for key in new:
    if snake_case(key) not in spelled:
      raise CallError(f'--new {key}: {owner} has no {key}; it has {", ".join(fields) or "no fields"}')
  kwargs = option_kwargs(
    owner, list(spelled), strings, secrets=secrets,
    secret_map={snake_case(keyword): name for keyword, name in (secret_map or {}).items()},
    base_url=base_url, ws_url=ws_url, new={snake_case(key): value for key, value in new.items()},
  )
  return {spelled[keyword]: value for keyword, value in kwargs.items()}


def job(project: Project, language: str, target: Target, arguments: Mapping[str, Any], options: Mapping[str, Any]) -> dict[str, Any]:
  """What a driver is handed: the call, and where the client is."""
  return {
    'packageDir': str((project.typescript_package_dir if language == 'typescript' else project.rust_package_dir).resolve()),
    'root': root_name(project, language),
    'function': target.function,
    'kind': target.endpoint.spec.kind,
    'arguments': dict(arguments),
    'options': dict(options),
  }


def command(project: Project, language: str, job: Mapping[str, Any], *, echo=None) -> list[str]:
  """The command that runs `job`: Node on the driver script, or the Rust driver binary,
  built first. The job itself goes on the driver's stdin, not in this command.

  Raises:
    CallError: The package or its toolchain is not there to run it.
  """
  try:
    directory = package_directory(project, language)
  except NoSuite as exc:
    raise CallError(str(exc)) from None
  if language == 'typescript':
    if shutil.which('node') is None:
      raise CallError('node not found on PATH; call typescript runs the client under Node 22.15 or later')
    version = subprocess.run(['node', '--version'], capture_output=True, text=True).stdout.strip()
    if node_version(version) < NODE_FLOOR:
      raise CallError(
        f'node {version or "(no version)"}: call typescript needs Node {".".join(map(str, NODE_FLOOR))} or later '
        '(module.registerHooks)'
      )
    if not (directory / 'node_modules').is_dir():
      raise CallError(f'{directory}: no node_modules; install the package\'s dependencies first')
    script = files('truewire').joinpath('resources', 'call.mjs')
    return ['node', '--experimental-transform-types', '--no-warnings', str(script)]
  return [str(build_rust_driver(project, directory, job, echo=echo))]


NODE_FLOOR = (22, 15)
"""The first Node with `module.registerHooks`, which the driver resolves `.ts` sources through."""


def node_version(text: str) -> tuple[int, int]:
  """`v22.14.0` as `(22, 14)`; `(0, 0)` when it reads as no version."""
  match = re.match(r'v?(\d+)\.(\d+)', text)
  return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


RUST_DRIVER = 'truewire-call'


def rust_driver_source(project: Project, crate: str, job: Mapping[str, Any]) -> str:
  """`main.rs` for one call: `CoreOptions` gets the fields `job` sets, then `<Root>::call`
  (or `subscribe`) makes it. Only the fields and the kind vary, so a repeated call reuses
  its build, and `build_rust_driver` keeps one crate per source."""
  root = root_name(project, 'rust')
  assignments = '\n'.join(
    f'    options.{field} = option(&job, "{field}");' for field in job['options']
  )
  body = RUST_STREAM if job['kind'] == 'stream' else RUST_RPC
  return RUST_MAIN.format(crate=crate, root=root, assignments=assignments, body=body)


RUST_MAIN = '''\
//! Written by `truewire call rust` on every call; do not edit.

use std::io::{{Read, Write}};

use truewire_core::serde_json::{{self, Map, Value}};
use truewire_core::CallOptions;
use {crate}::core::CoreOptions;
use {crate}::{root};

fn emit(key: &str, value: Value) {{
    let mut line = Map::new();
    line.insert(key.to_string(), value);
    let mut out = std::io::stdout().lock();
    // Nobody is reading any more: the CLI is gone.
    if writeln!(out, "{{}}", Value::Object(line)).and_then(|_| out.flush()).is_err() {{
        std::process::exit(1);
    }}
}}

fn fail(kind: &str, message: &str) -> ! {{
    let mut error = Map::new();
    error.insert("type".to_string(), Value::String(kind.to_string()));
    error.insert("message".to_string(), Value::String(message.to_string()));
    emit("error", Value::Object(error));
    std::process::exit(1)
}}

fn failed(error: truewire_core::Error) -> ! {{
    fail(error.name(), error.message())
}}

fn option<T: serde::de::DeserializeOwned>(job: &Value, field: &str) -> T {{
    serde_json::from_value(job["options"][field].clone())
        .unwrap_or_else(|error| fail("ValueError", &format!("CoreOptions.{{field}}: {{error}}")))
}}

#[tokio::main]
async fn main() {{
    let mut line = String::new();
    let job: Value = std::io::stdin()
        .read_line(&mut line)
        .ok()
        .and_then(|_| serde_json::from_str(&line).ok())
        .unwrap_or_else(|| fail("CallError", "the driver reads its job as one line of JSON on stdin"));
    // The CLI keeps stdin open for as long as it runs; its end means the CLI is gone,
    // however it died, so stop rather than hold a subscription nobody reads.
    std::thread::spawn(|| {{
        let mut rest = [0u8; 64];
        while matches!(std::io::stdin().read(&mut rest), Ok(n) if n > 0) {{}}
        std::process::exit(1);
    }});
    let function = job["function"].as_str().unwrap_or_default().to_string();
    let arguments = job["arguments"].clone();
    #[allow(unused_mut)]
    let mut options = CoreOptions::default();
{assignments}
    let client = {root}::new(options);
{body}
}}
'''

RUST_RPC = '''\
    match client.call(&function, arguments, CallOptions::default()).await {
        Ok(value) => emit("result", value),
        Err(error) => failed(error),
    }'''

RUST_STREAM = '''\
    use futures::StreamExt;
    let stop = async {
        let _ = tokio::signal::ctrl_c().await;
        // The second one exits at once, without waiting for the unsubscribe.
        tokio::spawn(async {
            let _ = tokio::signal::ctrl_c().await;
            std::process::exit(130);
        });
    };
    tokio::pin!(stop);
    let mut stream = tokio::select! {
        subscribed = client.subscribe(&function, arguments, CallOptions::default()) => {
            subscribed.unwrap_or_else(|error| failed(error))
        }
        _ = &mut stop => return,
    };
    emit("reply", stream.reply.clone().unwrap_or(Value::Null));
    loop {
        tokio::select! {
            item = stream.next() => match item {
                Some(Ok(message)) => emit("message", message),
                Some(Err(error)) => failed(error),
                None => return,
            },
            _ = &mut stop => break,
        }
    }
    let seconds = 10;
    match tokio::time::timeout(std::time::Duration::from_secs(seconds), stream.unsubscribe()).await {
        Ok(Ok(reply)) => emit("unsubscribed", reply.unwrap_or(Value::Null)),
        Ok(Err(error)) => failed(error),
        Err(_) => fail(
            "CallError",
            &format!("{function}: no unsubscribe reply within {seconds} s; closing the connection"),
        ),
    }'''


def rust_manifest(directory: Path) -> tuple[str, str, dict[str, Any]]:
  """The package's name, its library's name, and its `truewire-core` dependency with any
  `path` made absolute, so the driver links the same runtime."""
  manifest = tomllib.loads((directory / 'Cargo.toml').read_text())
  name = manifest.get('package', {}).get('name')
  if not name:
    raise CallError(f'{directory / "Cargo.toml"}: no [package] name')
  library = manifest.get('lib', {}).get('name') or name.replace('-', '_')
  core = manifest.get('dependencies', {}).get('truewire-core')
  if core is None:
    raise CallError(f'{directory / "Cargo.toml"}: no truewire-core dependency')
  core = {'version': core} if isinstance(core, str) else dict(core)
  if 'path' in core:
    core['path'] = str((directory / core['path']).resolve())
  return name, library, core


def toml_value(value: Any) -> str:
  if isinstance(value, dict):
    return '{ ' + ', '.join(f'{key} = {toml_value(item)}' for key, item in value.items()) + ' }'
  if isinstance(value, list):
    return '[' + ', '.join(toml_value(item) for item in value) + ']'
  if isinstance(value, bool):
    return 'true' if value else 'false'
  return json.dumps(value)


def write_if_changed(path: Path, text: str):
  """Write `text` unless `path` already holds it, so cargo sees nothing new to build; by a
  rename, so a concurrent build never reads half a file."""
  if not path.is_file() or path.read_text() != text:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{os.getpid()}')
    temporary.write_text(text)
    os.replace(temporary, path)


def build_rust_driver(project: Project, directory: Path, job: Mapping[str, Any], *, echo=None) -> Path:
  """Write and build the driver crate under `.truewire/cache/call/rust/` (git-ignored by `init`), against the package at
  `directory`; return its binary. It shares the package's `Cargo.lock` (copied when the
  driver has none) and its `target/`, so only the driver itself compiles once the
  package has been built.

  The crate is `.truewire/cache/call/rust/<digest>/`, its package and binary
  `truewire-call-<digest>`, the digest of what it is built from (the package, its
  `truewire-core`, `main.rs`). Two calls that set different `CoreOptions` fields thus
  never run each other's binary, even at once or with one `CARGO_TARGET_DIR` for every
  project, where a field the other binary lacks would be dropped without a word.

  Raises:
    CallError: cargo is not on `PATH`, or the build failed (with cargo's output).
  """
  if shutil.which('cargo') is None:
    raise CallError('cargo not found on PATH; call rust builds a small driver against the crate')
  name, library, core = rust_manifest(directory)
  source = rust_driver_source(project, library, job)
  digest = hashlib.sha256(f'{directory.resolve()}\n{toml_value(core)}\n{source}'.encode()).hexdigest()[:16]
  package = f'{RUST_DRIVER}-{digest}'
  driver = project.state_dir / 'cache' / 'call' / 'rust' / digest
  write_if_changed(driver / 'Cargo.toml', '\n'.join([
    '# Written by `truewire call rust`; do not edit.',
    '[package]',
    f'name = "{package}"',
    'version = "0.0.0"',
    'edition = "2021"',
    'publish = false',
    '',
    '[workspace]',
    '',
    '[dependencies]',
    f'{name} = {toml_value({"path": str(directory.resolve())})}',
    f'truewire-core = {toml_value(core)}',
    'futures = "0.3"',
    'serde = "1"',
    'tokio = { version = "1", features = ["macros", "rt-multi-thread", "signal", "time"] }',
    '',
  ]))
  write_if_changed(driver / 'src' / 'main.rs', source)
  if not (driver / 'Cargo.lock').is_file() and (directory / 'Cargo.lock').is_file():
    write_if_changed(driver / 'Cargo.lock', (directory / 'Cargo.lock').read_text())
  target = Path(os.environ.get('CARGO_TARGET_DIR') or directory / 'target')
  binary = target / 'debug' / package
  if not binary.is_file() and echo is not None:
    echo(f'call rust: building {driver} (the first build compiles the crate and its dependencies)')
  built = subprocess.run(
    ['cargo', 'build', '--quiet', '--manifest-path', str(driver / 'Cargo.toml')],
    env={**os.environ, 'CARGO_TARGET_DIR': str(target)},
    stdin=subprocess.DEVNULL, capture_output=True, text=True,
  )
  if built.returncode != 0:
    raise CallError(f'call rust: building {driver} failed:\n{built.stderr.rstrip()}')
  return binary


def events(process: subprocess.Popen[str]) -> Iterator[Event]:
  """The driver's stdout, one `Event` per line; a line that is not one is refused."""
  assert process.stdout is not None
  for line in process.stdout:
    try:
      (kind, value), = json.loads(line).items()
    except (ValueError, AttributeError):
      raise CallError(f'the driver printed something that is not an event: {line.rstrip()}') from None
    yield Event(kind, value)
