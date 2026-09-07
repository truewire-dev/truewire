# Changelog

## 0.2.0 (2026-09-07)

- `truewire capture <function> --request '{...}'`: call one endpoint against the live API
  through the project's own generated client and core, and record the pair under the
  endpoint's `examples/`; `--scrub KEY` replaces a secret-bearing response field with a
  placeholder, and the pair is checked against the spec afterwards. Requires
  `truewire-core>=0.1.1`.
- A `page` walk ended by a short or empty page (the `page`/`per_page` shape most REST
  APIs use) now generates the awaitable `PaginatedResponse` wrapper, like token, seek
  and page-with-total walks.
- `truewire check` reports a `$ref` that resolves to nothing as a violation instead of
  crashing.
- `examples/github`: a second real project, captured live from the GitHub REST API.

## 0.1.0 (2026-09-07)

First release. `truewire init | import openapi | check | examples | surface | generate python |
mock | mcp | standards | docs`: a directory-per-endpoint spec format with an 18-rule linter,
recorded-example coverage with honest `unverified` reasons, an HTTP and WebSocket mock server
that replays recorded examples, a Python generator with runtime validation and pagination
walkers, an OpenAPI 3.0/3.1 importer, and an MCP server that exposes a project's endpoints
as tools. `init` writes a `pyproject.toml` so the generated client installs with
`pip install -e .`.
