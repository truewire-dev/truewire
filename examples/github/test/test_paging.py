"""The generated `_paged` walkers, driven by the recorded multi-page captures.

Each walk below replays a real sequence of pages recorded from api.github.com. The mock
serves page N only for the exact `page`/`per_page` the walk sends, so a walker that
mis-computed the next index would get a 422, not a quietly wrong result.
"""

import pytest


@pytest.mark.asyncio
async def test_commits_walk_ends_on_the_short_fourth_page(client):
  """Eleven commits at three per page: three full pages, then a page of two."""
  async with client:
    pages = [
      page
      async for page in client.repos.list_commits_paged(owner='truewire-dev', repo='truewire', per_page=3)
    ]
  assert [len(page) for page in pages] == [3, 3, 3, 2]
  shas = [commit['sha'] for page in pages for commit in page]
  assert len(set(shas)) == 11
  for page in pages:
    for commit in page:
      assert commit['commit']['author']['date'].tzinfo is not None


@pytest.mark.asyncio
async def test_commits_walk_flattens_when_awaited(client):
  async with client:
    commits = await client.repos.list_commits_paged(owner='truewire-dev', repo='truewire', per_page=3)
  assert len(commits) == 11
  assert commits[0]['commit']['message'].startswith('Release truewire 0.1.0')


@pytest.mark.asyncio
async def test_issues_walk_ends_on_an_empty_page(client):
  """Two merged pull requests at one per page: the third page is empty and ends the walk."""
  async with client:
    issues = await client.issues.list_paged(owner='truewire-dev', repo='truewire', state='all', per_page=1)
  assert [issue['number'] for issue in issues] == [2, 1]
  assert all(issue['pull_request']['merged_at'] is not None for issue in issues)
  assert all(issue['state'] == 'closed' for issue in issues)
