//! Generic replay coverage: every recorded HTTP example, through the real client.
//!
//! Each `spec/endpoints/**/examples/<id>.request.json` is replayed against the mock by
//! calling the generated method the endpoint's function path names, with the recorded
//! request decoded into its `Request` struct. The typed call proves the struct accepts
//! the recorded response; the `_raw` twin proves the typed value dumps back to exactly
//! the body the wire sent, so nothing was lost or reshaped on the way through.

mod common;

use std::fs;
use std::path::{Path, PathBuf};

use github::{issues, repos, CallOptions, GitHub};
use truewire_core::serde_json::Value;
use truewire_core::{decode, dump, Result};

use common::Mock;

struct Example {
    function: String,
    id: String,
    request: Value,
}

/// Every recorded HTTP example under `spec/endpoints/`, with the function path of its endpoint.
fn recorded_examples() -> Vec<Example> {
    let endpoints = Path::new(env!("CARGO_MANIFEST_DIR")).join("spec/endpoints");
    let mut out = Vec::new();
    walk(&endpoints, &mut Vec::new(), &mut out);
    out
}

fn walk(dir: &Path, segments: &mut Vec<String>, out: &mut Vec<Example>) {
    let spec = dir.join("endpoint.json");
    if spec.is_file() {
        let endpoint: Value = read_json(&spec);
        let function = endpoint
            .get("function")
            .and_then(Value::as_str)
            .map(str::to_string)
            .unwrap_or_else(|| segments.join("."));
        let mut files: Vec<PathBuf> = match fs::read_dir(dir.join("examples")) {
            Ok(entries) => entries.map(|entry| entry.expect("entry").path()).collect(),
            Err(_) => return,
        };
        files.sort();
        for file in files {
            let name = file.file_name().unwrap().to_string_lossy().to_string();
            let Some(id) = name.strip_suffix(".request.json") else {
                continue;
            };
            if !dir
                .join("examples")
                .join(format!("{id}.response.json"))
                .is_file()
            {
                continue;
            }
            let recorded: Value = read_json(&file);
            let request = recorded
                .get("request")
                .or_else(|| recorded.get("parameters"))
                .cloned()
                .unwrap_or_else(|| Value::Object(Default::default()));
            out.push(Example {
                function: function.clone(),
                id: id.to_string(),
                request,
            });
        }
        return;
    }
    let mut children: Vec<PathBuf> = fs::read_dir(dir)
        .expect("readable directory")
        .map(|entry| entry.expect("entry").path())
        .filter(|path| path.is_dir() && path.file_name() != Some("examples".as_ref()))
        .collect();
    children.sort();
    for child in children {
        segments.push(child.file_name().unwrap().to_string_lossy().to_string());
        walk(&child, segments, out);
        segments.pop();
    }
}

fn read_json(path: &Path) -> Value {
    truewire_core::parse_json(&fs::read_to_string(path).expect("readable file"))
        .expect("valid JSON")
}

/// Call the method a function path names, typed and raw: the typed value dumped back to
/// the wire, and the body as the wire sent it.
async fn replay(client: &GitHub, function: &str, request: Value) -> Result<(Value, Value)> {
    let options = CallOptions::default;
    macro_rules! call {
        ($router:ident, $method:ident, $raw:ident, $request:ty) => {{
            let request: $request = decode(request)?;
            let typed = client.$router.$method(request.clone(), options()).await?;
            let raw = client.$router.$raw(request, options()).await?;
            Ok((dump(&typed)?, raw))
        }};
    }
    match function {
        "issues.list" => call!(issues, list, list_raw, issues::list::Request),
        "repos.get" => call!(repos, get, get_raw, repos::get::Request),
        "repos.get_commit" => call!(
            repos,
            get_commit,
            get_commit_raw,
            repos::get_commit::Request
        ),
        "repos.list_commits" => {
            call!(
                repos,
                list_commits,
                list_commits_raw,
                repos::list_commits::Request
            )
        }
        "repos.list_releases" => {
            call!(
                repos,
                list_releases,
                list_releases_raw,
                repos::list_releases::Request
            )
        }
        "repos.list_tags" => call!(repos, list_tags, list_tags_raw, repos::list_tags::Request),
        other => panic!("{other}: no generated method to replay it through"),
    }
}

/// The first JSON pointer at which two values differ, for a readable failure.
fn first_difference(left: &Value, right: &Value, path: &str) -> Option<String> {
    match (left, right) {
        (Value::Object(a), Value::Object(b)) => {
            for key in a.keys().chain(b.keys().filter(|key| !a.contains_key(*key))) {
                match (a.get(key), b.get(key)) {
                    (Some(x), Some(y)) => {
                        if let Some(found) = first_difference(x, y, &format!("{path}/{key}")) {
                            return Some(found);
                        }
                    }
                    _ => return Some(format!("{path}/{key}: present on one side only")),
                }
            }
            None
        }
        (Value::Array(a), Value::Array(b)) => {
            if a.len() != b.len() {
                return Some(format!("{path}: {} vs {} items", a.len(), b.len()));
            }
            a.iter()
                .zip(b)
                .enumerate()
                .find_map(|(i, (x, y))| first_difference(x, y, &format!("{path}/{i}")))
        }
        _ if left == right => None,
        _ => Some(format!("{path}: {left} vs {right}")),
    }
}

#[tokio::test]
async fn recorded_http_examples_replay_through_the_generated_client() {
    let examples = recorded_examples();
    assert!(!examples.is_empty(), "no recordings found");
    let mock = Mock::start();
    let client = mock.client();
    for example in examples {
        let label = format!("{} ({})", example.function, example.id);
        let (typed, raw) = replay(&client, &example.function, example.request)
            .await
            .unwrap_or_else(|error| panic!("{label}: {error}"));
        if let Some(difference) = first_difference(&typed, &raw, "") {
            panic!("{label}: the typed value does not round-trip to the wire body at {difference}");
        }
    }
}
