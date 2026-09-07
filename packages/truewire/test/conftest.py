"""Shared test helpers for the toolchain suite."""
import sys
from contextlib import contextmanager
from importlib.abc import MetaPathFinder
from typing_extensions import Iterator


class _ForbidImport(MetaPathFinder):
  """A `sys.meta_path` finder that refuses any import of one top-level package."""

  def __init__(self, package: str):
    self.package = package

  def find_spec(self, fullname, path=None, target=None):
    if fullname == self.package or fullname.startswith(self.package + '.'):
      raise ImportError(
        f'{fullname}: importing the target package {self.package!r} during generation is '
        'forbidden (ADR 0011: the generator reads truewire.toml, never the live package)'
      )
    return None


@contextmanager
def forbid_import(package: str) -> Iterator[None]:
  """Refuse every import of `package` (and its submodules) inside the block, however it
  is attempted -- `import_module`, `import x`, a lazy loader -- so a test can prove the
  generator never reaches into the package it is generating."""
  finder = _ForbidImport(package)
  sys.meta_path.insert(0, finder)
  try:
    yield
  finally:
    sys.meta_path.remove(finder)
