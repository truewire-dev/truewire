"""A `seek` cursor read through the row field's own timestamp format, not the bound's
(TRU-197), walked end to end in every backend against `truewire mock`.

lighter's `markets.fundings` is the real case: a row's `timestamp` is `epoch-seconds` and the
moving bound `end_timestamp` is `epoch-millis`. The walk must read `1783616400` off a row as
seconds and send the next page's `end_timestamp` as `1783616400000`. Read through the bound's
converter instead, the same value is taken as milliseconds (a date in January 1970), which is
what an unvalidated Python or TypeScript walk did: validation had not turned the row value
into a time yet, so the walk parsed the raw integer through the bound's converter. Rust's
walk did the same on every call, because it reads the cursor back through the row's wire form.

The project is `fixture_project`'s, with all four backends declared and one `markets.fundings`
endpoint added: three recorded pages of a walk backwards over one range, capped at 3 rows.
The mock matches a recording's query exactly, so a page requested with the wrong bound fails
the walk, and the queries it answered are asserted besides.
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

from test_score import fixture_project
from truewire.cli import app
from truewire.mock import build_server

REPO = Path(__file__).parents[3]

BACKENDS = '''
[typescript]
package = "forecasts"
src = "ts"
name = "Forecasts"

[rust]
package = "forecasts"
src = "rs"
name = "Forecasts"

[go]
package = "forecasts"
src = "go"
name = "Forecasts"
module = "example.com/forecasts"
root = "go"
'''

START = 1783600000000
"""The caller's far bound, `start_timestamp`, in milliseconds."""

PAGES = [
  (1783700000000, [1783616400, 1783620000, 1783623600]),
  (1783616400000, [1783609200, 1783612800, 1783616400]),
  (1783609200000, [1783605600]),
]
"""Each request's `end_timestamp` (milliseconds) and the row `timestamp`s (seconds) it gets
back. Every page after the first is requested at the earliest row of the one before, times
1000; the second re-serves that row, which the walk drops."""

SENT = [str(end) for end, _ in PAGES]
"""The `end_timestamp` of each request, as the query carries it."""

ROWS = [1783616400, 1783620000, 1783623600, 1783609200, 1783612800, 1783605600]
"""Every row's `timestamp` the walk yields, in seconds: page by page, each once."""


def millis(description: str) -> dict:
  return {'type': 'integer', 'format': 'epoch-millis', 'description': description}


def fundings_project(root: Path) -> Path:
  """`fixture_project` with every backend declared and a `markets.fundings` seek endpoint."""
  project = fixture_project(root, extra_toml=BACKENDS)
  group = project / 'spec' / 'endpoints' / 'markets'
  (group / 'fundings' / 'examples').mkdir(parents=True)
  (group / 'router.json').write_text(json.dumps({
    'description': 'Markets: fundings.', 'upstream': 'https://example.com/docs/markets', 'core': 'default',
  }))
  (group / 'fundings' / 'endpoint.json').write_text(json.dumps({
    'docs': 'https://example.com/docs/fundings',
    'meta': {'public': True},
    'spec': {
      'kind': 'rpc', 'transports': ['http'], 'path': '/fundings', 'method': 'GET',
      'description': 'Get the funding payments of a market over a time range, the newest ones first.',
      'request': {
        'title': 'FundingsRequest', 'type': 'object', 'description': 'Which market and range.',
        'required': ['market_id', 'start_timestamp', 'end_timestamp'],
        'properties': {
          'market_id': {'type': 'integer', 'description': 'Market id.'},
          'start_timestamp': millis('Start of the range.'),
          'end_timestamp': millis('End of the range.'),
        },
      },
      'response': {
        'title': 'Fundings', 'type': 'object', 'description': 'Funding payments.',
        'required': ['fundings'],
        'properties': {'fundings': {
          'type': 'array', 'description': 'Fundings, oldest first.',
          'items': {
            'title': 'Funding', 'type': 'object', 'description': 'One funding payment.',
            'required': ['timestamp', 'rate'],
            'properties': {
              'timestamp': {'type': 'integer', 'format': 'epoch-seconds', 'description': 'Funding time.'},
              'rate': {'type': 'string', 'description': 'Funding rate.'},
            },
          },
        }},
      },
    },
    'pagination': {
      'strategy': 'seek', 'cursor': {'field': '[-1].timestamp', 'unique': True},
      'bound': {'start': 'start_timestamp', 'end': 'end_timestamp'}, 'anchor': 'end',
      'cap': 3, 'rows': 'fundings',
    },
  }))
  for number, (end, stamps) in enumerate(PAGES, 1):
    examples = group / 'fundings' / 'examples'
    (examples / f'{number:02}.request.json').write_text(json.dumps({
      'parameters': {'market_id': 1, 'start_timestamp': START, 'end_timestamp': end},
    }))
    (examples / f'{number:02}.response.json').write_text(json.dumps({
      'status': 200, 'payload': {'fundings': [{'timestamp': stamp, 'rate': '0.0001'} for stamp in stamps]},
    }))
  return project


