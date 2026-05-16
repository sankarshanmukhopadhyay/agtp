//! Rust runtime module for AGTP.
//!
//! Mirrors `mod_python`, `mod_php`, `mod_go`, `mod_node`. Connects to
//! `agtpd` over a Unix domain socket or TCP loopback, performs the
//! handshake, receives the daemon's endpoint registration, dispatches
//! request frames through an `agtp::Registry` of `HandlerFn`s.
//!
//! Sync I/O on `std::net`. For higher concurrency, run multiple
//! processes against the same gateway socket.

pub mod protocol;
pub mod client;

pub use client::{GatewayClient, ModuleError};
pub use protocol::{
    FrameDecodeError, FrameTooLargeError, ProtocolError,
    GATEWAY_VERSION, MAX_FRAME_SIZE, read_frame, write_frame,
};
