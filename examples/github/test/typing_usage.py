"""Real, representative usage of `github`'s public surface, type-checked by pyright
(`pyrightconfig.json`) and never executed: the guardrail against a public return type
silently degrading, and the proof that `validate=False` is typed as what it returns.

`reveal_type(..., expected_text=...)` is pyright's own assertion: a mismatch is an error.
"""

from typing_extensions import reveal_type

from github import GitHub


async def validated_by_default(strict: bool) -> None:
  """The parsed record: by default, with `validate=True`, and with a flag decided
  elsewhere -- like the client-level default, that decision is the caller's own."""
  async with GitHub.new() as client:
    repo = await client.repos.get(owner='truewire-dev', repo='truewire')
    reveal_type(repo, expected_text='Repository')
    reveal_type(repo['created_at'], expected_text='datetime')
    reveal_type(
      await client.repos.get(owner='truewire-dev', repo='truewire', validate=True),
      expected_text='Repository',
    )
    reveal_type(
      await client.repos.get(owner='truewire-dev', repo='truewire', validate=strict),
      expected_text='Repository',
    )
    reveal_type(
      await client.repos.list_commits(owner='truewire-dev', repo='truewire', per_page=3),
      expected_text='list[Commit]',
    )
    reveal_type(
      client.repos.list_commits_paged(owner='truewire-dev', repo='truewire', per_page=3),
      expected_text='PaginatedResponse[Commit, int]',
    )
    reveal_type(
      client.issues.list_paged(owner='truewire-dev', repo='truewire', state='all'),
      expected_text='PaginatedResponse[Issue, int]',
    )


async def raw_bodies() -> None:
  """`validate=False` returns the body as the wire sent it, and says so: `Any`, and a
  walker's rows likewise, its state type kept."""
  async with GitHub.new() as client:
    raw = await client.repos.get(owner='truewire-dev', repo='truewire', validate=False)
    reveal_type(raw, expected_text='Any')
    reveal_type(
      await client.repos.list_commits(owner='truewire-dev', repo='truewire', validate=False),
      expected_text='Any',
    )
    reveal_type(
      client.repos.list_commits_paged(owner='truewire-dev', repo='truewire', validate=False),
      expected_text='PaginatedResponse[Any, int]',
    )
    reveal_type(
      client.issues.list_paged(owner='truewire-dev', repo='truewire', validate=False),
      expected_text='PaginatedResponse[Any, int]',
    )
