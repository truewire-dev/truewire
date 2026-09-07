from typing_extensions import Callable
from dataclasses import dataclass, field

from truewire.generation.schema import Operation, Reference, Schema, ensure_nonref, ensure_ref
from truewire.generation.types import RenderedTypes
from truewire.generation.python.util import escape_docstring, safe_identifier, body_param_name
from truewire.generation.openapi import BODY_KEY

@dataclass(kw_only=True)
class Param:
  """One entry of a docstring's `Args:` section."""
  name: str
  type: str | None = None
  required: bool = True
  default: str | None = None
  docstring: str | None = None

  def code(self) -> str:
    """Render the entry as `name: Description.`, the Google/Griffe spelling.

    The annotation and the default value stay in the signature, which is their single
    source of truth, so neither is repeated here.
    """
    out = f'{self.name}:'
    if self.docstring:
      out += f' {escape_docstring(self.docstring)}'
    return out

@dataclass(kw_only=True)
class Docstring:
  """Function docstring generator, emitting Google-compatible sections."""
  Param = Param

  description: str | None = None
  docs_url: str | None = None
  docs_label: str = 'Official docs'
  """Link text for the `References:` entry built from `docs_url`."""
  params: list[Param] = field(default_factory=list)
  tab: str = '  '

  def code(self) -> str:
    """Render the description, then an `Args:` and a `References:` section.

    Returns an empty string when there is nothing to document, so callers can skip
    emitting a docstring altogether rather than write an empty one.
    """
    out = escape_docstring(self.description or '') or ''
    if self.params:
      out += '\n\nArgs:\n'
      out += '\n'.join(self.tab + param.code() for param in self.params)
    if self.docs_url:
      out += f'\n\nReferences:\n{self.tab}- [{self.docs_label}]({self.docs_url})'
    return f'"""{out}\n"""' if out else ''

  _order = {
    'path': 0,
    'query': 1,
    'header': 2,
    'cookie': 3,
  }

  @classmethod
  def parse(
    cls, op: Operation, types: RenderedTypes, *,
    docs_url: str | None = None, docs_label: str = 'Official docs',
    identifier: Callable[[str], str] = safe_identifier,
    body_schema: Schema | Reference | None = None,
  ):
    """Build a docstring from an operation, as an instance of the class called on.

    Args:
      op: The operation to document.
      types: The rendered types, used to name each parameter's type.
      docs_url: Upstream documentation link, overriding the operation's own.
      docs_label: Link text for the `References:` entry.
      identifier: Maps an API parameter name to its Python spelling.
      body_schema: The request body's own raw schema, so its `Args:` entry names the same
        parameter `Function.parse`/`HttpRequest.parse` do (`body_param_name`) -- passing
        `types` alone here would independently re-derive a name from the rendered type
        and risk disagreeing with the actual generated signature.
    """
    docstring = cls(
      description=op.description or op.summary,
      docs_url=docs_url or (op.externalDocs and op.externalDocs.url),
      docs_label=docs_label,
    )
    params = [ensure_nonref(p) for p in op.parameters or []]
    for p in sorted(params, key=lambda p: cls._order[p.in_]):
      s = ensure_ref(p.schema_)
      type = types.identifiers[s.ref]
      docstring.params.append(Docstring.Param(
        name=identifier(p.name),
        type=type,
        required=p.required,
        docstring=p.description,
      ))

    if op.request_body:
      type = types.identifiers.get(BODY_KEY)
      docstring.params.append(Docstring.Param(
        name=body_param_name(type, body_schema),
        type=type,
        required=True,
        docstring=op.request_body.description,
      ))

    return docstring