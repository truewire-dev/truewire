//! Unary gRPC calls against an in-process server that answers dynamic messages: the JSON
//! request reaches the server intact, the response comes back as JSON, and statuses and
//! timeouts map onto the error taxonomy.

use std::convert::Infallible;
use std::path::PathBuf;
use std::sync::Arc;
use std::task::{Context, Poll};
use std::time::Duration;

use futures::future::BoxFuture;
use prost_reflect::DynamicMessage;
use serde_json::{json, Value};
use tonic::server::{NamedService, UnaryService};
use tonic::{Code, Status};
use truewire_core::grpc::{DynamicCodec, GrpcClient};
use truewire_core::proto::{from_value, to_value, Protos};
use truewire_core::{ApiKind, CallOptions, GrpcCall, GrpcEndpoint};

fn protos() -> Arc<Protos> {
    // One directory per call: tests run in parallel and must not write each other's files.
    static NEXT: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
    let n = NEXT.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!("truewire-grpc-{}-{n}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(
        dir.join("echo.proto"),
        r#"syntax = "proto3";
package demo.v1;
message SayRequest { string text = 1; uint64 height = 2; uint32 delay_ms = 3; string fail = 4; }
message SayResponse { string text = 1; uint64 height = 2; }
service Echo { rpc Say(SayRequest) returns (SayResponse); rpc Watch(SayRequest) returns (stream SayResponse); }
"#,
    )
    .unwrap();
    Arc::new(Protos::compile(&[PathBuf::from("echo.proto")], &[dir]).unwrap())
}

#[derive(Clone)]
struct Echo {
    protos: Arc<Protos>,
}

impl NamedService for Echo {
    const NAME: &'static str = "demo.v1.Echo";
}

struct Say(Arc<Protos>);

impl UnaryService<DynamicMessage> for Say {
    type Response = DynamicMessage;
    type Future = BoxFuture<'static, Result<tonic::Response<DynamicMessage>, Status>>;

    fn call(&mut self, request: tonic::Request<DynamicMessage>) -> Self::Future {
        let protos = self.0.clone();
        Box::pin(async move {
            let value = to_value(request.get_ref()).map_err(|e| Status::internal(e.to_string()))?;
            let delay = value.get("delay_ms").and_then(Value::as_u64).unwrap_or(0);
            tokio::time::sleep(Duration::from_millis(delay)).await;
            match value.get("fail").and_then(Value::as_str) {
                Some("not_found") => return Err(Status::not_found("no such height")),
                Some("unauthenticated") => return Err(Status::unauthenticated("who are you")),
                Some("internal") => return Err(Status::internal("boom")),
                _ => {}
            }
            let reply = json!({"text": format!("echo: {}", value["text"].as_str().unwrap_or("")), "height": value.get("height").cloned().unwrap_or(json!("0"))});
            let message = from_value(protos.message("demo.v1.SayResponse").unwrap(), &reply)
                .map_err(|e| Status::internal(e.to_string()))?;
            Ok(tonic::Response::new(message))
        })
    }
}

impl tower::Service<http::Request<tonic::body::Body>> for Echo {
    type Response = http::Response<tonic::body::Body>;
    type Error = Infallible;
    type Future = BoxFuture<'static, Result<Self::Response, Infallible>>;

    fn poll_ready(&mut self, _cx: &mut Context<'_>) -> Poll<Result<(), Infallible>> {
        Poll::Ready(Ok(()))
    }

    fn call(&mut self, request: http::Request<tonic::body::Body>) -> Self::Future {
        let protos = self.protos.clone();
        Box::pin(async move {
            let input = protos.message("demo.v1.SayRequest").unwrap();
            let mut grpc = tonic::server::Grpc::new(DynamicCodec::new(input));
            Ok(grpc.unary(Say(protos), request).await)
        })
    }
}

async fn serve() -> (String, Arc<Protos>) {
    let protos = protos();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let service = Echo { protos: protos.clone() };
    tokio::spawn(async move {
        tonic::transport::Server::builder()
            .add_service(service)
            .serve_with_incoming(tokio_stream::wrappers::TcpListenerStream::new(listener))
            .await
            .unwrap();
    });
    (url, protos)
}

