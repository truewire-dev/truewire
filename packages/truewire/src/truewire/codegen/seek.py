"""The doc sentences a `seek` walker with `exclusive` parameters carries (ADR 0013), worked
out once for every backend.

Each backend names the parameters its own way (Python identifiers, wire names elsewhere)
and places the sentences in its own doc idiom, but what they say about the walk is the
same: the far bound kept on the rows, the parameters sent on the first request only, and
what a caller may pass beside the moving bound.
"""

from collections.abc import Sequence

from truewire.spec.endpoint import last_row_field_prose


def exclusive_sentences(
  parameters: Sequence[str], *, moving: str, first: str | None,
  far_parameter: str | None, far_field: str | None,
) -> list[str]:
  """The sentences, in order, each ending in a full stop.

  Args:
    parameters: The exclusive parameters, as the backend names them.
    moving: The moving bound, named the same way.
    first: The parameter a walk without `moving` has to start from, if any.
    far_parameter: The exclusive parameter kept on the rows, if any.
    far_field: The row field it is compared with, in words (`last_row_field_prose`).
  """
  out: list[str] = []
  if far_parameter is not None:
    out.append(
      f'Drops every row whose {far_field} is past the caller\'s own `{far_parameter}`, '
      f'and ends the walk on the page that held one.'
    )
  listed = '/'.join(f'`{name}`' for name in parameters)
  pronoun = 'they are' if len(parameters) > 1 else 'it is'
  sentence = (
    f'The venue refuses {listed} alongside `{moving}`, so {pronoun} sent on the first '
    f'request only, and never when the caller gives `{moving}`'
  )
  refused = [name for name in parameters if name != far_parameter]
  if refused:
    sentence += f'; pass {"/".join(f"`{name}`" for name in refused)} or `{moving}`, not both'
  out.append(sentence + '.')
  if first is not None:
    out.append(f'Without `{moving}`, `{first}` is required.')
  return out


def plan_exclusive_sentences(exclusive: dict | None, *, moving: str) -> list[str]:
  """`exclusive_sentences` for the plan's `seek.exclusive` (wire names), `[]` without one.

  Args:
    exclusive: `PaginationPlan.seek['exclusive']`.
    moving: The moving bound's wire name.
  """
  if not exclusive:
    return []
  far = exclusive.get('far')
  return exclusive_sentences(
    exclusive['parameters'], moving=moving, first=exclusive.get('first'),
    far_parameter=far['parameter'] if far else None,
    far_field=last_row_field_prose(far['field']) if far else None,
  )
