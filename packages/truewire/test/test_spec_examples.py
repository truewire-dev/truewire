"""
Exercise `truewire.cli.spec.examples`'s `--require-verified` gate against a synthetic
fixture, never a real client.

`typed-dev spec examples` has always computed `has_examples`/`is_partial` per endpoint
(`truewire/spec/repo.py`) but never gated on them — its only `Exit(1)` fired on an unknown
client name, so a client could report `0/N` coverage and still exit 0. This suite builds a
minimal client tree under `tmp_path` (never `clients/`, so it stays isolation-safe the same
way `common/lib/test/fixtures/mock_server/` does — see that directory's own fixtures and
`test_mock.py`'s module docstring for the pattern this follows) and checks the transition:
the same coverage report, but `--require-verified` turns a `0/N` result from a passing report
into a failing one.

It also pins the scoping contract of `--path`, which is the other way this gate learned to
report success over nothing. `--path` used to mean "a client root" and only that: given a
subdivision — which is exactly what `build-typed-client` tells an agent to gate on — it
looked for `<subdivision>/spec/endpoints`, found none, and reported `0/0` at exit 0. A path
that did not exist failed cleanly at exit 1, so the failure mode a casual check finds is not
the dangerous one. `--path` now resolves a subdivision to the tree it belongs to, and a path
with no `spec/endpoints` above or below it is an error. The invariant under all of it:
**neither `spec examples` nor `spec test` may exit 0 having examined zero endpoints.**
"""

from pathlib import Path

import pytest
import typer
from pydantic import ValidationError

from truewire.cli.examples import examples
from truewire.cli.check import check as spec_test
from truewire.spec import Endpoint

ENDPOINT_TEMPLATE = {
  'function': 'widgets.get',
  'spec': {
    'kind': 'rpc',
    'transports': ['http'],
    'method': 'GET',
    'path': '/widgets/{id}',
    'openapi': {
      'description': 'Get one widget by id.',
      'parameters': [
        {
          'name': 'id',
          'in': 'path',
          'required': True,
          'description': 'Widget id.',
          'schema': {'type': 'string'},
        }
      ],
      'responses': {
        '200': {
          'description': 'The widget.',
          'content': {
            'application/json': {
              'schema': {
                'title': 'Widget',
                'type': 'object',
                'description': 'One widget.',
                'properties': {
                  'name': {'type': 'string', 'description': 'Widget name.'},
                },
              }
            }
          },
        }
      },
    },
  },
}
"""Minimal, valid `endpoint.json` body shared by every fixture endpoint below."""


def write_unverified_client(root: Path, *, count: int) -> None:
  """Build a synthetic client tree with `count` HTTP endpoints, none carrying examples.

  Each endpoint gets its own `endpoint.json` and no `examples/` directory at all, which is
  exactly the "specced, not excluded" shape `client-spec`'s `SKILL.md` now documents: an
  endpoint that cannot be called from here is still written from the venue's documentation,
  with the `examples/` directory left empty.
  """
  import json

  for index in range(count):
    endpoint_dir = root / 'spec' / 'endpoints' / 'http' / f'widget_{index}'
    endpoint_dir.mkdir(parents=True)
    endpoint = dict(ENDPOINT_TEMPLATE, function=f'widgets.get_{index}')
    (endpoint_dir / 'endpoint.json').write_text(json.dumps(endpoint))


def test_examples_reports_zero_coverage_without_requiring_it(tmp_path, capsys):
  """A client whose endpoints all lack examples still reports `0/N` and exits 0 by default."""
  write_unverified_client(tmp_path, count=3)

  examples(
    path=str(tmp_path),
    verbose=False,
    require_verified=False,
  )

  out = capsys.readouterr().out
  assert 'Coverage (paired examples): 0/3' in out


