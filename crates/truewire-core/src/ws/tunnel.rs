//! A WebSocket connection through an HTTP proxy (packages clause P18).
//!
//! `tokio-tungstenite` has no proxy support, so the tunnel is opened here: `CONNECT
//! host:port` to the proxy, then the WebSocket handshake inside it, after TLS for `wss://`.
//! Every connection is a tunnel, `ws://` included, as Python's websockets and core-ts do.

use base64::Engine;
use percent_encoding::percent_decode_str;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio_tungstenite::{MaybeTlsStream, WebSocketStream};
use url::{Host, Url};

use crate::errors::{Error, Result};

/// The longest proxy reply head read before giving up on it.
const MAX_HEAD: usize = 16 * 1024;

/// Check a proxy URL for [`Socket::with_proxy`](super::Socket::with_proxy): `http://`
/// with a host. The error never repeats the URL, which may carry credentials.
pub(crate) fn parse_proxy(url: &str) -> Result<Url> {
    let proxy = Url::parse(url).map_err(|e| Error::logic("`proxy` is not a URL").with_source(e))?;
    if proxy.scheme() != "http" {
        return Err(Error::logic(format!(
            "A WebSocket `proxy` must be an http:// URL, not {}://",
            proxy.scheme()
        )));
    }
    if proxy.host().is_none() {
        return Err(Error::logic("`proxy` has no host"));
    }
    Ok(proxy)
}

/// Open `url` through `proxy`; any failure is an `Error::Network` naming `url`.
pub(crate) async fn connect(proxy: &Url, url: &str) -> Result<WebSocketStream<MaybeTlsStream<TcpStream>>> {
    let failed = |e: Error| Error::network(format!("Failed to connect to {url}")).with_source(e);
    let target = Url::parse(url).map_err(|e| failed(Error::logic("Not a URL").with_source(e)))?;
    let authority = match (target.host(), target.port_or_known_default()) {
        (Some(host), Some(port)) => format!("{host}:{port}"),
        _ => return Err(failed(Error::logic("No host and port to tunnel to"))),
    };
    let stream = open_tunnel(proxy, &authority).await.map_err(failed)?;
    handshake(url, &target, stream).await.map_err(failed)
}

/// Connect to the proxy and ask it for a tunnel to `authority`; the stream once it says yes.
async fn open_tunnel(proxy: &Url, authority: &str) -> Result<TcpStream> {
    let port = proxy.port_or_known_default().unwrap_or(80);
    let dial = match proxy.host() {
        Some(Host::Ipv6(address)) => TcpStream::connect((address, port)).await,
        Some(Host::Ipv4(address)) => TcpStream::connect((address, port)).await,
        Some(Host::Domain(domain)) => TcpStream::connect((domain, port)).await,
        None => return Err(Error::logic("`proxy` has no host")),
    };
    let mut stream = dial.map_err(|e| Error::network("Cannot reach the proxy").with_source(e))?;
    let mut head = format!("CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n");
    if let Some(credentials) = credentials(proxy) {
        head.push_str(&format!("Proxy-Authorization: Basic {credentials}\r\n"));
    }
    head.push_str("\r\n");
    let lost = |e: std::io::Error| Error::network("The proxy dropped the connection").with_source(e);
    stream.write_all(head.as_bytes()).await.map_err(lost)?;
    // Byte by byte: whatever follows the reply head belongs to the tunnel.
    let mut reply = Vec::new();
    while !reply.ends_with(b"\r\n\r\n") {
        if reply.len() >= MAX_HEAD {
            return Err(Error::network("The proxy's reply head is too long"));
        }
        let mut byte = [0u8];
        if stream.read(&mut byte).await.map_err(lost)? == 0 {
            return Err(Error::network(
                "The proxy closed the connection before answering CONNECT",
            ));
        }
        reply.push(byte[0]);
    }
    let status_line = String::from_utf8_lossy(&reply);
    let status_line = status_line.lines().next().unwrap_or_default();
    match status_line.split(' ').nth(1) {
        Some(code) if code.starts_with('2') && code.len() == 3 => Ok(stream),
        _ => Err(Error::network(format!(
            "The proxy refused CONNECT {authority}: {status_line}"
        ))),
    }
}

/// `user:password` from the proxy URL's userinfo, percent-decoded and base64-encoded.
fn credentials(proxy: &Url) -> Option<String> {
    if proxy.username().is_empty() && proxy.password().is_none() {
        return None;
    }
    let user = percent_decode_str(proxy.username()).decode_utf8_lossy();
    let password = percent_decode_str(proxy.password().unwrap_or_default()).decode_utf8_lossy();
    Some(base64::engine::general_purpose::STANDARD.encode(format!("{user}:{password}")))
}

/// The WebSocket handshake over the tunnel, TLS first for `wss://`.
#[cfg(any(feature = "rustls-tls", feature = "native-tls"))]
async fn handshake(url: &str, _target: &Url, stream: TcpStream) -> Result<WebSocketStream<MaybeTlsStream<TcpStream>>> {
    let (ws, _) = tokio_tungstenite::client_async_tls(url, stream)
        .await
        .map_err(|e| Error::network("The WebSocket handshake through the proxy failed").with_source(e))?;
    Ok(ws)
}

/// The WebSocket handshake over the tunnel; without a TLS feature, `ws://` only.
#[cfg(not(any(feature = "rustls-tls", feature = "native-tls")))]
async fn handshake(url: &str, target: &Url, stream: TcpStream) -> Result<WebSocketStream<MaybeTlsStream<TcpStream>>> {
    if target.scheme() == "wss" {
        return Err(Error::logic(
            "wss:// needs TLS: enable truewire-core's `rustls-tls` or `native-tls` feature",
        ));
    }
    let (ws, _) = tokio_tungstenite::client_async(url, MaybeTlsStream::Plain(stream))
        .await
        .map_err(|e| Error::network("The WebSocket handshake through the proxy failed").with_source(e))?;
    Ok(ws)
}
