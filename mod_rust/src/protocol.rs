//! AGTP gateway protocol frame codec for Rust.
//!
//! 4-byte big-endian unsigned length prefix, then UTF-8 JSON,
//! max 16 MiB. Mirrors `core/gateway/protocol.py` from the Python
//! reference. See `docs/architecture/gateway-protocol-v1.md`.

use serde_json::{Map, Value};
use std::io::{Read, Write};

pub const GATEWAY_VERSION: &str = "1.0";
pub const MAX_FRAME_SIZE: usize = 16 * 1024 * 1024;

#[derive(Debug)]
pub enum ProtocolError {
    FrameDecodeError(String),
    FrameTooLargeError(usize),
    Io(std::io::Error),
    Json(serde_json::Error),
}

impl std::fmt::Display for ProtocolError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ProtocolError::FrameDecodeError(msg) => write!(f, "frame decode: {}", msg),
            ProtocolError::FrameTooLargeError(n) => {
                write!(f, "frame too large: {} bytes (max {})", n, MAX_FRAME_SIZE)
            }
            ProtocolError::Io(e) => write!(f, "io: {}", e),
            ProtocolError::Json(e) => write!(f, "json: {}", e),
        }
    }
}

impl std::error::Error for ProtocolError {}

impl From<std::io::Error> for ProtocolError {
    fn from(e: std::io::Error) -> Self {
        ProtocolError::Io(e)
    }
}

impl From<serde_json::Error> for ProtocolError {
    fn from(e: serde_json::Error) -> Self {
        ProtocolError::Json(e)
    }
}

// Re-export the error variants under names that mirror the other
// language clients for parity in cross-runtime docs.
pub type FrameDecodeError = ProtocolError;
pub type FrameTooLargeError = ProtocolError;

/// Read one frame from `reader` and return its parsed payload as a
/// JSON object.
pub fn read_frame<R: Read>(reader: &mut R) -> Result<Map<String, Value>, ProtocolError> {
    let mut header = [0u8; 4];
    reader.read_exact(&mut header).map_err(|e| {
        if e.kind() == std::io::ErrorKind::UnexpectedEof {
            ProtocolError::FrameDecodeError(
                "connection closed before length header".to_string(),
            )
        } else {
            ProtocolError::Io(e)
        }
    })?;
    let length = u32::from_be_bytes(header) as usize;
    if length > MAX_FRAME_SIZE {
        return Err(ProtocolError::FrameTooLargeError(length));
    }
    if length == 0 {
        return Err(ProtocolError::FrameDecodeError(
            "empty frame (length=0)".to_string(),
        ));
    }
    let mut body = vec![0u8; length];
    reader.read_exact(&mut body).map_err(|e| {
        if e.kind() == std::io::ErrorKind::UnexpectedEof {
            ProtocolError::FrameDecodeError(format!(
                "body truncated: wanted {} bytes",
                length
            ))
        } else {
            ProtocolError::Io(e)
        }
    })?;
    let value: Value = serde_json::from_slice(&body)?;
    let obj = value
        .as_object()
        .ok_or_else(|| ProtocolError::FrameDecodeError(
            "frame body must be a JSON object".to_string(),
        ))?
        .clone();
    if !obj.contains_key("type") {
        return Err(ProtocolError::FrameDecodeError(
            "missing required 'type' field".to_string(),
        ));
    }
    Ok(obj)
}

/// Encode `payload` and write it to `writer`.
pub fn write_frame<W: Write>(
    writer: &mut W,
    payload: &Map<String, Value>,
) -> Result<(), ProtocolError> {
    if !payload.contains_key("type") {
        return Err(ProtocolError::FrameDecodeError(
            "frame payload must carry a 'type' field".to_string(),
        ));
    }
    let body = serde_json::to_vec(payload)?;
    if body.len() > MAX_FRAME_SIZE {
        return Err(ProtocolError::FrameTooLargeError(body.len()));
    }
    let header = (body.len() as u32).to_be_bytes();
    writer.write_all(&header)?;
    writer.write_all(&body)?;
    writer.flush()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    fn obj(value: Value) -> Map<String, Value> {
        value.as_object().cloned().unwrap()
    }

    #[test]
    fn round_trip() {
        let payload = obj(serde_json::json!({"type": "hello", "version": "1.0"}));
        let mut buf = Vec::new();
        write_frame(&mut buf, &payload).unwrap();
        let mut cur = Cursor::new(&buf);
        let got = read_frame(&mut cur).unwrap();
        assert_eq!(got.get("type"), Some(&Value::String("hello".to_string())));
    }

    #[test]
    fn round_trip_multiple() {
        let payloads = vec![
            obj(serde_json::json!({"type": "hello"})),
            obj(serde_json::json!({"type": "request", "request_id": "r1"})),
            obj(serde_json::json!({"type": "goodbye"})),
        ];
        let mut buf = Vec::new();
        for p in &payloads {
            write_frame(&mut buf, p).unwrap();
        }
        let mut cur = Cursor::new(&buf);
        for want in &payloads {
            let got = read_frame(&mut cur).unwrap();
            assert_eq!(got.get("type"), want.get("type"));
        }
    }

    #[test]
    fn missing_type_rejected_on_write() {
        let bad = obj(serde_json::json!({"agent": "x"}));
        let mut buf = Vec::new();
        assert!(write_frame(&mut buf, &bad).is_err());
    }

    #[test]
    fn empty_frame_rejected() {
        let mut buf = vec![0u8, 0, 0, 0];
        let mut cur = Cursor::new(&mut buf);
        let err = read_frame(&mut cur).unwrap_err();
        assert!(matches!(err, ProtocolError::FrameDecodeError(_)));
    }

    #[test]
    fn oversize_frame_rejected() {
        let big = (MAX_FRAME_SIZE as u32 + 1).to_be_bytes();
        let buf: Vec<u8> = big.iter().chain(b"{}".iter()).copied().collect();
        let mut cur = Cursor::new(&buf);
        let err = read_frame(&mut cur).unwrap_err();
        assert!(matches!(err, ProtocolError::FrameTooLargeError(_)));
    }
}
