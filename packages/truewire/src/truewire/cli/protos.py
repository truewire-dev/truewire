"""`truewire protos`: build a language's protobuf stubs from `spec/proto/` (ADR 0017)."""
import typer

from .common import PROJECT_OPTION, resolve_project


def protos(
  language: str = typer.Argument(..., help='Target language: `typescript`, `go` or `rust`.'),
  project: str | None = PROJECT_OPTION,
  check: bool = typer.Option(False, '--check', help='Compare the stubs on disk with a fresh build; write nothing.'),
):
  """Build the protobuf stubs a generated gRPC client imports, from the project's `spec/proto/` tree.

  The tree is read without a compiler, stripped of custom options and of imports it does
  not hold, and handed to `buf generate` with the language's standard plugin
  (`protoc-gen-es` for TypeScript, `protoc-gen-go` for Go, `protoc-gen-prost` for Rust,
  which also gets the tree's descriptor set and a `mod.rs`). The stubs land in `protos/`
  under the generated package, a directory this command owns whole.

  Args:
    language: `typescript`, `go` or `rust`.
    project: Project directory (holding `truewire.toml`); the nearest one by default.
    check: Report stubs that are missing, stale or extra, and exit 1 when there are any.
  """
  from truewire.grpc.stubs import StubError, differences, render, write

  loaded = resolve_project(project)
  try:
    stub, files = render(loaded, language)
  except StubError as exc:
    typer.echo(str(exc), err=True)
    raise typer.Exit(code=1)
  try:
    where = stub.output.relative_to(loaded.root)
  except ValueError:
    where = stub.output
  if check:
    drift = differences(stub, files)
    if drift:
      typer.echo(f'{language} stubs in {where} differ from spec/proto/ (run `truewire protos {language}`):', err=True)
      for line in drift:
        typer.echo(f'  {line}', err=True)
      raise typer.Exit(code=1)
    typer.echo(f'{language} stubs in {where} are up to date ({len(files)} files)')
    return
  write(stub, files)
  typer.echo(f'Wrote {len(files)} {language} stub files into {where}')
