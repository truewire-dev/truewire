"""`seek.exclusive` (ADR 0013) walked end to end in every backend, against `truewire mock`.

The `seek_exclusive` fixture records the three pages a walk over `[1000, 5000]` with
`limit: 2` requests from aster's `userTrades` shape: the time range on the first request,
`fromId` alone after that, and a third page holding a trade past `endTime`. Each test
generates one language into a copy of the fixture, adds a core small enough to read at a
glance, and walks `trades.user_trades_paged` against the mock, which records every query it
answers. Two things are asserted, per backend:

- `startTime`/`endTime` are absent from requests 2 and 3. The mock matches a recording's
  query exactly, so a request still carrying them would fail the walk as well.
- trade 4, past `endTime`, is not returned, and the walk stops on the page that held it:
  that page is full, and the mock has no fourth page to answer.

The generated Rust must also pass `cargo fmt --check` and `clippy -D warnings`, and the Go
`gofmt` and `go vet`. A backend whose toolchain is missing (Node 22.7+, cargo, go) skips its
test.

Each backend then resumes: `fromId` 2 with `endTime` walks the last two recordings (no
request carries `endTime`, and trade 4 is still dropped), and `fromId` beside `startTime`,
or neither `fromId` nor `startTime`, is refused before any request is sent.
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

import pytest
from typer.testing import CliRunner
from typing_extensions import Iterator

from truewire.cli import app
from truewire.mock import build_server

FIXTURE = Path(__file__).parent / 'fixtures' / 'seek_exclusive'
REPO = Path(__file__).parents[3]

WALKED = [
  {'symbol': 'BTCUSDT', 'startTime': '1000', 'endTime': '5000', 'limit': '2'},
  {'symbol': 'BTCUSDT', 'fromId': '2', 'limit': '2'},
  {'symbol': 'BTCUSDT', 'fromId': '3', 'limit': '2'},
]
"""The queries the walk sends, in order: the range once, then the id cursor alone."""

RESUMED = WALKED[1:]
"""The queries a walk resumed from `fromId` 2 with `endTime` sends: never `endTime`."""


@contextmanager
def recording_mock() -> Iterator[tuple[str, list[dict[str, str]]]]:
  """`truewire mock` over the fixture, recording every request's query; yields the base
  URL and the list the queries land in."""
  server = build_server(FIXTURE)
  queries: list[dict[str, str]] = []
  handler = server.RequestHandlerClass
  handle = handler._handle  # type: ignore[attr-defined]

  def recorded(request) -> None:
    queries.append(dict(parse_qsl(urlparse(request.path).query)))
    handle(request)

  handler._handle = recorded  # type: ignore[attr-defined]
  thread = threading.Thread(target=server.serve_forever, daemon=True)
  thread.start()
  host, port = server.server_address[:2]
  try:
    yield f'http://{host}:{port}', queries
  finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def generated(tmp_path: Path, language: str) -> Path:
  """A copy of the fixture with `language` generated into it."""
  root = tmp_path / 'venue'
  shutil.copytree(FIXTURE, root)
  result = CliRunner().invoke(app, ['generate', language, '--project', str(root)])
  assert result.exit_code == 0, result.output
  return root


def assert_walked(ids: list[int], resumed: list[int], queries: list[dict[str, str]]) -> None:
  """Three pages, the range on the first only, then the resumed walk's two, which never
  carry `endTime`; trade 4 (past `endTime`) returned by neither. The refused calls sent
  nothing."""
  assert ids == [1, 2, 3]
  assert resumed == [2, 3]
  assert queries == [*WALKED, *RESUMED]
  assert all('startTime' not in query and 'endTime' not in query for query in queries[1:])


def test_the_plan_carries_the_declaration_as_a_seek_decision():
  """`truewire plan --json` shows `exclusive` in the language-neutral `seek` decision."""
  result = CliRunner().invoke(app, ['plan', '--project', str(FIXTURE), '--json'])
  assert result.exit_code == 0, result.output
  plan = json.loads(result.output)
  [endpoint] = plan['endpoints']
  assert endpoint['pagination']['seek']['exclusive'] == {
    'parameters': ['startTime', 'endTime'],
    'first': 'startTime',
    'far': {'parameter': 'endTime', 'field': '[-1].time'},
  }


@pytest.mark.parametrize(('language', 'path', 'moving', 'names', 'field', 'far'), [
  ('python', 'src/venue/trades/user_trades.py', 'from_id', ('start_time', 'end_time'), 'time', 'end_time'),
  ('typescript', 'src/venue/trades/user_trades.ts', 'fromId', ('startTime', 'endTime'), 'time', 'endTime'),
  ('rust', 'src/venue/trades/user_trades.rs', 'fromId', ('startTime', 'endTime'), 'time', 'endTime'),
  ('go', 'src/venue/trades/usertrades/usertrades.go', 'fromId', ('startTime', 'endTime'), 'time', 'endTime'),
])
def test_every_backend_documents_the_first_request_and_the_far_bound(
  tmp_path: Path, language: str, path: str, moving: str, names: tuple[str, str], field: str, far: str,
):
  """The walker's doc says what the caller may pass together and where the far bound is
  kept, in the language's own parameter names."""
  source = ' '.join(re.sub(r'^\s*(///|//|\*|#)', '', line) for line in (generated(tmp_path, language) / path).read_text().splitlines())
  text = ' '.join(source.split())
  start, end = names
  assert (
    f'Drops every row whose `{field}` is past the caller\'s own `{far}`, and ends the walk on the '
    f'page that held one. The venue refuses `{start}`/`{end}` alongside `{moving}`, so they are '
    f'sent on the first request only, and never when the caller gives `{moving}`; pass `{start}` '
    f'or `{moving}`, not both. Without `{moving}`, `{start}` is required.'
  ) in text
  assert 'the first row whose' not in text


