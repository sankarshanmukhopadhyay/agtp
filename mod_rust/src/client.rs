//! Module-side gateway client for Rust.
//!
//! Port of `mod_python/client.py`, `mod_go/client/client.go`, etc.
//! One connection, one in-flight request at a time. For concurrency,
//! run multiple instances against the same gateway socket.

use std::collections::HashMap;
use std::io::{BufReader, BufWriter, Write};
use std::net::TcpStream;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use agtp::{EndpointContext, HandlerOutcome, RegisteredHandler, Registry};
use serde_json::{json, Map, Value};

use crate::protocol::{
    read_frame, write_frame, ProtocolError, GATEWAY_VERSION,
};

#[derive(Debug)]
pub enum ModuleError {
    Protocol(ProtocolError),
    Handshake(String),
    Io(std::io::Error),
}

impl std::fmt::Display for ModuleError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ModuleError::Protocol(e) => write!(f, "{}", e),
            ModuleError::Handshake(msg) => write!(f, "handshake: {}", msg),
            ModuleError::Io(e) => write!(f, "io: {}", e),
        }
    }
}

impl std::error::Error for ModuleError {}

impl From<ProtocolError> for ModuleError {
    fn from(e: ProtocolError) -> Self {
        ModuleError::Protocol(e)
    }
}

impl From<std::io::Error> for ModuleError {
    fn from(e: std::io::Error) -> Self {
        ModuleError::Io(e)
    }
}

/// Read/write halves of the underlying transport. We hold a TCP
/// stream here because Unix-socket parity across Windows / non-tokio
/// Rust adds a chunk of feature-gated code; v1 uses TCP loopback,
/// which matches how the daemon's GatewayServer accepts modules.
struct Connection {
    reader: BufReader<TcpStream>,
    writer: BufWriter<TcpStream>,
}

impl Connection {
    fn connect(socket: &str) -> Result<Self, ModuleError> {
        // Today we only handle "host:port" form. Unix-socket support
        // lands when there's a real production deployment needing it.
        let stream = TcpStream::connect(socket).map_err(ModuleError::Io)?;
        stream.set_nodelay(true)?;
        let reader = BufReader::new(stream.try_clone()?);
        let writer = BufWriter::new(stream);
        Ok(Self { reader, writer })
    }
}

pub struct GatewayClient {
    socket_path: String,
    registry: Arc<Registry>,
    module_id: String,
    module_version: String,
    cached_manifest_hash: String,
    cached_bindings: HashMap<String, RegisteredHandler>,
    bindings: HashMap<String, RegisteredHandler>,
    stop: Arc<AtomicBool>,
}

impl GatewayClient {
    pub fn new(socket_path: impl Into<String>, registry: Arc<Registry>) -> Self {
        Self {
            socket_path: socket_path.into(),
            registry,
            module_id: "mod_rust".to_string(),
            module_version: "0.1.0".to_string(),
            cached_manifest_hash: String::new(),
            cached_bindings: HashMap::new(),
            bindings: HashMap::new(),
            stop: Arc::new(AtomicBool::new(false)),
        }
    }

    pub fn module_id(&mut self, id: impl Into<String>) -> &mut Self {
        self.module_id = id.into();
        self
    }

    pub fn module_version(&mut self, v: impl Into<String>) -> &mut Self {
        self.module_version = v.into();
        self
    }

    /// Returns a handle that can stop the serve loop from another thread.
    pub fn shutdown_handle(&self) -> Arc<AtomicBool> {
        Arc::clone(&self.stop)
    }

    pub fn run(&mut self) -> Result<(), ModuleError> {
        let mut conn = Connection::connect(&self.socket_path)?;
        self.handshake(&mut conn)?;
        self.serve_loop(&mut conn)?;
        Ok(())
    }

    fn handshake(&mut self, conn: &mut Connection) -> Result<(), ModuleError> {
        let mut hello = Map::new();
        hello.insert("type".to_string(), json!("hello"));
        hello.insert("gateway_versions".to_string(), json!([GATEWAY_VERSION]));
        hello.insert(
            "module".to_string(),
            json!({
                "id": self.module_id,
                "version": self.module_version,
                "runtime": format!("Rust {}", env!("CARGO_PKG_VERSION")),
                "pid": std::process::id() as u64,
            }),
        );
        hello.insert("capabilities".to_string(), json!(["registered_function"]));
        if !self.cached_manifest_hash.is_empty() {
            hello.insert(
                "cached_manifest_hash".to_string(),
                json!(self.cached_manifest_hash),
            );
        }
        write_frame(&mut conn.writer, &hello)?;

        let welcome = read_frame(&mut conn.reader)?;
        let welcome_type = welcome.get("type").and_then(|v| v.as_str()).unwrap_or("");
        if welcome_type == "error" {
            return Err(ModuleError::Handshake(format!(
                "daemon refused: {} / {}",
                welcome.get("code").map(|v| v.to_string()).unwrap_or_default(),
                welcome.get("message").map(|v| v.to_string()).unwrap_or_default(),
            )));
        }
        if welcome_type != "welcome" {
            return Err(ModuleError::Handshake(format!(
                "expected welcome, got type={}",
                welcome_type
            )));
        }
        if welcome.get("gateway_version").and_then(|v| v.as_str()) != Some(GATEWAY_VERSION) {
            return Err(ModuleError::Handshake(format!(
                "daemon chose unsupported gateway version"
            )));
        }

        let register = read_frame(&mut conn.reader)?;
        let reg_type = register.get("type").and_then(|v| v.as_str()).unwrap_or("");
        match reg_type {
            "register" => self.handle_register(conn, register)?,
            "register_resume" => self.handle_register_resume(conn, register)?,
            other => {
                return Err(ModuleError::Handshake(format!(
                    "expected register or register_resume, got type={}",
                    other
                )))
            }
        }
        Ok(())
    }

