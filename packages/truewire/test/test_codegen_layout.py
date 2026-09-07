"""`truewire.codegen.layout.load_generator`'s two loading paths: a real per-client
`codegen/python.py` backend (unchanged), and the bare `Generator()` fallback for a
client that has fully migrated onto `codegen/config.toml`/the universal `Generator` with
no per-client backend of its own (design §5c/Task 24a).

Also `package_root`'s stale-directory hardening: a client that migrated onto the
fleet-wide `typed_<name>` package name (PR #72) can be left with an untracked,
`__pycache__`-only leftover directory under `pkg/src` bearing the old bare name -- sorting
alphabetically before the real `typed_<name>` directory. Found independently by multiple
`client-review` passes in one session (2026-09-04) on alchemy, bybit, deribit, etherscan,
hyperliquid, kraken, and mexc: every surface/coverage-dependent command silently picked the
dead directory and reported every endpoint `no_module`, even though the real package was
fully generated and correct.
"""

from pathlib import Path

import pytest

from truewire.codegen.layout import BackendUnavailable, load_generator, package_root
from truewire.codegen.python import Generator


def test_loads_real_backend_module(tmp_path: Path):
  """An existing `codegen/python.py` backend is loaded and its `generator` returned,
  unaffected by the new fallback -- this is the path every currently-unmigrated real
  client still takes."""
  (tmp_path / 'truewire.toml').write_text(
    '[python]\nbackend = "backend.py"\n[python.cores.default]\nbase = "x.core:Endpoint"\n'
  )
  (tmp_path / 'backend.py').write_text(
    'class _Generator:\n'
    '  marker = "real-backend"\n'
    '\n'
    '\n'
    'generator = _Generator()\n'
  )
  generator = load_generator(tmp_path)
  assert getattr(generator, 'marker', None) == 'real-backend'


def test_falls_back_to_bare_generator_with_codegen_toml(tmp_path: Path):
  """No `codegen/python.py`, but a `codegen/config.toml` exists -- this client has fully
  migrated onto the universal `Generator` (design §5c/Task 24a). The returned
  `Generator` gets `output_base` forced to the constant `''`, matching a fully
  mechanized client's single, unsplit function tree."""
  (tmp_path / 'truewire.toml').write_text(
    '[python]\n'
    'name = "X"\n'
    '\n'
    '[python.cores.default]\n'
    'base = "x.core:Endpoint"\n'
  )
  generator = load_generator(tmp_path)
  assert isinstance(generator, Generator)
  assert generator.output_base(None) == ''  # type: ignore[arg-type]


def test_raises_with_neither_backend_nor_codegen_toml(tmp_path: Path):
  """A client with no per-client backend and no `codegen/config.toml` at all has
  nothing this function can load -- unchanged from before this fallback existed."""
  with pytest.raises(BackendUnavailable):
    load_generator(tmp_path)


def test_no_fallback_for_non_python_language(tmp_path: Path):
  """The `codegen/config.toml` fallback is Python-specific (design §6's own `[python]`
  section) -- a hypothetical future non-Python backend gets no special treatment here."""
  (tmp_path / 'truewire.toml').write_text(
    '[python]\n'
    'name = "X"\n'
    '\n'
    '[python.cores.default]\n'
    'base = "x.core:Endpoint"\n'
  )
  with pytest.raises(BackendUnavailable):
    load_generator(tmp_path, language='rust')


def test_package_root_is_the_declared_package_under_the_declared_src(tmp_path: Path):
  """`package_root` reads `[python].src`/`[python].package` -- never guesses by scanning."""
  (tmp_path / 'truewire.toml').write_text(
    '[project]\nname = "widgets"\n[python]\npackage = "typed_widgets"\nsrc = "pkg/src"\n'
    '[python.cores.default]\nbase = "x.core:Endpoint"\n'
  )
  assert package_root(tmp_path) == tmp_path / 'pkg' / 'src' / 'typed_widgets'


def test_package_root_defaults_to_the_project_name_under_src(tmp_path: Path):
  """With no `[python].package`, the package is named after the project, under `src/`."""
  (tmp_path / 'truewire.toml').write_text(
    '[project]\nname = "widgets"\n[python]\n[python.cores.default]\nbase = "x.core:Endpoint"\n'
  )
  assert package_root(tmp_path) == tmp_path / 'src' / 'widgets'


def test_package_root_raises_without_a_python_section(tmp_path: Path):
  """A spec-only project has no package to generate."""
  (tmp_path / 'truewire.toml').write_text('[project]\nname = "widgets"\n')
  with pytest.raises(BackendUnavailable):
    package_root(tmp_path)