# -- Python ---------------------------------------------------------------------------

PYTHON_CORE = '''"""The fixture's core: every call a GET, the dumped request its query."""
import json
from dataclasses import dataclass, field
from typing import Any

from truewire_core.http import HttpClient
from truewire_core.validation import validator


@dataclass(kw_only=True)
class Transport:
  base_url: str
  http: HttpClient = field(default_factory=HttpClient)


@dataclass(kw_only=True, frozen=True)
class Endpoint:
  client: Transport

  async def request(
    self, request: Any = None, *, method: str, path: str, validate: bool | None = None,
    request_type: Any = None, response_type: Any = None, meta: Any = None,
  ) -> Any:
    params = json.loads(validator(request_type).dump(request))
    response = await self.client.http.request(method, self.client.base_url + path, params=params)
    assert response.status_code == 200, response.text
    return validator(response_type).json(response.content)
'''


def test_python_sends_the_range_first_and_drops_the_trade_past_it(tmp_path: Path):
  root = generated(tmp_path, 'python')
  (root / 'src' / 'venue' / 'core.py').write_text(PYTHON_CORE)
  sys.path.insert(0, str(root / 'src'))
  try:
    from venue.core import Transport  # type: ignore[import-not-found]
    from venue.main import Venue  # type: ignore[import-not-found]

    def at(ms: int) -> datetime:
      return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)

    with recording_mock() as (base_url, queries):
      client = Venue(client=Transport(base_url=base_url))

      async def walk() -> list:
        return await client.trades.user_trades_paged('BTCUSDT', start_time=at(1000), end_time=at(5000), limit=2)

      async def resume() -> list:
        return await client.trades.user_trades_paged('BTCUSDT', from_id=2, end_time=at(5000), limit=2)

      trades = asyncio.run(walk())
      resumed = asyncio.run(resume())
      with pytest.raises(ValueError, match='refuses alongside `start_time`'):
        client.trades.user_trades_paged('BTCUSDT', from_id=2, start_time=at(1000))
      with pytest.raises(ValueError, match='needs `from_id` or `start_time`'):
        client.trades.user_trades_paged('BTCUSDT', end_time=at(5000))
      assert_walked([trade['id'] for trade in trades], [trade['id'] for trade in resumed], queries)
  finally:
    sys.path.remove(str(root / 'src'))
    for name in [name for name in sys.modules if name == 'venue' or name.startswith('venue.')]:
      del sys.modules[name]


# -- TypeScript -----------------------------------------------------------------------

