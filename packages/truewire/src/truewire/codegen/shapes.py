"""What a router's constructor takes, worked out once for every backend.

A router under a plain core is built from one transport: every endpoint beneath it holds
the same contract, or a set of them, and the router hands the same value to each child.

A router under a *composite* core is built from several: `[cores.<name>] children` maps a
child to the field it is handed, so the constructor takes one transport per field. That is
a fact about the spec and the project's declarations, not about a language, so it is
computed here and each backend renders it in its own idiom -- a fields object in
TypeScript, one parameter per field in Rust.

The rule that is easy to miss: a router under a *plain* core with a composite descendant
is itself composite. Its own endpoints have to be handed something, and once a sibling
subtree needs named fields there is no single transport to hand down, so its endpoints go
under the default field.
"""

from dataclasses import dataclass, field

from typing_extensions import Callable

from truewire.plan.model import PackagePlan

DEFAULT_FIELD = 'client'
"""The field a child with no `children` entry of its own is handed."""


@dataclass
class CoreShape:
  """One transport (`types`, the contracts its endpoints need), or a field per transport
  (`fields`) when this router or a descendant is composite."""

  types: set[str] = field(default_factory=set)
  fields: dict[str, set[str]] | None = None

  @property
  def composite(self) -> bool:
    return self.fields is not None


def is_composite(plan: PackagePlan, core: str | None) -> bool:
  """Whether `core` declares `children` or `forward`: built from more than one transport."""
  declared = plan.cores.get(core) if core is not None else None
  return declared is not None and (declared.children is not None or declared.forward is not None)


def core_shapes(
  plan: PackagePlan, contract: Callable[[str], str | None],
) -> dict[tuple[str, ...], CoreShape]:
  """The `CoreShape` of every router, leaves first.

  Args:
    contract: The contract an endpoint holds, by dotted function path, in the backend's
      own spelling (`HttpEndpoint<DefaultMeta>`); `None` for an endpoint that backend did
      not render, which is then left out of its parent's shape.
  """
  routers = {tuple(router.path): router for router in plan.routers}
  shapes: dict[tuple[str, ...], CoreShape] = {}

  def shape(path: tuple[str, ...]) -> CoreShape:
    if path in shapes:
      return shapes[path]
    router = routers[path]
    mapping = (plan.cores[router.core].children if router.core in plan.cores else None) or {}
    kids: list[tuple[str, CoreShape]] = []
    for child in router.children:
      if child.kind == 'endpoint':
        held = contract('.'.join([*path, child.name]))
        if held is not None:
          kids.append((child.name, CoreShape(types={held})))
      elif (*path, child.name) in routers:
        kids.append((child.name, shape((*path, child.name))))
    result = CoreShape()
    if is_composite(plan, router.core) or any(kid.composite for _, kid in kids):
      result.fields = {}
      for name, kid in kids:
        if kid.fields is not None:
          for field_name, types in kid.fields.items():
            result.fields.setdefault(field_name, set()).update(types)
        elif kid.types:
          # A subtree the backend rendered nothing for contributes no field: there would
          # be nothing to hand it, and a field with no contract renders as a hole.
          result.fields.setdefault(mapping.get(name, DEFAULT_FIELD), set()).update(kid.types)
    else:
      for _, kid in kids:
        result.types.update(kid.types)
    shapes[path] = result
    return result

  for path in routers:
    shape(path)
  return shapes


def field_for(plan: PackagePlan, path: tuple[str, ...], child: str) -> str:
  """The field a composite router hands `child`."""
  router = next(r for r in plan.routers if tuple(r.path) == path)
  mapping = (plan.cores[router.core].children if router.core in plan.cores else None) or {}
  return mapping.get(child, DEFAULT_FIELD)


__all__ = ['CoreShape', 'DEFAULT_FIELD', 'core_shapes', 'field_for', 'is_composite']
