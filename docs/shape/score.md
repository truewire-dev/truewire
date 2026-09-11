# The scorecard

`truewire score` is the definition of *finished*, run as a command. One table, one exit
code, no partial credit and no adjectives.

```
$ truewire score
bit2me                                            python  typescript  rust
  coverage     every documented endpoint is specified     147/147
  recorded     every endpoint has a recording or reason   147/147
  check        the spec satisfies the authoring rules     pass
  generated    codegen is current and has no orphans      pass    pass        pass
  surface      every endpoint is reachable as a method    pass    pass        pass
  tests        the suite passes against the mock          pass    pass        FAIL  3 failed
  lint         format, lint and type-check                pass    pass        pass
  standards    docstrings, schemas, secrets, coverage     pass
  docs         required pages, every snippet type-checks  pass
  published    the current version is on every registry   pass    pass        pass
  conform      a dated report from the last run           unchecked
  stranger     a newcomer completed the quickstart        2026-10-12
  9 pass, 1 fail, 1 unchecked -> not done
```

## The rows

**S1. coverage** — every endpoint the upstream documentation describes appears in the
spec. The denominator comes from the inventory that `truewire-discovery` produced and a
person approved; a spec cannot mark itself complete against its own opinion of the API.

**S2. recorded** — every endpoint carries a recorded pair, or an `unverified` block with a
reason from the closed set. `truewire examples --require-verified`.

**S3. check** — `truewire check`.

**S4. generated** — `truewire generate --check` for every declared language: the tree
matches the spec and the manifest has no orphans.

**S5. surface** — `truewire surface`: every endpoint in the spec is reachable as a method
on the generated client, per language.

**S6. tests** — `truewire test`: every declared package's suite passes against
`truewire mock`.

**S7. lint** — `truewire lint`: formatter, linter and type checker clean, per package.

**S8. standards** — `truewire standards`: docstrings present, no duplicated schema, no
leaked secret, every router covered.

**S9. docs** — `truewire docs check`, plus the presence of the pages
[W11](workspace.md#documentation) requires: an index, an api-keys page, at least one
how-to, and a reference section.

**S10. published** — the version in each package's manifest is present on that language's
registry. A project whose packages are built but unpublished is not finished; a caller
cannot install a build.

**S11. conform** — a conformance report exists from the last scheduled run, its findings
are dated, and none is older than its triage window ([ADR 0012](../adr/0012-conformance-runs.md)).

**S12. stranger** — a developer who has never seen the project completed the quickstart
using only the published package and the public docs, and the date they did it. This row
is a date, not a check, and it is the only one a machine does not produce.

## Rules

**S13.** A row whose checker does not exist prints `unchecked`, never `pass`. A scorecard
that turns green by not looking is the one failure mode that would make the whole document
worthless.

**S14.** A row is `n/a` only for a language the project does not declare. There is no
`n/a` for a row a project finds inconvenient.

**S15.** `truewire score` exits zero only when every row is `pass`, every language column
included, `stranger` carries a date, and nothing is `unchecked`.

**S16.** The scorecard is per project. A toolchain release is judged by the scorecards of
the projects it can produce, and `bluesky`, `weather-gov` and `kraken` are run against
every release.

**S17.** Scores are recorded over time, one line per run, so that a change that improves
one row and quietly breaks another is visible. The history lives with the project, not in
anyone's memory.