def test_require_verified_fails_when_unverified_endpoints_remain(tmp_path, capsys):
  """`--require-verified` turns the same `0/N` report into an `Exit(1)`."""
  write_unverified_client(tmp_path, count=3)

  with pytest.raises(typer.Exit) as exc_info:
    examples(
      path=str(tmp_path),
      verbose=False,
      require_verified=True,
    )

  assert exc_info.value.exit_code == 1
  out = capsys.readouterr().out
  assert 'Coverage (paired examples): 0/3' in out


def write_client_with_unverified_endpoints(
  root: Path, *, count: int, unverified: int
) -> None:
  """Build `count` HTTP endpoints with no examples; the first `unverified` declare `Endpoint.unverified`.

  This is the ADR-0001 "built, unverified" outcome: the spec exists, `examples/` doesn't,
  and — for the first `unverified` of them — `endpoint.json` says why, closing the gap
  `write_unverified_client` above predates (that helper's name is now a misnomer: it builds
  endpoints that are merely *uncovered*, not endpoints declared unverifiable).
  """
  import json

  for index in range(count):
    endpoint_dir = root / 'spec' / 'endpoints' / 'http' / f'widget_{index}'
    endpoint_dir.mkdir(parents=True)
    endpoint = dict(ENDPOINT_TEMPLATE, function=f'widgets.get_{index}')
    if index < unverified:
      endpoint['unverified'] = {
        'reason': 'missing_credentials',
        'detail': 'No API key provisioned for this scope.',
      }
    (endpoint_dir / 'endpoint.json').write_text(json.dumps(endpoint))


def test_examples_reports_unverified_endpoints_in_the_default_summary(tmp_path, capsys):
  """Endpoints declared `unverified` show up in the report even without `--require-verified`."""
  write_client_with_unverified_endpoints(tmp_path, count=3, unverified=2)

  examples(
    path=str(tmp_path),
    verbose=False,
    require_verified=False,
  )

  out = capsys.readouterr().out
  assert 'Coverage (paired examples): 0/3' in out
  assert 'Unverified: 2 endpoint(s)' in out


def test_examples_omits_the_unverified_line_when_none_are_declared(tmp_path, capsys):
  """No endpoint declares `unverified` here, so the new report line stays silent."""
  write_unverified_client(tmp_path, count=3)

  examples(
    path=str(tmp_path),
    verbose=False,
    require_verified=False,
  )

  out = capsys.readouterr().out
  assert 'Unverified:' not in out


def test_require_verified_passes_when_every_remaining_endpoint_is_declared_unverified(
  tmp_path, capsys
):
  """The gate this field exists for: every example-less endpoint is excused, so it passes."""
  write_client_with_unverified_endpoints(tmp_path, count=3, unverified=3)

  examples(
    path=str(tmp_path), verbose=False, require_verified=True
  )

  out = capsys.readouterr().out
  assert 'Unverified: 3 endpoint(s)' in out


def test_require_verified_still_fails_for_endpoints_without_examples_or_a_declaration(
  tmp_path, capsys
):
  """A declaration excuses only the endpoints that carry it; the rest still fail the gate."""
  write_client_with_unverified_endpoints(tmp_path, count=3, unverified=2)

  with pytest.raises(typer.Exit) as exc_info:
    examples(
      path=str(tmp_path),
      verbose=False,
      require_verified=True,
    )

  assert exc_info.value.exit_code == 1
  err = capsys.readouterr().err
  assert '1 endpoint(s) without paired examples' in err
  assert '2 marked unverified' in err


def test_an_unverified_endpoints_reason_is_a_closed_set():
  """A reason outside the closed vocabulary is prose again, and prose is what this replaces."""
  with pytest.raises(ValidationError):
    Endpoint.model_validate(
      {
        **ENDPOINT_TEMPLATE,
        'unverified': {'reason': 'because_reasons', 'detail': 'not a real reason'},
      }
    )


def test_an_unverified_endpoint_forbids_unknown_keys():
  """A stray key has to fail loudly, not be silently dropped — same rationale as `PaginationModel`."""
  with pytest.raises(ValidationError):
    Endpoint.model_validate(
      {
        **ENDPOINT_TEMPLATE,
        'unverified': {'reason': 'unsafe', 'detail': 'x', 'bogus': True},
      }
    )


