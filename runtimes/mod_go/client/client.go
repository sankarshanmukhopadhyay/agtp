// Package client implements the module-side gateway client for Go.
//
// A GatewayClient connects to agtpd over a Unix domain socket or TCP
// loopback, performs the handshake, receives the daemon's endpoint
// registration, resolves each handler_reference against an agtp.Registry,
// then serves request frames in a synchronous loop.
//
// One connection, one in-flight request at a time. For concurrency,
// run multiple GatewayClient instances pointing at the same socket —
// agtpd accepts multiple module connections.
package client

import (
	"bufio"
	"fmt"
	"net"
	"os"
	"runtime"
	"strings"

	"agtp.io/agtp-go"
	"agtp.io/mod-go/internal/protocol"
)

// GatewayClient is one module-side gateway connection.
type GatewayClient struct {
	SocketPath          string
	Registry            *agtp.Registry
	ModuleID            string
	ModuleVersion       string
	CachedManifestHash  string

	conn               net.Conn
	reader             *bufio.Reader
	writer             *bufio.Writer
	stop               chan struct{}
	bindings           map[string]agtp.RegisteredHandler
	cachedBindings     map[string]agtp.RegisteredHandler
}

// NewGatewayClient builds a client; call Run to start serving.
func NewGatewayClient(socketPath string, registry *agtp.Registry) *GatewayClient {
	return &GatewayClient{
		SocketPath:    socketPath,
		Registry:      registry,
		ModuleID:      "mod_go",
		ModuleVersion: "0.1.0",
		stop:          make(chan struct{}),
		bindings:      make(map[string]agtp.RegisteredHandler),
	}
}

// Run connects, handshakes, registers, and serves until the daemon
// sends goodbye, the socket closes, or Stop is called.
func (c *GatewayClient) Run() error {
	if err := c.connect(); err != nil {
		return err
	}
	defer c.close()

	if err := c.handshake(); err != nil {
		return err
	}
	return c.serveLoop()
}

// Stop signals the serve loop to exit between frames. The current
// in-flight request still completes.
func (c *GatewayClient) Stop() {
	select {
	case <-c.stop:
	default:
		close(c.stop)
	}
}

func (c *GatewayClient) connect() error {
	var network, address string
	if isHostPort(c.SocketPath) {
		network = "tcp"
		address = c.SocketPath
	} else {
		network = "unix"
		address = c.SocketPath
	}
	conn, err := net.Dial(network, address)
	if err != nil {
		return fmt.Errorf("could not connect to gateway socket %s: %w", c.SocketPath, err)
	}
	c.conn = conn
	c.reader = bufio.NewReader(conn)
	c.writer = bufio.NewWriter(conn)
	return nil
}

func (c *GatewayClient) close() {
	if c.conn != nil {
		c.conn.Close()
	}
}

func (c *GatewayClient) handshake() error {
	hello := map[string]any{
		"type":             "hello",
		"gateway_versions": []string{protocol.GatewayVersion},
		"module": map[string]any{
			"id":      c.ModuleID,
			"version": c.ModuleVersion,
			"runtime": fmt.Sprintf("Go %s", runtime.Version()),
			"pid":     os.Getpid(),
		},
		"capabilities": []string{"registered_function"},
	}
	if c.CachedManifestHash != "" {
		hello["cached_manifest_hash"] = c.CachedManifestHash
	}
	if err := protocol.WriteFrame(c.writer, hello); err != nil {
		return err
	}
	if err := c.writer.Flush(); err != nil {
		return err
	}

	welcome, err := protocol.ReadFrame(c.reader)
	if err != nil {
		return err
	}
	if welcome["type"] == "error" {
		return fmt.Errorf("daemon refused handshake: %v: %v", welcome["code"], welcome["message"])
	}
	if welcome["type"] != "welcome" {
		return fmt.Errorf("expected welcome, got type=%v", welcome["type"])
	}
	if chosen, _ := welcome["gateway_version"].(string); chosen != protocol.GatewayVersion {
		return fmt.Errorf("daemon chose gateway version %q; this module speaks %q",
			chosen, protocol.GatewayVersion)
	}

	register, err := protocol.ReadFrame(c.reader)
	if err != nil {
		return err
	}
	switch register["type"] {
	case "register":
		return c.handleRegister(register)
	case "register_resume":
		return c.handleRegisterResume(register)
	default:
		return fmt.Errorf("expected register or register_resume, got type=%v", register["type"])
	}
}

