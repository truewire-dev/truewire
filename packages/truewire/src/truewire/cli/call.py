"""`truewire call`: one call, or one subscription, through the project's generated client."""
import asyncio
import json
import os
import signal
import subprocess
import sys

import typer
from typing_extensions import TYPE_CHECKING, Any

from .common import PROJECT_OPTION, resolve_project

if TYPE_CHECKING:
  from truewire.call import Target
  from truewire.project import Project


def call(
  language: str = typer.Argument(..., help='The generated package to call through: `python`, `typescript` or `rust`.'),
  function: str = typer.Argument(..., help='Endpoint function path, e.g. `pets.get_pet`.'),
  arguments: list[str] = typer.Argument(
    None, help='API-named key=value arguments. A list or an object is given as JSON.',
  ),
  base_url: str | None = typer.Option(None, '--base-url', help='Passed to `.new(base_url=...)`, e.g. a `truewire mock` URL.'),
  ws_url: str | None = typer.Option(None, '--ws-url', help='Passed to `.new(ws_url=...)`, e.g. a `truewire mock` WS URL.'),
  secret: list[str] = typer.Option(
    [], '--secret',
    help='keyword=NAME: pass the [secrets] variable NAME as `.new(keyword=...)`, when its name does not say which. Repeatable.',
  ),
  new: list[str] = typer.Option(
    [], '--new', help='key=value passed to the root client\'s `.new(...)`; JSON values are decoded. Repeatable.',
  ),
  project: str | None = PROJECT_OPTION,
):
  """Instantiate the client from `truewire.toml` and `.env` and make one call.

  `truewire call python pets.get_pet petId=42 --base-url http://127.0.0.1:8321` prints the
  validated reply as JSON. On a stream endpoint it subscribes and prints the reply, then
  each message, one JSON document per line, until Ctrl+C unsubscribes; the unsubscribe
  reply goes to stderr.

  The credentials `[secrets]` names are read from the environment, else from `.env` at the
  project root, and passed to `.new(...)` under the keyword each variable's name ends
  with (`PETSTORE_API_KEY` as `api_key`), or under the one `--secret api_key=NAME` names.
  A missing required one fails before any request. Arguments are the API's names
  (`petId`, not `pet_id`); a missing, unknown or ill-typed one exits 1 naming it.

  A second Ctrl+C stops at once, without waiting for the unsubscribe.

  `typescript` and `rust` make the same call through the generated client in that
  language, built as `new <Root>(new Core(options))` and `<Root>::new(CoreOptions { .. })`:
  the secrets and `--base-url`, `--ws-url` and `--new` set `CoreOptions` fields (`baseUrl`
  in TypeScript). TypeScript runs under Node from the package's sources; Rust builds a
  small driver under `.truewire/cache/call/rust/` against the crate, once for each set of
  `CoreOptions` fields a call sets.
  """
  from truewire.call import (
    CallError, build_client, call as call_rpc, check_arguments, check_language, find_target, parse_arguments,
  )
  from truewire.call.native import NATIVE
  from truewire.mcp import parse_new_kwargs
  from truewire_core.exceptions import ApiError

  loaded = resolve_project(project)
  process: subprocess.Popen[str] | None = None
  client: Any = None
  try:
    parsed = parse_arguments(arguments or [])
    target = find_target(loaded, function)
    check_arguments(target, parsed, loaded)
    check_language(loaded, language)
    try:
      extra = parse_new_kwargs(new)
    except ValueError as exc:
      raise CallError(f'--new: {exc}') from None
    try:
      secret_map = parse_arguments(secret)
    except CallError as exc:
      raise CallError(f'--secret: {exc}') from None
    if language in NATIVE:
      process = start_native(loaded, language, target, parsed, secret_map=secret_map, base_url=base_url, ws_url=ws_url, new=extra)
    else:
      client = build_client(
        loaded, language, secret_map=secret_map, base_url=base_url, ws_url=ws_url, new=extra,
      )
  except CallError as exc:
    typer.echo(str(exc), err=True)
    raise typer.Exit(code=1)

  if process is not None:
    raise typer.Exit(code=follow_native(process, function, stream=target.endpoint.spec.kind == 'stream'))

  try:
    if target.endpoint.spec.kind == 'stream':
      unsubscribed = asyncio.run(stream(client, target, parsed, loaded))
      if unsubscribed is not None:
        typer.echo(f'unsubscribed: {json.dumps(unsubscribed, ensure_ascii=False)}', err=True)
    else:
      typer.echo(json.dumps(asyncio.run(call_rpc(client, target, parsed, loaded)), indent=2, ensure_ascii=False))
  except CallError as exc:
    typer.echo(str(exc), err=True)
    raise typer.Exit(code=1)
  except ApiError as exc:
    message = exc.args[0] if len(exc.args) == 1 else ', '.join(map(str, exc.args))
    typer.echo(f'{function}: {type(exc).__name__}: {message}', err=True)
    raise typer.Exit(code=1)
  except KeyboardInterrupt:
    raise typer.Exit(code=130)
  except BrokenPipeError:  # `| head`: the reader has what it wanted
    silence_stdout()
  except Exception as exc:  # noqa: BLE001 -- a transport or validation failure is reported, not a traceback
    typer.echo(f'{function}: {type(exc).__name__}: {exc}', err=True)
    raise typer.Exit(code=1)


