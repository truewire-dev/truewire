"""`truewire call`: instantiate a project's generated client and make one call.

The client is built the way a caller builds it: `<Root>.new(...)`, with the credentials
`[secrets]` names read from the environment or the project's `.env` (T8), plus whatever
the command line adds (`--base-url`, `--ws-url`, `--new key=value`). Arguments are
API-named `key=value` pairs, bound to the generated method through the same path the
replay tests and `truewire mcp` use, so a call here is typed and validated exactly as a
caller's is.

An `rpc` endpoint answers once. A `stream` endpoint is subscribed: the reply is emitted,
then each message, until `stop` is set, when the subscription is unsubscribed before the
client closes. Nothing here touches `sys.exit` or a signal; `truewire.cli.call` does.
"""
import asyncio
import inspect
import json
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing_extensions import Any, TypeVar, get_args, get_type_hints

from pydantic import PydanticUserError, TypeAdapter, ValidationError
from pydantic_core import PydanticSerializationError

from truewire.examples import (
  PARAMETER_NOTE,
  client_identifier,
  coerce_example_call,
  coerce_ws_example_call,
  resolve_endpoint_function,
)
from truewire.mcp import load_root, to_json
from truewire.project import Project
from truewire.spec import Endpoint, ExampleRequest, WsParametersExample, endpoint_records

LANGUAGES = ('python', 'typescript', 'rust')
"""Languages `truewire call` can instantiate a client in, so far."""

UNSUBSCRIBE_TIMEOUT = 10.0
"""Seconds to wait for the server to answer an unsubscribe before giving up on it."""

T = TypeVar('T')


class CallError(Exception):
  """A call that cannot be made as asked: an unknown endpoint, a missing argument, an
  unset required secret. The message names the thing to fix."""


@dataclass(frozen=True)
class Target:
  """The endpoint a call is for, found by its function path."""
  function: str
  endpoint: Endpoint
  endpoint_path: Path


def find_target(project: Project, function: str) -> Target:
  """The endpoint whose function path is `function`, e.g. `pets.get_pet`.

  Raises:
    CallError: No endpoint has that function path, or it is neither `rpc` nor `stream`.
  """
  spec_root = project.spec_dir
  for record in endpoint_records(project):
    if record.endpoint.resolved_function(record.path, spec_root) != function:
      continue
    if record.endpoint.spec.kind not in ('rpc', 'stream'):
      raise CallError(f'{function} is a {record.endpoint.spec.kind} endpoint; call makes rpc calls and stream subscriptions')
    return Target(function=function, endpoint=record.endpoint, endpoint_path=record.path)
  raise CallError(f'no endpoint with function {function!r} in {spec_root}')


def parse_arguments(pairs: list[str]) -> dict[str, str]:
  """API-named `key=value` arguments, values kept as the strings they were typed as.

  The generated method's own parameter types decode them (`42` for an int, `true` for a
  bool, JSON for a list or an object), so a string parameter is never turned into a number.

  Raises:
    CallError: A pair without `=`, or a key given twice.
  """
  out: dict[str, str] = {}
  for pair in pairs:
    key, sep, value = pair.partition('=')
    if not sep or not key:
      raise CallError(f'expected key=value, got {pair!r}')
    if key in out:
      raise CallError(f'argument {key} given twice')
    out[key] = value
  return out


def argument_schema(endpoint: Endpoint) -> dict[str, Any] | None:
  """The JSON Schema of an endpoint's call arguments: `request` for rpc, `parameters`
  (else `request`) for a stream. `None` for a legacy `openapi` endpoint."""
  spec = endpoint.spec
  if spec.kind == 'stream':
    return spec.parameters or spec.request
  return getattr(spec, 'request', None)