func (c *GatewayClient) handleRegister(register map[string]any) error {
	manifestHash, _ := register["manifest_hash"].(string)
	endpoints, _ := register["endpoints"].([]any)

	resolved := []string{}
	errors_ := []map[string]any{}
	newBindings := make(map[string]agtp.RegisteredHandler)

	for _, ep := range endpoints {
		epMap, _ := ep.(map[string]any)
		method := strings.ToUpper(asString(epMap["method"]))
		path := asString(epMap["path"])
		ref := asString(epMap["handler_reference"])
		entry := c.Registry.Lookup(method, path)
		if entry == nil {
			errors_ = append(errors_, map[string]any{
				"endpoint": fmt.Sprintf("%s %s", method, path),
				"reason":   "handler_not_found",
				"detail":   fmt.Sprintf("no registered handler for (%s, %s); reference was %q", method, path, ref),
			})
			continue
		}
		newBindings[method+" "+path] = *entry
		resolved = append(resolved, method+" "+path)
	}

	if len(errors_) > 0 {
		protocol.WriteFrame(c.writer, map[string]any{
			"type":   "register_ack",
			"ok":     false,
			"errors": errors_,
		})
		c.writer.Flush()
		return fmt.Errorf("could not resolve %d endpoint reference(s)", len(errors_))
	}

	c.bindings = newBindings
	c.cachedBindings = make(map[string]agtp.RegisteredHandler, len(newBindings))
	for k, v := range newBindings {
		c.cachedBindings[k] = v
	}
	c.CachedManifestHash = manifestHash

	if err := protocol.WriteFrame(c.writer, map[string]any{
		"type":     "register_ack",
		"ok":       true,
		"resolved": resolved,
	}); err != nil {
		return err
	}
	return c.writer.Flush()
}

func (c *GatewayClient) handleRegisterResume(register map[string]any) error {
	manifestHash, _ := register["manifest_hash"].(string)
	if len(c.cachedBindings) == 0 || manifestHash != c.CachedManifestHash {
		protocol.WriteFrame(c.writer, map[string]any{
			"type": "register_ack",
			"ok":   false,
			"errors": []map[string]any{{
				"endpoint": "*",
				"reason":   "cache_miss",
				"detail":   fmt.Sprintf("no cached bindings for manifest_hash=%q", manifestHash),
			}},
		})
		c.writer.Flush()
		return fmt.Errorf("register_resume cache miss for hash %s", manifestHash)
	}
	c.bindings = make(map[string]agtp.RegisteredHandler, len(c.cachedBindings))
	resolved := make([]string, 0, len(c.cachedBindings))
	for k, v := range c.cachedBindings {
		c.bindings[k] = v
		resolved = append(resolved, k)
	}
	if err := protocol.WriteFrame(c.writer, map[string]any{
		"type":     "register_ack",
		"ok":       true,
		"resolved": resolved,
	}); err != nil {
		return err
	}
	return c.writer.Flush()
}

func (c *GatewayClient) serveLoop() error {
	for {
		select {
		case <-c.stop:
			return nil
		default:
		}
		frame, err := protocol.ReadFrame(c.reader)
		if err != nil {
			return nil // EOF or peer closed; not a fatal program error
		}
		switch frame["type"] {
		case "goodbye":
			return nil
		case "ping":
			protocol.WriteFrame(c.writer, map[string]any{
				"type":  "pong",
				"nonce": asString(frame["nonce"]),
			})
			c.writer.Flush()
		case "request":
			c.handleRequest(frame)
		default:
			protocol.WriteFrame(c.writer, map[string]any{
				"type":    "error",
				"code":    "phase_violation",
				"message": fmt.Sprintf("unexpected frame type %v", frame["type"]),
			})
			c.writer.Flush()
		}
	}
}

