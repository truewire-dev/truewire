# Kraken

A typed client for Kraken Spot (REST, WebSocket v2 streams and WebSocket v2 trading),
built with [Truewire](https://truewire.dev) and kept in this repository as an example. This
file is the entrypoint an agent reads; `CLAUDE.md` points here.

## Layout

This is an in-repo example, so its code sits where the toolchain's own CI builds it, not in
the `packages/<language>/` tree of a standalone workspace:

- `spec/`: the specification. One `endpoint.json` per endpoint, with the upstream docs it
  was written from in `upstream.md` and recordings in `examples/` beside it. `spec/core.md`
  describes auth, the envelope and the two sockets. The spec is the product; everything
  else is rendered from it or describes it.
- `src/kraken/`: the four clients, generated from the spec over the hand-written core in
  `src/kraken/core/` (Python, TypeScript, Rust and Go files side by side).
  `truewire.toml` points every backend at `src`. `Cargo.toml`, `go.mod`, `package.json`
  and `pyrightconfig.json` sit at the root.
- `test/`: the Python and TypeScript suites. `tests/`: the Rust and Go suites. All four
  replay the recordings through `truewire mock`.
- `dev/capture/`: empty. The recordings came in with the example (66d76e37), and no
  capture session for them was kept. Record the session here when you re-record one.
- `docs/`: the documentation site. `docs.yml` is its nav and quickstart.
- `.truewire/codegen/`: what codegen wrote, one manifest per language, committed.
- `.agents/skills/`, `.agents/rules/`: the skills to follow and the rules per language.
  `.claude/skills` and `.claude/rules` are symlinks to them.
- `truewire.toml`: the one project file.

## Credentials

Private endpoints read `KRAKEN_API_KEY` and `KRAKEN_PRIVATE_KEY`. Their values go in `.env`,
which is git-ignored, and nowhere else. The tests never need them: they run against the
mock.

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
