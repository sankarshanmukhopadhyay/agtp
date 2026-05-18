//! `mod_rust` CLI entry point.
//!
//! In Rust the typical deployment shape is for the operator's binary
//! to import `mod_rust::GatewayClient` directly and register handlers
//! through `agtp::Registry`. This bin exists as a thin diagnostic
//! shim for the empty-registry case.

use std::process;
use std::sync::Arc;

use agtp::Registry;
use mod_rust::{GatewayClient, ModuleError};

fn main() {
    let mut socket = None;
    let mut module_id = "mod_rust".to_string();
    let mut module_version = "0.1.0".to_string();
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--gateway-socket" => {
                i += 1;
                socket = args.get(i).cloned();
            }
            "--module-id" => {
                i += 1;
                if let Some(v) = args.get(i) {
                    module_id = v.clone();
                }
            }
            "--module-version" => {
                i += 1;
                if let Some(v) = args.get(i) {
                    module_version = v.clone();
                }
            }
            "-h" | "--help" => {
                eprintln!("Usage: mod_rust --gateway-socket=host:port");
                process::exit(0);
            }
            other => {
                eprintln!("[mod_rust] unknown argument: {}", other);
                process::exit(2);
            }
        }
        i += 1;
    }
    let socket = match socket {
        Some(s) => s,
        None => {
            eprintln!("[mod_rust] --gateway-socket is required");
            process::exit(2);
        }
    };

    eprintln!("[mod_rust] starting against {}", socket);
    let registry = Arc::new(Registry::new());
    let mut client = GatewayClient::new(socket, registry);
    client.module_id(module_id).module_version(module_version);
    if let Err(ModuleError::Handshake(msg)) = client.run() {
        eprintln!("[mod_rust] {}", msg);
        process::exit(1);
    }
}