TS_HOOKS = '''// Run the generated sources with Node's own type stripping: `@truewire/core` from the
// repository's source, and a `.js` specifier to the `.ts` file beside it.
import { existsSync } from 'node:fs'
import { fileURLToPath, pathToFileURL } from 'node:url'

export async function resolve(specifier, context, next) {
  if (specifier === '@truewire/core') return { url: pathToFileURL(process.env.TRUEWIRE_CORE_TS).href, shortCircuit: true }
  if (specifier.endsWith('.js') && context.parentURL?.startsWith('file:')) {
    const source = new URL(specifier.replace(/\\.js$/, '.ts'), context.parentURL)
    if (existsSync(fileURLToPath(source))) return { url: source.href, shortCircuit: true }
  }
  return next(specifier, context)
}
'''

TS_REGISTER = '''import { register } from 'node:module'
register('./hooks.mjs', import.meta.url)
'''

TS_WALK = '''import { HttpClient, parseJson, type HttpCall, type HttpEndpoint } from '@truewire/core'
import { Venue } from './src/venue/index.js'

/** The fixture's core: every call a GET, the dumped request its query. */
const core: HttpEndpoint = {
  async request<Req, Res>(call: HttpCall<Req, Res, Record<string, never>>): Promise<Res> {
    const query = call.requestCodec!.dump(call.request!) as Record<string, string | number>
    const response = await new HttpClient().request('GET', `${process.env.BASE_URL}${call.path}`, { query })
    const text = await response.text()
    if (response.status !== 200) throw new Error(text)
    return parseJson(call.responseCodec!, text)
  },
}

const client = new Venue(core)
const trades = await client.trades.userTradesPaged({ symbol: 'BTCUSDT', startTime: new Date(1000), endTime: new Date(5000), limit: 2 })
const resumed = await client.trades.userTradesPaged({ symbol: 'BTCUSDT', fromId: 2, endTime: new Date(5000), limit: 2 })
const refuses = (request: Parameters<typeof client.trades.userTradesPaged>[0]) => {
  try {
    client.trades.userTradesPaged(request)
    return false
  } catch (error) {
    return error instanceof TypeError
  }
}
const refused = [
  refuses({ symbol: 'BTCUSDT', fromId: 2, startTime: new Date(1000) }),
  refuses({ symbol: 'BTCUSDT', endTime: new Date(5000) }),
]
// The first request sends the clamped size too: `limit: 1` goes out as 2, the recorded walk.
const clamped = await client.trades.userTradesPaged({ symbol: 'BTCUSDT', startTime: new Date(1000), endTime: new Date(5000), limit: 1 })
console.log(JSON.stringify({
  ids: trades.map(trade => trade.id), resumed: resumed.map(trade => trade.id), refused,
  clamped: clamped.map(trade => trade.id),
}))
'''


def _node() -> str | None:
  """A Node that runs TypeScript with `--experimental-transform-types` (22.7 and later)."""
  node = shutil.which('node')
  if node is None:
    return None
  version = subprocess.run([node, '--version'], capture_output=True, text=True).stdout.strip()
  match = re.match(r'v(\d+)\.(\d+)', version)
  if match is None or (int(match[1]), int(match[2])) < (22, 7):
    return None
  return node


