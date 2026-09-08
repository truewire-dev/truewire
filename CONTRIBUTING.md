# Contributing

Thanks for looking. This file covers the dev setup, how the tests run, where the two most common kinds of contribution go, and how decisions get recorded.

## Dev setup

The repository is a [uv](https://docs.astral.sh/uv/) workspace with two packages:

- `packages/truewire`: the CLI, spec loader, checks, mock server and Python generator.
- `packages/core-python`: `truewire-core`, the runtime that generated clients import.

```bash
git clone https://github.com/truewire-dev/truewire
cd truewire
uv sync --all-packages
uv run truewire --help
```

Python 3.11 or newer for the toolchain (the runtime supports 3.10). `uv sync` creates `.venv` and installs both packages in editable mode.

## Running tests

```bash
uv run pytest                                  # everything
uv run pytest packages/truewire/test           # toolchain only
uv run pytest packages/core-python/test        # runtime only
uv run pytest -k pagination                    # by keyword
(cd packages/core-ts && yarn test)             # the TypeScript runtime
(cd examples/github && yarn test)              # the TypeScript example, against truewire mock
```

Lint and format with ruff, using the config in the repository root:

```bash
uv run ruff check .
uv run ruff format .
```

CI runs all three. A pull request that fails any of them will not be reviewed until it passes.

Code style is short: 2-space indentation, single quotes, `typing_extensions` over `typing`, `TypedDict` and `Literal` over dataclasses and enums, a one-line docstring on every function, class and module. Read a file next to the one you are editing and match it.

## Adding a spec check

A check is a function in `packages/truewire/src/truewire/spec/authoring.py` that takes an endpoint's operation (a plain JSON view of its request and response schemas) and returns a list of violations. Each violation carries a `rule` id, a JSON path into the spec, and a message.

1. Write the rule in `docs/spec/authoring.md` first. State what is checked, why, and one concrete example of a wrong and a right spec. If you cannot write the "why" in two sentences, the check is probably a preference, not a rule.
2. Add `check_<name>` to `authoring.py`. Reuse `nodes()` to walk schemas and `is_ref()` to skip references. Keep it operation-local if you can; a check that needs the whole project (like `check_meta`) is wired separately in the CLI.
3. Append it to the `CHECKS` tuple.
4. Decide the severity. A check that can be wrong (a name heuristic, like the timestamp-suffix rule) goes into `WARNING_RULES` and reports as a warning. A check that is never wrong when it fires is an error. Warnings do not fail `truewire check`; errors do.
5. Add tests in `packages/truewire/test/test_authoring.py`: one endpoint that violates the rule, one that does not, and one that looks like a violation but is not.
6. Run `truewire check` against the example projects under `examples/` and fix or justify every new finding.

## Adding a pagination strategy

Pagination is a discriminated union on `strategy` in `packages/truewire/src/truewire/spec/endpoint.py`. Each strategy is a closed pydantic model. Adding one touches four places, in this order:

1. **Model.** Add a `<Name>Pagination` class in `endpoint.py` with `strategy: Literal['<name>']`, the parameters the walk needs, and a `done` terminator restricted to the kinds that strategy can actually decide. Add it to the `Pagination` union.
2. **Audit.** Extend `check_pagination` in `spec/authoring.py` so every parameter the block names is a real request parameter, every response path resolves against the response schema, and any parameter the walk does arithmetic on is numerically typed.
3. **Generator.** Add the walk in `packages/truewire/src/truewire/codegen/python.py`. `Generator.paged_method` renders the async-generator shape; `Generator.paged_response_method` and its siblings render the awaitable `PaginatedResponse` shape. Prefer the second where the strategy can compute `(rows, next_state)` per page, since it is strictly more useful to callers.
4. **Tests.** A model test (`test_endpoint_pagination_model.py`) for the declaration, a generator test for the emitted code, and a mock-backed test that walks at least three pages. One page cannot tell a correct walk from one that stops immediately.

Then document the strategy in `docs/spec/authoring.md` rule 8 with a worked `pagination` block, and open an ADR if the strategy changes what an existing declaration means.

## Architecture Decision Records

`docs/adr/` holds decisions that are hard to reverse, cost real evidence to reach, or trade one guarantee for another. Not a changelog. Not a restatement of what the code makes obvious.

Write one when a decision would otherwise survive only in a commit message or a pull request thread. Copy `docs/adr/0000-template.md`, take the next number, add a row to `docs/adr/README.md`. Three sections: Context (what forces were at play, what evidence prompted this), Decision (stated as a decision, not a description of the mechanism), Consequences (what gets easier, what gets harder, what is left open).

An ADR is never edited in place. A changed decision gets a new ADR that supersedes the old one, and the old one's `Status` line is updated to say so.

## Commits and pull requests

- One logical change per commit. Subject line under 72 characters, imperative mood, prefixed with the area: `spec:`, `check:`, `mock:`, `codegen:`, `core:`, `cli:`, `docs:`.
- The body says why, not what. The diff already says what.
- A pull request that changes generated output includes the regenerated example projects in the same PR, so reviewers see the effect.
- A pull request that changes the spec format includes the ADR, the authoring rule and the check together.

## Contributions written with Claude or another coding agent

Welcome. Much of Truewire was written this way. Two requests:

1. Say so in the pull request description, and say what you checked yourself. "Generated with Claude, I ran the tests and read the diff" is a fine sentence. "Generated with Claude" alone is not enough.
2. Every claim in a docstring, a doc page or an ADR has to be true of the code in the PR, not of the code the agent imagined. Reviewers will run the examples.

Agent-written PRs get the same review as any other: tests, a read of the diff, and a check that the ADR or doc rule matches the behavior. They are not held to a higher bar, and they are not waved through.

## Reporting problems

Open an issue with the spec (or a minimal `endpoint.json`), the command you ran, and what you expected. For a wrong generated type, include the recorded example that shows the real wire shape. That example is usually the whole fix.

## Releasing

Each package releases from its own pull request, merged into `main`:

1. Branch `release/core` (for `truewire-core`) or `release/truewire` (for `truewire`) off `main`.
2. Bump `version` in the package's `pyproject.toml` and add the entry to its `CHANGELOG.md`.
3. Open the pull request; its body becomes the top of the GitHub release notes.
4. Merge. `release-core.yml` / `release-truewire.yml` run the package's tests, build it, publish
   to PyPI (Trusted Publishing, no tokens), push the `truewire-core-v<version>` /
   `truewire-v<version>` tag, and create the GitHub release. A version already on PyPI is
   skipped, so re-merging is safe.

`truewire` depends on `truewire-core`, so when both change, release core first.

`@truewire/core` (`packages/core-ts`) releases to npm the same way:

1. Branch `release/core-ts` off `main`.
2. Bump `version` in `packages/core-ts/package.json` and turn the `## <version> (unreleased)`
   heading in `packages/core-ts/CHANGELOG.md` into the released one.
3. Open the pull request titled `Release @truewire/core <version>`; its body becomes the top
   of the GitHub release notes.
4. Merge. `release-core-ts.yml` runs `yarn typecheck`, `yarn test` and `yarn build`, checks
   that every entry point in `package.json` is in the tarball, runs `npm publish --access
   public --provenance`, pushes the `core-ts-v<version>` tag and creates the GitHub release.
   A version already on npm is skipped, so re-merging is safe.

npm has no trusted publisher here: the workflow authenticates with the `NPM_TOKEN` repository
secret, a granular access token for the `@truewire` org with publish rights on
`@truewire/core` (bypassing 2FA, since the workflow cannot answer a prompt). Without the
secret the publish step fails and nothing is tagged.
