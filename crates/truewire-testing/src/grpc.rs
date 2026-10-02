//! gRPC replay (feature `grpc`, ADR 0017): an in-process fake gRPC server answering every
//! recorded gRPC example of a project, and the replay of each through a generated client.
//!
//! Recordings are `<id>.request.json` / `<id>.response.json` in proto JSON. They are read
//! with the descriptor set `truewire protos rust` writes beside the stubs (the generated
//! crate's `protos::FILE_DESCRIPTOR_SET`), so nothing here depends on the generated types.
//! A call whose request equals a recorded one (compared as messages, so field order,
//! spelled-out defaults and field-name spelling do not matter) gets that example's
//! response; a call no recording matches gets `NOT_FOUND`, and a method with no recording
//! at all `UNIMPLEMENTED`.
//!
//! A `google.protobuf.Any` is read in either spelling a recording uses: proto JSON's
//! `{"@type": url, ...fields}`, and `{"type_url": url, "value": base64}` (what betterproto
//! writes). An `@type` naming a message the descriptors do not hold cannot be encoded; the
//! mock leaves that example out and [`replay_grpc`] reports it skipped, naming the type.
//!
//! ```ignore
//! let protos = Arc::new(Protos::from_descriptor_set(grpc_demo::protos::FILE_DESCRIPTOR_SET)?);
//! let mock = GrpcMock::start(env!("CARGO_MANIFEST_DIR"), protos.clone()).await?;
//! let client = GrpcDemo::from_core(GrpcClient::new(mock.url())?);
//! let examples = grpc_examples(env!("CARGO_MANIFEST_DIR"));
//! let client = &client;
//! replay_grpc(&examples, &protos, |function, request| async move {
//!     client.call_grpc(&function, &request, CallOptions::default()).await
//! })
//! .await
//! .assert_passed();
//! ```

use std::collections::{HashMap, HashSet};
use std::convert::Infallible;
use std::fmt;
use std::future::Future;
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::task::{Context, Poll};

use base64::Engine;
use futures::future::BoxFuture;
use prost::Message;
use prost_reflect::{DynamicMessage, Kind, MapKey, MessageDescriptor, ReflectMessage, Value as ProtoValue};
use serde_json::{Map, Value};
use tonic::server::UnaryService;
use tonic::Status;
use truewire_core::grpc::DynamicCodec;
use truewire_core::proto::{from_value, to_value, Protos};

use crate::examples::{endpoints, ids, read};
use crate::json::first_difference;
use crate::replay::{replay, Labelled, Replayed, Report};

const ANY: &str = "google.protobuf.Any";

/// One recorded unary gRPC exchange.
#[derive(Debug, Clone, PartialEq)]
pub struct GrpcExample {
    /// The endpoint's dotted function path (`chain.bank.all_balances`).
    pub function: String,
    pub id: String,
    /// The call's HTTP/2 path, `/<package>.<Service>/<Rpc>`.
    pub method: String,
    /// The request and response messages' fully-qualified names.
    pub request_type: String,
    pub response_type: String,
    /// The recorded request message, proto JSON.
    pub request: Value,
    /// The recorded response message, proto JSON.
    pub response: Value,
}

/// Why a recorded message cannot be read.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RecordingError {
    /// An `Any` names a message the descriptors do not declare, so it cannot be encoded.
    Unresolvable(String),
    /// The recording does not decode as its message.
    Invalid(String),
}

impl fmt::Display for RecordingError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Unresolvable(url) => write!(f, "an Any names `{url}`, which the descriptors do not declare"),
            Self::Invalid(why) => f.write_str(why),
        }
    }
}

impl std::error::Error for RecordingError {}

impl GrpcExample {
    /// The recorded request, read as its message.
    pub fn request_message(&self, protos: &Protos) -> Result<DynamicMessage, RecordingError> {
        recorded(protos, &self.request_type, &self.request)
    }

    /// The recorded response, read as its message.
    pub fn response_message(&self, protos: &Protos) -> Result<DynamicMessage, RecordingError> {
        recorded(protos, &self.response_type, &self.response)
    }

    /// The recorded request, encoded.
    pub fn encode_request(&self, protos: &Protos) -> Result<Vec<u8>, RecordingError> {
        self.request_message(protos).map(|message| message.encode_to_vec())
    }