def write_client_with_a_stale_unverified_endpoint(root: Path) -> None:
  """Build one endpoint that declares `unverified` despite having a paired example.

  The declaration has gone stale — someone captured an example after marking it
  unverifiable and never removed the field. This is a spec correctness bug, not a coverage
  gap, so it fails `spec examples` outright rather than only under `--require-verified`.
  """
  import json

  endpoint_dir = root / 'spec' / 'endpoints' / 'http' / 'widget_0'
  endpoint_dir.mkdir(parents=True)
  endpoint = dict(
    ENDPOINT_TEMPLATE,
    function='widgets.get_0',
    unverified={'reason': 'missing_credentials', 'detail': 'stale'},
  )
  (endpoint_dir / 'endpoint.json').write_text(json.dumps(endpoint))
  examples_dir = endpoint_dir / 'examples'
  examples_dir.mkdir()
  (examples_dir / 'a.request.json').write_text(json.dumps({'parameters': {'id': '1'}}))
  (examples_dir / 'a.response.json').write_text(
    json.dumps({'status': 200, 'payload': {'name': 'x'}})
  )


def test_a_stale_unverified_declaration_fails_regardless_of_require_verified(
  tmp_path, capsys
):
  """A captured example makes a lingering `unverified` field wrong, not merely uncounted."""
  write_client_with_a_stale_unverified_endpoint(tmp_path)

  with pytest.raises(typer.Exit) as exc_info:
    examples(
      path=str(tmp_path),
      verbose=False,
      require_verified=False,
    )

  assert exc_info.value.exit_code == 1
  assert 'declare `unverified` despite having' in capsys.readouterr().err


def write_subdivided_client(root: Path, *, subdivisions: dict[str, int]) -> None:
  """Build a synthetic client tree with one named subdivision per entry.

  Args:
    root: Client root to create `spec/endpoints/<subdivision>/` under.
    subdivisions: Subdivision name to the number of endpoints it holds.
  """
  import json

  for name, count in subdivisions.items():
    for index in range(count):
      endpoint_dir = root / 'spec' / 'endpoints' / name / f'widget_{index}'
      endpoint_dir.mkdir(parents=True)
      endpoint = dict(ENDPOINT_TEMPLATE, function=f'{name}.get_{index}')
      (endpoint_dir / 'endpoint.json').write_text(json.dumps(endpoint))


def test_examples_scopes_to_a_subdivision_path(tmp_path, capsys):
  """`--path` pointed at a subdivision counts that subdivision, not the whole client.

  This is the T95 regression. Before the fix this path resolved no endpoints at all and
  the command reported `0/0` at exit 0 — a green gate over an empty set, recorded by the
  agent that ran it as proof the subdivision was clean.
  """
  write_subdivided_client(tmp_path, subdivisions={'market': 3, 'trade': 2})

  examples(
    path=str(tmp_path / 'spec' / 'endpoints' / 'market'),
    verbose=False,
    require_verified=False,
  )

  out = capsys.readouterr().out
  assert 'Scope: market (subdivision)' in out
  assert 'Endpoints: 3' in out
  assert 'Coverage (paired examples): 0/3' in out


def test_examples_refuses_a_path_holding_no_endpoint_tree(tmp_path, capsys):
  """A directory with no `spec/endpoints` above or below it is an error, not a `0/0` pass."""
  (tmp_path / 'spec').mkdir(parents=True)

  with pytest.raises(typer.Exit) as exc_info:
    examples(
      path=str(tmp_path / 'spec'),
      verbose=False,
      require_verified=False,
    )

  assert exc_info.value.exit_code == 1
  assert 'no `endpoints`' in capsys.readouterr().err


