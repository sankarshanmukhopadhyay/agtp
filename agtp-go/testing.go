package agtp

import "fmt"

// MakeContext builds a synthetic EndpointContext for unit testing.
//
// Pair with AssertOK and AssertError to exercise handlers as plain
// functions without spinning up agtpd or the gateway socket.
type ContextOption func(*EndpointContext)

// WithInput sets the request body for the context.
func WithInput(input map[string]any) ContextOption {
	return func(ec *EndpointContext) {
		ec.Input = input
	}
}

// WithCtxMethod sets the AGTP verb.
func WithCtxMethod(method string) ContextOption {
	return func(ec *EndpointContext) {
		ec.Method = method
	}
}

// WithCtxPath sets the URI path.
func WithCtxPath(path string) ContextOption {
	return func(ec *EndpointContext) {
		ec.Path = path
	}
}

// WithCtxAgentID sets the invoking agent's identity.
func WithCtxAgentID(agentID string) ContextOption {
	return func(ec *EndpointContext) {
		ec.AgentID = agentID
	}
}

// WithCtxAuthorityScope sets the claimed scopes.
func WithCtxAuthorityScope(scopes ...string) ContextOption {
	return func(ec *EndpointContext) {
		ec.AuthorityScope = append([]string(nil), scopes...)
	}
}

// MakeContext returns an EndpointContext with sensible defaults
// (method=QUERY, path=/, agent_id="test-agent") overridden by opts.
func MakeContext(opts ...ContextOption) EndpointContext {
	ec := EndpointContext{
		Input:     map[string]any{},
		Method:    "QUERY",
		Path:      "/",
		AgentID:   "test-agent",
		RequestID: "test-req-1",
		Headers:   map[string]string{},
	}
	for _, opt := range opts {
		opt(&ec)
	}
	return ec
}

// AssertOK asserts that result is an EndpointResponse and returns it.
// Use in tests; panics with a clear message on mismatch.
func AssertOK(result HandlerResult, err error) EndpointResponse {
	if err != nil {
		panic(fmt.Sprintf("expected EndpointResponse, got unexpected error: %v", err))
	}
	resp, ok := result.(EndpointResponse)
	if !ok {
		if errResult, isErr := result.(EndpointError); isErr {
			panic(fmt.Sprintf("expected EndpointResponse, got EndpointError code=%s message=%s",
				errResult.Code, errResult.Message))
		}
		panic(fmt.Sprintf("expected EndpointResponse, got %T", result))
	}
	return resp
}

// AssertError asserts that result is an EndpointError with the given
// code. Pass code="" to skip the code check.
func AssertError(result HandlerResult, err error, code string) EndpointError {
	if err != nil {
		panic(fmt.Sprintf("expected EndpointError, got unexpected error: %v", err))
	}
	errResult, ok := result.(EndpointError)
	if !ok {
		if resp, isResp := result.(EndpointResponse); isResp {
			panic(fmt.Sprintf("expected EndpointError, got EndpointResponse status=%d", resp.Status))
		}
		panic(fmt.Sprintf("expected EndpointError, got %T", result))
	}
	if code != "" && errResult.Code != code {
		panic(fmt.Sprintf("expected EndpointError code=%q, got code=%q", code, errResult.Code))
	}
	return errResult
}