    fn handle_register(
        &mut self,
        conn: &mut Connection,
        register: Map<String, Value>,
    ) -> Result<(), ModuleError> {
        let manifest_hash = register
            .get("manifest_hash")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let endpoints = register
            .get("endpoints")
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default();

        let mut resolved: Vec<String> = Vec::new();
        let mut errors: Vec<Value> = Vec::new();
        let mut new_bindings: HashMap<String, RegisteredHandler> = HashMap::new();

        for ep in endpoints {
            let ep_obj = match ep.as_object() {
                Some(o) => o,
                None => continue,
            };
            let method = ep_obj
                .get("method")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_uppercase();
            let path = ep_obj.get("path").and_then(|v| v.as_str()).unwrap_or("/");
            let reference = ep_obj
                .get("handler_reference")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            match self.registry.lookup(&method, path) {
                Some(entry) => {
                    new_bindings.insert(format!("{} {}", method, path), entry.clone());
                    resolved.push(format!("{} {}", method, path));
                }
                None => {
                    errors.push(json!({
                        "endpoint": format!("{} {}", method, path),
                        "reason": "handler_not_found",
                        "detail": format!(
                            "no registered handler for ({}, {}); reference was {:?}",
                            method, path, reference
                        ),
                    }));
                }
            }
        }

        if !errors.is_empty() {
            let mut ack = Map::new();
            ack.insert("type".to_string(), json!("register_ack"));
            ack.insert("ok".to_string(), json!(false));
            ack.insert("errors".to_string(), json!(errors));
            write_frame(&mut conn.writer, &ack)?;
            return Err(ModuleError::Handshake(format!(
                "could not resolve {} endpoint reference(s)",
                ack.get("errors").and_then(|v| v.as_array()).map_or(0, |a| a.len())
            )));
        }

        self.bindings = new_bindings.clone();
        self.cached_bindings = new_bindings;
        self.cached_manifest_hash = manifest_hash;

        let mut ack = Map::new();
        ack.insert("type".to_string(), json!("register_ack"));
        ack.insert("ok".to_string(), json!(true));
        ack.insert("resolved".to_string(), json!(resolved));
        write_frame(&mut conn.writer, &ack)?;
        Ok(())
    }

    fn handle_register_resume(
        &mut self,
        conn: &mut Connection,
        register: Map<String, Value>,
    ) -> Result<(), ModuleError> {
        let manifest_hash = register
            .get("manifest_hash")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        if self.cached_bindings.is_empty() || manifest_hash != self.cached_manifest_hash {
            let mut ack = Map::new();
            ack.insert("type".to_string(), json!("register_ack"));
            ack.insert("ok".to_string(), json!(false));
            ack.insert(
                "errors".to_string(),
                json!([{
                    "endpoint": "*",
                    "reason": "cache_miss",
                    "detail": format!("no cached bindings for manifest_hash={:?}", manifest_hash),
                }]),
            );
            write_frame(&mut conn.writer, &ack)?;
            return Err(ModuleError::Handshake(format!(
                "register_resume cache miss for hash {}",
                manifest_hash
            )));
        }
        self.bindings = self.cached_bindings.clone();
        let resolved: Vec<String> = self.bindings.keys().cloned().collect();
        let mut ack = Map::new();
        ack.insert("type".to_string(), json!("register_ack"));
        ack.insert("ok".to_string(), json!(true));
        ack.insert("resolved".to_string(), json!(resolved));
        write_frame(&mut conn.writer, &ack)?;
        Ok(())
    }

