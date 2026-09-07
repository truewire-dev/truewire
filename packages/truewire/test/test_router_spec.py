"""`truewire.spec.router`: loading an optional `router.json` (ADR 0019)."""
import json

import pytest
from pydantic import ValidationError

from truewire.spec import RouterDoc, load_router


def test_load_router_returns_none_when_absent(tmp_path):
  assert load_router(tmp_path) is None


def test_load_router_parses_a_declared_file(tmp_path):
  (tmp_path / 'router.json').write_text(json.dumps({
    'description': 'Perpetual futures and delivery contracts.',
    'upstream': 'https://example.com/api-doc/contract/intro',
  }))
  doc = load_router(tmp_path)
  assert doc == RouterDoc(
    description='Perpetual futures and delivery contracts.',
    upstream='https://example.com/api-doc/contract/intro',
  )


def test_router_doc_rejects_missing_field():
  with pytest.raises(ValidationError):
    RouterDoc.model_validate({'description': 'Missing the upstream field.'})


def test_router_doc_rejects_unknown_field():
  with pytest.raises(ValidationError):
    RouterDoc.model_validate({
      'description': 'x', 'upstream': 'https://example.com', 'name': 'Futures',
    })


def test_router_doc_rejects_empty_description():
  with pytest.raises(ValidationError):
    RouterDoc.model_validate({'description': '', 'upstream': 'https://example.com'})


def test_router_doc_rejects_empty_upstream():
  with pytest.raises(ValidationError):
    RouterDoc.model_validate({'description': 'x', 'upstream': ''})


def test_router_doc_rejects_a_non_url_upstream():
  with pytest.raises(ValidationError):
    RouterDoc.model_validate({'description': 'x', 'upstream': 'see the docs'})


def test_router_doc_rejects_a_description_with_a_triple_quote():
  with pytest.raises(ValidationError):
    RouterDoc.model_validate({'description': 'has \"\"\" in it', 'upstream': 'https://example.com'})


def test_router_doc_rejects_an_upstream_with_a_triple_quote():
  with pytest.raises(ValidationError):
    RouterDoc.model_validate({'description': 'x', 'upstream': 'https://example.com/\"\"\"'})


def test_router_doc_accepts_core():
  doc = RouterDoc.model_validate({
    'description': 'Perpetual futures.', 'upstream': 'https://example.com/futures', 'core': 'futures',
  })
  assert doc.core == 'futures'


def test_router_doc_core_defaults_to_none():
  doc = RouterDoc.model_validate({'description': 'X.', 'upstream': 'https://example.com/x'})
  assert doc.core is None
