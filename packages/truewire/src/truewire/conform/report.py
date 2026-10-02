"""The conformance report, and the ledger that gives each finding its first-seen date.

A run writes three files under `<state>/<project>/`:

- `<YYYY-MM-DD>.json`: the run, for machines. One entry per endpoint.
- `<YYYY-MM-DD>.md`: the same, for people.
- `ledger.json`: every finding ever seen, keyed by a fingerprint that survives from one
  run to the next, with `first_seen`, `last_seen` and `resolved`. It is what lets a
  report say "since 2026-10-02" rather than "today" (ADR 0012).

A finding is resolved on the first run that exercised its endpoint's example live,
judged the part of the response the finding is about, and did not see it. An example
that was skipped, errored or filtered out by `--only` proves nothing either way, so its
open findings stay open; nor does a 404 prove anything about the body the recording
has, or a body that fails the schema about what the client makes of a valid one.
"""

from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path
from typing_extensions import Any

from .shape import Finding

STATUSES = ('ok', 'drift', 'client', 'error', 'skipped')
"""Endpoint outcomes, in the order a report counts them."""


@dataclass
class ExampleResult:
  """One recorded request, called once."""
  id: str
  status: str
  """`ok`, `drift`, `client`, `error` or `skipped`."""
  http_status: int | None = None
  recorded_status: int | None = None
  client: str | None = None
  """What the generated client made of the response: `accepted`, `rejected`, or
  `rejected (the body fails the schema)` when that is the API's doing, not ours."""
  reason: str | None = None
  """Why a `skipped` example was not called (`missing_credentials`, say), or was called and
  not judged (`stale_recording`)."""
  detail: str | None = None
  """Why an `error` or `skipped` example has no findings. Method, path, status and
  exception class only: never a response body."""
  findings: list[Finding] = field(default_factory=list)
  judged: set[str] = field(default_factory=set)
  """The parts of the live response that were checked (`status`, `body`, `client`), so
  an absent finding about one of them means resolved."""
  body: Any = field(default=None, repr=False)
  """The live JSON body, kept for the ledger's `reaches` only. Never written."""
  withdraws: str | None = None
  """Why this run closes the example's open findings without judging them: the recording
  went stale, or the spec now declares the endpoint private. Not in the report; the
  ledger entry keeps it as `withdrawn`."""

  @property
  def exercised(self) -> bool:
    """Whether a live response was evaluated at all."""
    return self.status in ('ok', 'drift', 'client')


def judged_part(kind: str, check: str) -> str:
  """The part of a response a finding (or ledger entry) is about: its HTTP `status`,
  its `body` against the schema and the recording, or the `client`'s verdict on it."""
  if kind.startswith('client'):
    return 'client'
  return 'status' if check == 'status' else 'body'


@dataclass
class EndpointResult:
  function: str
  path: str
  """The endpoint's directory under `spec/endpoints`."""
  status: str = 'ok'
  reason: str | None = None
  """Why the endpoint was skipped: `no_recording`, `missing_credentials`, `stream: phase 2`, ..."""
  detail: str | None = None
  examples: list[ExampleResult] = field(default_factory=list)

  def settle(self) -> None:
    """Derive the endpoint's status from its examples: the worst of them wins."""
    if self.reason is not None:
      self.status = 'skipped'
      return
    statuses = {example.status for example in self.examples}
    for status in ('drift', 'client', 'error', 'ok'):
      if status in statuses:
        self.status = status
        return
    self.status = 'skipped'
    if self.examples:
      self.reason, self.detail = self.examples[0].reason, self.examples[0].detail


def fingerprint(function: str, example: str, finding: Finding) -> str:
  """A finding's identity across runs: where it is and what kind of difference it is,
  never the values on either side (they change nightly), except an unknown enum value,
  which is what the finding is about."""
  kind, check, pointer, extra = finding.key()
  text = json.dumps([function, example, kind, check, pointer, extra])
  return hashlib.sha256(text.encode()).hexdigest()[:16]