#[tokio::test]
async fn a_unary_call_sends_and_answers_json() {
    let (url, protos) = serve().await;
    let client = GrpcClient::new(url).unwrap().with_protos(protos);
    let reply = client
        .unary(
            "demo.v1.Echo",
            "Say",
            &json!({"text": "hi", "height": "18446744073709551615"}),
            None,
        )
        .await
        .unwrap();
    assert_eq!(reply, json!({"text": "echo: hi", "height": "18446744073709551615"}));
}

#[tokio::test]
async fn statuses_map_onto_the_error_taxonomy() {
    let (url, protos) = serve().await;
    let client = GrpcClient::new(url).unwrap().with_protos(protos);
    let call = |fail: &'static str| {
        let client = client.clone();
        async move {
            client
                .unary("demo.v1.Echo", "Say", &json!({"fail": fail}), None)
                .await
                .unwrap_err()
        }
    };
    let not_found = call("not_found").await;
    assert!(not_found.is_api(ApiKind::BadRequest), "{not_found}");
    assert_eq!(
        not_found.as_api().unwrap().body,
        Some(json!({"code": Code::NotFound as i32, "message": "no such height"}))
    );
    assert!(call("unauthenticated").await.is_api(ApiKind::Auth));
    assert!(call("internal").await.is_api(ApiKind::Api));
}

#[tokio::test]
async fn timeouts_refused_connections_and_bad_calls_fail_cleanly() {
    let (url, protos) = serve().await;
    let client = GrpcClient::new(url).unwrap().with_protos(protos.clone());
    let slow = client
        .unary(
            "demo.v1.Echo",
            "Say",
            &json!({"delay_ms": 500}),
            Some(Duration::from_millis(50)),
        )
        .await
        .unwrap_err();
    assert!(slow.is_network() && slow.message().contains("timed out"), "{slow}");
    assert!(client
        .unary("demo.v1.Nope", "Say", &json!({}), None)
        .await
        .unwrap_err()
        .is_logic());
    assert!(client
        .unary("demo.v1.Echo", "Nope", &json!({}), None)
        .await
        .unwrap_err()
        .is_logic());
    assert!(client
        .unary("demo.v1.Echo", "Watch", &json!({}), None)
        .await
        .unwrap_err()
        .is_logic());
    assert!(client
        .unary("demo.v1.Echo", "Say", &json!({"height": "tall"}), None)
        .await
        .unwrap_err()
        .is_validation());
    let refused = GrpcClient::new("http://127.0.0.1:1").unwrap().with_protos(protos);
    let error = refused
        .unary("demo.v1.Echo", "Say", &json!({}), Some(Duration::from_secs(5)))
        .await
        .unwrap_err();
    assert!(error.is_network(), "{error}");
}

#[tokio::test]
async fn the_client_is_a_core_for_encoded_messages() {
    let (url, protos) = serve().await;
    let client = GrpcClient::new(url.clone()).unwrap();
    let request = protos
        .encode("demo.v1.SayRequest", &json!({"text": "bytes", "height": "7"}))
        .unwrap();
    let call = GrpcCall {
        method: "/demo.v1.Echo/Say",
        request,
        meta: &(),
        options: CallOptions::default(),
    };
    let reply = GrpcEndpoint::<()>::invoke(&client, call).await.unwrap();
    assert_eq!(
        protos.decode("demo.v1.SayResponse", &reply).unwrap(),
        json!({"text": "echo: bytes", "height": "7"})
    );
    let untyped = client.unary("demo.v1.Echo", "Say", &json!({}), None).await.unwrap_err();
    assert!(untyped.is_logic(), "{untyped}");
    let failing = GrpcCall {
        method: "/demo.v1.Echo/Say",
        request: protos
            .encode("demo.v1.SayRequest", &json!({"fail": "not_found"}))
            .unwrap(),
        meta: &(),
        options: CallOptions::default(),
    };
    let error = GrpcEndpoint::<()>::invoke(&client, failing).await.unwrap_err();
    assert!(error.is_api(ApiKind::BadRequest), "{error}");
}
