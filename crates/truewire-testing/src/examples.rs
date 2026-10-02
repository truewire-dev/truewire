use std::fs;
use std::path::{Path, PathBuf};

use serde_json::{Map, Value};

/// One recorded HTTP exchange: `examples/<id>.request.json` beside `<id>.response.json`.
#[derive(Debug, Clone, PartialEq)]
pub struct HttpExample {
    /// The endpoint's dotted function path (`spot.market_data.ticker`).
    pub function: String,
    pub id: String,
    /// The recorded request, keyed by the wire's own names (`{}` when it recorded none).
    pub request: Value,
    /// The recorded response file as it stands.
    pub response: Value,
}

/// Whether a WebSocket example belongs to a command or a subscription.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WsKind {
    Rpc,
    Stream,
}

/// One recorded WebSocket exchange: `examples/<id>.parameters.json`, with the optional
/// `<id>.reply.json` and `<id>.messages.json` beside it.
#[derive(Debug, Clone, PartialEq)]
pub struct WsExample {
    pub function: String,
    pub id: String,
    pub kind: WsKind,
    /// The method's parameters, keyed by the wire's own names (`{}` when none).
    pub parameters: Value,
    /// The frame the client sent, when recorded.
    pub payload: Option<Value>,
    /// The reply (or subscription acknowledgement), when recorded.
    pub reply: Option<Value>,
    /// The pushed messages, in order; empty for a command.
    pub messages: Vec<Value>,
}

/// The project's spec directory: `[spec].dir` in `truewire.toml`, `spec` when unset.
pub fn spec_dir(project: impl AsRef<Path>) -> PathBuf {
    let project = project.as_ref();
    let declared = fs::read_to_string(project.join("truewire.toml")).ok().and_then(|toml| {
        let mut in_spec = false;
        for line in toml.lines() {
            let line = line.trim();
            if line.starts_with('[') {
                in_spec = line == "[spec]";
            } else if in_spec {
                if let Some(value) = line
                    .strip_prefix("dir")
                    .map(str::trim)
                    .and_then(|rest| rest.strip_prefix('='))
                {
                    return Some(value.trim().trim_matches('"').to_string());
                }
            }
        }
        None
    });
    project.join(declared.unwrap_or_else(|| "spec".to_string()))
}

/// Every recorded HTTP example of the project, sorted by function path then id. Endpoints
/// whose `surface` is hand-written or absent are left out: nothing generated serves them;
/// so are gRPC endpoints, whose recordings `grpc_examples` reads.
pub fn http_examples(project: impl AsRef<Path>) -> Vec<HttpExample> {
    let mut out = Vec::new();
    for endpoint in endpoints(project.as_ref()) {
        // A gRPC recording shares the file names; `grpc_examples` (feature `grpc`) serves it.
        if endpoint.kind == "grpc" {
            continue;
        }
        for id in ids(&endpoint.examples, ".request.json") {
            let Some(response) = read(&endpoint.examples.join(format!("{id}.response.json"))) else {
                continue;
            };
            let recorded = read(&endpoint.examples.join(format!("{id}.request.json"))).unwrap_or(Value::Null);
            let request = recorded
                .get("request")
                .or_else(|| recorded.get("parameters"))
                .cloned()
                .unwrap_or_else(|| Value::Object(Map::new()));
            out.push(HttpExample {
                function: endpoint.function.clone(),
                id,
                request,
                response,
            });
        }
    }
    out
}

/// Every recorded WebSocket example of the project, sorted by function path then id.
pub fn ws_examples(project: impl AsRef<Path>) -> Vec<WsExample> {
    let mut out = Vec::new();
    for endpoint in endpoints(project.as_ref()) {
        let kind = match endpoint.kind.as_str() {
            "stream" => WsKind::Stream,
            "rpc" => WsKind::Rpc,
            _ => continue,
        };
        for id in ids(&endpoint.examples, ".parameters.json") {
            let recorded = read(&endpoint.examples.join(format!("{id}.parameters.json"))).unwrap_or(Value::Null);
            let messages = match read(&endpoint.examples.join(format!("{id}.messages.json"))) {
                Some(Value::Array(items)) => items,
                Some(other) => vec![other],
                None => Vec::new(),
            };
            out.push(WsExample {
                function: endpoint.function.clone(),
                id: id.clone(),
                kind,
                parameters: recorded
                    .get("parameters")
                    .cloned()
                    .unwrap_or_else(|| Value::Object(Map::new())),
                payload: recorded.get("payload").cloned(),
                reply: read(&endpoint.examples.join(format!("{id}.reply.json"))),
                messages,
            });
        }
    }
    out
}

pub(crate) struct Endpoint {
    pub(crate) function: String,
    pub(crate) kind: String,
    pub(crate) examples: PathBuf,
    /// The whole `endpoint.json`.
    #[cfg_attr(not(feature = "grpc"), allow(dead_code))]
    pub(crate) spec: Value,
}

pub(crate) fn endpoints(project: &Path) -> Vec<Endpoint> {
    let mut out = Vec::new();
    walk(&spec_dir(project).join("endpoints"), &mut Vec::new(), &mut out);
    out.sort_by(|a, b| a.function.cmp(&b.function));
    out
}

fn walk(dir: &Path, segments: &mut Vec<String>, out: &mut Vec<Endpoint>) {
    if let Some(spec) = read(&dir.join("endpoint.json")) {
        let surface = spec.pointer("/surface/kind").and_then(Value::as_str);
        if matches!(surface, Some("handwritten" | "absent")) {
            return;
        }
        out.push(Endpoint {
            function: spec
                .get("function")
                .and_then(Value::as_str)
                .map(str::to_string)
                .unwrap_or_else(|| segments.join(".")),
            kind: spec
                .pointer("/spec/kind")
                .and_then(Value::as_str)
                .unwrap_or("rpc")
                .to_string(),
            examples: dir.join("examples"),
            spec,
        });
        return;
    }
    let Ok(entries) = fs::read_dir(dir) else { return };
    let mut children: Vec<PathBuf> = entries
        .filter_map(|entry| entry.ok().map(|entry| entry.path()))
        .filter(|path| path.is_dir() && path.file_name() != Some("examples".as_ref()))
        .collect();
    children.sort();
    for child in children {
        segments.push(child.file_name().unwrap_or_default().to_string_lossy().to_string());
        walk(&child, segments, out);
        segments.pop();
    }
}

pub(crate) fn ids(examples: &Path, suffix: &str) -> Vec<String> {
    let Ok(entries) = fs::read_dir(examples) else {
        return Vec::new();
    };
    let mut ids: Vec<String> = entries
        .filter_map(|entry| entry.ok())
        .filter_map(|entry| {
            entry
                .file_name()
                .to_string_lossy()
                .strip_suffix(suffix)
                .map(str::to_string)
        })
        .collect();
    ids.sort();
    ids
}

pub(crate) fn read(path: &Path) -> Option<Value> {
    let text = fs::read_to_string(path).ok()?;
    serde_json::from_str(&text).ok()
}