    fn serve_loop(&mut self, conn: &mut Connection) -> Result<(), ModuleError> {
        while !self.stop.load(Ordering::SeqCst) {
            let frame = match read_frame(&mut conn.reader) {
                Ok(f) => f,
                Err(_) => return Ok(()), // EOF or peer closed; exit cleanly
            };
            let ftype = frame.get("type").and_then(|v| v.as_str()).unwrap_or("");
            match ftype {
                "goodbye" => return Ok(()),
                "ping" => {
                    let mut pong = Map::new();
                    pong.insert("type".to_string(), json!("pong"));
                    pong.insert(
                        "nonce".to_string(),
                        frame.get("nonce").cloned().unwrap_or(json!("")),
                    );
                    write_frame(&mut conn.writer, &pong)?;
                }
                "request" => self.handle_request(conn, frame)?,
                other => {
                    let mut err = Map::new();
                    err.insert("type".to_string(), json!("error"));
                    err.insert("code".to_string(), json!("phase_violation"));
                    err.insert(
                        "message".to_string(),
                        json!(format!("unexpected frame type {:?}", other)),
                    );
                    write_frame(&mut conn.writer, &err)?;
                }
            }
        }
        Ok(())
    }

    fn handle_request(
        &mut self,
        conn: &mut Connection,
        frame: Map<String, Value>,
    ) -> Result<(), ModuleError> {
        let request_id = frame
            .get("request_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let envelope = frame
            .get("envelope")
            .and_then(|v| v.as_object())
            .cloned()
            .unwrap_or_default();

        let method = envelope
            .get("method")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_uppercase();
        let path = envelope.get("path").and_then(|v| v.as_str()).unwrap_or("/");
        let key = format!("{} {}", method, path);

        let entry = match self.bindings.get(&key) {
            Some(e) => e.clone(),
            None => {
                let mut err = Map::new();
                err.insert("type".to_string(), json!("error"));
                err.insert("request_id".to_string(), json!(request_id));
                err.insert("code".to_string(), json!("handler_exception"));
                err.insert(
                    "message".to_string(),
                    json!(format!("no handler bound for ({}, {})", method, path)),
                );
                write_frame(&mut conn.writer, &err)?;
                return Ok(());
            }
        };

        let ctx = context_from_envelope(&envelope, &request_id);

        let result = (entry.handler)(&ctx);
        match result {
            Ok(HandlerOutcome::Response(resp)) => {
                let mut resp_env = Map::new();
                resp_env.insert("body".to_string(), Value::Object(resp.body));
                resp_env.insert("status".to_string(), json!(resp.status));
                if let Some(headers) = resp.headers {
                    resp_env.insert("headers".to_string(), json!(headers));
                }
                let mut response = Map::new();
                response.insert("type".to_string(), json!("response"));
                response.insert("request_id".to_string(), json!(request_id));
                response.insert("envelope".to_string(), Value::Object(resp_env));
                write_frame(&mut conn.writer, &response)?;
            }
            Ok(HandlerOutcome::Error(err)) => {
                let mut err_env = Map::new();
                err_env.insert("code".to_string(), json!(err.code));
                err_env.insert("message".to_string(), json!(err.message));
                if let Some(details) = err.details {
                    err_env.insert("details".to_string(), Value::Object(details));
                }
                let mut envelope = Map::new();
                envelope.insert("endpoint_error".to_string(), Value::Object(err_env));
                let mut response = Map::new();
                response.insert("type".to_string(), json!("response"));
                response.insert("request_id".to_string(), json!(request_id));
                response.insert("envelope".to_string(), Value::Object(envelope));
                write_frame(&mut conn.writer, &response)?;
            }
            Err(msg) => {
                let mut err = Map::new();
                err.insert("type".to_string(), json!("error"));
                err.insert("request_id".to_string(), json!(request_id));
                err.insert("code".to_string(), json!("handler_exception"));
                err.insert("message".to_string(), json!(msg));
                write_frame(&mut conn.writer, &err)?;
            }
        }
        conn.writer.flush()?;
        Ok(())
    }
}

fn context_from_envelope(envelope: &Map<String, Value>, request_id: &str) -> EndpointContext {
    let s = |k: &str| {
        envelope
            .get(k)
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string()
    };
    let opt_str = |k: &str| envelope.get(k).and_then(|v| v.as_str()).map(String::from);
    let str_array = |k: &str| {
        envelope
            .get(k)
            .and_then(|v| v.as_array())
            .map(|arr| arr.iter().filter_map(|v| v.as_str().map(String::from)).collect())
            .unwrap_or_default()
    };
    let headers = envelope
        .get("headers")
        .and_then(|v| v.as_object())
        .map(|m| {
            m.iter()
                .map(|(k, v)| (k.clone(), v.as_str().unwrap_or("").to_string()))
                .collect()
        })
        .unwrap_or_default();
    let input = envelope
        .get("input")
        .and_then(|v| v.as_object())
        .cloned()
        .unwrap_or_default();
    let req_id = envelope
        .get("request_id")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .unwrap_or(request_id)
        .to_string();
    EndpointContext {
        input,
        agent_id: s("agent_id"),
        principal_id: s("principal_id"),
        agent_scopes: str_array("agent_scopes"),
        authority_scope: str_array("authority_scope"),
        session_id: opt_str("session_id"),
        task_id: opt_str("task_id"),
        request_id: req_id,
        method: s("method").to_uppercase(),
        path: envelope
            .get("path")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .unwrap_or("/")
            .to_string(),
        headers,
    }
}
