package protocol

import (
	"bytes"
	"encoding/binary"
	"errors"
	"testing"
)

func TestRoundTrip(t *testing.T) {
	payload := map[string]any{
		"type": "hello",
		"gateway_versions": []any{"1.0"},
	}
	buf := &bytes.Buffer{}
	if err := WriteFrame(buf, payload); err != nil {
		t.Fatal(err)
	}
	got, err := ReadFrame(buf)
	if err != nil {
		t.Fatal(err)
	}
	if got["type"] != "hello" {
		t.Errorf("type mismatch: %v", got["type"])
	}
}

func TestRoundTripMultiple(t *testing.T) {
	payloads := []map[string]any{
		{"type": "hello"},
		{"type": "request", "request_id": "r1"},
		{"type": "request", "request_id": "r2"},
		{"type": "goodbye"},
	}
	buf := &bytes.Buffer{}
	for _, p := range payloads {
		if err := WriteFrame(buf, p); err != nil {
			t.Fatal(err)
		}
	}
	for _, want := range payloads {
		got, err := ReadFrame(buf)
		if err != nil {
			t.Fatal(err)
		}
		if got["type"] != want["type"] {
			t.Errorf("got type=%v, want %v", got["type"], want["type"])
		}
	}
}

func TestEmptyFrameRejected(t *testing.T) {
	buf := &bytes.Buffer{}
	binary.Write(buf, binary.BigEndian, uint32(0))
	_, err := ReadFrame(buf)
	if err == nil {
		t.Fatal("expected error on empty frame")
	}
	var decodeErr *FrameDecodeError
	if !errors.As(err, &decodeErr) {
		t.Fatalf("expected FrameDecodeError, got %T: %v", err, err)
	}
}

func TestNonJSONBodyRejected(t *testing.T) {
	body := []byte("not json")
	buf := &bytes.Buffer{}
	binary.Write(buf, binary.BigEndian, uint32(len(body)))
	buf.Write(body)
	_, err := ReadFrame(buf)
	if err == nil {
		t.Fatal("expected error on non-JSON body")
	}
}

func TestMissingTypeRejected(t *testing.T) {
	buf := &bytes.Buffer{}
	WriteFrame(buf, map[string]any{"type": "x"})
	// Manually craft a frame without the type field.
	bad := []byte(`{"foo": "bar"}`)
	buf2 := &bytes.Buffer{}
	binary.Write(buf2, binary.BigEndian, uint32(len(bad)))
	buf2.Write(bad)
	_, err := ReadFrame(buf2)
	if err == nil {
		t.Fatal("expected error when type field missing")
	}
}

func TestOversizeFrameRefusedOnRead(t *testing.T) {
	buf := &bytes.Buffer{}
	binary.Write(buf, binary.BigEndian, uint32(MaxFrameSize+1))
	buf.WriteString("{}")
	_, err := ReadFrame(buf)
	if !errors.Is(err, ErrFrameTooLarge) {
		t.Fatalf("expected ErrFrameTooLarge, got %v", err)
	}
}