async def stream(client: Any, target: 'Target', arguments: dict[str, str], project: 'Project') -> Any:
  """Run `subscribe` until SIGINT or SIGTERM, or until stdout is closed, printing each
  document as one line.

  The first signal asks for a clean stop; its handler is then removed, so a second one
  acts as it would anywhere else, and a server that never answers cannot hold the process.
  """
  from truewire.call import subscribe

  stop = asyncio.Event()
  loop = asyncio.get_running_loop()
  handled: list[signal.Signals] = []

  def on_signal():
    stop.set()
    for signum in handled:
      loop.remove_signal_handler(signum)
    handled.clear()

  for signum in (signal.SIGINT, signal.SIGTERM):
    try:
      loop.add_signal_handler(signum, on_signal)
      handled.append(signum)
    except (NotImplementedError, RuntimeError):  # no loop signal handlers here (Windows)
      pass

  def emit(value: Any):
    if stop.is_set():
      return
    try:
      sys.stdout.write(json.dumps(value, ensure_ascii=False) + '\n')
      sys.stdout.flush()
    except BrokenPipeError:  # `| head`: stop as if interrupted
      silence_stdout()
      stop.set()

  try:
    return await subscribe(client, target, arguments, project, emit=emit, stop=stop)
  finally:
    for signum in handled:
      loop.remove_signal_handler(signum)


def start_native(
  project: 'Project', language: str, target: 'Target', arguments: dict[str, str], *,
  secret_map: dict[str, str], base_url: str | None, ws_url: str | None, new: dict[str, Any],
) -> subprocess.Popen[str]:
  """Start the `typescript` or `rust` driver on this call, in its own session, so the
  terminal's Ctrl+C reaches it only through `follow_native`. The job goes on its stdin:
  on the command line, `ps` would show the secrets in it to every local user.

  Stdin then stays open until `follow_native` is done: the driver exits when it ends, so a
  CLI that dies any way at all (a closed terminal, SIGKILL) takes the driver with it. The
  driver starts with SIGINT and SIGTERM at their defaults, even when this process was
  started ignoring them (a script's `&` job), so what `follow_native` forwards arrives.

  Raises:
    CallError: As `native_options` and `command`.
  """
  from truewire.call import declared_secrets
  from truewire.call.native import command, job, native_options, wire_arguments

  options = native_options(
    project, language, secrets=declared_secrets(project, secret_map), secret_map=secret_map,
    base_url=base_url, ws_url=ws_url, new=new,
  )
  work = job(project, language, target, wire_arguments(target, arguments, project), options)
  argv = command(project, language, work, echo=lambda line: typer.echo(line, err=True))
  process = subprocess.Popen(
    argv, cwd=project.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding='utf-8',
    start_new_session=True, preexec_fn=default_signals,
  )
  assert process.stdin is not None
  try:
    process.stdin.write(json.dumps(work, allow_nan=False) + '\n')
    process.stdin.flush()
  except BrokenPipeError:  # the driver died before reading; its exit code says so
    pass
  return process


def default_signals():
  """In the driver, before it runs: SIGINT and SIGTERM back to their defaults."""
  signal.signal(signal.SIGINT, signal.SIG_DFL)
  signal.signal(signal.SIGTERM, signal.SIG_DFL)


def follow_native(process: subprocess.Popen[str], function: str, *, stream: bool) -> int:
  """Print what the driver sends, as the Python call prints it, and return the exit code.

  SIGINT and SIGTERM are passed on as SIGINT: the first unsubscribes, a second stops the
  driver at once. A closed stdout (`| head`) stops it the same way.
  """
  from truewire.call import CallError
  from truewire.call.native import events

  def forward(*_):
    if process.poll() is None:
      process.send_signal(signal.SIGINT)

  previous = {signum: signal.signal(signum, forward) for signum in (signal.SIGINT, signal.SIGTERM)}
  failed = False
  closed = False
  try:
    for event in events(process):
      if event.kind == 'error':
        failed = True
        kind, message = event.value.get('type'), event.value.get('message')
        typer.echo(message if kind == 'CallError' else f'{function}: {kind}: {message}', err=True)
      elif event.kind == 'unsubscribed':
        if event.value is not None:
          typer.echo(f'unsubscribed: {json.dumps(event.value, ensure_ascii=False)}', err=True)
      elif closed:
        continue
      else:
        text = json.dumps(event.value, indent=None if stream else 2, ensure_ascii=False)
        try:
          sys.stdout.write(text + '\n')
          sys.stdout.flush()
        except BrokenPipeError:  # `| head`: stop as if interrupted
          silence_stdout()
          closed = True
          forward()
  except CallError as exc:
    typer.echo(str(exc), err=True)
    failed = True
    forward()
  finally:
    code = process.wait()
    if process.stdin is not None:
      try:
        process.stdin.close()
      except BrokenPipeError:
        pass
    for signum, handler in previous.items():
      signal.signal(signum, handler)
  if failed:
    return 1
  if code in (130, -signal.SIGINT, -signal.SIGTERM):
    return 0 if closed else 130
  return 0 if code == 0 else code if code > 0 else 1


def silence_stdout():
  """Point stdout at /dev/null once its reader has gone, so the interpreter's own flush
  at exit does not raise `BrokenPipeError` a second time."""
  devnull = os.open(os.devnull, os.O_WRONLY)
  os.dup2(devnull, sys.stdout.fileno())
  os.close(devnull)