def test_typescript_sends_the_range_first_and_drops_the_trade_past_it(tmp_path: Path):
  node = _node()
  if node is None:
    pytest.skip('needs Node 22.7 or later')
  root = generated(tmp_path, 'typescript')
  (root / 'package.json').write_text('{"type": "module"}\n')
  (root / 'hooks.mjs').write_text(TS_HOOKS)
  (root / 'register.mjs').write_text(TS_REGISTER)
  (root / 'walk.ts').write_text(TS_WALK)
  with recording_mock() as (base_url, queries):
    env = {**os.environ, 'BASE_URL': base_url, 'TRUEWIRE_CORE_TS': str(REPO / 'packages' / 'core-ts' / 'src' / 'index.ts')}
    ran = subprocess.run(
      [node, '--experimental-transform-types', '--no-warnings', '--import', './register.mjs', 'walk.ts'],
      cwd=root, capture_output=True, text=True, env=env, timeout=120,
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr
    out = json.loads(ran.stdout.strip().splitlines()[-1])
    assert out['refused'] == [True, True]
    assert_walked(out['ids'], out['resumed'], queries[:len(WALKED) + len(RESUMED)])
    assert out['clamped'] == [1, 2, 3]
    assert queries[len(WALKED) + len(RESUMED):] == WALKED


def test_typescript_binds_an_exclusive_parameter_named_like_a_walker_local(tmp_path: Path):
  """An exclusive parameter named `size` is bound beside the walker's own `size` (the
  clamped page size), not over it: Node refuses a module declaring `size` twice."""
  node = _node()
  if node is None:
    pytest.skip('needs Node 22.7 or later')
  root = tmp_path / 'venue'
  shutil.copytree(FIXTURE, root)
  path = root / 'spec' / 'endpoints' / 'trades' / 'user_trades' / 'endpoint.json'
  endpoint = json.loads(path.read_text())
  properties = endpoint['spec']['request']['properties']
  properties['size'] = properties.pop('startTime')
  endpoint['pagination']['exclusive'].update(parameters=['size', 'endTime'], first='size')
  path.write_text(json.dumps(endpoint))
  result = CliRunner().invoke(app, ['generate', 'typescript', '--project', str(root)])
  assert result.exit_code == 0, result.output
  (root / 'package.json').write_text('{"type": "module"}\n')
  (root / 'hooks.mjs').write_text(TS_HOOKS)
  (root / 'register.mjs').write_text(TS_REGISTER)
  (root / 'load.ts').write_text("import './src/venue/trades/user_trades.js'\n")
  env = {**os.environ, 'TRUEWIRE_CORE_TS': str(REPO / 'packages' / 'core-ts' / 'src' / 'index.ts')}
  ran = subprocess.run(
    [node, '--experimental-transform-types', '--no-warnings', '--import', './register.mjs', 'load.ts'],
    cwd=root, capture_output=True, text=True, env=env, timeout=120,
  )
  assert ran.returncode == 0, ran.stdout + ran.stderr


# -- Rust -----------------------------------------------------------------------------

CARGO_TOML = '''[package]
name = "venue"
version = "0.1.0"
edition = "2021"
publish = false

[lib]
path = "src/venue/lib.rs"

[dependencies]
serde = {{ version = "1", features = ["derive"] }}
truewire-core = {{ path = "{crates}/truewire-core" }}

[dev-dependencies]
async-trait = "0.1"
tokio = {{ version = "1", features = ["macros", "rt-multi-thread"] }}
'''

RUST_WALK = '''//! `trades.user_trades_paged` against `truewire mock` at `BASE_URL`.

use async_trait::async_trait;
use truewire_core::http::{query_from, RequestOptions};
use truewire_core::serde_json::{self, json, Value};
use truewire_core::{Error, HttpCall, HttpClient, HttpEndpoint, Result, TimestampMillis};
use venue::trades::user_trades::UserTradesPagedRequest;
use venue::{CallOptions, Venue};

/// The fixture's core: every call a GET, the dumped request its query.
struct Core {
    base_url: String,
    http: HttpClient,
}

#[async_trait]
impl HttpEndpoint for Core {
    async fn request(&self, call: HttpCall<'_, ()>) -> Result<Value> {
        let params = match call.request {
            Some(Value::Object(fields)) => fields.into_iter().collect(),
            _ => Vec::new(),
        };
        let url = format!("{}{}", self.base_url, call.path);
        let options = RequestOptions::new().query(query_from(params));
        let response = self.http.request("GET", &url, options).await?;
        if response.status != 200 {
            return Err(Error::api(response.text()));
        }
        response.json()
    }
}

fn at(millis: i64) -> TimestampMillis {
    serde_json::from_value(json!(millis)).unwrap()
}

#[tokio::test]
async fn walks_three_pages_and_drops_the_trade_past_end_time() {
    let base_url = std::env::var("BASE_URL").expect("BASE_URL");
    let client = Venue::from_core(Core {
        base_url,
        http: HttpClient::default(),
    });
    let request = UserTradesPagedRequest {
        symbol: "BTCUSDT".to_string(),
        start_time: Some(at(1000)),
        end_time: Some(at(5000)),
        limit: Some(2),
        ..UserTradesPagedRequest::default()
    };
    let trades = client
        .trades
        .user_trades_paged(request, CallOptions::default())
        .await
        .unwrap();
    let ids: Vec<i64> = trades.iter().map(|trade| trade.id).collect();
    println!("ids {}", json!(ids));

    let resume = UserTradesPagedRequest {
        symbol: "BTCUSDT".to_string(),
        from_id: Some(2),
        end_time: Some(at(5000)),
        limit: Some(2),
        ..UserTradesPagedRequest::default()
    };
    let resumed = client
        .trades
        .user_trades_paged(resume, CallOptions::default())
        .await
        .unwrap();
    let resumed: Vec<i64> = resumed.iter().map(|trade| trade.id).collect();
    println!("resumed {}", json!(resumed));

    let both = UserTradesPagedRequest {
        symbol: "BTCUSDT".to_string(),
        from_id: Some(2),
        start_time: Some(at(1000)),
        ..UserTradesPagedRequest::default()
    };
    let neither = UserTradesPagedRequest {
        symbol: "BTCUSDT".to_string(),
        end_time: Some(at(5000)),
        ..UserTradesPagedRequest::default()
    };
    for request in [both, neither] {
        let refused = client
            .trades
            .user_trades_paged(request, CallOptions::default())
            .await;
        assert!(matches!(refused, Err(Error::Logic(_))), "{refused:?}");
    }
}
'''


def _cargo() -> str | None:
  cargo = shutil.which('cargo') or str(Path.home() / '.cargo' / 'bin' / 'cargo')
  return cargo if Path(cargo).is_file() else None


def test_rust_sends_the_range_first_and_drops_the_trade_past_it(tmp_path: Path):
  cargo = _cargo()
  if cargo is None:
    pytest.skip('needs cargo')
  root = generated(tmp_path, 'rust')
  (root / 'Cargo.toml').write_text(CARGO_TOML.format(crates=REPO / 'crates'))
  (root / 'src' / 'venue' / 'core.rs').write_text('//! No hand-written core: the test brings its own.\n')
  (root / 'tests').mkdir()
  (root / 'tests' / 'walk.rs').write_text(RUST_WALK)
  env = {**os.environ, 'CARGO_TARGET_DIR': os.environ.get('TRUEWIRE_CARGO_TARGET_DIR', str(tmp_path / 'target'))}
  formatted = subprocess.run([cargo, 'fmt', '--check'], cwd=root, capture_output=True, text=True, env=env)
  assert formatted.returncode == 0, formatted.stdout + formatted.stderr
  linted = subprocess.run(
    [cargo, 'clippy', '--all-targets', '--', '-D', 'warnings'], cwd=root, capture_output=True, text=True, env=env, timeout=1800,
  )
  assert linted.returncode == 0, linted.stderr[-6000:]
  with recording_mock() as (base_url, queries):
    tested = subprocess.run(
      [cargo, 'test', '--', '--nocapture'], cwd=root, capture_output=True, text=True,
      env={**env, 'BASE_URL': base_url}, timeout=1800,
    )
    assert tested.returncode == 0, tested.stdout[-6000:] + tested.stderr[-6000:]
    ids = json.loads(re.search(r'^ids (.*)$', tested.stdout, re.M)[1])  # type: ignore[index]
    resumed = json.loads(re.search(r'^resumed (.*)$', tested.stdout, re.M)[1])  # type: ignore[index]
    assert_walked(ids, resumed, queries)


# -- Go -------------------------------------------------------------------------------

GO_WALK = '''package tests

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"testing"
	"time"

	truewire "truewire.dev/core"

	venue "truewire.dev/fixtures/venue/src/venue"
	"truewire.dev/fixtures/venue/src/venue/trades/usertrades"
)

// core is the fixture's core: every call a GET, the dumped request its query.
type core struct {
	baseURL string
	http    *truewire.HttpClient
}

func (c core) Request(ctx context.Context, call truewire.HttpCall) (json.RawMessage, error) {
	object, err := truewire.Object(call.Request)
	if err != nil {
		return nil, err
	}
	response, err := c.http.Request(ctx, "GET", c.baseURL+call.Path, truewire.RequestOptions{Query: truewire.QueryFrom(object)})
	if err != nil {
		return nil, err
	}
	if response.Status != 200 {
		return nil, fmt.Errorf("HTTP %d: %s", response.Status, response.Text())
	}
	return response.JSON()
}

func at(millis int64) *truewire.TimestampMillis {
	return &truewire.TimestampMillis{Time: time.UnixMilli(millis).UTC()}
}

func TestWalksThreePagesAndDropsTheTradePastEndTime(t *testing.T) {
	client := venue.FromCore(core{baseURL: os.Getenv("BASE_URL"), http: &truewire.HttpClient{}})
	request := usertrades.Request{Symbol: "BTCUSDT", StartTime: at(1000), EndTime: at(5000), Limit: truewire.Ptr(int64(2))}
	trades, err := client.Trades.UserTradesPaged(request).All(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	fmt.Printf("ids %s\\n", idsOf(trades))

	resume := usertrades.Request{Symbol: "BTCUSDT", FromID: truewire.Ptr(int64(2)), EndTime: at(5000), Limit: truewire.Ptr(int64(2))}
	resumed, err := client.Trades.UserTradesPaged(resume).All(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	fmt.Printf("resumed %s\\n", idsOf(resumed))

	both := usertrades.Request{Symbol: "BTCUSDT", FromID: truewire.Ptr(int64(2)), StartTime: at(1000)}
	neither := usertrades.Request{Symbol: "BTCUSDT", EndTime: at(5000)}
	for _, request := range []usertrades.Request{both, neither} {
		if _, err := client.Trades.UserTradesPaged(request).All(context.Background()); !errors.Is(err, truewire.ErrLogic) {
			t.Fatalf("not refused as a LogicError: %v", err)
		}
	}
}

func idsOf(trades []usertrades.Trade) []byte {
	ids := []int64{}
	for _, trade := range trades {
		ids = append(ids, trade.ID)
	}
	out, _ := json.Marshal(ids)
	return out
}
'''


def _go() -> str | None:
  for candidate in (shutil.which('go'), str(Path.home() / '.local' / 'go' / 'bin' / 'go')):
    if candidate and Path(candidate).is_file():
      return candidate
  return None


def test_go_sends_the_range_first_and_drops_the_trade_past_it(tmp_path: Path):
  go = _go()
  if go is None:
    pytest.skip('needs a Go toolchain')
  root = generated(tmp_path, 'go')
  (root / 'go.mod').write_text(
    'module truewire.dev/fixtures/venue\n\ngo 1.23\n\nrequire truewire.dev/core v0.0.0\n\n'
    f'replace truewire.dev/core => {REPO / "packages" / "core-go"}\n'
  )
  (root / 'tests').mkdir()
  (root / 'tests' / 'walk_test.go').write_text(GO_WALK)
  env = {**os.environ, 'GOFLAGS': '-mod=mod'}
  subprocess.run([go, 'mod', 'tidy'], cwd=root, capture_output=True, text=True, env=env)
  gofmt = Path(go).with_name('gofmt')
  formatted = subprocess.run([str(gofmt), '-l', 'src', 'tests'], cwd=root, capture_output=True, text=True)
  assert formatted.stdout == '', formatted.stdout
  vetted = subprocess.run([go, 'vet', './...'], cwd=root, capture_output=True, text=True, env=env, timeout=600)
  assert vetted.returncode == 0, vetted.stdout + vetted.stderr
  with recording_mock() as (base_url, queries):
    tested = subprocess.run(
      [go, 'test', '-count=1', '-v', './...'], cwd=root, capture_output=True, text=True,
      env={**env, 'BASE_URL': base_url}, timeout=600,
    )
    assert tested.returncode == 0, tested.stdout + tested.stderr
    ids = json.loads(re.search(r'^ids (.*)$', tested.stdout, re.M)[1])  # type: ignore[index]
    resumed = json.loads(re.search(r'^resumed (.*)$', tested.stdout, re.M)[1])  # type: ignore[index]
    assert_walked(ids, resumed, queries)
