//! Protobuf messages as JSON values (feature `proto`).
//!
//! A generated client and its core speak `serde_json::Value` on both sides of every call,
//! so a protobuf API is met in that form too: a [`Protos`] pool holds the message
//! descriptors -- compiled in-process from `.proto` sources with `protox` (no `protoc`
//! needed), or loaded from a serialized `FileDescriptorSet` -- and turns a message's bytes
//! into a JSON value and back. The JSON form is proto3's, with the `.proto` file's own field
//! names (`delegator_address`, the names recorded examples use) and 64-bit integers as
//! strings; decoding accepts either field-name spelling.
//!
//! [`narrow`] is the one-field read a wrapper message with a `oneof` body calls for: a
//! channel whose pushes all share one envelope type and differ by which field is set.

use std::path::Path;

use prost::Message;
use prost_reflect::{
    DescriptorPool, DeserializeOptions, DynamicMessage, MessageDescriptor, ReflectMessage, SerializeOptions,
};
use serde_json::Value;

use crate::errors::{Error, Result};

/// A pool of message and service descriptors.
#[derive(Debug, Clone)]
pub struct Protos {
    pool: DescriptorPool,
}

impl Protos {
    /// Compile `files` (paths relative to one of `includes`) and everything they import.
    pub fn compile(files: &[impl AsRef<Path>], includes: &[impl AsRef<Path>]) -> Result<Self> {
        let set =
            protox::compile(files, includes).map_err(|e| Error::logic(format!("cannot compile the protos: {e}")))?;
        let pool = DescriptorPool::from_file_descriptor_set(set).map_err(|e| Error::logic(e.to_string()))?;
        Ok(Self { pool })
    }

    /// Compile `.proto` sources held in memory, each keyed by the path an `import` names it
    /// by: a generated package's `protos::SOURCES` (ADR 0016), so no file is read at run
    /// time. The well-known `google/protobuf/*.proto` files resolve too.
    pub fn from_sources(sources: &[(&str, &str)]) -> Result<Self> {
        let mut resolver = protox::file::ChainFileResolver::new();
        resolver.add(Sources(
            sources
                .iter()
                .map(|(name, text)| (name.to_string(), text.to_string()))
                .collect(),
        ));
        resolver.add(protox::file::GoogleFileResolver::new());
        let mut compiler = protox::Compiler::with_file_resolver(resolver);
        compiler.include_imports(true);
        for (name, _) in sources {
            compiler
                .open_file(name)
                .map_err(|e| Error::logic(format!("cannot compile the protos: {e}")))?;
        }
        let pool = DescriptorPool::from_file_descriptor_set(compiler.file_descriptor_set())
            .map_err(|e| Error::logic(e.to_string()))?;
        Ok(Self { pool })
    }

    /// Load a serialized `google.protobuf.FileDescriptorSet` (`protoc -o`, `buf build -o`).
    pub fn from_descriptor_set(bytes: &[u8]) -> Result<Self> {
        let pool = DescriptorPool::decode(bytes).map_err(|e| Error::logic(format!("not a FileDescriptorSet: {e}")))?;
        Ok(Self { pool })
    }

    /// The underlying pool, for services and anything else not wrapped here.
    pub fn pool(&self) -> &DescriptorPool {
        &self.pool
    }

    /// The descriptor of the fully-qualified message `name` (`demo.v1.EchoRequest`).
    pub fn message(&self, name: &str) -> Result<MessageDescriptor> {
        self.pool
            .get_message_by_name(name)
            .ok_or_else(|| Error::logic(format!("no message `{name}` in the descriptor pool")))
    }

    /// The message `name` encoded in `bytes`, as a JSON value.
    pub fn decode(&self, name: &str, bytes: &[u8]) -> Result<Value> {
        let message = DynamicMessage::decode(self.message(name)?, bytes)
            .map_err(|e| Error::validation(format!("`{name}`: not a valid message: {e}")))?;
        to_value(&message)
    }

    /// `value` encoded as the message `name`.
    pub fn encode(&self, name: &str, value: &Value) -> Result<Vec<u8>> {
        Ok(from_value(self.message(name)?, value)?.encode_to_vec())
    }
}

/// In-memory `.proto` sources by import name.
struct Sources(std::collections::HashMap<String, String>);

impl protox::file::FileResolver for Sources {
    fn open_file(&self, name: &str) -> std::result::Result<protox::file::File, protox::Error> {
        match self.0.get(name) {
            Some(source) => protox::file::File::from_source(name, source),
            None => Err(protox::Error::file_not_found(name)),
        }
    }
}

/// A message as a JSON value, with proto field names and default fields left out.
pub fn to_value(message: &DynamicMessage) -> Result<Value> {
    let options = SerializeOptions::new().use_proto_field_name(true);
    message
        .serialize_with_options(serde_json::value::Serializer, &options)
        .map_err(|e| Error::validation(format!("`{}`: {e}", message.descriptor().full_name())))
}

/// A JSON value as a message of `descriptor`; unknown fields are ignored, as proto3 JSON does.
pub fn from_value(descriptor: MessageDescriptor, value: &Value) -> Result<DynamicMessage> {
    let name = descriptor.full_name().to_string();
    let options = DeserializeOptions::new().deny_unknown_fields(false);
    DynamicMessage::deserialize_with_options(descriptor, value.clone(), &options)
        .map_err(|e| Error::validation(format!("`{name}`: {e}")))
}

/// The field `field` of a decoded wrapper message, or `None` when this message carries
/// another branch of its `oneof` (or the field is unset).
pub fn narrow(wrapper: &Value, field: &str) -> Option<Value> {
    wrapper.get(field).filter(|value| !value.is_null()).cloned()
}
