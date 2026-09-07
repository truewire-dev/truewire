import inspect
import json
from collections import Counter
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing_extensions import Any

from pydantic import TypeAdapter, ValidationError
from truewire.generation.python.util import safe_identifier

from truewire.codegen.layout import BackendUnavailable, load_generator
from truewire.project import Project, resolve
from truewire.spec import Endpoint, ExampleRequest, WsParametersExample


def resolve_endpoint_function(
  client: Any, endpoint: Endpoint, *,
  endpoint_path: Path | None = None, spec_root: Path | None = None,
) -> Callable[..., Awaitable[Any]]:
  """Chase `endpoint.function`'s dotted path off a live client instance via `getattr`.

  Args:
    client: The instantiated client (or a sub-router of it) to chase the path off.
    endpoint: The endpoint whose callable is being resolved.
    endpoint_path: This endpoint's own `endpoint.json`, needed only when `endpoint.function`
      is unset (derived from directory position, not authored) -- mirrors
      `truewire.codegen.layout.output_function`'s identical optional pair.
    spec_root: The project's `spec/` directory, needed only for the same derivation.

  Raises:
    ValueError: `endpoint.function` is unset and `endpoint_path`/`spec_root` weren't given
      to derive one from.
  """
  function = endpoint.function
  if function is None:
    if endpoint_path is None or spec_root is None:
      raise ValueError(
        f'{endpoint_path or endpoint}: endpoint.function is unset and no '
        'endpoint_path/spec_root were given to derive one from'
      )
    function = endpoint.resolved_function(endpoint_path, spec_root)
  fn = client
  for part in function.split('.'):
    fn = getattr(fn, part)
  return fn


def client_identifier(client_root: Path | Project) -> Callable[[str], str]:
  """The Python-identifier rule a project's own codegen backend uses.

  Delegates to `truewire.codegen.layout.load_generator` — the same loader
  `truewire generate` runs — so a backend override (`type` -> `type_`, say, a rule
  `safe_identifier` alone does not cover) is honored rather than approximated. Falls back
  to codegen's own default rule for a project with no backend to consult at all.

  Args:
    client_root: Project (or project root).
  """
  try:
    return load_generator(client_root, 'python').identifier
  except BackendUnavailable:
    return safe_identifier


def _api_named_to_identifiers(
  kwargs: dict[str, Any],
  signature: inspect.Signature,
  identifier: Callable[[str], str],
) -> dict[str, Any]:
  """Rename API-named keys (e.g. from an example's `parameters`) to the Python identifiers
  a generated method declares, so `parameters`-shaped examples bind the same way `args`/
  `kwargs`-shaped ones do. See `docs/spec/spec.md#example-parameters-are-api-named`.

  A key already matching a declared parameter, or whose derived identifier does not match
  one, is passed through unchanged; anything the given rule can't resolve surfaces as the
  same `TypeError` binding already raised before this translation existed.
  """
  translated = {}
  for key, value in kwargs.items():
    derived = key if key in signature.parameters else identifier(key)
    translated[derived if derived in signature.parameters else key] = value
  return translated


def _sole_body_parameter(signature: inspect.Signature) -> str | None:
  """Name the one non-`validate`/`transport` parameter a signature declares, or `None`
  when it takes more than one -- see `coerce_example_call`'s `requestBody` fallback.

  `transport` is excluded the same way `validate` already is: a genuine multi-transport
  `requestBody`-shaped method (an order-placing `buy`/`sell` that combines a
  discriminated-union body with a real `transport` keyword) still has exactly one real
  body parameter, and without this exclusion `names` reads as length 2, so this always
  returned `None` for them -- `coerce_example_call` then had nothing to bind the
  recorded example's flat, API-named fields onto at all.

  Args:
    signature: Signature of the resolved generated method.
  """
  names = [name for name in signature.parameters if name not in ('validate', 'transport')]
  return names[0] if len(names) == 1 else None


