package agtp

import (
	"errors"
	"testing"
)

func TestRegisterAndLookup(t *testing.T) {
	reg := NewRegistry()
	handler := func(ec EndpointContext) (HandlerResult, error) {
		return EndpointResponse{Body: map[string]any{"ok": true}}, nil
	}
	if err := reg.Register("BOOK", "/room", handler); err != nil {
		t.Fatalf("unexpected error registering: %v", err)
	}
	if got := reg.Lookup("BOOK", "/room"); got == nil {
		t.Fatal("expected handler to be looked up")
	}
}

func TestRegisterDuplicateRejected(t *testing.T) {
	reg := NewRegistry()
	handler := func(ec EndpointContext) (HandlerResult, error) {
		return EndpointResponse{}, nil
	}
	if err := reg.Register("BOOK", "/room", handler); err != nil {
		t.Fatal(err)
	}
	err := reg.Register("BOOK", "/room", handler)
	if err == nil {
		t.Fatal("expected duplicate registration to error")
	}
}

func TestMethodNormalizedToUppercase(t *testing.T) {
	reg := NewRegistry()
	handler := func(ec EndpointContext) (HandlerResult, error) {
		return EndpointResponse{}, nil
	}
	if err := reg.Register("book", "/room", handler); err != nil {
		t.Fatal(err)
	}
	if reg.Lookup("BOOK", "/room") == nil {
		t.Fatal("expected lowercase registration to be found by uppercase lookup")
	}
}

func TestRegisterWithOptions(t *testing.T) {
	reg := NewRegistry()
	handler := func(ec EndpointContext) (HandlerResult, error) {
		return EndpointResponse{}, nil
	}
	err := reg.Register("BOOK", "/room", handler,
		WithErrors("room_unavailable", "invalid_dates"),
		WithRequiredScopes("booking:write"),
		WithDescription("Books a room."),
	)
	if err != nil {
		t.Fatal(err)
	}
	entry := reg.Lookup("BOOK", "/room")
	if entry == nil {
		t.Fatal("expected entry")
	}
	if len(entry.Errors) != 2 {
		t.Fatalf("expected 2 errors, got %v", entry.Errors)
	}
	if entry.RequiredScopes[0] != "booking:write" {
		t.Fatalf("expected scope booking:write, got %v", entry.RequiredScopes)
	}
	if entry.Description != "Books a room." {
		t.Fatalf("unexpected description: %q", entry.Description)
	}
}

func TestMakeContextDefaults(t *testing.T) {
	ctx := MakeContext()
	if ctx.Method != "QUERY" {
		t.Errorf("expected method=QUERY, got %q", ctx.Method)
	}
	if ctx.Path != "/" {
		t.Errorf("expected path=/, got %q", ctx.Path)
	}
	if ctx.AgentID != "test-agent" {
		t.Errorf("expected agent_id=test-agent, got %q", ctx.AgentID)
	}
}

func TestMakeContextOverrides(t *testing.T) {
	ctx := MakeContext(
		WithCtxMethod("BOOK"),
		WithCtxPath("/room"),
		WithInput(map[string]any{"value": "x"}),
		WithCtxAuthorityScope("a", "b"),
	)
	if ctx.Method != "BOOK" {
		t.Errorf("expected BOOK, got %q", ctx.Method)
	}
	if ctx.Input["value"] != "x" {
		t.Errorf("expected value=x, got %v", ctx.Input["value"])
	}
	if len(ctx.AuthorityScope) != 2 {
		t.Errorf("expected 2 scopes, got %v", ctx.AuthorityScope)
	}
}

func TestAssertOK(t *testing.T) {
	response := EndpointResponse{Body: map[string]any{"ok": true}}
	got := AssertOK(response, nil)
	if !got.Body["ok"].(bool) {
		t.Fail()
	}
}

func TestAssertOKPanicsOnError(t *testing.T) {
	defer func() {
		if r := recover(); r == nil {
			t.Fatal("expected panic on error result")
		}
	}()
	AssertOK(EndpointError{Code: "x", Message: "y"}, nil)
}

func TestAssertOKPanicsOnUnexpectedError(t *testing.T) {
	defer func() {
		if r := recover(); r == nil {
			t.Fatal("expected panic on unexpected error")
		}
	}()
	AssertOK(nil, errors.New("boom"))
}

func TestAssertErrorChecksCode(t *testing.T) {
	err := EndpointError{Code: "room_unavailable", Message: "full"}
	got := AssertError(err, nil, "room_unavailable")
	if got.Message != "full" {
		t.Fail()
	}
}

func TestAssertErrorPanicsOnWrongCode(t *testing.T) {
	defer func() {
		if r := recover(); r == nil {
			t.Fatal("expected panic on wrong code")
		}
	}()
	AssertError(EndpointError{Code: "x"}, nil, "y")
}