def check_arguments(target: Target, arguments: Mapping[str, Any], project: Project):
  """Refuse a call missing a required argument, or naming one the endpoint does not take,
  in the API's own names, before any client is built.

  A schema that is a union (`anyOf`, no top-level `properties`) is left to the generated
  method's own validation.

  Raises:
    CallError: Naming every missing and every unknown argument. An unknown one that is the
      Python name of an API argument (`pet_id` for `petId`) says so.
  """
  schema = argument_schema(target.endpoint)
  if not schema:
    return
  problems: list[str] = []
  missing = [name for name in schema.get('required', []) if name not in arguments]
  if missing:
    problems.append(f'missing required argument{"s" if len(missing) > 1 else ""} {", ".join(missing)}')
  properties = schema.get('properties')
  if isinstance(properties, dict) and schema.get('additionalProperties') is not True:
    identifier = client_identifier(project)
    python_names = {identifier(name): name for name in properties}
    for name in arguments:
      if name in properties:
        continue
      if name in python_names:
        problems.append(f'unknown argument {name}; call takes API names: {python_names[name]}')
      else:
        problems.append(f'unknown argument {name}; it takes {", ".join(properties) or "no arguments"}')
  if problems:
    raise CallError(f'{target.function}: {"; ".join(problems)}')


def read_dotenv(path: Path) -> dict[str, str]:
  """`NAME=value` lines of a `.env` file: `export ` prefixes, `#` comments and one layer of
  quotes are handled; nothing is expanded. A quoted value ends at its closing quote, so a
  comment after it is dropped. A missing file reads as empty."""
  if not path.is_file():
    return {}
  values: dict[str, str] = {}
  for line in path.read_text().splitlines():
    line = line.strip()
    if not line or line.startswith('#'):
      continue
    if line.startswith('export '):
      line = line[len('export '):].lstrip()
    name, sep, value = line.partition('=')
    if not sep:
      continue
    value = value.strip()
    if value[:1] in ('"', "'") and (end := value.find(value[0], 1)) > 0:
      value = value[1:end]
    elif ' #' in value:
      value = value.split(' #', 1)[0].rstrip()
    values[name.strip()] = value
  return values


def secret_values(project: Project, environ: Mapping[str, str] | None = None) -> dict[str, str]:
  """The values of the variables `[secrets]` names, from the environment first, then from
  `.env` at the project root. Optional ones that are unset are left out.

  Raises:
    CallError: A `[secrets].required` variable is set in neither.
  """
  environ = os.environ if environ is None else environ
  dotenv = read_dotenv(project.root / '.env')
  values: dict[str, str] = {}
  missing: list[str] = []
  for name in project.secrets.required:
    value = environ.get(name, dotenv.get(name))
    if value is None:
      missing.append(name)
    else:
      values[name] = value
  if missing:
    raise CallError(
      f'{", ".join(missing)}: listed in [secrets].required and set neither in the environment '
      f'nor in {project.root / ".env"}'
    )
  for name in project.secrets.optional:
    value = environ.get(name, dotenv.get(name))
    if value is not None:
      values[name] = value
  return values


def secret_parameter(name: str, parameters: list[str]) -> str | None:
  """The `new(...)` keyword a secret variable is passed as: the one its lower-cased name
  equals or ends with (`_<keyword>`), the longest such when several do.

  `PETSTORE_API_KEY` is `api_key`, `GITHUB_TOKEN` is `token`, `API_SECRET` is `api_secret`.
  """
  lowered = name.lower()
  matches = [p for p in parameters if lowered == p or lowered.endswith('_' + p)]
  return max(matches, key=len) if matches else None


def takes_str(annotation: Any) -> bool:
  """Whether a `new(...)` keyword's type admits a string: a credential is one."""
  if annotation is None or annotation is Any or annotation is str:
    return True
  return any(takes_str(arg) for arg in get_args(annotation) if arg is not type(None))