def _bind_and_coerce(
  signature: inspect.Signature,
  args: 'list[Any] | tuple[Any, ...]',
  kwargs: dict[str, Any],
  payload: Any,
  identifier: Callable[[str], str],
) -> inspect.BoundArguments:
  """Bind API-named `kwargs`/`payload` onto `signature`, translating names and coercing
  types. Shared by `coerce_example_call` (`ExampleRequest`'s `args`/`kwargs`/`payload`)
  and `coerce_ws_example_call` (`WsParametersExample`'s `parameters`/`payload`, always
  called with `args=()` since a subscribe call is never recorded positionally) -- see
  either for the reasoning behind each step below.
  """
  kwargs = _api_named_to_identifiers(kwargs, signature, identifier)
  if (
    kwargs and not args
    and not any(key in signature.parameters for key in kwargs)
    and (sole := _sole_body_parameter(signature)) is not None
  ):
    # A `requestBody`-shaped operation (a `buy`/`sell` with a discriminated-union
    # payload, per spec-authoring rule 0) generates one method parameter carrying the
    # whole body, but its recorded example still records the API's own flat, wire-named
    # fields (`instrument_name`, `amount`, ...) -- that's what the mock server matches on,
    # and nesting them under a made-up wrapper key would misrepresent the wire call. None
    # of them individually names a real parameter here, so the whole translated dict
    # becomes that one parameter's value instead of failing to bind at all.
    kwargs = {sole: kwargs}
  bound = signature.bind_partial(*args, **kwargs)

  if payload is not None:
    # A recorded `payload` is one of two shapes, and only the values it carries tell
    # them apart -- a `batch_cancel` with flat, `in: 'query'`-role parameters that
    # merely travel as a JSON body on the wire (ADR 0006) records `{"order_ids":
    # [...]}` where `order_ids` already names the sole real parameter; an `add_order`
    # with a genuine `requestBody`-shaped `body` parameter records
    # `{"clientOid": ..., "symbol": ...}`, none of which names anything on the
    # signature. Try the payload's own keys as parameter names first (through the same
    # API-name translation `parameters`-shaped examples get); only fall back to
    # wrapping the whole blob under the sole unfilled required parameter when that
    # doesn't hold. A partial match (some but not all keys resolve) is ambiguous
    # evidence, not a decomposition, so it falls through too.
    decomposed = (
      _api_named_to_identifiers(payload, signature, identifier)
      if isinstance(payload, dict) and payload
      else None
    )
    if decomposed is not None and all(
      name in signature.parameters and name not in bound.arguments
      for name in decomposed
    ):
      bound.arguments.update(decomposed)
    else:
      # The other valid recording of a `requestBody`-shaped call: the whole body
      # doesn't decompose onto the signature, so bind it to whichever single required
      # parameter `args`/`kwargs` left unfilled -- ambiguous cases (zero or more than
      # one still-unfilled required parameter) are left alone, surfacing the same clear
      # `TypeError` binding already raises rather than guessing which one it meant.
      unfilled_required = [
        name
        for name, param in signature.parameters.items()
        if name not in bound.arguments and param.default is inspect.Parameter.empty
      ]
      if len(unfilled_required) == 1:
        bound.arguments[unfilled_required[0]] = payload

  for name, value in list(bound.arguments.items()):
    annotation = signature.parameters[name].annotation
    if annotation is inspect.Signature.empty:
      continue
    adapter = TypeAdapter(annotation)
    try:
      bound.arguments[name] = adapter.validate_python(value)
    except ValidationError as error:
      # An API can JSON-encode an array-typed query param into one wire string
      # (`symbols=["BTCUSDT","ETHUSDT"]`) while the generated method takes a
      # real `list[str]` -- validating the recorded string against that type always
      # fails. Retry once against the parsed value; a genuine mismatch (not just a
      # JSON-encoded one) still surfaces the *original* error, not a `JSONDecodeError`.
      if not isinstance(value, str):
        raise
      try:
        parsed = json.loads(value)
      except json.JSONDecodeError:
        raise error from None
      bound.arguments[name] = adapter.validate_python(parsed)

  return bound


def coerce_example_call(
  fn: Callable[..., Any],
  request: ExampleRequest,
  *,
  identifier: Callable[[str], str] = safe_identifier,
) -> tuple[tuple[Any, ...], dict[str, Any]]:
  """Bind an example's recorded call to a generated method's real signature.

  Args:
    fn: The resolved generated method, e.g. from `resolve_endpoint_function`.
    request: The example whose `args`/`kwargs` (or API-named `parameters`, merged into
      `kwargs` by `ExampleRequest`) drive the call -- or, for a migrated endpoint, its
      single `request` value, taking `payload`'s exact place below:
      `_bind_and_coerce`'s existing decompose-onto-named-kwargs-or-wrap-under-the-sole-
      parameter logic already handles both a flat `request` dict
      and a titled-`anyOf` request's one union-member dict (which never decomposes, so it
      falls straight to the wrap branch) with no further changes needed -- the same dual
      handling `truewire.mock`'s own `spec_example.request.request if ... else
      spec_example.request.payload` already established for the mock server.
    identifier: Rule translating an API parameter name to its Python identifier. Defaults
      to codegen's own default rule; pass `client_identifier(client_root)` to honor a
      project-specific override instead.
  """
  signature = inspect.signature(fn)
  payload = request.request if request.request is not None else request.payload
  bound = _bind_and_coerce(signature, request.args, request.kwargs, payload, identifier)
  return bound.args, bound.kwargs


