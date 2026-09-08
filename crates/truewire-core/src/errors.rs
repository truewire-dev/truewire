//! The error taxonomy a generated client's callers program against.
//!
//! One [`Error`] enum with the same leaves as `truewire_core.exceptions` and
//! `@truewire/core`'s classes: transport failures ([`Error::Network`]), replies that do not
//! match their declared shape ([`Error::Validation`]), errors the API returned
//! ([`Error::Api`], carrying the wire status and body and a [`ApiKind`] of `Api`,
//! `BadRequest`, `Auth` or `RateLimited`) and SDK-side bugs ([`Error::Logic`]). Every
//! variant is a sibling: matching on `Error::Api` never swallows a dropped connection.
//!
//! Each error carries a string [`code`](Error::code) equal to the TypeScript runtime's
//! `ErrorCode`, so a log line or a foreign boundary can name the kind without the type.
//! Errors are `Clone`: a WebSocket connection's failure is delivered to every request
//! waiting on it, and the cause is shared behind an `Arc`.

use std::fmt;
use std::sync::Arc;

/// The cause behind an error, shared so the error stays `Clone`.
pub type Source = Arc<dyn std::error::Error + Send + Sync + 'static>;

/// `Result` with this crate's [`Error`].
pub type Result<T, E = Error> = std::result::Result<T, E>;

/// Every error a generated client raises.
#[derive(Debug, Clone)]
pub enum Error {
    /// Error reaching the server: DNS, TCP, TLS, a timeout, a dropped connection.
    Network(NetworkError),
    /// A value that does not match its declared wire shape.
    Validation(ValidationError),
    /// An error the API returned.
    Api(ApiError),
    /// Invalid assumptions, logic, or other bugs on the SDK side.
    Logic(LogicError),
}

/// Which `ApiError` an API-returned error is.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ApiKind {
    /// Any error the API returned that is none of the ones below.
    Api,
    /// Bad request: invalid request, invalid input, etc.
    BadRequest,
    /// Authentication error: invalid API key, invalid API secret, etc.
    Auth,
    /// Rate limited: the API has reached the rate limit.
    RateLimited,
}

/// Error reaching the server.
#[derive(Debug, Clone, Default)]
pub struct NetworkError {
    pub message: String,
    pub source: Option<Source>,
}

/// One failed check inside a value, located by a JSON pointer.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Issue {
    /// JSON pointer to the offending value, `""` for the root.
    pub path: String,
    pub message: String,
}

/// Invalid wire format.
#[derive(Debug, Clone, Default)]
pub struct ValidationError {
    pub message: String,
    /// Every check that failed, in order; the first names [`ValidationError::path`].
    pub issues: Vec<Issue>,
    pub source: Option<Source>,
}

/// Error returned by the API.
#[derive(Debug, Clone)]
pub struct ApiError {
    pub kind: ApiKind,
    pub message: String,
    /// HTTP status of the reply that carried the error, when there was one.
    pub status: Option<u16>,
    /// Decoded body of the reply that carried the error, when there was one.
    pub body: Option<serde_json::Value>,
    pub source: Option<Source>,
}

/// Logic error: invalid assumptions, logic, or other bugs on the SDK side.
#[derive(Debug, Clone, Default)]
pub struct LogicError {
    pub message: String,
    pub source: Option<Source>,
}

impl NetworkError {
    pub fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            source: None,
        }
    }

    pub fn with_source(mut self, source: impl std::error::Error + Send + Sync + 'static) -> Self {
        self.source = Some(Arc::new(source));
        self
    }
}

impl ValidationError {
    /// One issue at the root.
    pub fn new(message: impl Into<String>) -> Self {
        let message = message.into();
        Self {
            issues: vec![Issue {
                path: String::new(),
                message: message.clone(),
            }],
            message,
            source: None,
        }
    }

    /// A message with its own issue list; the message should already name the path.
    pub fn with_issues(message: impl Into<String>, issues: Vec<Issue>) -> Self {
        Self {
            message: message.into(),
            issues,
            source: None,
        }
    }

    pub fn with_source(mut self, source: impl std::error::Error + Send + Sync + 'static) -> Self {
        self.source = Some(Arc::new(source));
        self
    }

    /// JSON pointer to the first offending value, `""` for the root.
    pub fn path(&self) -> &str {
        self.issues.first().map(|issue| issue.path.as_str()).unwrap_or("")
    }
}

impl ApiError {
    pub fn new(kind: ApiKind, message: impl Into<String>) -> Self {
        Self {
            kind,
            message: message.into(),
            status: None,
            body: None,
            source: None,
        }
    }

    pub fn with_status(mut self, status: u16) -> Self {
        self.status = Some(status);
        self
    }

    pub fn with_body(mut self, body: serde_json::Value) -> Self {
        self.body = Some(body);
        self
    }

    pub fn with_source(mut self, source: impl std::error::Error + Send + Sync + 'static) -> Self {
        self.source = Some(Arc::new(source));
        self
    }
}

impl LogicError {
    pub fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            source: None,
        }
    }

    pub fn with_source(mut self, source: impl std::error::Error + Send + Sync + 'static) -> Self {
        self.source = Some(Arc::new(source));
        self
    }
}

impl Error {
    /// A [`NetworkError`].
    pub fn network(message: impl Into<String>) -> Self {
        Self::Network(NetworkError::new(message))
    }

    /// A [`ValidationError`] with one issue at the root.
    pub fn validation(message: impl Into<String>) -> Self {
        Self::Validation(ValidationError::new(message))
    }

    /// An [`ApiError`] of kind [`ApiKind::Api`].
    pub fn api(message: impl Into<String>) -> Self {
        Self::Api(ApiError::new(ApiKind::Api, message))
    }