@pytest.fixture(scope='module')
def spec(tmp_path_factory: pytest.TempPathFactory) -> Path:
  """The project, spec only: each test generates its language into a copy."""
  return fundings_project(tmp_path_factory.mktemp('fundings'))


def generated(spec: Path, tmp_path: Path, language: str) -> Path:
  """A copy of the project with `language` generated into it."""
  root = tmp_path / 'forecasts'
  shutil.copytree(spec, root)
  result = CliRunner().invoke(app, ['generate', language, '--project', str(root)])
  assert result.exit_code == 0, result.output
  return root


@contextmanager
def recording_mock(project: Path) -> Iterator[tuple[str, list[str | None]]]:
  """`truewire mock` over the project; yields the base URL and the list every answered
  request's `end_timestamp` lands in."""
  server = build_server(project)
  sent: list[str | None] = []
  handler = server.RequestHandlerClass
  handle = handler._handle  # type: ignore[attr-defined]

  def recorded(request) -> None:
    sent.append(dict(parse_qsl(urlparse(request.path).query)).get('end_timestamp'))
    handle(request)

  handler._handle = recorded  # type: ignore[attr-defined]
  thread = threading.Thread(target=server.serve_forever, daemon=True)
  thread.start()
  host, port = server.server_address[:2]
  try:
    yield f'http://{host}:{port}', sent
  finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_check_is_clean(spec: Path):
  """A seconds cursor under a millis bound is a declaration the walk now converts."""
  result = CliRunner().invoke(app, ['check', '--project', str(spec)])
  assert result.exit_code == 0, result.output
  assert 'Result: OK' in result.output


# -- Python ---------------------------------------------------------------------------

@pytest.mark.parametrize('validate', [True, False])
def test_python_sends_the_earliest_row_seconds_as_millis(spec: Path, tmp_path: Path, validate: bool):
  root = generated(spec, tmp_path, 'python')
  sys.path.insert(0, str(root / 'src'))
  try:
    from forecasts.main import Forecasts  # type: ignore[import-not-found]

    def at(ms: int) -> datetime:
      return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)

    with recording_mock(root) as (base_url, sent):
      client = Forecasts.new(base_url=base_url)

      async def walk() -> list:
        return await client.markets.fundings_paged(
          1, start_timestamp=at(START), end_timestamp=at(PAGES[0][0]), validate=validate,
        )

      try:
        rows = asyncio.run(walk())
      finally:
        assert sent == SENT
    stamps = [row['timestamp'] for row in rows]
    if validate:
      stamps = [int(stamp.timestamp()) for stamp in stamps]
    assert stamps == ROWS
  finally:
    sys.path.remove(str(root / 'src'))
    for name in [name for name in sys.modules if name == 'forecasts' or name.startswith('forecasts.')]:
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
import { Forecasts } from './ts/forecasts/index.js'

/** A core over `BASE_URL`: every call a GET, the dumped request its query, validated unless told not to. */
const core: HttpEndpoint<{ public?: boolean }> = {
  async request<Req, Res>(call: HttpCall<Req, Res, { public?: boolean }>): Promise<Res> {
    const query = call.requestCodec!.dump(call.request!) as Record<string, string | number>
    const response = await new HttpClient().request('GET', `${process.env.BASE_URL}${call.path}`, { query })
    const text = await response.text()
    if (response.status !== 200) throw new Error(text)
    return call.validate === false ? JSON.parse(text) : parseJson(call.responseCodec!, text)
  },
}

const validate = process.env.VALIDATE === 'true'
const client = new Forecasts(core)
const rows = await client.markets.fundingsPaged(
  { market_id: 1, start_timestamp: new Date(START), end_timestamp: new Date(END) }, { validate },
) as { timestamp: Date | number }[]
console.log(JSON.stringify(rows.map(row => row.timestamp instanceof Date ? row.timestamp.getTime() / 1000 : row.timestamp)))
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