def client_kwargs(
  root: Any, *, secrets: Mapping[str, str], secret_map: Mapping[str, str] | None = None,
  base_url: str | None = None, ws_url: str | None = None, new: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
  """Keyword arguments for `root.new(...)`: each secret under its keyword, then
  `base_url`/`ws_url`, then `new`, each later one winning.

  Args:
    root: The generated root client class.
    secrets: Variable name to value, for each `[secrets]` variable that is set.
    secret_map: `--secret keyword=NAME`: keyword to variable name, chosen by hand. Every
      other variable goes under the keyword its name ends with (`secret_parameter`),
      among the keywords that take a string. A variable whose keyword was chosen by hand
      for another variable is left out.

  Raises:
    CallError: A set secret matches no keyword; two match one keyword; a `secret_map`
      name has no value or its keyword is not one `new` takes; or `--base-url`/`--ws-url`
      is given to a `new` without that keyword. Dropping a credential silently would make
      a call that fails for a reason nobody can see.
  """
  signature = inspect.signature(root.new)
  try:
    hints = get_type_hints(root.new)
  except (NameError, TypeError):
    hints = {}
  keywords = [
    name for name, parameter in signature.parameters.items()
    if parameter.kind in (inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
  ]
  return option_kwargs(
    f'{root.__name__}.new()', keywords, [k for k in keywords if takes_str(hints.get(k))],
    secrets=secrets, secret_map=secret_map, base_url=base_url, ws_url=ws_url, new=new,
  )


def option_kwargs(
  owner: str, keywords: list[str], string_keywords: list[str], *, secrets: Mapping[str, str],
  secret_map: Mapping[str, str] | None = None,
  base_url: str | None = None, ws_url: str | None = None, new: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
  """`client_kwargs` for any constructor: `owner` is what the messages call it
  (`Pets.new()`, `CoreOptions`), `keywords` what it takes, `string_keywords` those a
  credential can go under.

  Raises:
    CallError: As `client_kwargs`.
  """
  takes = ', '.join(keywords) or 'nothing'
  secret_map = secret_map or {}
  kwargs: dict[str, Any] = {}
  for keyword, name in secret_map.items():
    if keyword not in keywords:
      raise CallError(f'--secret {keyword}={name}: {owner} takes no {keyword}; it takes {takes}')
    if name not in secrets:
      raise CallError(f'--secret {keyword}={name}: {name} is set neither in the environment nor in .env')
    kwargs[keyword] = secrets[name]
  chosen: dict[str, str] = {}
  for name in secrets:
    if name in secret_map.values():
      continue
    keyword = secret_parameter(name, string_keywords)
    if keyword is None:
      raise CallError(
        f'{name} (from [secrets]) matches no keyword of {owner}, which takes {takes}; '
        f'pass it with --secret <keyword>={name}'
      )
    if keyword in secret_map:
      continue
    if keyword in chosen:
      raise CallError(
        f'{chosen[keyword]} and {name} (from [secrets]) both go to {slot(owner, keyword)}; '
        f'choose one with --secret {keyword}=<NAME>'
      )
    chosen[keyword] = name
    kwargs[keyword] = secrets[name]
  for keyword, value, flag in (('base_url', base_url, '--base-url'), ('ws_url', ws_url, '--ws-url')):
    if value is None:
      continue
    if keyword not in keywords:
      raise CallError(f'{flag}: {owner} takes no {keyword}; it takes {takes}. Use --new')
    kwargs[keyword] = value
  kwargs.update(new or {})
  return kwargs


def slot(owner: str, keyword: str) -> str:
  """Where a value goes, for a message: `Pets.new(api_key=...)`, or `CoreOptions.token`."""
  return f'{owner[:-2]}({keyword}=...)' if owner.endswith('()') else f'{owner}.{keyword}'


def check_language(project: Project, language: str):
  """Raises `CallError` unless `call` supports `language` and the project declares a package in it."""
  if language not in LANGUAGES:
    raise CallError(f'call supports {", ".join(LANGUAGES)}, not {language}')
  if getattr(project.config, language) is None:
    raise CallError(f'{project.root / "truewire.toml"} declares no [{language}] package')


def declared_secrets(
  project: Project, secret_map: Mapping[str, str] | None = None, environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
  """`secret_values`, after checking every `--secret` names a `[secrets]` variable.

  Raises:
    CallError: A `secret_map` variable is not in `[secrets]`, or `secret_values` refuses.
  """
  declared = (*project.secrets.required, *project.secrets.optional)
  for keyword, name in (secret_map or {}).items():
    if name not in declared:
      raise CallError(f'--secret {keyword}={name}: {name} is not in [secrets]; declare it in truewire.toml first')
  return secret_values(project, environ)


def build_client(
  project: Project, language: str, *,
  secret_map: Mapping[str, str] | None = None,
  base_url: str | None = None, ws_url: str | None = None, new: Mapping[str, Any] | None = None,
  environ: Mapping[str, str] | None = None,
) -> Any:
  """Import the project's generated package in `language` and return `<Root>.new(...)`,
  built from `[secrets]` (`secret_values`) and the overrides (`client_kwargs`).

  Raises:
    CallError: The language is not one `call` supports, the project declares no package
      in it, a `secret_map` variable is not in `[secrets]`, or `secret_values`/
      `client_kwargs` refuse.
  """
  check_language(project, language)
  secrets = declared_secrets(project, secret_map, environ)
  root = load_root(project)
  return root.new(**client_kwargs(
    root, secrets=secrets, secret_map=secret_map, base_url=base_url, ws_url=ws_url, new=new,
  ))


def bind(client: Any, target: Target, arguments: Mapping[str, Any], project: Project) -> tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any]]:
  """Resolve the generated method and bind `arguments` to it.

  Raises:
    CallError: The client has no method for the endpoint, an argument does not fit its
      parameter's type (named in the API's own name), or a parameter without a default is
      left unbound.
  """
  try:
    fn = resolve_endpoint_function(
      client, target.endpoint, endpoint_path=target.endpoint_path, spec_root=project.spec_dir,
    )
  except AttributeError as exc:
    raise CallError(
      f'{target.function}: the generated client has no such method ({exc}); run `truewire generate python`'
    ) from None
  identifier = client_identifier(project)
  try:
    if target.endpoint.spec.kind == 'stream':
      args, kwargs = coerce_ws_example_call(fn, WsParametersExample(parameters=dict(arguments)), identifier=identifier)
    else:
      args, kwargs = coerce_example_call(fn, ExampleRequest(request=dict(arguments) or None), identifier=identifier)
  except ValidationError as exc:
    raise CallError(f'{target.function}: {invalid_argument(exc, arguments, identifier)}') from None
  except TypeError as exc:
    raise CallError(f'{target.function}: {exc}') from None
  signature = inspect.signature(fn)
  bound = signature.bind_partial(*args, **kwargs)
  unbound = [
    name for name, parameter in signature.parameters.items()
    if name not in bound.arguments and parameter.default is inspect.Parameter.empty
    and parameter.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
  ]
  if unbound:
    raise CallError(f'{target.function}: missing required argument{"s" if len(unbound) > 1 else ""} {", ".join(unbound)}')
  return fn, args, kwargs


def invalid_argument(error: ValidationError, arguments: Mapping[str, Any], identifier: Callable[[str], str]) -> str:
  """Say which argument a `ValidationError` from binding is about, by its API name, and
  what is wrong with it, without pydantic's type title or its documentation link."""
  parameter = next(
    (note[len(PARAMETER_NOTE):] for note in getattr(error, '__notes__', []) if note.startswith(PARAMETER_NOTE)),
    None,
  )
  name = next(
    (key for key in arguments if parameter in (key, identifier(key))),
    parameter,
  )
  reasons = '; '.join(
    f'{".".join(map(str, e["loc"]))}: {e["msg"]}' if e['loc'] else e['msg'] for e in error.errors()
  )
  if name is None:
    return f'invalid argument: {reasons}'
  value = arguments.get(name)
  shown = f'{name}={value}' if isinstance(value, str) else name
  return f'invalid argument {shown}: {reasons}'


def return_type(fn: Callable[..., Any]) -> Any:
  """The generated method's declared return type, or `None` when it cannot be resolved."""
  try:
    return get_type_hints(fn).get('return')
  except (NameError, TypeError):  # an unresolvable hint only costs the wire-shaped dump
    return None


def json_data(value: Any, annotation: Any = None) -> Any:
  """`value` as JSON-ready data, dumped through `annotation` when there is one, so a
  timestamp or a decimal string reads the way the wire and the recording write it rather
  than as Python renders it. Falls back to `truewire.mcp.to_json`'s plain rendering."""
  if annotation is not None and annotation is not Any:
    try:
      return TypeAdapter(annotation).dump_python(value, mode='json')
    except (PydanticSerializationError, PydanticUserError):  # a type with no schema, or a value it cannot dump
      pass
  return json.loads(to_json(value, indent=None))


async def call(client: Any, target: Target, arguments: Mapping[str, Any], project: Project) -> Any:
  """Make one `rpc` call inside the client's own context.

  Returns:
    The validated reply as JSON-ready data (`json_data`).
  """
  async with client:
    fn, args, kwargs = bind(client, target, arguments, project)
    return json_data(await fn(*args, **kwargs), return_type(fn))


async def until_stopped(awaitable: Awaitable[T], stop: asyncio.Event) -> T | None:
  """Await `awaitable` unless `stop` is set first, in which case it is cancelled and
  `None` is returned."""
  task = asyncio.ensure_future(awaitable)
  stopping = asyncio.create_task(stop.wait())
  try:
    await asyncio.wait({task, stopping}, return_when=asyncio.FIRST_COMPLETED)
  finally:
    stopping.cancel()
  if task.done():
    return task.result()
  task.cancel()
  await asyncio.gather(task, return_exceptions=True)
  return None


async def subscribe(
  client: Any, target: Target, arguments: Mapping[str, Any], project: Project, *,
  emit: Callable[[Any], None], stop: asyncio.Event,
) -> Any:
  """Subscribe to a `stream` endpoint: `emit` the reply, then each message, as JSON-ready
  data, until `stop` is set or the stream ends; then unsubscribe, and close the client.

  `stop` is honoured while the subscribe is still waiting for its reply too: the pending
  subscribe is cancelled and nothing is emitted.

  Returns:
    The unsubscribe reply as JSON-ready data, or `None` when the stream ended on its own,
    or `stop` came before the subscribe was answered.

  Raises:
    CallError: The server did not answer the unsubscribe within `UNSUBSCRIBE_TIMEOUT`.
  """
  async with client:
    fn, args, kwargs = bind(client, target, arguments, project)
    # `StreamManager[Message, Reply, UnsubscribeReply]`: dump each through its own type.
    types = list(get_args(return_type(fn))) + [None, None, None]
    message_type, reply_type, unsubscribe_type = types[:3]
    stream = await until_stopped(fn(*args, **kwargs), stop)
    if stream is None:
      return None
    emit(json_data(stream.reply, reply_type))

    async def pump():
      async for message in stream:
        emit(json_data(message, message_type))

    pumping = asyncio.create_task(pump())
    await until_stopped(pumping, stop)
    if not pumping.cancelled():
      return None  # the stream ended on its own
    try:
      reply = await asyncio.wait_for(stream.unsubscribe(), UNSUBSCRIBE_TIMEOUT)
    except asyncio.TimeoutError:
      raise CallError(
        f'{target.function}: no unsubscribe reply within {UNSUBSCRIBE_TIMEOUT:g} s; closing the connection'
      ) from None
    return json_data(reply, unsubscribe_type)