    /// Whether `reply` (the encoded response a generated method returned, re-encoded) is the
    /// recorded response, both compared as messages.
    pub fn compare(&self, protos: &Protos, reply: &[u8]) -> Replayed {
        let want = match self.response_message(protos) {
            Ok(want) => canonical(&want),
            Err(error) => return Replayed::Failed(format!("the recorded response: {error}")),
        };
        let got = match DynamicMessage::decode(want.descriptor(), reply) {
            Ok(got) => got,
            Err(error) => return Replayed::Failed(format!("the reply does not decode: {error}")),
        };
        if got == want {
            return Replayed::Ok;
        }
        let at = match (to_value(&got), to_value(&want)) {
            (Ok(got), Ok(want)) => first_difference(&got, &want).unwrap_or_default(),
            _ => String::new(),
        };
        Replayed::Failed(format!("the response differs from the recording at {at}"))
    }
}

impl Labelled for GrpcExample {
    fn label(&self) -> String {
        format!("{} ({})", self.function, self.id)
    }
}

/// Every recorded gRPC example of the project (`spec.kind: grpc`), sorted by function path
/// then id; hand-written and absent surfaces are left out, as [`http_examples`](crate::http_examples) does.
pub fn grpc_examples(project: impl AsRef<Path>) -> Vec<GrpcExample> {
    let mut out = Vec::new();
    for endpoint in endpoints(project.as_ref()) {
        if endpoint.kind != "grpc" {
            continue;
        }
        let spec = &endpoint.spec;
        let text = |key: &str| {
            spec.pointer(&format!("/spec/{key}"))
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string()
        };
        let method = format!("/{}/{}", text("service"), text("rpc"));
        for id in ids(&endpoint.examples, ".request.json") {
            let (Some(request), Some(response)) = (
                read(&endpoint.examples.join(format!("{id}.request.json"))),
                read(&endpoint.examples.join(format!("{id}.response.json"))),
            ) else {
                continue;
            };
            out.push(GrpcExample {
                function: endpoint.function.clone(),
                id,
                method: method.clone(),
                request_type: text("request"),
                response_type: text("response"),
                request,
                response,
            });
        }
    }
    out
}

/// Replay every example through `call` (function path, encoded request -> encoded reply,
/// the generated `call_grpc`), comparing each reply with its recording. An example whose
/// recording names a type the descriptors lack is skipped, naming it.
pub async fn replay_grpc<F, Fut>(examples: &[GrpcExample], protos: &Protos, call: F) -> Report
where
    F: Fn(String, Vec<u8>) -> Fut,
    Fut: Future<Output = truewire_core::Result<Vec<u8>>>,
{
    let call = &call;
    replay(examples, |example| async move {
        // Both recordings are read before the call: the mock leaves out an example whose
        // response it cannot encode, so calling it would only report `NOT_FOUND`.
        let read = example
            .encode_request(protos)
            .and_then(|request| example.response_message(protos).map(|_| request));
        let request = match read {
            Ok(request) => request,
            Err(RecordingError::Unresolvable(url)) => {
                return Ok(Replayed::Skipped(format!(
                    "the recording names `{url}`, which spec/proto does not declare"
                )))
            }
            Err(error) => return Err(format!("the recording: {error}")),
        };
        let reply = call(example.function.clone(), request)
            .await
            .map_err(|error| error.to_string())?;
        Ok::<_, String>(example.compare(protos, &reply))
    })
    .await
}

/// The message `name` a recording holds, read by its descriptor.
fn recorded(protos: &Protos, name: &str, value: &Value) -> Result<DynamicMessage, RecordingError> {
    let descriptor = protos
        .message(name)
        .map_err(|error| RecordingError::Invalid(error.to_string()))?;
    read_message(&descriptor, value)
}

/// A message encoded and decoded again: field order, defaults and unknown spellings gone, so
/// two messages meaning the same thing compare equal.
fn canonical(message: &DynamicMessage) -> DynamicMessage {
    DynamicMessage::decode(message.descriptor(), message.encode_to_vec().as_slice()).unwrap_or_else(|_| message.clone())
}

fn invalid(error: impl fmt::Display) -> RecordingError {
    RecordingError::Invalid(error.to_string())
}

