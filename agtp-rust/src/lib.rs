//! Public AGTP handler API for Rust.
//!
//! Mirrors the Python (`agtp/`), PHP (`agtp-php/`), Go (`agtp-go/`),
//! and Node (`agtp-node/`) libraries. Handlers are `Fn` closures
//! returning `HandlerResult` (a `Result` whose `Ok` is a sum type for
//! response-or-declared-error and whose `Err` is the panic-equivalent
//! path).
//!
//! ```ignore
//! use agtp::{EndpointContext, EndpointResponse, HandlerOutcome, Registry, RegisterOpts};
//! use serde_json::json;
//!
//! fn book(ctx: &EndpointContext) -> Result<HandlerOutcome, String> {
//!     Ok(HandlerOutcome::Response(EndpointResponse::new(
//!         json!({"reservation_id": "res-1"}),
//!     )))
//! }
//!
//! let mut reg = Registry::new();
//! reg.register("BOOK", "/room", book, RegisterOpts::default()).unwrap();
//! ```

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use std::collections::HashMap;

/// Per-request envelope handed to a handler.
///
/// Mirrors `agtp.handlers.EndpointContext` from the Python reference.
/// All fields have been validated by `agtpd` before crossing the
/// gateway; handlers may trust them.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EndpointContext {
    pub input: Map<String, Value>,
    pub agent_id: String,
    pub principal_id: String,
    pub agent_scopes: Vec<String>,
    pub authority_scope: Vec<String>,
    pub session_id: Option<String>,
    pub task_id: Option<String>,
    pub request_id: String,
    pub method: String,
    pub path: String,
    pub headers: HashMap<String, String>,
}

/// Success shape returned from a handler.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EndpointResponse {
    pub body: Map<String, Value>,
    #[serde(default = "default_status")]
    pub status: u16,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub headers: Option<HashMap<String, String>>,
}

fn default_status() -> u16 {
    200
}

impl EndpointResponse {
    pub fn new(body: Value) -> Self {
        let body = body.as_object().cloned().unwrap_or_default();
        Self {
            body,
            status: 200,
            headers: None,
        }
    }
}

/// Declared-failure shape. `code` MUST be one of the names in the
/// endpoint's declared errors list; undeclared codes are a protocol
/// violation.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EndpointError {
    pub code: String,
    pub message: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub details: Option<Map<String, Value>>,
}

impl EndpointError {
    pub fn new(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
            details: None,
        }
    }

    pub fn with_details(mut self, details: Value) -> Self {
        self.details = details.as_object().cloned();
        self
    }
}

/// Either a success response or a declared failure. The handler's
/// `Ok` value.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(untagged)]
pub enum HandlerOutcome {
    Response(EndpointResponse),
    Error(EndpointError),
}

/// Handler signature: takes a borrowed context, returns either an
/// outcome (success / declared error) or an unexpected error string
/// (the panic-equivalent path; the gateway client translates it to
/// a `handler_exception` frame).
pub type HandlerFn = fn(&EndpointContext) -> Result<HandlerOutcome, String>;

#[derive(Debug, Clone)]
pub struct RegisteredHandler {
    pub method: String,
    pub path: String,
    pub handler: HandlerFn,
    pub errors: Vec<String>,
    pub required_scopes: Vec<String>,
    pub description: String,
}

#[derive(Debug, Default, Clone)]
pub struct RegisterOpts {
    pub errors: Vec<String>,
    pub required_scopes: Vec<String>,
    pub description: String,
}

/// Process-wide registry of handlers keyed by `(method, path)`.
#[derive(Debug, Default)]
pub struct Registry {
    handlers: HashMap<String, RegisteredHandler>,
}

impl Registry {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn register(
        &mut self,
        method: &str,
        path: &str,
        handler: HandlerFn,
        opts: RegisterOpts,
    ) -> Result<(), String> {
        let method_up = method.to_uppercase();
        let key = format!("{} {}", method_up, path);
        if self.handlers.contains_key(&key) {
            return Err(format!("handler already registered for ({}, {})", method_up, path));
        }
        let entry = RegisteredHandler {
            method: method_up,
            path: path.to_string(),
            handler,
            errors: opts.errors,
            required_scopes: opts.required_scopes,
            description: opts.description,
        };
        self.handlers.insert(key, entry);
        Ok(())
    }

