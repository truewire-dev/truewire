"""`[policy]` (W15) rendered as code: the root's `RATE`/`RETRY` and the `policy` module.

`rate` and `retry` configure the runtime's `HttpClient`, which the hand-written core
builds, so the generated root states them as two constants for the core to pass on:
`RATE` (requests per second, or none) and `RETRY`. Python puts them on the root class only
when `[policy]` sets one, since the core's base class declares the defaults the root
inherits; TypeScript (`static readonly`) and Rust (associated `const`s) always put them on
the root, which the core names directly.

`refuse` is the `policy` module each backend writes.

An endpoint `truewire.toml` refuses keeps its generated method, signature and docs, and
that method fails with the package's own `RefusedByPolicy` before any request is made:
Python wraps every public method of the endpoint class (`@refused`), TypeScript and Rust
open every body that reaches the core with `refuse(...)`. The error is generated into the
package rather than shipped by the runtimes, so a refusal needs no runtime release and no
new dependency floor. It subclasses each runtime's `LogicError` (in Rust, it is the
`source` of an `Error::Logic`), so a handler for the runtime's errors still catches it.

A project with nothing refused gets no `policy` module, and no generated file changes.
"""
from collections.abc import Mapping, Sequence
from typing_extensions import Any

from truewire.plan.model import PackagePlan
from truewire.project import Project

POLICY_MODULE = 'policy'
"""Module name, relative to the package root, of the generated refusal."""


def http_policy_lines(project: Project) -> list[str]:
  """The Python root class's `RATE`/`RETRY` lines, or none when `[policy]` leaves both unset."""
  policy = project.policy
  if policy.rate is None and not policy.retry:
    return []
  return [
    f'RATE: ClassVar[float | None] = {policy.rate!r}',
    '"""`[policy].rate`: requests per second the core\'s `HttpClient` paces to."""',
    f'RETRY: ClassVar[bool] = {policy.retry!r}',
    '"""`[policy].retry`: whether the core\'s `HttpClient` retries on its own."""',
  ]


def typescript_rate(project: Project | None) -> str:
  """`[policy].rate` as a TypeScript expression: a number, or `undefined`."""
  rate = project.policy.rate if project is not None else None
  return 'undefined' if rate is None else _number(rate)


def rust_rate(project: Project | None) -> str:
  """`[policy].rate` as a Rust `Option<f64>` expression."""
  rate = project.policy.rate if project is not None else None
  return 'None' if rate is None else f'Some({float(rate)!r})'


def _number(rate: float) -> str:
  """A float as the shortest TypeScript literal: `5` for `5.0`, `0.5` as is."""
  return str(int(rate)) if rate.is_integer() else repr(rate)


def refused_functions(plan: PackagePlan) -> list[str]:
  """The function paths of the plan's refused endpoints, in plan order."""
  return [endpoint.function for endpoint in plan.endpoints if endpoint.refused]


def extras_replaced(extras: Mapping[str, Sequence[Any]] | None) -> set[str]:
  """The function paths a `[python.extras]` or `[typescript.extras]` entry stands in for
  (`replaces`): its hand-written class sits behind the router in the generated leaf's
  place, and an override of the method that reaches the core skips the generated guard."""
  return {
    f'{node}.{entry.replaces}' if node else entry.replaces
    for node, entries in (extras or {}).items() for entry in entries if entry.replaces is not None
  }


def refusal_problems(plan: PackagePlan, project: Project | None, *, handwritten: set[str] = frozenset()) -> list[str]:
  """Why `[policy].refuse` cannot be generated as written; empty when it can.

  A refusal that renders nothing would leave the endpoint callable behind a generator that
  reported success, so each one is an error: an id with no generated endpoint (a typo, or
  an `absent` surface) and an endpoint whose method is written by hand, which no generator
  can guard. A root router or endpoint named `policy` would share the module's file.

  Args:
    plan: The package plan.
    project: Its project; `None` (a plan built without one) refuses nothing.
    handwritten: Function paths whose callable is written by hand beyond the plan's own
      `surface: handwritten` endpoints: a backend's own hand-written surfaces, a
      `replaces` extras entry (`extras_replaced`), a Python backend's `skip_endpoint`.
  """
  if project is None or not project.policy.refuse:
    return []
  problems: list[str] = []
  for function in project.policy.refuse:
    endpoint = plan.endpoint(function)
    if endpoint is None:
      problems.append(f'[policy].refuse names {function!r}, which is no generated endpoint (see `truewire check`)')
    elif endpoint.surface == 'handwritten' or function in handwritten:
      problems.append(
        f'[policy].refuse names {function!r}, whose method is written by hand (`surface: '
        'handwritten`, an extras entry that `replaces` it, or a backend\'s `skip_endpoint`): no '
        'generated code can refuse it; refuse it in that method and drop it from [policy].refuse'
      )
  roots = {router.path[0] for router in plan.routers if router.path} | {
    endpoint.path[0] for endpoint in plan.endpoints if len(endpoint.path) == 1
  }
  if POLICY_MODULE in roots:
    problems.append(
      f'a root router or endpoint named {POLICY_MODULE!r} would share the file of the generated '
      f'{POLICY_MODULE!r} module that [policy].refuse needs'
    )
  return problems


