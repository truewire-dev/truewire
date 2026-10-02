//! `spot.account.retrieve_export`, hand-written (the spec's `surface`): the reply is a zip
//! archive, not a value a schema describes, so no method is generated. It is an inherent
//! `impl` on the generated `Account` router, sent through the core the router keeps.
//!
//! The router holds its core as `Arc<dyn HttpEndpoint<SpotMeta>>`, whose replies are JSON
//! values, so [`SpotCore`](super::SpotCore) answers this one path with the archive as a
//! base64 string, decoded back to bytes here.

use base64::Engine;
use truewire_core::serde_json::{json, Value};
use truewire_core::{CallOptions, Error, HttpCall, Result};

use super::spot::EXPORT_PATH;
use crate::meta::SpotMeta;
use crate::spot::account::Account;

impl Account {
    /// Retrieve a processed data export: the zip archive's bytes. Unlike every other
    /// Account Data endpoint, the response is not the `{error, result}` JSON envelope.
    ///
    /// **API Key Permissions Required:** `Data - Export data`
    ///
    /// See <https://docs.kraken.com/api-reference/account-data/retrieve-data-export>.
    pub async fn retrieve_export(&self, id: &str, options: CallOptions) -> Result<Vec<u8>> {
        let meta = SpotMeta { signed: Some(true) };
        let call = HttpCall {
            method: Some("POST"),
            path: EXPORT_PATH,
            request: Some(json!({ "id": id })),
            meta: &meta,
            options,
        };
        match self.core.request(call).await? {
            Value::String(encoded) => base64::engine::general_purpose::STANDARD
                .decode(encoded)
                .map_err(|e| Error::validation(format!("export archive is not base64: {e}"))),
            other => Err(Error::validation(format!(
                "expected the export archive, got {other}"
            ))),
        }
    }
}
