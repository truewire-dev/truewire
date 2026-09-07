from truewire.generation.schema import Reference, Schema
from truewire.generation.util import snake_case

def safe_identifier(name: str) -> str:
  import keyword
  snake = snake_case(name)
  return snake + '_' if keyword.iskeyword(snake) else snake

def body_param_name(type: str | None, body_schema: Schema | Reference | None = None) -> str:
  """The generated parameter name for a request body.

  Args:
    type: The body's rendered type (`None` for a request with no body type to name it
      after).
    body_schema: The body's own raw schema, when the caller has it.

  Preferred: `body_schema`'s own declared `title`, when present -- an ordinary
  plain-object body's title already equals its rendered type, so this changes nothing
  for the common case, but an `anyOf`-shaped discriminated-union body only has a name to
  give a parameter if its *wrapper* schema is titled too (`docs/spec/authoring.md` rule 0);
  each variant's own title (`LimitOrderRequest`, `MarketOrderRequest`) doesn't help name
  the parameter that holds either one of them.

  Falls back to `type` (snake-cased) when `body_schema` carries no title -- the previous
  behavior, still right for a titled non-union body reached without a schema at hand. And
  falls back further to the generic `'body'` when there's nothing to name it after at all,
  or when `type` is a union expression (`'LimitOrderRequest | MarketOrderRequest'`):
  blindly snake-casing a union string concatenates every variant's own name into one
  identifier (`limit_order_request_market_order_request`), which reads worse than a plain
  generic name for something that's already ambiguous by construction -- there's no single
  variant name that's "the" right one to pick, and no title was given to pick instead.
  """
  if isinstance(body_schema, Schema) and body_schema.title:
    return safe_identifier(body_schema.title)
  if type is None or ' | ' in type:
    return 'body'
  return safe_identifier(type)

def escape_docstring(docstring: str) -> str:
  """Escape '\\', '"' and '\n' in a docstring."""
  if docstring is not None:
    docstring = docstring.strip('\n').strip()
    docstring = docstring.replace('\\', '\\\\')
    if docstring.startswith('"'):
      docstring = '\\' + docstring
    if docstring.endswith('"'):
      docstring = docstring[:-1] + '\\"'
    return docstring