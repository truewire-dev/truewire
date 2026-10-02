"""`python -m truewire.schemas [--check]`: write the published schemas, or check them."""
import sys

from .publish import PUBLISHED, published_dir, render


def main(argv: list[str]) -> int:
  """Write every schema of `PUBLISHED` into `published_dir()`; with `--check`, write nothing
  and exit 1 naming each file that differs from its model."""
  check = '--check' in argv
  stale = []
  for name in PUBLISHED:
    path = published_dir() / name
    text = render(name)
    if path.is_file() and path.read_text() == text:
      continue
    stale.append(name)
    if not check:
      path.write_text(text)
  for name in stale:
    print(f'{"stale" if check else "wrote"}: {published_dir() / name}')
  return 1 if check and stale else 0


if __name__ == '__main__':
  sys.exit(main(sys.argv[1:]))
