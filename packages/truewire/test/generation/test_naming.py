"""
Pin name reservation in the Python naming path.

Generated types do not own their module alone: the client factory emits an endpoint
class beside them, and a collision silently mistypes the public surface. `disambiguate`
has always accepted `forbidden`, but nothing upstream of it passed the parameter, so the
reservation could not be expressed. These tests pin the wiring end to end.
"""
from truewire.generation.python.types import TypeGenerator, disambiguate
from truewire.generation.python.types.naming import path_name, type_name
from truewire.generation.schema import Schema

def generate(schemas: dict[str, dict], **kwargs):
  """Run the full Python type pipeline over raw JSON Schema fragments."""
  return TypeGenerator(**kwargs)({k: Schema.model_validate(v) for k, v in schemas.items()})

ORDERBOOK = {
  '$response/response200': {
    'title': 'Orderbook',
    'type': 'object',
    'required': ['s'],
    'properties': {'s': {'type': 'string'}},
  },
}

class TestDisambiguate:
  """`disambiguate` itself already honours `forbidden`; pin it as the contract."""

  def test_title_is_used_when_free(self):
    names = disambiguate({k: Schema.model_validate(v) for k, v in ORDERBOOK.items()})
    assert names['$response/response200'] == 'Orderbook'

  def test_forbidden_title_is_not_assigned(self):
    names = disambiguate(
      {k: Schema.model_validate(v) for k, v in ORDERBOOK.items()},
      forbidden={'Orderbook'},
    )
    assert names['$response/response200'] != 'Orderbook'

class TestTypeGeneratorForbidden:
  """The generator must forward the reservation, not drop it."""

  def test_unreserved_name_reaches_the_identifier(self):
    rendered = generate(ORDERBOOK)
    assert rendered.identifiers['$response/response200'] == 'Orderbook'

  def test_reserved_name_is_not_taken_by_a_schema(self):
    rendered = generate(ORDERBOOK, forbidden={'Orderbook'})
    assert rendered.identifiers['$response/response200'] != 'Orderbook'

  def test_reserved_name_does_not_appear_as_a_definition(self):
    rendered = generate(ORDERBOOK, forbidden={'Orderbook'})
    assert 'class Orderbook(' not in rendered.definitions['$response/response200']

  def test_reserving_an_unrelated_name_changes_nothing(self):
    rendered = generate(ORDERBOOK, forbidden={'Something'})
    assert rendered.identifiers['$response/response200'] == 'Orderbook'

# A `title` is already correctly PascalCase (`docs/spec/authoring.md` rule 1) and must be
# used verbatim. `caseconverter.pascalcase` doesn't treat a digit -> uppercase-letter
# transition as a word boundary, so re-casing an already-cased title through `type_name`
# lowercases the letter right after a digit (`L2PlasmaDeposit` -> `L2plasmaDeposit`,
# `Erc20Transfer` -> `Erc20transfer`). This was discovered building etherscan's codegen
# backend, worked around locally there, and fixed at the source here.
DIGIT_BOUNDARY_TITLES = {
  '$response/response200': {
    'title': 'L2PlasmaDeposit',
    'type': 'object',
    'required': ['amount'],
    'properties': {'amount': {'type': 'string'}},
  },
}

class TestDigitBoundaryTitle:
  """A schema title with a digit-then-letter boundary must round-trip unchanged."""

  def test_path_name_uses_title_verbatim(self):
    schema = Schema.model_validate(DIGIT_BOUNDARY_TITLES['$response/response200'])
    # Sanity check against the bug this test exists to catch: naively re-casing the title
    # through the same `type_name` used for genuine (uncased) path segments corrupts it.
    assert type_name(schema.title) == 'L2plasmaDeposit', (
      'if this fails, caseconverter fixed its digit-boundary handling upstream and this '
      'regression test needs a different corrupting title'
    )
    assert path_name('$response/response200', schema, 0) == 'L2PlasmaDeposit'
    assert path_name('$response/response200', schema, 1) == 'L2PlasmaDeposit' + type_name('response200')

  def test_disambiguate_preserves_title_casing(self):
    names = disambiguate({k: Schema.model_validate(v) for k, v in DIGIT_BOUNDARY_TITLES.items()})
    assert names['$response/response200'] == 'L2PlasmaDeposit'

  def test_type_generator_preserves_title_casing(self):
    rendered = generate(DIGIT_BOUNDARY_TITLES)
    assert rendered.identifiers['$response/response200'] == 'L2PlasmaDeposit'
    assert 'class L2PlasmaDeposit(' in rendered.definitions['$response/response200']

  def test_erc20_style_digit_boundary_title(self):
    """A second, differently-shaped digit-boundary title (`Erc20Transfer`) as a control."""
    schema = Schema.model_validate({
      'title': 'Erc20Transfer', 'type': 'object',
      'required': ['value'], 'properties': {'value': {'type': 'string'}},
    })
    assert path_name('$response/response200', schema, 0) == 'Erc20Transfer'