def test_examples_refuses_an_empty_endpoint_tree(tmp_path, capsys):
  """An existing but empty `spec/endpoints` fails rather than reporting `0/0` at exit 0."""
  (tmp_path / 'spec' / 'endpoints').mkdir(parents=True)

  with pytest.raises(typer.Exit) as exc_info:
    examples(
      path=str(tmp_path),
      verbose=False,
      require_verified=False,
    )

  assert exc_info.value.exit_code == 1
  assert 'nothing was checked' in capsys.readouterr().err


def test_spec_test_scopes_to_a_subdivision_path(tmp_path, capsys):
  """`spec test --path <subdivision>` audits that subdivision instead of finding nothing."""
  write_subdivided_client(tmp_path, subdivisions={'market': 3, 'trade': 2})

  spec_test(
    path=str(tmp_path / 'spec' / 'endpoints' / 'market'),
    verbose=False,
  )

  out = capsys.readouterr().out
  assert 'Scope: market (subdivision)' in out
  assert 'Endpoints: 3 (rpc=3, stream=0)' in out
  assert 'Spec authoring:' in out


def test_spec_test_refuses_an_empty_endpoint_tree(tmp_path, capsys):
  """`spec test` carries the same invariant: no endpoints examined is never a pass."""
  (tmp_path / 'spec' / 'endpoints').mkdir(parents=True)

  with pytest.raises(typer.Exit) as exc_info:
    spec_test(path=str(tmp_path), verbose=False)

  assert exc_info.value.exit_code == 1
  assert 'nothing was checked' in capsys.readouterr().err


def test_example_coverage_dual_transport_ignores_the_ws_half(tmp_path):
  """
  Pins today's known-incomplete behavior for a dual-transport `rpc` -- not a spec of
  correct behavior to preserve forever.

  `kind: 'rpc', transports: ['http', 'ws']` is representable per the design doc's
  Decision 1 (a future Deribit-style method reachable over both transports), but
  `example_coverage` still branches `if 'http' in transports: ... elif 'ws' in
  transports: ...`, left over from the mechanical `HttpEndpointSpec`/`WsEndpointSpec`
  removal. For a dual-transport endpoint the `http` branch always wins and the `ws`
  pair is invisible to `has_examples`/`is_partial` -- not even flagged as partial. No
  real client declares a dual-transport endpoint yet (see the design doc's
  Non-goals), so this is deliberately not fixed here; this test exists so a future
  change to it is made knowingly, per
  docs/superpowers/specs/2026-08-08-rpc-stream-transports-and-declared-envelope-design.md.
  """
  from truewire.spec import example_coverage

  endpoint_dir = tmp_path / 'spec' / 'endpoints' / 'get_order_state'
  examples_dir = endpoint_dir / 'examples'
  examples_dir.mkdir(parents=True)
  # only a complete WS pair is recorded -- no HTTP request/response files at all.
  (examples_dir / 'a.parameters.json').write_text('{}')
  (examples_dir / 'a.reply.json').write_text('{}')

  endpoint = Endpoint.model_validate(
    {
      'function': 'trading.get_order_state',
      'spec': {
        'kind': 'rpc',
        'transports': ['http', 'ws'],
        'path': '/order',
        'method': 'GET',
        'openapi': {
          'description': 'Dual-transport op.',
          'responses': {'200': {'description': 'ok'}},
        },
      },
    }
  )

  coverage = example_coverage(endpoint_dir / 'endpoint.json', endpoint)

  # the WS pair is still recorded as raw files...
  assert coverage.parameter_files and coverage.reply_files
  # ...but the branch taken (`http`) never looks at them, so they count for nothing.
  assert coverage.has_examples is False
  assert coverage.is_partial is False


