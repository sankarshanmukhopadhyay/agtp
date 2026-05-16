// Package protocol implements the AGTP gateway protocol frame codec.
//
// 4-byte big-endian unsigned length prefix, followed by UTF-8 JSON.
// Max payload size 16 MiB. Mirrors core/gateway/protocol.py in the
// Python reference. See docs/architecture/gateway-protocol-v1.md.
package protocol

import (
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
)

// GatewayVersion is the protocol version this implementation speaks.
const GatewayVersion = "1.0"

// MaxFrameSize is the hard cap on a single frame's JSON payload.
const MaxFrameSize = 16 * 1024 * 1024

// ErrFrameTooLarge is returned when an announced or encoded frame
// length exceeds MaxFrameSize.
var ErrFrameTooLarge = errors.New("frame too large")

// ErrFrameDecode is returned for malformed frames (truncated, non-
// JSON, non-object, missing type field).
type FrameDecodeError struct {
	Reason string
}

func (e *FrameDecodeError) Error() string { return "frame decode: " + e.Reason }

// ReadFrame reads one frame from r and returns its parsed payload.
func ReadFrame(r io.Reader) (map[string]any, error) {
	var header [4]byte
	if _, err := io.ReadFull(r, header[:]); err != nil {
		if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) {
			return nil, &FrameDecodeError{Reason: "connection closed before length header"}
		}
		return nil, err
	}
	length := binary.BigEndian.Uint32(header[:])
	if length > MaxFrameSize {
		return nil, fmt.Errorf("%w: %d > %d", ErrFrameTooLarge, length, MaxFrameSize)
	}
	if length == 0 {
		return nil, &FrameDecodeError{Reason: "empty frame (length=0)"}
	}
	body := make([]byte, length)
	if _, err := io.ReadFull(r, body); err != nil {
		return nil, &FrameDecodeError{Reason: fmt.Sprintf("body truncated: %v", err)}
	}
	var payload map[string]any
	if err := json.Unmarshal(body, &payload); err != nil {
		return nil, &FrameDecodeError{Reason: "not valid JSON: " + err.Error()}
	}
	if _, ok := payload["type"]; !ok {
		return nil, &FrameDecodeError{Reason: "missing required 'type' field"}
	}
	return payload, nil
}

// WriteFrame encodes payload and writes it to w. Caller is responsible
// for flushing if w is buffered.
func WriteFrame(w io.Writer, payload map[string]any) error {
	if _, ok := payload["type"]; !ok {
		return errors.New("frame payload must carry a 'type' field")
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return fmt.Errorf("encode frame: %w", err)
	}
	if len(body) > MaxFrameSize {
		return fmt.Errorf("%w: %d > %d", ErrFrameTooLarge, len(body), MaxFrameSize)
	}
	var header [4]byte
	binary.BigEndian.PutUint32(header[:], uint32(len(body)))
	if _, err := w.Write(header[:]); err != nil {
		return err
	}
	if _, err := w.Write(body); err != nil {
		return err
	}
	return nil
}
