//! Unary gRPC calls (feature `grpc`).
//!
//! [`GrpcClient`] is a lazily connected channel with two ways to call it:
//!
//! - As a core: it implements [`GrpcEndpoint`] for every `Meta`, sending the encoded
//!   request a generated method hands it to `call.method` and returning the encoded reply,
//!   so a generated client over `prost` stubs (ADR 0017) needs nothing else.
//! - Untyped: [`GrpcClient::unary`] sends a JSON request to `/<service>/<rpc>` and answers
//!   with the JSON response, the descriptors coming from the [`Protos`] pool given to
//!   [`GrpcClient::with_protos`].
//!
//! A status the server returns maps onto the one error taxonomy: `UNAVAILABLE`,
//! `DEADLINE_EXCEEDED` and `CANCELLED` are network errors; `UNAUTHENTICATED`/
//! `PERMISSION_DENIED` auth; `RESOURCE_EXHAUSTED` rate limited; `INVALID_ARGUMENT`,
//! `NOT_FOUND`, `FAILED_PRECONDITION`, `OUT_OF_RANGE` and `ALREADY_EXISTS` bad requests;
//! anything else an API error, each carrying `{code, message}` as its body.

use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use bytes::{Buf, BufMut};
use prost_reflect::{DynamicMessage, MessageDescriptor};
use serde_json::{json, Value};
use tonic::codec::{Codec, DecodeBuf, Decoder, EncodeBuf, Encoder};
use tonic::transport::{Channel, ClientTlsConfig, Endpoint};
use tonic::{Code, Status};

use crate::contract::{GrpcCall, GrpcEndpoint};
use crate::errors::{Error, Result};
use crate::proto::{from_value, to_value, Protos};

/// A `tonic` codec over [`DynamicMessage`]s: encodes whatever it is given and decodes into
/// `decode`'s message type. A client decodes the method's output; a server its input.
#[derive(Debug, Clone)]
pub struct DynamicCodec {
    decode: MessageDescriptor,
}

impl DynamicCodec {
    pub fn new(decode: MessageDescriptor) -> Self {
        Self { decode }
    }
}

#[derive(Debug)]
pub struct DynamicEncoder;

#[derive(Debug)]
pub struct DynamicDecoder(MessageDescriptor);

impl Encoder for DynamicEncoder {
    type Item = DynamicMessage;
    type Error = Status;

    fn encode(&mut self, item: DynamicMessage, dst: &mut EncodeBuf<'_>) -> std::result::Result<(), Status> {
        prost::Message::encode(&item, dst).map_err(|e| Status::internal(e.to_string()))
    }
}

impl Decoder for DynamicDecoder {
    type Item = DynamicMessage;
    type Error = Status;

    fn decode(&mut self, src: &mut DecodeBuf<'_>) -> std::result::Result<Option<DynamicMessage>, Status> {
        DynamicMessage::decode(self.0.clone(), src)
            .map(Some)
            .map_err(|e| Status::internal(e.to_string()))
    }
}

impl Codec for DynamicCodec {
    type Encode = DynamicMessage;
    type Decode = DynamicMessage;
    type Encoder = DynamicEncoder;
    type Decoder = DynamicDecoder;

    fn encoder(&mut self) -> DynamicEncoder {
        DynamicEncoder
    }

    fn decoder(&mut self) -> DynamicDecoder {
        DynamicDecoder(self.decode.clone())
    }
}

/// A `tonic` codec passing encoded messages through untouched: what a call whose message
/// types only the caller knows (a generated method over `prost` stubs) is sent with.
#[derive(Debug, Clone, Copy, Default)]
pub struct BytesCodec;

#[derive(Debug)]
pub struct BytesEncoder;

#[derive(Debug)]
pub struct BytesDecoder;

impl Encoder for BytesEncoder {
    type Item = Vec<u8>;
    type Error = Status;

    fn encode(&mut self, item: Vec<u8>, dst: &mut EncodeBuf<'_>) -> std::result::Result<(), Status> {
        dst.put_slice(&item);
        Ok(())
    }
}

impl Decoder for BytesDecoder {
    type Item = Vec<u8>;
    type Error = Status;

    fn decode(&mut self, src: &mut DecodeBuf<'_>) -> std::result::Result<Option<Vec<u8>>, Status> {
        Ok(Some(src.copy_to_bytes(src.remaining()).to_vec()))
    }
}

impl Codec for BytesCodec {
    type Encode = Vec<u8>;
    type Decode = Vec<u8>;
    type Encoder = BytesEncoder;
    type Decoder = BytesDecoder;

    fn encoder(&mut self) -> BytesEncoder {
        BytesEncoder
    }

    fn decoder(&mut self) -> BytesDecoder {
        BytesDecoder
    }
}

/// A lazily connected gRPC channel, and optionally the descriptors untyped calls need.
#[derive(Debug, Clone)]
pub struct GrpcClient {
    url: String,
    channel: Channel,
    protos: Option<Arc<Protos>>,
    timeout: Option<Duration>,
}

