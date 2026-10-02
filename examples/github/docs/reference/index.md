# Reference

Two routers, six endpoints, every one recorded against the live API. Each signature is
generated from the endpoint's schema, so the types in your editor are the reference: every
field carries its description.

## `repos`

| Endpoint | Upstream | Paged |
| --- | --- | --- |
| `get` | `GET /repos/{owner}/{repo}` | |
| `list_commits` | `GET /repos/{owner}/{repo}/commits` | `list_commits_paged` |
| `get_commit` | `GET /repos/{owner}/{repo}/commits/{ref}` | |
| `list_tags` | `GET /repos/{owner}/{repo}/tags` | `list_tags_paged` |
| `list_releases` | `GET /repos/{owner}/{repo}/releases` | `list_releases_paged` |

## `issues`

| Endpoint | Upstream | Paged |
| --- | --- | --- |
| `list` | `GET /repos/{owner}/{repo}/issues` | `list_paged` |

Every call takes `validate` (default `True`): `False` returns the body as GitHub sent it,
typed `Any`. In TypeScript, Rust and Go the names follow each language's convention:
`listCommitsPaged`, `list_commits_paged` and `ListCommitsPaged`.
