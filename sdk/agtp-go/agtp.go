// Package agtp is the public AGTP handler API for Go.
//
// The handler-author surface — value types, registration, and testing
// helpers — mirrors the validated Python (agtp/) and PHP (agtp-php/)
// libraries. Wire-level concerns and the gateway client live in the
// sibling mod_go module.
//
// Minimal handler example:
//
//	import (
//	    "agtp.io/agtp-go"
//	)
//
//	func bookRoom(ctx agtp.EndpointContext) (agtp.HandlerResult, error) {
//	    if ctx.Input["room_type"] == "presidential_suite" {
//	        return agtp.EndpointError{
//	            Code: "room_unavailable",
//	            Message: "Suite not available.",
//	        }, nil
//	    }
//	    return agtp.EndpointResponse{
//	        Body: map[string]any{"reservation_id": "res-1"},
//	    }, nil
//	}
//
//	func main() {
//	    reg := agtp.NewRegistry()
//	    reg.Register("BOOK", "/room", bookRoom,
//	        agtp.WithErrors("room_unavailable"))
//	    // Hand reg to mod_go's GatewayClient.
//	}
package agtp

// EndpointContext is the per-request envelope handed to a handler.
//
// Mirrors agtp.handlers.EndpointContext in the Python reference. Every
// field has already been validated by agtpd before this envelope
// crosses the gateway: Input is schema-checked, AgentID is
// authenticated, AuthorityScope is claim-validated. Handlers trust
// the contents.
type EndpointContext struct {
	Input          map[string]any    `json:"input"`
	AgentID        string            `json:"agent_id"`
	PrincipalID    string            `json:"principal_id"`
	AgentScopes    []string          `json:"agent_scopes"`
	AuthorityScope []string          `json:"authority_scope"`
	SessionID      *string           `json:"session_id,omitempty"`
	TaskID         *string           `json:"task_id,omitempty"`
	RequestID      string            `json:"request_id"`
	Method         string            `json:"method"`
	Path           string            `json:"path"`
	Headers        map[string]string `json:"headers"`
}

// HandlerResult is implemented by EndpointResponse and EndpointError
// and represents either a success or a declared failure. A handler
// returns one of these as its first value; the second return is for
// unexpected errors (the panic-equivalent path).
type HandlerResult interface {
	isHandlerResult()
}

// EndpointResponse is the success-shape returned from a handler.
//
// The Body is validated against the endpoint's output schema by
// agtpd before serialization to the AGTP wire.
type EndpointResponse struct {
	Body    map[string]any    `json:"body"`
	Status  int               `json:"status,omitempty"`
	Headers map[string]string `json:"headers,omitempty"`
}

func (EndpointResponse) isHandlerResult() {}

// EndpointError is a declared failure. Code MUST be one of the names
// in the endpoint's declared errors list. Undeclared codes are a
// protocol violation and become 500 errors logged against the module.
type EndpointError struct {
	Code    string         `json:"code"`
	Message string         `json:"message"`
	Details map[string]any `json:"details,omitempty"`
}

func (EndpointError) isHandlerResult() {}

// HandlerFunc is the contract every AGTP handler in Go satisfies.
//
// Return (EndpointResponse{...}, nil) on success.
// Return (EndpointError{...}, nil) for declared failures.
// Return (nil, err) for unexpected errors — the gateway client
// translates these to a handler_exception frame.
type HandlerFunc func(EndpointContext) (HandlerResult, error)
