"""Combined contracts: one trait per set of endpoint traits a single core must satisfy.

A struct holds its core as one trait object, and Rust spells a trait object with exactly
one non-auto trait: `Arc<dyn CommandEndpoint + StreamEndpoint>` does not compile. So when a
core has to be two things at once -- a dual-transport `rpc` endpoint (`HttpEndpoint<M> +
CommandEndpoint<M>`), a router whose endpoints mix commands and streams, a composite field
handed to both -- the generator names that set as one trait in `contract.rs`, with a blanket
impl so every core satisfying the parts satisfies the whole:

```rust
pub trait CommandStreamEndpoint: CommandEndpoint + StreamEndpoint {}

impl<T: CommandEndpoint + StreamEndpoint + ?Sized> CommandStreamEndpoint for T {}
```

`Arc<dyn CommandStreamEndpoint>` then coerces to `Arc<dyn StreamEndpoint>` where one child
needs only that half (trait upcasting), so a router hands the value it holds straight down.

A bound is spelled here as its atoms joined by ` + ` (`CommandEndpoint + StreamEndpoint`),
which is also how `core_shapes` carries it, so one string means the same set everywhere.
"""
from collections.abc import Iterable

from .meta import META_FILE
from .printer import BANNER, MAX_WIDTH, Imports, Writer
from .types import CORE, Module

CONTRACT_FILE = 'contract.rs'
CONTRACT_MODULE = 'contract'


def atoms(bounds: str | Iterable[str]) -> tuple[str, ...]:
  """The single traits a bound (or several) is made of, sorted and unique."""
  items = [bounds] if isinstance(bounds, str) else list(bounds)
  out: set[str] = set()
  for item in items:
    out.update(part.strip() for part in item.split(' + ') if part.strip())
  return tuple(sorted(out))


def bound_string(parts: Iterable[str]) -> str:
  return ' + '.join(atoms(parts))


def _split(atom: str) -> tuple[str, str | None]:
  """`HttpEndpoint<SpotMeta>` -> (`HttpEndpoint`, `SpotMeta`)."""
  if '<' in atom:
    return atom[:atom.index('<')], atom[atom.index('<') + 1:-1]
  return atom, None


def combined_name(parts: Iterable[str]) -> str:
  """`CommandEndpoint + StreamEndpoint` -> `CommandStreamEndpoint`;
  `HttpEndpoint<SpotMeta> + CommandEndpoint<SpotMeta>` -> `CommandSpotHttpSpotEndpoint`."""
  words: list[str] = []
  for atom in atoms(parts):
    trait, meta = _split(atom)
    words.append(trait.removesuffix('Endpoint') + (meta.removesuffix('Meta') if meta else ''))
  return ''.join(words) + 'Endpoint'


def import_atoms(imports: Imports, parts: Iterable[str]) -> None:
  for atom in atoms(parts):
    trait, meta = _split(atom)
    imports.add(CORE, trait)
    if meta is not None:
      imports.add(f'crate::{META_FILE[:-3]}', meta)


class Contracts:
  """The combined traits a package needs, collected while its modules render."""

  def __init__(self):
    self.sets: set[tuple[str, ...]] = set()

  def bound(self, module: Module, parts: Iterable[str]) -> str:
    """The bound as a `+`-list of its atoms (for `impl A + B` and `where C: A + B`),
    importing each."""
    import_atoms(module.imports, parts)
    return ' + '.join(atoms(parts))

  def holder(self, module: Module, parts: Iterable[str]) -> str:
    """The one trait a `dyn` holding this bound names: the atom itself, or the combined
    trait `contract.rs` declares for the set, importing whichever it is."""
    found = atoms(parts)
    if len(found) == 1:
      import_atoms(module.imports, found)
      return found[0]
    self.sets.add(found)
    name = combined_name(found)
    module.imports.add(f'crate::{CONTRACT_MODULE}', name)
    return name

  def render(self) -> str | None:
    """`contract.rs`, or `None` when no core has to satisfy more than one trait."""
    if not self.sets:
      return None
    imports = Imports()
    for found in self.sets:
      import_atoms(imports, found)
    w = Writer()
    w.line(BANNER)
    w.line('//!')
    w.doc(
      'Every set of endpoint traits one core has to satisfy at once, named as one trait so a '
      'struct can hold the core as a single `Arc<dyn ...>`. Each has a blanket impl: a core '
      'implementing the parts implements the whole, and nothing here is implemented by hand.',
      inner=True,
    )
    w.blank()
    for line in imports.render():
      w.line(line)
    for found in sorted(self.sets, key=combined_name):
      name = combined_name(found)
      joined = ' + '.join(found)
      # A smaller set this one contains is a supertrait too, so a value held as this trait
      # upcasts to the one a child holds (`user_client` handed to a router of commands and
      # streams, and to one of protobuf streams): trait objects upcast only to supertraits.
      within = sorted(combined_name(other) for other in self.sets if len(other) > 1 and set(other) < set(found))
      supertraits = ' + '.join([*found, *within])
      w.blank()
      w.doc(f'`{joined}` as one trait.')
      declaration = f'pub trait {name}: {supertraits} {{}}'
      # `rustfmt` splits a declaration that reaches the last column too (binance's is 100 wide).
      if len(declaration) < MAX_WIDTH:
        w.line(declaration)
      else:
        # `rustfmt`: the supertraits on their own line, and the empty body split.
        w.line(f'pub trait {name}:')
        with w.indented():
          if len(f'    {supertraits}') <= MAX_WIDTH:
            w.line(supertraits)
          else:
            # `rustfmt`: one bound per line, each after the first led by `+`.
            parts = [*found, *within]
            w.line(parts[0])
            for part in parts[1:]:
              w.line(f'+ {part}')
        w.line('{')
        w.line('}')
      w.blank()
      head = f'impl<T: {joined} + ?Sized> {name} for T {{}}'
      if len(head) <= MAX_WIDTH:
        w.line(head)
      else:
        # `rustfmt` keeps `where` on the head line of an empty impl, with no trailing comma.
        w.line(f'impl<T> {name} for T where')
        with w.indented():
          w.bounds('T: ', [*found, '?Sized'], '')
        w.line('{')
        w.line('}')
    return w.render()


__all__ = [
  'CONTRACT_FILE', 'CONTRACT_MODULE', 'Contracts', 'atoms', 'bound_string', 'combined_name', 'import_atoms',
]