impl GrpcClient {
    /// A client for `url` (`https://...` connects over TLS with the webpki roots,
    /// `http://...` is plaintext HTTP/2). Nothing connects until the first call.
    pub fn new(url: impl Into<String>) -> Result<Self> {
        let url = url.into();
        let mut endpoint =
            Endpoint::from_shared(url.clone()).map_err(|e| Error::logic(format!("bad gRPC URL {url}: {e}")))?;
        if url.starts_with("https://") {
            endpoint = endpoint
                .tls_config(ClientTlsConfig::new().with_webpki_roots())
                .map_err(|e| Error::logic(format!("TLS for {url}: {e}")))?;
        }
        Ok(Self {
            channel: endpoint.connect_lazy(),
            url,
            protos: None,
            timeout: None,
        })
    }

    /// The descriptors [`unary`](Self::unary) types its JSON calls by.
    pub fn with_protos(mut self, protos: Arc<Protos>) -> Self {
        self.protos = Some(protos);
        self
    }

    /// The default time a call may take before it fails with a `NetworkError`.
    pub fn timeout(mut self, timeout: Duration) -> Self {
        self.timeout = Some(timeout);
        self
    }

    pub fn protos(&self) -> Option<&Protos> {
        self.protos.as_deref()
    }

    /// Call `service`/`rpc` with `request` (a JSON value, `{}` for an empty message); needs
    /// [`with_protos`](Self::with_protos).
    pub async fn unary(&self, service: &str, rpc: &str, request: &Value, timeout: Option<Duration>) -> Result<Value> {
        let protos = self
            .protos
            .as_ref()
            .ok_or_else(|| Error::logic("a JSON gRPC call needs descriptors: build the client `with_protos`"))?;
        let descriptor = protos
            .pool()
            .get_service_by_name(service)
            .ok_or_else(|| Error::logic(format!("no service `{service}` in the descriptor pool")))?;
        let method = descriptor
            .methods()
            .find(|method| method.name() == rpc)
            .ok_or_else(|| Error::logic(format!("no method `{rpc}` on `{service}`")))?;
        if method.is_client_streaming() || method.is_server_streaming() {
            return Err(Error::logic(format!(
                "`{service}/{rpc}` is a streaming method; only unary calls are supported"
            )));
        }
        let message = from_value(method.input(), request)?;
        let response = self
            .send(
                &format!("/{service}/{rpc}"),
                message,
                DynamicCodec::new(method.output()),
                timeout,
            )
            .await?;
        to_value(&response)
    }

    /// Call `method` (`/<service>/<rpc>`) with an encoded request; the encoded reply.
    pub async fn call(&self, method: &str, request: Vec<u8>, timeout: Option<Duration>) -> Result<Vec<u8>> {
        self.send(method, request, BytesCodec, timeout).await
    }

    async fn send<C>(&self, method: &str, message: C::Encode, codec: C, timeout: Option<Duration>) -> Result<C::Decode>
    where
        C: Codec + Send + 'static,
        C::Encode: Send + Sync + 'static,
        C::Decode: Send + Sync + 'static,
    {
        let path: http::uri::PathAndQuery = method
            .parse()
            .map_err(|e| Error::logic(format!("`{method}` is not a valid gRPC path: {e}")))?;
        let label = format!("{}{method}", self.url.trim_end_matches('/'));
        let call = async {
            let mut grpc = tonic::client::Grpc::new(self.channel.clone());
            grpc.ready()
                .await
                .map_err(|e| Error::network(format!("{label}: the channel is not ready")).with_source(e))?;
            grpc.unary(tonic::Request::new(message), path, codec)
                .await
                .map_err(|status| map_status(&label, status))
        };
        let response = match timeout.or(self.timeout) {
            Some(limit) => tokio::time::timeout(limit, call)
                .await
                .map_err(|_| Error::network(format!("{label}: timed out after {limit:?}")))??,
            None => call.await?,
        };
        Ok(response.into_inner())
    }
}

#[async_trait]
impl<Meta: Send + Sync> GrpcEndpoint<Meta> for GrpcClient {
    async fn invoke(&self, call: GrpcCall<'_, Meta>) -> Result<Vec<u8>> {
        self.call(call.method, call.request, call.options.timeout).await
    }
}

/// A gRPC status as the `Error` its code calls for.
pub fn map_status(label: &str, status: Status) -> Error {
    let message = format!("{label}: {:?}: {}", status.code(), status.message());
    let body = json!({"code": status.code() as i32, "message": status.message()});
    let error = match status.code() {
        Code::Unavailable | Code::DeadlineExceeded | Code::Cancelled => return Error::network(message),
        Code::Unauthenticated | Code::PermissionDenied => Error::auth(message),
        Code::ResourceExhausted => Error::rate_limited(message),
        Code::InvalidArgument | Code::NotFound | Code::FailedPrecondition | Code::OutOfRange | Code::AlreadyExists => {
            Error::bad_request(message)
        }
        _ => Error::api(message),
    };
    error.with_body(body)
}