/// Read a message, handling every `Any` inside it by hand (prost-reflect's JSON reader only
/// accepts `@type` naming a type it holds) and everything else through proto JSON.
fn read_message(descriptor: &MessageDescriptor, value: &Value) -> Result<DynamicMessage, RecordingError> {
    if descriptor.full_name() == ANY {
        return read_any(descriptor, value);
    }
    let Value::Object(object) = value else {
        return from_value(descriptor.clone(), value).map_err(invalid);
    };
    if !reaches_any(descriptor, &mut HashSet::new()) {
        return from_value(descriptor.clone(), value).map_err(invalid);
    }
    let mut plain = Map::new();
    let mut by_hand = Vec::new();
    for (key, item) in object {
        let field = descriptor
            .get_field_by_name(key)
            .or_else(|| descriptor.get_field_by_json_name(key));
        match field {
            Some(field)
                if message_kind(&field.kind()).is_some_and(|inner| reaches_any(&inner, &mut HashSet::new())) =>
            {
                by_hand.push((field, item))
            }
            _ => {
                plain.insert(key.clone(), item.clone());
            }
        }
    }
    let mut message = from_value(descriptor.clone(), &Value::Object(plain)).map_err(invalid)?;
    for (field, item) in by_hand {
        if item.is_null() {
            continue;
        }
        let value = if field.is_map() {
            let Some(entry) = message_kind(&field.kind()) else {
                return Err(invalid(format!("`{}` is a map without an entry message", field.name())));
            };
            let (key_field, value_field) = (entry.map_entry_key_field(), entry.map_entry_value_field());
            let Some(inner) = message_kind(&value_field.kind()) else {
                return Err(invalid(format!("`{}` maps to no message", field.name())));
            };
            let Value::Object(entries) = item else {
                return Err(invalid(format!("`{}` is not an object", field.name())));
            };
            let mut map = HashMap::new();
            for (key, entry_value) in entries {
                map.insert(
                    map_key(&key_field.kind(), key)?,
                    ProtoValue::Message(read_message(&inner, entry_value)?),
                );
            }
            ProtoValue::Map(map)
        } else {
            let Some(inner) = message_kind(&field.kind()) else {
                return Err(invalid(format!("`{}` is not a message field", field.name())));
            };
            if field.is_list() {
                let Value::Array(items) = item else {
                    return Err(invalid(format!("`{}` is not an array", field.name())));
                };
                let items: Result<Vec<_>, _> = items
                    .iter()
                    .map(|item| read_message(&inner, item).map(ProtoValue::Message))
                    .collect();
                ProtoValue::List(items?)
            } else {
                ProtoValue::Message(read_message(&inner, item)?)
            }
        };
        message.set_field(&field, value);
    }
    Ok(message)
}

fn read_any(descriptor: &MessageDescriptor, value: &Value) -> Result<DynamicMessage, RecordingError> {
    let mut any = DynamicMessage::new(descriptor.clone());
    let Value::Object(object) = value else {
        return Err(invalid("an Any is not an object"));
    };
    if let Some(url) = object.get("@type").and_then(Value::as_str) {
        let name = url.rsplit('/').next().unwrap_or(url);
        let inner = descriptor
            .parent_pool()
            .get_message_by_name(name)
            .ok_or_else(|| RecordingError::Unresolvable(url.to_string()))?;
        let message = if name.starts_with("google.protobuf.") {
            // A well-known type's JSON is its `value`, not its fields.
            from_value(inner, object.get("value").unwrap_or(&Value::Null)).map_err(invalid)?
        } else {
            let mut fields = object.clone();
            fields.remove("@type");
            read_message(&inner, &Value::Object(fields))?
        };
        any.set_field_by_name("type_url", ProtoValue::String(url.to_string()));
        any.set_field_by_name("value", ProtoValue::Bytes(message.encode_to_vec().into()));
        return Ok(any);
    }
    let url = object.get("type_url").or_else(|| object.get("typeUrl"));
    if let Some(url) = url.and_then(Value::as_str) {
        any.set_field_by_name("type_url", ProtoValue::String(url.to_string()));
    }
    if let Some(encoded) = object.get("value").and_then(Value::as_str) {
        any.set_field_by_name("value", ProtoValue::Bytes(base64_bytes(encoded)?.into()));
    }
    Ok(any)
}