def coerce_ws_example_call(
  fn: Callable[..., Any],
  request: WsParametersExample,
  *,
  identifier: Callable[[str], str] = safe_identifier,
) -> tuple[tuple[Any, ...], dict[str, Any]]:
  """Bind a WebSocket example's recorded subscribe call to a generated stream method's
  real signature.

  Mirrors `coerce_example_call`, for `WsParametersExample`'s shape instead of
  `ExampleRequest`'s -- a subscribe call is always keyword-shaped (`parameters`/
  `payload`, no `args`/`kwargs`), so there's no positional-args case to carry. See
  `coerce_example_call`/`_bind_and_coerce` for the payload-decompose and type-coercion
  steps, which this shares rather than duplicates.

  Args:
    fn: The resolved generated stream method, e.g. from `resolve_endpoint_function`.
    request: The example's recorded `parameters`/`payload`.
    identifier: Rule translating an API parameter name to its Python identifier. Defaults
      to codegen's own default rule; pass `client_identifier(client_root)` to honor a
      project-specific override instead.
  """
  signature = inspect.signature(fn)
  bound = _bind_and_coerce(signature, (), request.parameters, request.payload, identifier)
  return bound.args, bound.kwargs


async def run_example_request(
  client: Any,
  endpoint: Endpoint,
  request: ExampleRequest,
  *,
  client_root: Path | Project | None = None,
  endpoint_path: Path | None = None,
) -> Any:
  """Replay one recorded example through the real client.

  Args:
    client: The instantiated client to call.
    endpoint: The endpoint spec `request` belongs to.
    request: The recorded example request.
    client_root: Project (or project root), used to resolve `client`'s own naming rule via
      `client_identifier`, and (with `endpoint_path`) to derive `endpoint.function` when
      the endpoint doesn't author one. Omit to use codegen's default rule
      unconditionally and skip derivation.
    endpoint_path: This endpoint's own `endpoint.json`, needed only when `endpoint.function`
      is unset -- see `resolve_endpoint_function`.
  """
  fn = resolve_endpoint_function(
    client, endpoint,
    endpoint_path=endpoint_path,
    spec_root=resolve(client_root).spec_dir if client_root is not None else None,
  )
  identifier = (
    client_identifier(client_root) if client_root is not None else safe_identifier
  )
  args, kwargs = coerce_example_call(fn, request, identifier=identifier)
  return await fn(*args, **kwargs)


def _summarize_statuses(statuses: list[int]) -> dict[str, Any]:
  if not statuses:
    return {
      'count': 0,
      'min': None,
      'max': None,
      'by_status': {},
      'by_family': {},
    }

  by_status = dict(sorted(Counter(statuses).items()))
  by_family = dict(sorted(Counter(f'{status // 100}xx' for status in statuses).items()))
  return {
    'count': len(statuses),
    'min': min(statuses),
    'max': max(statuses),
    'by_status': by_status,
    'by_family': by_family,
  }


def build_response_status_statistics(statuses: list[int]) -> dict[str, dict[str, Any]]:
  """Build response status statistics for all responses and 2xx-only responses."""
  statuses_2xx = [status for status in statuses if 200 <= status < 300]
  return {
    'all': _summarize_statuses(statuses),
    '2xx': _summarize_statuses(statuses_2xx),
  }


def display_response_status_statistics(statuses: list[int]) -> None:
  """Print response status statistics for all responses and 2xx-only responses."""
  stats = build_response_status_statistics(statuses)
  for label in ('all', '2xx'):
    block = stats[label]
    print(f'[{label}] count={block["count"]} min={block["min"]} max={block["max"]}')
    print(f'[{label}] by_family={block["by_family"]}')
    print(f'[{label}] by_status={block["by_status"]}')
