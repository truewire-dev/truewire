from dataclasses import dataclass, field
from typing_extensions import Collection
from truewire.generation.types import Imports as ImportsType

def is_std_lib(pkg: str) -> bool:
  import sys
  return pkg in sys.stdlib_module_names

@dataclass
class Imports:
  imports: ImportsType
  pkg_name: str | None = None
  """Name of the current package. Imports from this package will be ordered last."""
  max_line_length: int = field(default=80, kw_only=True)

  def package(self, pkg: str, names: Collection[str]) -> str:
    names = sorted(names)
    out = f'from {pkg} import '
    single_line = out + ', '.join(names)
    if len(single_line) <= self.max_line_length:
      return single_line
    else:
      out += '(\n  '
      out += ',\n  '.join(names)
      out += '\n)'
      return out

  def code(self) -> str:
    def order(pkg: str) -> int:
      first = pkg.split('.')[0]
      if first == self.pkg_name or pkg.startswith('.'):
        return 2 # local/relative imports
      elif is_std_lib(pkg):
        return 0 # stdlib imports
      else:
        return 1 # third-party imports

    packages = sorted(self.imports, key=lambda pkg: (order(pkg), pkg))
    return '\n'.join(self.package(pkg, self.imports[pkg]) for pkg in packages)