    /// An [`ApiError`] of kind [`ApiKind::BadRequest`].
    pub fn bad_request(message: impl Into<String>) -> Self {
        Self::Api(ApiError::new(ApiKind::BadRequest, message))
    }

    /// An [`ApiError`] of kind [`ApiKind::Auth`].
    pub fn auth(message: impl Into<String>) -> Self {
        Self::Api(ApiError::new(ApiKind::Auth, message))
    }

    /// An [`ApiError`] of kind [`ApiKind::RateLimited`].
    pub fn rate_limited(message: impl Into<String>) -> Self {
        Self::Api(ApiError::new(ApiKind::RateLimited, message))
    }

    /// A [`LogicError`].
    pub fn logic(message: impl Into<String>) -> Self {
        Self::Logic(LogicError::new(message))
    }

    /// The `ErrorCode` string the TypeScript runtime uses for the same kind: `network`,
    /// `validation`, `api`, `bad-request`, `auth`, `rate-limited` or `logic`.
    pub fn code(&self) -> &'static str {
        match self {
            Self::Network(_) => "network",
            Self::Validation(_) => "validation",
            Self::Api(e) => match e.kind {
                ApiKind::Api => "api",
                ApiKind::BadRequest => "bad-request",
                ApiKind::Auth => "auth",
                ApiKind::RateLimited => "rate-limited",
            },
            Self::Logic(_) => "logic",
        }
    }

    /// The concrete name the other runtimes give this error (`AuthError`, `RateLimited`, ...).
    pub fn name(&self) -> &'static str {
        match self {
            Self::Network(_) => "NetworkError",
            Self::Validation(_) => "ValidationError",
            Self::Api(e) => match e.kind {
                ApiKind::Api => "ApiError",
                ApiKind::BadRequest => "BadRequest",
                ApiKind::Auth => "AuthError",
                ApiKind::RateLimited => "RateLimited",
            },
            Self::Logic(_) => "LogicError",
        }
    }

    /// The message alone, without the name.
    pub fn message(&self) -> &str {
        match self {
            Self::Network(e) => &e.message,
            Self::Validation(e) => &e.message,
            Self::Api(e) => &e.message,
            Self::Logic(e) => &e.message,
        }
    }

    /// The API error inside, if this is one.
    pub fn as_api(&self) -> Option<&ApiError> {
        match self {
            Self::Api(e) => Some(e),
            _ => None,
        }
    }

    /// The validation error inside, if this is one.
    pub fn as_validation(&self) -> Option<&ValidationError> {
        match self {
            Self::Validation(e) => Some(e),
            _ => None,
        }
    }

    /// Whether this is an [`ApiError`] of `kind`.
    pub fn is_api(&self, kind: ApiKind) -> bool {
        matches!(self, Self::Api(e) if e.kind == kind)
    }

    /// Whether this is a [`NetworkError`].
    pub fn is_network(&self) -> bool {
        matches!(self, Self::Network(_))
    }

    /// Whether this is a [`ValidationError`].
    pub fn is_validation(&self) -> bool {
        matches!(self, Self::Validation(_))
    }

    /// Whether this is a [`LogicError`].
    pub fn is_logic(&self) -> bool {
        matches!(self, Self::Logic(_))
    }

    /// Set the HTTP status on an API error; a no-op on every other kind.
    pub fn with_status(self, status: u16) -> Self {
        match self {
            Self::Api(e) => Self::Api(e.with_status(status)),
            other => other,
        }
    }

    /// Set the decoded body on an API error; a no-op on every other kind.
    pub fn with_body(self, body: serde_json::Value) -> Self {
        match self {
            Self::Api(e) => Self::Api(e.with_body(body)),
            other => other,
        }
    }

    /// Attach a cause.
    pub fn with_source(self, source: impl std::error::Error + Send + Sync + 'static) -> Self {
        match self {
            Self::Network(e) => Self::Network(e.with_source(source)),
            Self::Validation(e) => Self::Validation(e.with_source(source)),
            Self::Api(e) => Self::Api(e.with_source(source)),
            Self::Logic(e) => Self::Logic(e.with_source(source)),
        }
    }

    fn source_ref(&self) -> Option<&Source> {
        match self {
            Self::Network(e) => e.source.as_ref(),
            Self::Validation(e) => e.source.as_ref(),
            Self::Api(e) => e.source.as_ref(),
            Self::Logic(e) => e.source.as_ref(),
        }
    }
}

impl fmt::Display for Error {
    /// `Name: message`, or `Name` alone with an empty message, as the other runtimes render.
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let message = self.message();
        if message.is_empty() {
            f.write_str(self.name())
        } else {
            write!(f, "{}: {}", self.name(), message)
        }
    }
}

impl std::error::Error for Error {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        self.source_ref()
            .map(|s| s.as_ref() as &(dyn std::error::Error + 'static))
    }
}

impl From<NetworkError> for Error {
    fn from(e: NetworkError) -> Self {
        Self::Network(e)
    }
}

impl From<ValidationError> for Error {
    fn from(e: ValidationError) -> Self {
        Self::Validation(e)
    }
}

impl From<ApiError> for Error {
    fn from(e: ApiError) -> Self {
        Self::Api(e)
    }
}

impl From<LogicError> for Error {
    fn from(e: LogicError) -> Self {
        Self::Logic(e)
    }
}

macro_rules! leaf_display {
    ($t:ty) => {
        impl fmt::Display for $t {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str(&self.message)
            }
        }
        impl std::error::Error for $t {
            fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
                self.source
                    .as_ref()
                    .map(|s| s.as_ref() as &(dyn std::error::Error + 'static))
            }
        }
    };
}

leaf_display!(NetworkError);
leaf_display!(ValidationError);
leaf_display!(ApiError);
leaf_display!(LogicError);