func (c *GatewayClient) handleRequest(frame map[string]any) {
	requestID := asString(frame["request_id"])
	envelope, _ := frame["envelope"].(map[string]any)
	method := strings.ToUpper(asString(envelope["method"]))
	path := asString(envelope["path"])
	entry, ok := c.bindings[method+" "+path]
	if !ok {
		protocol.WriteFrame(c.writer, map[string]any{
			"type":       "error",
			"request_id": requestID,
			"code":       "handler_exception",
			"message":    fmt.Sprintf("no handler bound for (%s, %s)", method, path),
		})
		c.writer.Flush()
		return
	}

	ctx := contextFromEnvelope(envelope, requestID)
	result, err := entry.Handler(ctx)
	if err != nil {
		protocol.WriteFrame(c.writer, map[string]any{
			"type":       "error",
			"request_id": requestID,
			"code":       "handler_exception",
			"message":    err.Error(),
		})
		c.writer.Flush()
		return
	}

	switch r := result.(type) {
	case agtp.EndpointResponse:
		respEnv := map[string]any{
			"body":   r.Body,
			"status": ifZero(r.Status, 200),
		}
		if len(r.Headers) > 0 {
			respEnv["headers"] = r.Headers
		}
		protocol.WriteFrame(c.writer, map[string]any{
			"type":       "response",
			"request_id": requestID,
			"envelope":   respEnv,
		})
	case agtp.EndpointError:
		errEnv := map[string]any{
			"code":    r.Code,
			"message": r.Message,
		}
		if r.Details != nil {
			errEnv["details"] = r.Details
		}
		protocol.WriteFrame(c.writer, map[string]any{
			"type":       "response",
			"request_id": requestID,
			"envelope":   map[string]any{"endpoint_error": errEnv},
		})
	default:
		protocol.WriteFrame(c.writer, map[string]any{
			"type":       "error",
			"request_id": requestID,
			"code":       "handler_exception",
			"message":    fmt.Sprintf("handler returned unexpected type %T", result),
		})
	}
	c.writer.Flush()
}

func contextFromEnvelope(envelope map[string]any, requestID string) agtp.EndpointContext {
	scopesList := func(key string) []string {
		raw, _ := envelope[key].([]any)
		out := make([]string, 0, len(raw))
		for _, v := range raw {
			out = append(out, asString(v))
		}
		return out
	}
	headers := map[string]string{}
	if raw, ok := envelope["headers"].(map[string]any); ok {
		for k, v := range raw {
			headers[k] = asString(v)
		}
	}
	var sessionID, taskID *string
	if v, ok := envelope["session_id"].(string); ok && v != "" {
		sessionID = &v
	}
	if v, ok := envelope["task_id"].(string); ok && v != "" {
		taskID = &v
	}
	input := map[string]any{}
	if raw, ok := envelope["input"].(map[string]any); ok {
		input = raw
	}
	return agtp.EndpointContext{
		Input:          input,
		AgentID:        asString(envelope["agent_id"]),
		PrincipalID:    asString(envelope["principal_id"]),
		AgentScopes:    scopesList("agent_scopes"),
		AuthorityScope: scopesList("authority_scope"),
		SessionID:      sessionID,
		TaskID:         taskID,
		RequestID:      asStringDefault(envelope["request_id"], requestID),
		Method:         strings.ToUpper(asString(envelope["method"])),
		Path:           asStringDefault(envelope["path"], "/"),
		Headers:        headers,
	}
}

func asString(v any) string {
	if v == nil {
		return ""
	}
	s, _ := v.(string)
	return s
}

func asStringDefault(v any, def string) string {
	if s := asString(v); s != "" {
		return s
	}
	return def
}

func ifZero(v, def int) int {
	if v == 0 {
		return def
	}
	return v
}

func isHostPort(s string) bool {
	// Heuristic: contains a colon and the part before it is a numeric
	// dotted quad or "localhost". Unix socket paths don't typically
	// look like that.
	if !strings.Contains(s, ":") {
		return false
	}
	host, _, ok := strings.Cut(s, ":")
	if !ok {
		return false
	}
	if host == "localhost" || host == "127.0.0.1" || host == "::1" || host == "[::1]" {
		return true
	}
	return false
}