def test_example_coverage_stream_endpoint_with_only_an_ack_reply_is_not_covered(tmp_path):
  """A `kind: 'stream'` endpoint whose only recorded pair is `parameters` + `reply` (the
  subscribe/unsubscribe ack) must not read as covered -- no push (`.messages.json`) was ever
  captured. Before this fix, `reply_ids` was ORed in unconditionally regardless of `spec.kind`,
  so this exact shape false-positived as `has_examples=True` on 14 real kucoin stream
  endpoints (e.g. `streams/futures/private/all_orders`), which then tripped `stale_unverified`
  against their correct `unverified: requires_state`/etc. declarations."""
  from truewire.spec import example_coverage

  endpoint_dir = tmp_path / 'spec' / 'endpoints' / 'all_orders'
  examples_dir = endpoint_dir / 'examples'
  examples_dir.mkdir(parents=True)
  (examples_dir / 'a.parameters.json').write_text('{}')
  (examples_dir / 'a.reply.json').write_text('{}')

  endpoint = Endpoint.model_validate(
    {
      'function': 'streams.futures.private.all_orders',
      'spec': {
        'kind': 'stream',
        'channel': 'orders',
        'openapi': {
          'description': 'Order update stream.',
          'responses': {'200': {'description': 'ok'}},
        },
      },
    }
  )

  coverage = example_coverage(endpoint_dir / 'endpoint.json', endpoint)

  assert coverage.parameter_files and coverage.reply_files
  assert coverage.has_examples is False
  assert coverage.is_partial is True


def test_example_coverage_stream_endpoint_with_messages_is_covered(tmp_path):
  """A `kind: 'stream'` endpoint with `parameters` + `messages` recorded is covered -- a real
  push was captured, unaffected by this fix (sanity check)."""
  from truewire.spec import example_coverage

  endpoint_dir = tmp_path / 'spec' / 'endpoints' / 'all_orders'
  examples_dir = endpoint_dir / 'examples'
  examples_dir.mkdir(parents=True)
  (examples_dir / 'a.parameters.json').write_text('{}')
  (examples_dir / 'a.messages.json').write_text('[]')

  endpoint = Endpoint.model_validate(
    {
      'function': 'streams.futures.private.all_orders',
      'spec': {
        'kind': 'stream',
        'channel': 'orders',
        'openapi': {
          'description': 'Order update stream.',
          'responses': {'200': {'description': 'ok'}},
        },
      },
    }
  )

  coverage = example_coverage(endpoint_dir / 'endpoint.json', endpoint)

  assert coverage.has_examples is True
  assert coverage.is_partial is False


def test_example_coverage_ws_rpc_endpoint_with_reply_is_still_covered(tmp_path):
  """A `kind: 'rpc'` (WS command) endpoint with `parameters` + `reply` recorded is still
  covered -- the `rpc` pairing path is unchanged by this fix."""
  from truewire.spec import example_coverage

  endpoint_dir = tmp_path / 'spec' / 'endpoints' / 'get_balance'
  examples_dir = endpoint_dir / 'examples'
  examples_dir.mkdir(parents=True)
  (examples_dir / 'a.parameters.json').write_text('{}')
  (examples_dir / 'a.reply.json').write_text('{}')

  endpoint = Endpoint.model_validate(
    {
      'function': 'account.get_balance',
      'spec': {
        'kind': 'rpc',
        'transports': ['ws'],
        'path': 'get_balance',
        'openapi': {
          'description': 'Get account balance.',
          'responses': {'200': {'description': 'ok'}},
        },
      },
    }
  )

  coverage = example_coverage(endpoint_dir / 'endpoint.json', endpoint)

  assert coverage.has_examples is True
  assert coverage.is_partial is False


def test_spec_test_validates_the_whole_frame_of_an_enveloped_client(capsys):
  """A client with per-endpoint `envelope` declarations validates each recording as the
  wire frame it is, against a response schema that describes the whole frame (ADR 0010);
  `envelope.payload` selects nothing here.

  Covers both an HTTP endpoint (`get_widget`, a `{retCode, retMsg, result, ...}` frame)
  and a WS RPC endpoint (`get_balance`, a JSON-RPC `{jsonrpc, id, result}` reply) under
  the same fixture root, each declaring its own `envelope`.
  """
  fixture_root = Path(__file__).resolve().parent / 'fixtures' / 'mock_server_enveloped'

  spec_test(path=str(fixture_root), verbose=False)

  out = capsys.readouterr().out
  assert 'Example validation:\n  endpoints  2/2\n  files      2\n  errors     0' in out
  assert 'Result: OK' in out