    pub fn lookup(&self, method: &str, path: &str) -> Option<&RegisteredHandler> {
        let key = format!("{} {}", method.to_uppercase(), path);
        self.handlers.get(&key)
    }

    pub fn all(&self) -> Vec<&RegisteredHandler> {
        self.handlers.values().collect()
    }

    pub fn count(&self) -> usize {
        self.handlers.len()
    }

    pub fn clear(&mut self) {
        self.handlers.clear();
    }
}

/// Build a synthetic EndpointContext for unit testing.
pub fn make_context(method: &str, path: &str, input: Value) -> EndpointContext {
    EndpointContext {
        input: input.as_object().cloned().unwrap_or_default(),
        agent_id: "test-agent".to_string(),
        principal_id: String::new(),
        agent_scopes: vec![],
        authority_scope: vec![],
        session_id: None,
        task_id: None,
        request_id: "test-req-1".to_string(),
        method: method.to_uppercase(),
        path: path.to_string(),
        headers: HashMap::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn ok_handler(_ctx: &EndpointContext) -> Result<HandlerOutcome, String> {
        Ok(HandlerOutcome::Response(EndpointResponse::new(json!({"ok": true}))))
    }

    fn err_handler(_ctx: &EndpointContext) -> Result<HandlerOutcome, String> {
        Ok(HandlerOutcome::Error(EndpointError::new(
            "room_unavailable",
            "full",
        )))
    }

    #[test]
    fn register_and_lookup() {
        let mut reg = Registry::new();
        reg.register("BOOK", "/room", ok_handler, RegisterOpts::default()).unwrap();
        assert!(reg.lookup("BOOK", "/room").is_some());
    }

    #[test]
    fn duplicate_registration_rejected() {
        let mut reg = Registry::new();
        reg.register("BOOK", "/room", ok_handler, RegisterOpts::default()).unwrap();
        assert!(reg.register("BOOK", "/room", ok_handler, RegisterOpts::default()).is_err());
    }

    #[test]
    fn method_normalized_to_uppercase() {
        let mut reg = Registry::new();
        reg.register("book", "/room", ok_handler, RegisterOpts::default()).unwrap();
        assert!(reg.lookup("BOOK", "/room").is_some());
        assert!(reg.lookup("book", "/room").is_some());
    }

    #[test]
    fn options_carried() {
        let mut reg = Registry::new();
        reg.register(
            "BOOK",
            "/room",
            ok_handler,
            RegisterOpts {
                errors: vec!["room_unavailable".to_string()],
                required_scopes: vec!["booking:write".to_string()],
                description: "Books a room.".to_string(),
            },
        )
        .unwrap();
        let entry = reg.lookup("BOOK", "/room").unwrap();
        assert_eq!(entry.errors, vec!["room_unavailable"]);
        assert_eq!(entry.required_scopes, vec!["booking:write"]);
        assert_eq!(entry.description, "Books a room.");
    }

    #[test]
    fn handler_round_trip() {
        let mut reg = Registry::new();
        reg.register("BOOK", "/room", ok_handler, RegisterOpts::default()).unwrap();
        reg.register("ABORT", "/room", err_handler, RegisterOpts::default()).unwrap();

        let ctx = make_context("BOOK", "/room", json!({}));
        match (reg.lookup("BOOK", "/room").unwrap().handler)(&ctx).unwrap() {
            HandlerOutcome::Response(r) => assert_eq!(r.body.get("ok"), Some(&Value::Bool(true))),
            HandlerOutcome::Error(_) => panic!("expected response"),
        }

        match (reg.lookup("ABORT", "/room").unwrap().handler)(&ctx).unwrap() {
            HandlerOutcome::Error(e) => assert_eq!(e.code, "room_unavailable"),
            HandlerOutcome::Response(_) => panic!("expected error"),
        }
    }

    #[test]
    fn make_context_defaults() {
        let ctx = make_context("BOOK", "/room", json!({"value": "x"}));
        assert_eq!(ctx.method, "BOOK");
        assert_eq!(ctx.path, "/room");
        assert_eq!(ctx.input.get("value"), Some(&Value::String("x".to_string())));
    }
}
