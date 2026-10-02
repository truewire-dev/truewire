//! Displaying or debugging the proxy accessor must not disclose its credentials.

use serde_json::Value;
use truewire_core::ws::{Data, Dialect, Incoming, Outgoing, Socket, SocketOptions};
use truewire_core::Error;

struct Nop;

impl Dialect for Nop {
    type Request = Value;
    type Reply = Value;
    type Notification = Value;
    type Params = Value;
    fn parse(&self, _frame: Data) -> Result<Incoming<Value, Value>, Error> {
        unimplemented!()
    }
    fn encode_request(&self, _id: u64, _request: &Value) -> Result<Data, Error> {
        unimplemented!()
    }
    fn subscribe(&self, _channel: &str, _params: Option<&Value>) -> Result<Outgoing<Value>, Error> {
        unimplemented!()
    }
    fn unsubscribe(&self, _channel: &str, _params: Option<&Value>) -> Result<Outgoing<Value>, Error> {
        unimplemented!()
    }
}

#[test]
fn the_socket_proxy_accessor_never_shows_the_credentials() {
    let socket = Socket::new(Nop, SocketOptions::new("wss://ws.example.com/v2"))
        .with_proxy("http://alice:hunter2-secret@127.0.0.1:3128")
        .unwrap();
    let shown = socket.proxy().map(|url| url.to_string()).unwrap();
    let debugged = format!("{:?}", socket.proxy());
    for text in [&shown, &debugged] {
        assert!(!text.contains("hunter2-secret"), "password shown: {text}");
        assert!(!text.contains("alice"), "username shown: {text}");
    }
    assert_eq!(shown, "http://127.0.0.1:3128/");
}