fn base64_bytes(encoded: &str) -> Result<Vec<u8>, RecordingError> {
    use base64::engine::general_purpose::{STANDARD, STANDARD_NO_PAD, URL_SAFE, URL_SAFE_NO_PAD};
    [STANDARD, STANDARD_NO_PAD, URL_SAFE, URL_SAFE_NO_PAD]
        .iter()
        .find_map(|engine| engine.decode(encoded).ok())
        .ok_or_else(|| invalid("an Any's `value` is not base64"))
}

fn message_kind(kind: &Kind) -> Option<MessageDescriptor> {
    match kind {
        Kind::Message(message) => Some(message.clone()),
        _ => None,
    }
}

fn map_key(kind: &Kind, key: &str) -> Result<MapKey, RecordingError> {
    let bad = || invalid(format!("`{key}` is not a map key of this map"));
    Ok(match kind {
        Kind::String => MapKey::String(key.to_string()),
        Kind::Bool => MapKey::Bool(key.parse().map_err(|_| bad())?),
        Kind::Int32 | Kind::Sint32 | Kind::Sfixed32 => MapKey::I32(key.parse().map_err(|_| bad())?),
        Kind::Int64 | Kind::Sint64 | Kind::Sfixed64 => MapKey::I64(key.parse().map_err(|_| bad())?),
        Kind::Uint32 | Kind::Fixed32 => MapKey::U32(key.parse().map_err(|_| bad())?),
        Kind::Uint64 | Kind::Fixed64 => MapKey::U64(key.parse().map_err(|_| bad())?),
        _ => return Err(bad()),
    })
}

/// Whether a message is, or holds at any depth, a `google.protobuf.Any`.
fn reaches_any(descriptor: &MessageDescriptor, seen: &mut HashSet<String>) -> bool {
    if descriptor.full_name() == ANY {
        return true;
    }
    if !seen.insert(descriptor.full_name().to_string()) {
        return false;
    }
    descriptor
        .fields()
        .any(|field| message_kind(&field.kind()).is_some_and(|inner| reaches_any(&inner, seen)))
}

/// Why a [`GrpcMock`] did not start.
#[derive(Debug)]
pub enum GrpcMockError {
    /// A recording names a message the descriptors do not hold, or does not decode as it.
    Recording(String),
    Io(std::io::Error),
}

impl fmt::Display for GrpcMockError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Recording(why) => write!(f, "grpc mock: {why}"),
            Self::Io(error) => write!(f, "grpc mock: {error}"),
        }
    }
}

impl std::error::Error for GrpcMockError {}

struct Recorded {
    function: String,
    request: DynamicMessage,
    response: DynamicMessage,
}

#[derive(Clone)]
struct Answers {
    by_method: Arc<HashMap<String, Vec<Recorded>>>,
    answered: Arc<Mutex<HashMap<String, usize>>>,
}

/// An in-process gRPC server (plaintext HTTP/2 on `127.0.0.1`) answering every recorded gRPC
/// example of a project; stopped when dropped.
pub struct GrpcMock {
    url: String,
    answered: Arc<Mutex<HashMap<String, usize>>>,
    left_out: Vec<(String, String)>,
    shutdown: Option<tokio::sync::oneshot::Sender<()>>,
}