def python_module() -> str:
  """`<package>/policy.py`: `RefusedByPolicy` and the `@refused` class decorator."""
  return '''"""`[policy].refuse` (W15): the endpoints this client refuses to call."""
import inspect
from collections.abc import Callable
from functools import wraps
from typing_extensions import Any, NoReturn, TypeVar

from truewire_core.exceptions import LogicError

T = TypeVar('T', bound=type)


class RefusedByPolicy(LogicError):
  """A call to an endpoint `[policy].refuse` names; raised before any request is made."""

  def __init__(self, endpoint: str):
    super().__init__(f'{endpoint} is refused by [policy].refuse; no request was made')
    self.endpoint = endpoint
    """The refused endpoint's function path (`account.withdraw`)."""


def refused(endpoint: str) -> Callable[[T], T]:
  """Make every public method of an endpoint class (and `__call__`) raise
  `RefusedByPolicy` when called.

  A coroutine method raises when awaited; any other method (a walker, a subscription)
  raises when called. Either way the core is never reached.

  Args:
    endpoint: The refused endpoint's function path.
  """

  def refuse(method: Callable[..., Any]) -> Callable[..., Any]:
    if inspect.iscoroutinefunction(method):

      @wraps(method)
      async def refused_coroutine(*args: Any, **kwargs: Any) -> NoReturn:
        raise RefusedByPolicy(endpoint)

      return refused_coroutine

    @wraps(method)
    def refused_call(*args: Any, **kwargs: Any) -> NoReturn:
      raise RefusedByPolicy(endpoint)

    return refused_call

  def decorate(cls: T) -> T:
    for name, value in list(vars(cls).items()):
      if (not name.startswith('_') or name == '__call__') and inspect.isfunction(value):
        setattr(cls, name, refuse(value))
    return cls

  return decorate
'''


def typescript_module() -> str:
  """`<package>/policy.ts`: `RefusedByPolicy` and the `refuse` guard."""
  return """// Generated by truewire — do not edit by hand.
import { LogicError } from '@truewire/core'

/** A call to an endpoint `[policy].refuse` names (W15); thrown before any request is made. */
export class RefusedByPolicy extends LogicError {
  // No `name` of its own: `isTruewireError` recognises a runtime error that crossed a
  // duplicated `@truewire/core` bundle by the runtime's class names, `LogicError` among them.

  /** @param endpoint The refused endpoint's function path (`account.withdraw`). */
  constructor(readonly endpoint: string) {
    super(`${endpoint} is refused by [policy].refuse; no request was made`)
  }
}

/** Throws `RefusedByPolicy`: the first statement of every call a refused endpoint makes. */
export function refuse(endpoint: string): void {
  throw new RefusedByPolicy(endpoint)
}
"""


def rust_module() -> str:
  """`<package>/policy.rs`: `RefusedByPolicy` and the `refuse` guard."""
  return """//! Generated by truewire — do not edit by hand.
//!
//! `[policy].refuse` (W15): the endpoints this client refuses to call.

use std::fmt;
use std::sync::Arc;

use truewire_core::{Error, LogicError, Result};

/// A call to an endpoint `[policy].refuse` names, failed before any request was made.
///
/// Returned as the `source` of an [`Error::Logic`]; [`RefusedByPolicy::of`] finds it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RefusedByPolicy {
    /// The refused endpoint's function path (`account.withdraw`).
    pub endpoint: &'static str,
}

impl RefusedByPolicy {
    /// The refusal `error` carries, when it is one.
    pub fn of(error: &Error) -> Option<&RefusedByPolicy> {
        match error {
            Error::Logic(logic) => logic.source.as_deref()?.downcast_ref(),
            _ => None,
        }
    }
}

impl fmt::Display for RefusedByPolicy {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "{} is refused by [policy].refuse; no request was made",
            self.endpoint
        )
    }
}

impl std::error::Error for RefusedByPolicy {}

impl From<RefusedByPolicy> for Error {
    fn from(refused: RefusedByPolicy) -> Self {
        Error::Logic(LogicError {
            message: refused.to_string(),
            source: Some(Arc::new(refused)),
        })
    }
}

/// Fails with [`RefusedByPolicy`]: the first statement of every call a refused endpoint makes.
pub fn refuse(endpoint: &'static str) -> Result<()> {
    Err(RefusedByPolicy { endpoint }.into())
}
"""


__all__ = [
  'POLICY_MODULE', 'python_module', 'refusal_problems', 'refused_functions', 'rust_module', 'typescript_module',
]