@dataclass
class Ledger:
  project: str
  findings: dict[str, dict[str, Any]] = field(default_factory=dict)

  @classmethod
  def load(cls, path: Path, project: str) -> 'Ledger':
    if not path.is_file():
      return cls(project)
    data = json.loads(path.read_text())
    return cls(project, dict(data.get('findings') or {}))

  def save(self, path: Path) -> None:
    path.write_text(json.dumps({'project': self.project, 'findings': self.findings}, indent=2) + '\n')

  def record(self, results: list[EndpointResult], today: str, *, complete: bool = False) -> list[dict[str, Any]]:
    """Carry every finding of this run forward and resolve the ones that went away.

    Sets `fingerprint` and `first_seen` on each finding in `results`. `complete` says the
    run covered every endpoint in the spec (no `--only`), so an endpoint missing from
    `results` is gone from the spec.

    Returns:
      The ledger entries resolved by this run.
    """
    seen: set[str] = set()
    judged: set[tuple[str, str, str]] = set()
    withdrawn: dict[tuple[str, str], str] = {}
    bodies: dict[tuple[str, str], Any] = {}
    # The examples each endpoint has now, where the run knows them: a finding on an
    # example that is gone can never be judged again.
    recorded: dict[str, set[str]] = {}
    # Endpoints the run does not call at all (a stream, a write): nothing on them is judged.
    uncalled: dict[str, str] = {}
    present = {endpoint.function for endpoint in results}
    for endpoint in results:
      if endpoint.examples or endpoint.reason == 'no_recording':
        recorded[endpoint.function] = {example.id for example in endpoint.examples}
      elif endpoint.reason is not None:
        uncalled[endpoint.function] = f'not called: {endpoint.reason}'
      for example in endpoint.examples:
        if example.withdraws:
          withdrawn[(endpoint.function, example.id)] = example.withdraws
        if example.body is not None:
          bodies[(endpoint.function, example.id)] = example.body
        if example.exercised:
          judged.update((endpoint.function, example.id, part) for part in example.judged)
        for finding in example.findings:
          fp = fingerprint(endpoint.function, example.id, finding)
          finding.fingerprint = fp
          seen.add(fp)
          entry = self.findings.get(fp)
          if entry is None or entry.get('resolved') is not None:
            reopened = entry.get('resolved') if entry else None
            entry = {
              'function': endpoint.function, 'example': example.id, 'kind': finding.kind,
              'check': finding.check, 'pointer': finding.pointer, 'first_seen': today,
            }
            if reopened:
              entry['previously_resolved'] = reopened
            self.findings[fp] = entry
          entry.update(expected=finding.expected, actual=finding.actual, last_seen=today, resolved=None)
          finding.first_seen = entry['first_seen']
    resolved: list[dict[str, Any]] = []
    for fp, entry in sorted(self.findings.items()):
      if fp in seen or entry.get('resolved') is not None:
        continue
      where = (entry['function'], entry['example'])
      part = judged_part(entry['kind'], entry['check'])
      if (*where, part) in judged and (part != 'body' or reaches(bodies.get(where), entry['pointer'], entry['check'])):
        entry['resolved'] = today
      elif where in withdrawn:
        entry.update(resolved=today, withdrawn=withdrawn[where])
      elif entry['function'] in recorded and entry['example'] not in recorded[entry['function']]:
        entry.update(resolved=today, withdrawn='the recording is gone')
      elif entry['function'] in uncalled:
        entry.update(resolved=today, withdrawn=uncalled[entry['function']])
      elif complete and entry['function'] not in present:
        entry.update(resolved=today, withdrawn='the endpoint is gone from the spec')
      else:
        continue
      resolved.append({'fingerprint': fp, **entry})
    return resolved


def reaches(body: Any, pointer: str, check: str | None = None) -> bool:
  """Whether tonight's `body` says anything at `pointer` about a finding's `check`: every
  `*` on the way is a non-empty array or object, every key or index before the last is
  there, a last index is there too, and a last key's parent is an object. The last key need
  not be there, since a key that is gone is what resolves `key_added`. An `array_element`
  finding (a tuple's length) needs a non-empty tuple at `pointer`. An empty order book says
  nothing about its rows, an empty row nothing about its length or its positions, and a null
  `fees` nothing about `fees/maker`, so none of them resolves a finding about those."""
  parts = [part.replace('~1', '/').replace('~0', '~') for part in pointer.split('/')[1:]]

  def walk(node: Any, rest: list[str]) -> bool:
    if not rest:
      return check != 'array_element' or (isinstance(node, list) and len(node) > 0)
    head, tail = rest[0], rest[1:]
    if head == '*':
      items = node if isinstance(node, list) else list(node.values()) if isinstance(node, dict) else []
      return any(walk(item, tail) for item in items)
    if isinstance(node, dict):
      if not tail and check != 'array_element':
        return True
      return head in node and walk(node[head], tail)
    if isinstance(node, list) and head.isdigit():
      return int(head) < len(node) and walk(node[int(head)], tail)
    return False

  return walk(body, parts)


def counts(results: list[EndpointResult]) -> dict[str, int]:
  out = dict.fromkeys(STATUSES, 0)
  for endpoint in results:
    out[endpoint.status] += 1
  return out


def finding_json(finding: Finding) -> dict[str, Any]:
  data = asdict(finding)
  if data['count'] == 1:
    del data['count']
  return data


