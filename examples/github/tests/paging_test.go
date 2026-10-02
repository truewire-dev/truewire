// The generated `Paged` walkers, driven by the recorded multi-page captures.
//
// The mock serves page N only for the exact `page`/`per_page` the walk sends, so a walker
// that mis-computed the next index would get a 422, not a quietly wrong result. The same
// walks as `test/test_paging.py` and `tests/paging.rs`, through the Go client.
package tests

import (
	"context"
	"strings"
	"testing"

	truewire "truewire.dev/core"

	"truewire.dev/examples/github/src/github/issues/list"
	"truewire.dev/examples/github/src/github/repos/listcommits"
)

// release010 is the fixed commit the walks start from, so re-recording yields the same pages.
const release010 = "934a718509b0cfd1244db90821201afbbb728797"

func commitsRequest() listcommits.PagedRequest {
	return listcommits.PagedRequest{Owner: "truewire-dev", Repo: "truewire", Sha: truewire.Ptr(release010), PerPage: truewire.Ptr(int64(3))}
}

func TestCommitsWalkEndsOnTheShortFourthPage(t *testing.T) {
	c := client(t)
	var sizes []int
	shas := map[string]bool{}
	for rows, err := range c.Repos.ListCommitsPaged(commitsRequest()).Rows(context.Background()) {
		if err != nil {
			t.Fatal(err)
		}
		sizes = append(sizes, len(rows))
		for _, commit := range rows {
			shas[commit.Sha] = true
			if commit.Commit.Author.Date.Unix() <= 0 {
				t.Fatalf("commit %s has no author date", commit.Sha)
			}
		}
	}
	if len(sizes) != 4 || sizes[0] != 3 || sizes[3] != 2 || len(shas) != 11 {
		t.Fatalf("pages %v, %d distinct commits", sizes, len(shas))
	}
}

func TestCommitsWalkFlattensWithAll(t *testing.T) {
	commits, err := client(t).Repos.ListCommitsPaged(commitsRequest()).All(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(commits) != 11 || !strings.HasPrefix(commits[0].Commit.Message, "Release truewire 0.1.0") {
		t.Fatalf("%d commits", len(commits))
	}
}

func TestIssuesWalkEndsOnAnEmptyPage(t *testing.T) {
	// Merged pull requests at one per page, newest first, until an empty page ends the walk.
	state := list.RequestStateAll
	request := list.PagedRequest{Owner: "truewire-dev", Repo: "truewire", State: &state, PerPage: truewire.Ptr(int64(1))}
	issues, err := client(t).Issues.ListPaged(request).All(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(issues) == 0 {
		t.Fatal("no issues")
	}
	for i, issue := range issues {
		if i > 0 && issues[i-1].Number <= issue.Number {
			t.Fatalf("not newest first at %d", i)
		}
		if issue.PullRequest == nil || !issue.PullRequest.MergedAt.Set || issue.PullRequest.MergedAt.Value == nil {
			t.Fatalf("issue %d is not a merged pull request", issue.Number)
		}
		if issue.State != list.IssueStateClosed {
			t.Fatalf("issue %d is %s", issue.Number, issue.State)
		}
	}
}
