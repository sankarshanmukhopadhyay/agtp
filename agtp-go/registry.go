package agtp

import (
	"fmt"
	"strings"
	"sync"
)

// RegisteredHandler is one entry in a Registry.
type RegisteredHandler struct {
	Method         string
	Path           string
	Handler        HandlerFunc
	Errors         []string
	RequiredScopes []string
	Description    string
}

// Registry maps (method, path) pairs to handler functions. Concurrent
// reads are safe after construction; concurrent registrations are
// serialized by an internal mutex.
type Registry struct {
	mu       sync.RWMutex
	handlers map[string]RegisteredHandler
}

// NewRegistry returns an empty registry. The default pattern is to
// build one per process at startup and hand it to a GatewayClient.
func NewRegistry() *Registry {
	return &Registry{
		handlers: make(map[string]RegisteredHandler),
	}
}

// RegisterOption is the functional-options shape for Register.
type RegisterOption func(*RegisteredHandler)

// WithErrors declares the error-code names this handler may return
// via EndpointError. These codes must match what the endpoint's
// agtp-server.toml declaration says.
func WithErrors(codes ...string) RegisterOption {
	return func(h *RegisteredHandler) {
		h.Errors = append(h.Errors, codes...)
	}
}

// WithRequiredScopes declares the authority scopes the calling agent
// must present for this handler. The daemon enforces these before
// dispatch; the handler can additionally consult ctx.AuthorityScope
// for finer-grained checks.
func WithRequiredScopes(scopes ...string) RegisterOption {
	return func(h *RegisteredHandler) {
		h.RequiredScopes = append(h.RequiredScopes, scopes...)
	}
}

// WithDescription attaches a short description to the registration.
func WithDescription(desc string) RegisterOption {
	return func(h *RegisteredHandler) {
		h.Description = desc
	}
}

// Register binds a handler for the given (method, path). Returns an
// error on duplicate registration. Method names are normalized to
// uppercase to match the AGTP catalog.
func (r *Registry) Register(method, path string, handler HandlerFunc, opts ...RegisterOption) error {
	method = strings.ToUpper(method)
	key := r.key(method, path)
	r.mu.Lock()
	defer r.mu.Unlock()
	if _, exists := r.handlers[key]; exists {
		return fmt.Errorf("handler already registered for (%s, %s)", method, path)
	}
	entry := RegisteredHandler{
		Method:  method,
		Path:    path,
		Handler: handler,
	}
	for _, opt := range opts {
		opt(&entry)
	}
	r.handlers[key] = entry
	return nil
}

// Lookup returns the registered handler for (method, path), or nil.
func (r *Registry) Lookup(method, path string) *RegisteredHandler {
	r.mu.RLock()
	defer r.mu.RUnlock()
	if entry, ok := r.handlers[r.key(method, path)]; ok {
		return &entry
	}
	return nil
}

// All returns a copy of every registered handler.
func (r *Registry) All() []RegisteredHandler {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := make([]RegisteredHandler, 0, len(r.handlers))
	for _, entry := range r.handlers {
		out = append(out, entry)
	}
	return out
}

// Count returns the number of registered handlers.
func (r *Registry) Count() int {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return len(r.handlers)
}

// Clear removes every registration. For test isolation.
func (r *Registry) Clear() {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.handlers = make(map[string]RegisteredHandler)
}

func (r *Registry) key(method, path string) string {
	return strings.ToUpper(method) + " " + path
}
