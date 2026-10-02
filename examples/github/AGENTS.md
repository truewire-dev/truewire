# GitHub

A typed client for a repository-scoped slice of the GitHub REST API, built with
[Truewire](https://truewire.dev) and kept in this repository as an example. This file is
the entrypoint an agent reads; `CLAUDE.md` points here.

## Layout

This is an in-repo example, so its code sits where the toolchain's own CI builds it, not in
the `packages/<language>/` tree of a standalone workspace:

- `spec/`: the specification. One `endpoint.json` per endpoint, recordings in `examples/`
  beside it. The spec is the product; everything else is rendered from it or describes it.
- `src/github/`: the four clients, generated from the spec over the hand-written core in
  `src/github/core/` (Python, TypeScript, Rust and Go files side by side).
  `truewire.toml` points every backend at `src`. `Cargo.toml`, `go.mod`, `package.json`
  and `pyrightconfig.json` sit at the root.
- `test/`: the Python and TypeScript suites. `tests/`: the Rust and Go suites. All four
  replay the recordings through `truewire mock`.
- `test/recapture.sh`: how every recording was produced, so `dev/capture/` is empty. It
  reads `$GITHUB_TOKEN` when set; it never contains one.
- `docs/`: the documentation site. `docs.yml` is its nav and quickstart.
- `.truewire/codegen/`: what codegen wrote, one manifest per language, committed.
- `.agents/skills/`, `.agents/rules/`: the skills to follow and the rules per language.
  `.claude/skills` and `.claude/rules` are symlinks to them.
- `truewire.toml`: the one project file.

## Credentials

Every endpoint here is public. A token is optional: it raises GitHub's rate limit, and
`test/recapture.sh` reads it from `GITHUB_TOKEN`. Keep it in `.env`, which is git-ignored,
and nowhere else.

## Gates

CI runs these from this directory (`.github/workflows/ci.yml`, jobs `examples*`):

```
truewire check
truewire standards
truewire generate python --check      # and typescript, rust, go
PYTHONPATH=src pytest -q
yarn install --frozen-lockfile && yarn typecheck && yarn test
cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo test
test -z "$(gofmt -l src tests)" && go vet ./... && go test ./...
```

`truewire standards` includes `docs check`, `examples --require-verified` and the layout
check.
