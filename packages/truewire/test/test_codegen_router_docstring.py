"""`Generator.router_doc` / `router_docstring`: rendering a router class's docstring from
an optional `router.json` (ADR 0019, `docs/spec/authoring.md` rule 14)."""
import json

from truewire.project import resolve
from truewire.codegen.python import Generator, router_docstring
from truewire.spec import RouterDoc


def test_router_docstring_falls_back_without_a_doc():
  assert router_docstring('classic/mix', None) == '"""`classic/mix` endpoints."""'


def test_router_docstring_renders_a_declared_doc():
  doc = RouterDoc(
    description='Perpetual futures and delivery contracts.',
    upstream='https://example.com/api-doc/contract/intro',
  )
  rendered = router_docstring('classic/mix', doc)
  assert rendered == (
    '"""Perpetual futures and delivery contracts.\n'
    '\n'
    'References:\n'
    '  - [Upstream docs](https://example.com/api-doc/contract/intro)\n'
    '"""'
  )


def test_generator_router_doc_returns_none_without_client_root():
  generator = Generator()
  assert generator.router_doc('classic/mix') is None


def test_generator_router_doc_returns_none_for_a_nested_section(tmp_path):
  # A nested router's section is a bare last-segment name ('order'), not a real path --
  # see `Generator.router_doc`'s own docstring for why this can never resolve.
  (tmp_path / 'spec' / 'endpoints' / 'order').mkdir(parents=True)
  (tmp_path / 'spec' / 'endpoints' / 'order' / 'router.json').write_text(json.dumps({
    'description': 'x', 'upstream': 'https://example.com',
  }))
  generator = Generator()
  generator.project = resolve(tmp_path)
  assert generator.router_doc('order') is None


def test_generator_router_doc_loads_a_base_root_section(tmp_path):
  section_dir = tmp_path / 'spec' / 'endpoints' / 'classic' / 'mix'
  section_dir.mkdir(parents=True)
  (section_dir / 'router.json').write_text(json.dumps({
    'description': 'Perpetual futures and delivery contracts.',
    'upstream': 'https://example.com/api-doc/contract/intro',
  }))
  generator = Generator()
  generator.project = resolve(tmp_path)
  doc = generator.router_doc('classic/mix')
  assert doc == RouterDoc(
    description='Perpetual futures and delivery contracts.',
    upstream='https://example.com/api-doc/contract/intro',
  )


def test_generator_router_doc_resolves_a_nested_section_via_router_context(tmp_path):
  # `router_context` gives `router_doc` the lossless (base, node) pair, so it can resolve
  # a nested grouping's real directory even though the collapsed `section` string alone
  # ('addresses') carries no path information.
  section_dir = tmp_path / 'spec' / 'endpoints' / 'accounts' / 'addresses'
  section_dir.mkdir(parents=True)
  (section_dir / 'router.json').write_text(json.dumps({
    'description': 'Deposit addresses.', 'upstream': 'https://example.com/addresses',
  }))
  generator = Generator()
  generator.project = resolve(tmp_path)
  generator.router_context = ('accounts', ('addresses',))
  doc = generator.router_doc('addresses')
  assert doc == RouterDoc(
    description='Deposit addresses.', upstream='https://example.com/addresses',
  )


def test_generator_router_doc_via_router_context_returns_none_without_a_matching_directory(tmp_path):
  # `base` doesn't correspond to a real `spec/endpoints/` directory (an output_base
  # artifact, e.g. bybit's uniform 'http') -- `Path(base, *node)` doesn't exist, and this
  # must not guess at some other path; it returns None, same as `load_router` always does
  # for a missing file.
  (tmp_path / 'spec' / 'endpoints' / 'rfq').mkdir(parents=True)
  (tmp_path / 'spec' / 'endpoints' / 'rfq' / 'router.json').write_text(json.dumps({
    'description': 'x', 'upstream': 'https://example.com',
  }))
  generator = Generator()
  generator.project = resolve(tmp_path)
  generator.router_context = ('http', ('rfq',))
  assert generator.router_doc('rfq') is None


def test_generator_router_doc_via_router_context_resolves_a_base_root_section_too(tmp_path):
  # node == () (a base-root section) resolves the same way through router_context as it
  # already did through the old `/`-in-section heuristic.
  section_dir = tmp_path / 'spec' / 'endpoints' / 'classic' / 'mix'
  section_dir.mkdir(parents=True)
  (section_dir / 'router.json').write_text(json.dumps({
    'description': 'Perpetual futures and delivery contracts.',
    'upstream': 'https://example.com/api-doc/contract/intro',
  }))
  generator = Generator()
  generator.project = resolve(tmp_path)
  generator.router_context = ('classic/mix', ())
  doc = generator.router_doc('classic/mix')
  assert doc == RouterDoc(
    description='Perpetual futures and delivery contracts.',
    upstream='https://example.com/api-doc/contract/intro',
  )


def test_generator_router_doc_prefers_an_explicit_slash_section_over_router_context(tmp_path):
  # A subclass override delegating a *different*, explicitly-remapped path (bit2me's real
  # bug: `super().router_doc('ws/trading')` while `router_context` still held the
  # un-remapped `('streams/trading', ())`) must not be silently discarded in favor of
  # router_context -- an explicit argument always wins over ambient state.
  section_dir = tmp_path / 'spec' / 'endpoints' / 'ws' / 'trading'
  section_dir.mkdir(parents=True)
  (section_dir / 'router.json').write_text(json.dumps({
    'description': 'Trading Spot WebSocket.', 'upstream': 'https://example.com/ws-trading',
  }))
  generator = Generator()
  generator.project = resolve(tmp_path)
  generator.router_context = ('streams/trading', ())  # the un-remapped original position
  doc = generator.router_doc('ws/trading')  # the caller's explicit, remapped path
  assert doc == RouterDoc(
    description='Trading Spot WebSocket.', upstream='https://example.com/ws-trading',
  )
