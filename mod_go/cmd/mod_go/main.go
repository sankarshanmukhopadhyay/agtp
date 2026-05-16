// Command mod_go is the Go runtime module for AGTP.
//
// Connects to agtpd over the gateway socket and serves AGTP requests
// by dispatching to handlers registered through agtp.Registry. The
// CLI is intentionally tiny — the operator's Go program wires its
// own registry; this command just runs the gateway client.
//
// Operators with multiple handler binaries typically don't use this
// CLI at all — they import client.GatewayClient directly in their own
// main package. This command exists for the "single binary that uses
// a Go-plugins-style registry" deployment shape.
package main

import (
	"flag"
	"fmt"
	"os"
	"os/signal"
	"syscall"

	"agtp.io/agtp-go"
	"agtp.io/mod-go/client"
)

// Hook for tests / sample programs: register your handlers against
// this registry before starting the gateway client.
//
// In a real deployment, replace this with your own init code that
// imports your handler package and calls reg.Register(...).
var DefaultRegistry = agtp.NewRegistry()

func main() {
	socketPath := flag.String("gateway-socket", "", "Path to the agtpd gateway socket (or host:port for TCP loopback).")
	moduleID := flag.String("module-id", "mod_go", "Module identifier reported in the hello frame.")
	moduleVersion := flag.String("module-version", "0.1.0", "Module version reported in the hello frame.")
	flag.Parse()

	if *socketPath == "" {
		fmt.Fprintln(os.Stderr, "[mod_go] --gateway-socket is required")
		os.Exit(2)
	}

	fmt.Fprintf(os.Stderr, "[mod_go] handlers registered: %d\n", DefaultRegistry.Count())

	c := client.NewGatewayClient(*socketPath, DefaultRegistry)
	c.ModuleID = *moduleID
	c.ModuleVersion = *moduleVersion

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGTERM, syscall.SIGINT)
	go func() {
		<-sigCh
		fmt.Fprintln(os.Stderr, "[mod_go] shutting down")
		c.Stop()
	}()

	if err := c.Run(); err != nil {
		fmt.Fprintf(os.Stderr, "[mod_go] %v\n", err)
		os.Exit(1)
	}
}
