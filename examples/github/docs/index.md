# GitHub

GitHub's REST API serves the repositories GitHub hosts. This client covers the read side
of one repository: its metadata, commits, tags, releases, and issues with pull requests.

| Call | What it returns |
| --- | --- |
| `repos.get` | the repository: name, owner, visibility, counts, dates |
| `repos.list_commits` | one page of commits, newest first, from a branch, tag or SHA |
| `repos.get_commit` | one commit, with its files and stats |
| `repos.list_tags` | one page of tags |
| `repos.list_releases` | one page of published releases |
| `issues.list` | one page of issues and pull requests |

Each list call also has a `_paged` twin that walks every page (see
[Walk every page](how-to/walk-pages.md)).

## Why a validated client

GitHub returns large JSON objects in which many fields are optional or nullable, and the
difference matters. The spec records which is which from real responses, and the client
checks every response against it. A field GitHub stops sending fails loudly at the call
instead of turning up later as a `KeyError`. Dates arrive as timezone-aware `datetime`s,
not as strings. Pass `validate=False` to any call to get the body exactly as GitHub sent
it.

## First call

```python
import asyncio

from github import GitHub


async def main() -> None:
  async with GitHub.new() as client:
    repo = await client.repos.get(owner='truewire-dev', repo='truewire')
    print(repo['full_name'], repo['created_at'].year)


asyncio.run(main())
```

The same call in TypeScript, Rust and Go is in the quickstart (`docs.yml`). No key is
needed. [API keys](api-keys.md) explains when to add one.