impl GrpcMock {
    /// Start a mock for the project at `project` on a free port, reading recordings with
    /// `protos`. Must be called inside a Tokio runtime. A recording that does not decode is
    /// an error; one naming a type the descriptors lack is left out ([`left_out`](Self::left_out)).
    pub async fn start(project: impl AsRef<Path>, protos: Arc<Protos>) -> Result<Self, GrpcMockError> {
        let mut by_method: HashMap<String, Vec<Recorded>> = HashMap::new();
        let mut left_out = Vec::new();
        for example in grpc_examples(project) {
            let label = example.label();
            let read = example
                .request_message(&protos)
                .and_then(|request| Ok((canonical(&request), example.response_message(&protos)?)));
            let (request, response) = match read {
                Ok(read) => read,
                Err(RecordingError::Unresolvable(url)) => {
                    left_out.push((label, url));
                    continue;
                }
                Err(error) => return Err(GrpcMockError::Recording(format!("{label}: {error}"))),
            };
            by_method.entry(example.method).or_default().push(Recorded {
                function: example.function,
                request,
                response,
            });
        }
        let answered = Arc::new(Mutex::new(HashMap::new()));
        let answers = Answers {
            by_method: Arc::new(by_method),
            answered: answered.clone(),
        };
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .map_err(GrpcMockError::Io)?;
        let url = format!("http://{}", listener.local_addr().map_err(GrpcMockError::Io)?);
        let router = axum::Router::new().fallback_service(Fallback(answers));
        let (shutdown, stop) = tokio::sync::oneshot::channel::<()>();
        tokio::spawn(async move {
            let _ = tonic::transport::Server::builder()
                .add_routes(tonic::service::Routes::from(router))
                .serve_with_incoming_shutdown(tokio_stream::wrappers::TcpListenerStream::new(listener), async {
                    let _ = stop.await;
                })
                .await;
        });
        Ok(Self {
            url,
            answered,
            left_out,
            shutdown: Some(shutdown),
        })
    }

    /// `http://127.0.0.1:<port>`, for `GrpcClient::new`.
    pub fn url(&self) -> &str {
        &self.url
    }

    /// How many calls of `function` the mock answered with a recording.
    pub fn answered(&self, function: &str) -> usize {
        self.answered
            .lock()
            .map(|counts| counts.get(function).copied().unwrap_or(0))
            .unwrap_or(0)
    }

    /// The examples not served, `function (id)` and the `Any` type URL the descriptors lack.
    pub fn left_out(&self) -> &[(String, String)] {
        &self.left_out
    }
}

impl Drop for GrpcMock {
    fn drop(&mut self) {
        if let Some(shutdown) = self.shutdown.take() {
            let _ = shutdown.send(());
        }
    }
}

/// The one service behind every path: routes a call to the recordings of its method.
#[derive(Clone)]
struct Fallback(Answers);

impl tower_service::Service<http::Request<axum::body::Body>> for Fallback {
    type Response = http::Response<tonic::body::Body>;
    type Error = Infallible;
    type Future = BoxFuture<'static, Result<Self::Response, Infallible>>;

    fn poll_ready(&mut self, _cx: &mut Context<'_>) -> Poll<Result<(), Infallible>> {
        Poll::Ready(Ok(()))
    }

    fn call(&mut self, request: http::Request<axum::body::Body>) -> Self::Future {
        let answers = self.0.clone();
        Box::pin(async move {
            let method = request.uri().path().to_string();
            let Some(input) = answers
                .by_method
                .get(&method)
                .and_then(|recorded| recorded.first())
                .map(|first| first.request.descriptor())
            else {
                let status = Status::unimplemented(format!("grpc mock: no recorded example for {method}"));
                return Ok(status.into_http());
            };
            let mut grpc = tonic::server::Grpc::new(DynamicCodec::new(input));
            let request = request.map(tonic::body::Body::new);
            Ok(grpc.unary(Answer { answers, method }, request).await)
        })
    }
}

struct Answer {
    answers: Answers,
    method: String,
}

impl UnaryService<DynamicMessage> for Answer {
    type Response = DynamicMessage;
    type Future = BoxFuture<'static, Result<tonic::Response<DynamicMessage>, Status>>;

    fn call(&mut self, request: tonic::Request<DynamicMessage>) -> Self::Future {
        let answers = self.answers.clone();
        let method = self.method.clone();
        Box::pin(async move {
            let got = canonical(request.get_ref());
            let recorded = answers.by_method.get(&method).map(Vec::as_slice).unwrap_or_default();
            for example in recorded {
                if example.request == got {
                    if let Ok(mut counts) = answers.answered.lock() {
                        *counts.entry(example.function.clone()).or_default() += 1;
                    }
                    return Ok(tonic::Response::new(example.response.clone()));
                }
            }
            let function = recorded.first().map(|first| first.function.as_str()).unwrap_or("?");
            let shown = to_value(&got)
                .map(|value| value.to_string())
                .unwrap_or_else(|_| format!("{got:?}"));
            Err(Status::not_found(format!(
                "grpc mock: {function}: no recorded example matches the request {shown}"
            )))
        })
    }
}
