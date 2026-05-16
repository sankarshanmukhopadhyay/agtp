//! Sample Rust handler binary for the gateway-protocol e2e test.

use std::process;
use std::sync::Arc;

use agtp::{
    EndpointContext, EndpointError, EndpointResponse, HandlerOutcome, RegisterOpts, Registry,
};
use mod_rust::GatewayClient;
use serde_json::json;

fn echo_handler(ctx: &EndpointContext) -> Result<HandlerOutcome, String> {
    let value = ctx.input.get("value").and_then(|v| v.as_str()).unwrap_or("");
    Ok(HandlerOutcome::Response(EndpointResponse::new(json!({
        "echo": value,
    }))))
}

fn book_room(ctx: &EndpointContext) -> Result<HandlerOutcome, String> {
    let room_type = ctx
        .input
        .get("room_type")
        .and_then(|v| v.as_str())
        .unwrap_or("double");
    if room_type == "presidential_suite" {
        return Ok(HandlerOutcome::Error(
            EndpointError::new("room_unavailable", "The presidential suite is not available.")
                .with_details(json!({ "room_type": room_type })),
        ));
    }
    let guest = ctx
        .input
        .get("guest")
        .and_then(|v| v.as_str())
        .unwrap_or("anon");
    Ok(HandlerOutcome::Response(EndpointResponse::new(json!({
        "reservation_id": format!("res-{}-{}", guest, room_type),
        "agent": ctx.agent_id,
    }))))
}

fn main() {
    let mut socket = None;
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut i = 0;
    while i < args.len() {
        if args[i] == "--gateway-socket" {
            i += 1;
            socket = args.get(i).cloned();
        }
        i += 1;
    }
    let socket = match socket {
        Some(s) => s,
        None => {
            eprintln!("[gateway-demo-rust] --gateway-socket is required");
            process::exit(2);
        }
    };

    let mut registry = Registry::new();
    registry
        .register("QUERY", "/echo", echo_handler, RegisterOpts::default())
        .expect("register echo");
    registry
        .register(
            "BOOK",
            "/room",
            book_room,
            RegisterOpts {
                errors: vec!["room_unavailable".to_string()],
                ..RegisterOpts::default()
            },
        )
        .expect("register book_room");

    let registry = Arc::new(registry);
    let mut client = GatewayClient::new(socket, registry);
    client.module_id("gateway-demo-rust");
    if let Err(e) = client.run() {
        eprintln!("[gateway-demo-rust] {}", e);
        process::exit(1);
    }
}
