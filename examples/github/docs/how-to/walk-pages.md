# Walk every page

GitHub's list endpoints return one page at a time, chosen by `page` and `per_page`. Every
list call here has a `_paged` twin, which starts at `page=1` and asks for the next page until
GitHub returns a short or empty one. It never computes a page number it was not given.

Iterate to handle one page at a time:

```python
from github import GitHub


async def commit_messages() -> None:
  async with GitHub.new() as client:
    async for page in client.repos.list_commits_paged(
      owner='truewire-dev', repo='truewire', per_page=50
    ):
      for commit in page:
        print(commit['sha'][:7], commit['commit']['message'].splitlines()[0])
```

Await it to get every row as one list:

```python
from github import GitHub


async def all_issue_numbers() -> list[int]:
  async with GitHub.new() as client:
    issues = await client.issues.list_paged(
      owner='truewire-dev', repo='truewire', state='all'
    )
    return [issue['number'] for issue in issues]
```

Each page is one request against the rate limit ([API keys](../api-keys.md)). A large
`per_page` (GitHub's maximum is 100) means fewer requests. Pin the starting point with `sha`
when you walk commits, so that a push in the middle of the walk cannot shift the pages.
