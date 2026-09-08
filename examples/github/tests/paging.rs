//! The generated `<method>_paged` walkers, driven by the recorded multi-page captures.
//!
//! Each walk below replays a real sequence of pages recorded from api.github.com. The
//! mock serves page N only for the exact `page`/`per_page` the walk sends, so a walker
//! that mis-computed the next index would get a 422, not a quietly wrong result. The same
//! walks as `test/test_paging.py`, through the Rust client.

mod common;

use std::collections::HashSet;

use futures::StreamExt;
use github::issues::list::{IssueState, ListPagedRequest, RequestState};
use github::repos::list_commits::ListCommitsPagedRequest;
use github::CallOptions;

use common::Mock;

/// The walks start from a fixed commit so re-recording them yields the same pages.
const RELEASE_0_1_0: &str = "934a718509b0cfd1244db90821201afbbb728797";

fn commits_request() -> ListCommitsPagedRequest {
    ListCommitsPagedRequest {
        owner: "truewire-dev".to_string(),
        repo: "truewire".to_string(),
        sha: Some(RELEASE_0_1_0.to_string()),
        per_page: Some(3),
        ..ListCommitsPagedRequest::default()
    }
}

#[tokio::test]
async fn commits_walk_ends_on_the_short_fourth_page() {
    // Eleven commits at three per page: three full pages, then a page of two.
    let mock = Mock::start();
    let client = mock.client();
    let paging = client
        .repos
        .list_commits_paged(commits_request(), CallOptions::default());
    let mut rows = paging.rows();
    let mut sizes = Vec::new();
    let mut shas = HashSet::new();
    while let Some(page) = rows.next().await {
        let page = page.expect("a page");
        sizes.push(page.len());
        for commit in &page {
            shas.insert(commit.sha.clone());
            assert!(commit.commit.author.date.timestamp() > 0);
        }
    }
    assert_eq!(sizes, [3, 3, 3, 2]);
    assert_eq!(shas.len(), 11);
}

#[tokio::test]
async fn commits_walk_flattens_when_awaited() {
    let mock = Mock::start();
    let client = mock.client();
    let commits = client
        .repos
        .list_commits_paged(commits_request(), CallOptions::default())
        .await
        .expect("every commit");
    assert_eq!(commits.len(), 11);
    assert!(commits[0]
        .commit
        .message
        .starts_with("Release truewire 0.1.0"));
}

#[tokio::test]
async fn issues_walk_ends_on_an_empty_page() {
    // Merged pull requests at one per page, newest first, until an empty page ends the
    // walk. The count moves with the repository (every release adds a pull request), so
    // the assertions are about the walk, not the number.
    let mock = Mock::start();
    let client = mock.client();
    let request = ListPagedRequest {
        owner: "truewire-dev".to_string(),
        repo: "truewire".to_string(),
        state: Some(RequestState::All),
        per_page: Some(1),
        ..ListPagedRequest::default()
    };
    let issues = client
        .issues
        .list_paged(request, CallOptions::default())
        .await
        .expect("every issue");
    let numbers: Vec<i64> = issues.iter().map(|issue| issue.number).collect();
    assert!(!numbers.is_empty());
    assert!(numbers.windows(2).all(|pair| pair[0] > pair[1]));
    for issue in &issues {
        let pull_request = issue.pull_request.as_ref().expect("a pull request");
        assert!(matches!(pull_request.merged_at, Some(Some(_))));
        assert_eq!(issue.state, IssueState::Closed);
    }
}