def report_json(
  project: str, today: str, meta: dict[str, Any], results: list[EndpointResult], resolved: list[dict[str, Any]],
) -> dict[str, Any]:
  endpoints = []
  for endpoint in results:
    entry: dict[str, Any] = {'function': endpoint.function, 'path': endpoint.path, 'status': endpoint.status}
    if endpoint.reason:
      entry['reason'] = endpoint.reason
    if endpoint.detail:
      entry['detail'] = endpoint.detail
    if endpoint.examples:
      entry['examples'] = [
        {
          key: value for key, value in (
            ('id', example.id), ('status', example.status), ('http_status', example.http_status),
            ('recorded_status', example.recorded_status), ('client', example.client),
            ('reason', example.reason), ('detail', example.detail),
            ('findings', [finding_json(f) for f in example.findings]),
          ) if value is not None and value != []
        }
        for example in endpoint.examples
      ]
    endpoints.append(entry)
  return {
    'project': project, 'date': today, **meta, 'counts': counts(results),
    'endpoints': endpoints, 'resolved': resolved,
  }


def cell(value: Any) -> str:
  """One Markdown table cell: JSON for structures, pipes escaped."""
  if value is None:
    return ''
  text = value if isinstance(value, str) else json.dumps(value)
  return text.replace('|', '\\|').replace('\n', ' ')


def report_markdown(report: dict[str, Any]) -> str:
  lines = [f'# Conformance: {report["project"]}, {report["date"]}', '']
  lines.append(
    f'`truewire conform` {report.get("truewire", "")}, started {report.get("started_at", "")}, '
    f'finished {report.get("finished_at", "")}. One live call per recorded request, '
    'sequential. Shapes are compared, never values.'
  )
  lines += ['', '| ok | drift | client | error | skipped |', '|---:|---:|---:|---:|---:|']
  c = report['counts']
  lines.append(f'| {c["ok"]} | {c["drift"]} | {c["client"]} | {c["error"]} | {c["skipped"]} |')

  findings = [
    (endpoint, example, finding)
    for endpoint in report['endpoints']
    for example in endpoint.get('examples', [])
    for finding in example.get('findings', [])
  ]
  lines += ['', f'## Findings ({len(findings)})', '']
  if findings:
    lines += [
      '| endpoint | example | kind | check | pointer | expected | actual | against | since |',
      '|---|---|---|---|---|---|---|---|---|',
    ]
    for endpoint, example, finding in findings:
      count = f' (x{finding["count"]})' if finding.get('count', 1) > 1 else ''
      lines.append(
        f'| `{endpoint["function"]}` | {example["id"]} | {finding["kind"]} | {finding["check"]}{count} '
        f'| `{cell(finding["pointer"]) or "/"}` | {cell(finding["expected"])} | {cell(finding["actual"])} '
        f'| {finding["against"]} | {finding.get("first_seen") or ""} |'
      )
  else:
    lines.append('None.')

  errors = [
    (endpoint, example)
    for endpoint in report['endpoints'] if endpoint['status'] != 'skipped'
    for example in endpoint.get('examples', []) if example['status'] == 'error'
  ]
  if errors:
    lines += ['', f'## Errors ({len(errors)})', '', '| endpoint | example | detail |', '|---|---|---|']
    for endpoint, example in errors:
      lines.append(f'| `{endpoint["function"]}` | {example["id"]} | {cell(example.get("detail"))} |')

  if report['resolved']:
    lines += ['', f'## Resolved today ({len(report["resolved"])})', '',
              '| endpoint | example | check | pointer | since |', '|---|---|---|---|---|']
    for entry in report['resolved']:
      lines.append(
        f'| `{entry["function"]}` | {entry["example"]} | {entry["check"]} | `{cell(entry["pointer"]) or "/"}` '
        f'| {entry["first_seen"]}{" (withdrawn: " + cell(entry["withdrawn"]) + ")" if entry.get("withdrawn") else ""} |'
      )

  lines += ['', '## Endpoints', '', '| endpoint | status | examples | note |', '|---|---|---|---|']
  for endpoint in report['endpoints']:
    examples = ', '.join(
      f'{example["id"]}: {example["status"]}' for example in endpoint.get('examples', [])
    )
    note = endpoint.get('reason') or ''
    if endpoint.get('detail') and endpoint.get('detail') != note:
      note = f'{note} ({endpoint["detail"]})' if note else endpoint['detail']
    lines.append(f'| `{endpoint["function"]}` | {endpoint["status"]} | {cell(examples)} | {cell(note)} |')
  return '\n'.join(lines) + '\n'
