#!/usr/bin/env sh
# Re-record every example in this project from the live GitHub API with `truewire capture`.
# Run from the project root. Set GITHUB_TOKEN to raise the rate limit; the generated client
# sends it as a bearer token. `temp_clone_token` only appears on authenticated calls and is
# scrubbed to a placeholder (authoring rule 6). Re-running overwrites the recordings, so the
# values move with the repository they describe.
set -e
NEW="--new base_url=https://api.github.com"
if [ -n "$GITHUB_TOKEN" ]; then NEW="$NEW --new api_key=$GITHUB_TOKEN"; fi
R='"owner": "truewire-dev", "repo": "truewire"'
C="$R, \"sha\": \"934a718509b0cfd1244db90821201afbbb728797\""

truewire capture repos.get           $NEW --id default -d "The toolchain repository itself" --scrub temp_clone_token --request "{$R}"
truewire capture repos.list_tags     $NEW --id default -d "Release tags"                     --request "{$R, \"per_page\": 30, \"page\": 1}"
truewire capture repos.list_releases $NEW --id default -d "Published releases"               --request "{$R, \"per_page\": 30, \"page\": 1}"
truewire capture repos.list_commits  $NEW --id page1   -d "First page of three"              --request "{$C, \"per_page\": 3, \"page\": 1}"
truewire capture repos.list_commits  $NEW --id page2   -d "Second page of three"             --request "{$C, \"per_page\": 3, \"page\": 2}"
truewire capture repos.list_commits  $NEW --id page3   -d "Third page of three"              --request "{$C, \"per_page\": 3, \"page\": 3}"
truewire capture repos.list_commits  $NEW --id page4   -d "Last, short page"                 --request "{$C, \"per_page\": 3, \"page\": 4}"
truewire capture repos.get_commit    $NEW --id default -d "The 0.1.0 release merge commit"   --request "{$R, \"ref\": \"934a718509b0cfd1244db90821201afbbb728797\"}"
truewire capture issues.list         $NEW --id page1   -d "First closed pull request, one per page" --request "{$R, \"state\": \"all\", \"per_page\": 1, \"page\": 1}"
truewire capture issues.list         $NEW --id page2   -d "Second closed pull request"       --request "{$R, \"state\": \"all\", \"per_page\": 1, \"page\": 2}"
truewire capture issues.list         $NEW --id page3   -d "Past the end: an empty page"      --request "{$R, \"state\": \"all\", \"per_page\": 1, \"page\": 3}"