def test_spec_test_validates_the_whole_frame_when_reply_payload_is_empty(capsys):
  """`envelope.reply_payload: ''` (the whole-frame sentinel) validates a flat subscribe ack
  -- kucoin's real `{id, type}` shape -- against its own schema as-is, no field extracted."""
  fixture_root = (
    Path(__file__).resolve().parent / 'fixtures' / 'mock_server_whole_frame_ack'
  )

  spec_test(
    path=str(fixture_root), verbose=False
  )

  out = capsys.readouterr().out
  assert 'Example validation:\n  endpoints  1/1\n  files      1\n  errors     0' in out
  assert 'Result: OK' in out


def write_client_with_one_grpc_endpoint(root: Path) -> None:
  """One `kind: 'grpc'` endpoint with a paired example, alongside one HTTP endpoint.

  Regression fixture for the `KeyError: 'grpc'` both `examples()`'s and `test()`'s
  kind-count dicts raised before they pre-seeded a `'grpc'` key -- `totals`/`kinds`
  were built assuming only `rpc`/`stream` ever appear as `spec.kind`.
  """
  import json

  http_dir = root / 'spec' / 'endpoints' / 'http' / 'widget'
  http_dir.mkdir(parents=True)
  (http_dir / 'endpoint.json').write_text(json.dumps(ENDPOINT_TEMPLATE))

  grpc_dir = root / 'spec' / 'endpoints' / 'chain' / 'bank' / 'balance'
  grpc_examples = grpc_dir / 'examples'
  grpc_examples.mkdir(parents=True)
  grpc_endpoint = {
    'function': 'chain.bank.balance',
    'spec': {
      'kind': 'grpc',
      'service': 'cosmos.bank.v1beta1.Query',
      'rpc': 'Balance',
      'request': 'cosmos.bank.v1beta1.QueryBalanceRequest',
      'response': 'cosmos.bank.v1beta1.QueryBalanceResponse',
      'proto': 'cosmos/bank/v1beta1/query.proto',
      'description': 'Query one denom balance.',
    },
  }
  (grpc_dir / 'endpoint.json').write_text(json.dumps(grpc_endpoint))
  (grpc_examples / '01.request.json').write_text(
    json.dumps({'address': 'dydx1x', 'denom': 'uusdc'})
  )
  (grpc_examples / '01.response.json').write_text(
    json.dumps({'balance': {'denom': 'uusdc', 'amount': '1'}})
  )


def test_examples_counts_a_grpc_endpoint_without_crashing(tmp_path, capsys):
  """`spec examples` used to raise `KeyError: 'grpc'` building its kind-count totals."""
  write_client_with_one_grpc_endpoint(tmp_path)

  examples(path=str(tmp_path), verbose=False, require_verified=False)

  out = capsys.readouterr().out
  assert 'Endpoints: 2 (rpc=1, stream=0, grpc=1)' in out
  # the grpc endpoint's request/response pair counts; the HTTP widget has no examples at all.
  assert 'Coverage (paired examples): 1/2' in out


def test_spec_test_counts_a_grpc_endpoint_without_crashing(tmp_path, capsys):
  """`spec test` used to raise `KeyError: 'grpc'` building its kind-count summary."""
  write_client_with_one_grpc_endpoint(tmp_path)

  spec_test(path=str(tmp_path), verbose=False)

  out = capsys.readouterr().out
  assert 'Endpoints: 2 (rpc=1, stream=0, grpc=1)' in out
  # a grpc endpoint has no openapi response schema to validate against (design doc
  # Decision 1), and the HTTP widget has no examples at all here -- zero endpoints
  # reach schema validation, which is the correct outcome, not a crash.
  assert 'Example validation:\n  endpoints  0/2\n  files      0\n  errors     0' in out
  assert 'Result: OK' in out
