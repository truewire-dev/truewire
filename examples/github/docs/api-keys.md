# API keys

Every call in this client reads public data, so none of them needs a key. Without one,
GitHub allows 60 requests an hour per IP address. With a token it allows 5,000 an hour per
user, and that is the only reason to add one.

## Getting a token

Create a **fine-grained personal access token** under GitHub → Settings → Developer
settings → Personal access tokens → Fine-grained tokens:

- **Repository access:** *Public repositories (read-only)*.
- **Permissions:** none. Public reads need none.
- **Expiration:** as short as your use allows.

Pass the token to the client, which sends it as `Authorization: Bearer`:

```python
import os

from github import GitHub

client = GitHub.new(api_key=os.environ['GITHUB_TOKEN'])
```

Keep the value in `.env` (git-ignored) or your shell's environment, never in code or in a
recording. `test/recapture.sh` reads the same `GITHUB_TOKEN`.

## What to refuse

- **Classic tokens.** Their scopes are account-wide, and the narrowest one that still
  covers repositories (`public_repo`) can also write to them.
- **Any write permission** (Contents, Issues, Pull requests, Administration). Nothing here
  writes, so a token that can write is only a liability.
- **Access to private repositories.** The spec describes public repositories. A private
  repository's responses would also put your own data in any recording made with the token.