@pytest.mark.parametrize('validate', [True, False])
def test_typescript_sends_the_earliest_row_seconds_as_millis(spec: Path, tmp_path: Path, validate: bool):
  node = _node()
  if node is None:
    pytest.skip('needs Node 22.7 or later')
  root = generated(spec, tmp_path, 'typescript')
  (root / 'package.json').write_text('{"type": "module"}\n')
  (root / 'hooks.mjs').write_text(TS_HOOKS)
  (root / 'register.mjs').write_text(TS_REGISTER)
  (root / 'walk.ts').write_text(TS_WALK.replace('START', str(START)).replace('END', str(PAGES[0][0])))
  with recording_mock(root) as (base_url, sent):
    env = {
      **os.environ, 'BASE_URL': base_url, 'VALIDATE': str(validate).lower(),
      'TRUEWIRE_CORE_TS': str(REPO / 'packages' / 'core-ts' / 'src' / 'index.ts'),
    }
    ran = subprocess.run(
      [node, '--experimental-transform-types', '--no-warnings', '--import', './register.mjs', 'walk.ts'],
      cwd=root, capture_output=True, text=True, env=env, timeout=120,
    )
    assert sent == SENT, ran.stderr[-2000:]
  assert ran.returncode == 0, ran.stdout + ran.stderr
  assert json.loads(ran.stdout.strip().splitlines()[-1]) == ROWS


# -- Rust -----------------------------------------------------------------------------

CARGO_TOML = '''[package]
name = "forecasts"
version = "0.1.0"
edition = "2021"
publish = false

[lib]
path = "rs/forecasts/lib.rs"

[dependencies]
serde = {{ version = "1", features = ["derive"] }}
truewire-core = {{ path = "{crates}/truewire-core" }}

[dev-dependencies]
async-trait = "0.1"
tokio = {{ version = "1", features = ["macros", "rt-multi-thread"] }}
'''

RUST_WALK = '''//! `markets.fundings_paged` against `truewire mock` at `BASE_URL`.

use async_trait::async_trait;
use forecasts::markets::fundings::FundingsEndpointPagedRequest;
use forecasts::meta::DefaultMeta;
use forecasts::{CallOptions, Forecasts};
use truewire_core::http::{query_from, RequestOptions};
use truewire_core::serde_json::{self, json, Value};
use truewire_core::{Error, HttpCall, HttpClient, HttpEndpoint, Result, TimestampMillis};

/// A core over `BASE_URL`: every call a GET, the dumped request its query.
struct Core {
    base_url: String,
    http: HttpClient,
}

#[async_trait]
impl HttpEndpoint<DefaultMeta> for Core {
    async fn request(&self, call: HttpCall<'_, DefaultMeta>) -> Result<Value> {
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
async fn sends_the_earliest_row_seconds_as_millis() {
    let base_url = std::env::var("BASE_URL").expect("BASE_URL");
    let client = Forecasts::from_core(Core {
        base_url,
        http: HttpClient::default(),
    });
    let request = FundingsEndpointPagedRequest {
        market_id: 1,
        start_timestamp: at(START),
        end_timestamp: at(END),
        extra: Default::default(),
    };
    let rows = client
        .markets
        .fundings_paged(request, CallOptions::default())
        .await
        .unwrap();
    let stamps: Vec<i64> = rows.iter().map(|row| row.timestamp.0.timestamp()).collect();
    println!("rows {}", json!(stamps));
}
'''


def _cargo() -> str | None:
  cargo = shutil.which('cargo') or str(Path.home() / '.cargo' / 'bin' / 'cargo')
  return cargo if Path(cargo).is_file() else None


