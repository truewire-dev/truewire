"""The plan: the language-neutral intermediate representation every backend renders from.

`truewire.plan.types` is the type tree; `truewire.plan.model` the frozen plan every
endpoint, router and core is described by; `truewire.plan.build` computes it once from a
`Project`. `truewire plan --json` prints it.

Import the submodules directly: the type tree is imported by the type pipeline the
builder itself runs, so this package loads nothing eagerly.
"""
from typing_extensions import Any


def __getattr__(name: str) -> Any:
  if name in ('PlanBuilder', 'build_plan'):
    from . import build

    return getattr(build, name)
  raise AttributeError(name)