def test_rust_sends_the_earliest_row_seconds_as_millis(spec: Path, tmp_path: Path):
  """Rust decodes every row, so its walk has no unvalidated form: the typed walk is the one."""
  cargo = _cargo()
  if cargo is None:
    pytest.skip('needs cargo')
  root = generated(spec, tmp_path, 'rust')
  (root / 'Cargo.toml').write_text(CARGO_TOML.format(crates=REPO / 'crates'))
  (root / 'rs' / 'forecasts' / 'core.rs').write_text('//! No hand-written core: the test brings its own.\n')
  (root / 'tests').mkdir()
  (root / 'tests' / 'walk.rs').write_text(RUST_WALK.replace('START', str(START)).replace('END', str(PAGES[0][0])))
  env = {**os.environ, 'CARGO_TARGET_DIR': os.environ.get('TRUEWIRE_CARGO_TARGET_DIR', str(tmp_path / 'target'))}
  formatted = subprocess.run([cargo, 'fmt', '--check'], cwd=root, capture_output=True, text=True, env=env)
  assert formatted.returncode == 0, formatted.stdout + formatted.stderr
  linted = subprocess.run(
    [cargo, 'clippy', '--all-targets', '--', '-D', 'warnings'], cwd=root, capture_output=True, text=True,
    env=env, timeout=1800,
  )
  assert linted.returncode == 0, linted.stderr[-6000:]
  with recording_mock(root) as (base_url, sent):
    tested = subprocess.run(
      [cargo, 'test', '--', '--nocapture'], cwd=root, capture_output=True, text=True,
      env={**env, 'BASE_URL': base_url}, timeout=1800,
    )
    assert sent == SENT, tested.stdout[-3000:] + tested.stderr[-3000:]
  assert tested.returncode == 0, tested.stdout[-6000:] + tested.stderr[-6000:]
  assert json.loads(re.search(r'^rows (.*)$', tested.stdout, re.M)[1]) == ROWS  # type: ignore[index]


# -- Go -------------------------------------------------------------------------------

GO_WALK = '''package tests

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"testing"
	"time"

	truewire "truewire.dev/core"

	"example.com/forecasts/forecasts"
	"example.com/forecasts/forecasts/markets/fundings"
)

// core is a core over BASE_URL: every call a GET, the dumped request its query.
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

func at(millis int64) truewire.TimestampMillis {
	return truewire.TimestampMillis{Time: time.UnixMilli(millis).UTC()}
}

func TestSendsTheEarliestRowSecondsAsMillis(t *testing.T) {
	client := forecasts.FromCore(core{baseURL: os.Getenv("BASE_URL"), http: &truewire.HttpClient{}})
	request := fundings.Request{MarketID: 1, StartTimestamp: at(START), EndTimestamp: at(END)}
	rows, err := client.Markets.FundingsPaged(request).All(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	stamps := []int64{}
	for _, row := range rows {
		stamps = append(stamps, row.Timestamp.Unix())
	}
	out, _ := json.Marshal(stamps)
	fmt.Printf("rows %s\\n", out)
}
'''


def _go() -> str | None:
  for candidate in (shutil.which('go'), str(Path.home() / '.local' / 'go' / 'bin' / 'go')):
    if candidate and Path(candidate).is_file():
      return candidate
  return None


def test_go_sends_the_earliest_row_seconds_as_millis(spec: Path, tmp_path: Path):
  """Go decodes every row, so its walk has no unvalidated form: the typed walk is the one."""
  go = _go()
  if go is None:
    pytest.skip('needs a Go toolchain')
  root = generated(spec, tmp_path, 'go')
  module = root / 'go'
  (module / 'go.mod').write_text(
    'module example.com/forecasts\n\ngo 1.23\n\nrequire truewire.dev/core v0.0.0\n\n'
    f'replace truewire.dev/core => {REPO / "packages" / "core-go"}\n'
  )
  (module / 'tests').mkdir()
  (module / 'tests' / 'walk_test.go').write_text(GO_WALK.replace('START', str(START)).replace('END', str(PAGES[0][0])))
  env = {**os.environ, 'GOFLAGS': '-mod=mod'}
  subprocess.run([go, 'mod', 'tidy'], cwd=module, capture_output=True, text=True, env=env)
  vetted = subprocess.run([go, 'vet', './...'], cwd=module, capture_output=True, text=True, env=env, timeout=600)
  assert vetted.returncode == 0, vetted.stdout + vetted.stderr
  with recording_mock(root) as (base_url, sent):
    tested = subprocess.run(
      [go, 'test', '-count=1', '-v', './...'], cwd=module, capture_output=True, text=True,
      env={**env, 'BASE_URL': base_url}, timeout=600,
    )
    assert sent == SENT, tested.stdout + tested.stderr
  assert tested.returncode == 0, tested.stdout + tested.stderr
  assert json.loads(re.search(r'^rows (.*)$', tested.stdout, re.M)[1]) == ROWS  # type: ignore[index]
