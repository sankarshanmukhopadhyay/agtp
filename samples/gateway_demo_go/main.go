// Command gateway-demo-go is a sample Go binary that registers two
// handlers and serves them via the gateway client. Used by the
// Python-side e2e test tests/test_gateway_e2e_go.py.
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

func echo(ctx agtp.EndpointContext) (agtp.HandlerResult, error) {
	value, _ := ctx.Input["value"].(string)
	return agtp.EndpointResponse{
		Body: map[string]any{"echo": value},
	}, nil
}

func bookRoom(ctx agtp.EndpointContext) (agtp.HandlerResult, error) {
	roomType, _ := ctx.Input["room_type"].(string)
	if roomType == "" {
		roomType = "double"
	}
	if roomType == "presidential_suite" {
		return agtp.EndpointError{
			Code:    "room_unavailable",
			Message: "The presidential suite is not available.",
			Details: map[string]any{"room_type": roomType},
		}, nil
	}
	guest, _ := ctx.Input["guest"].(string)
	if guest == "" {
		guest = "anon"
	}
	return agtp.EndpointResponse{
		Body: map[string]any{
			"reservation_id": fmt.Sprintf("res-%s-%s", guest, roomType),
			"agent":          ctx.AgentID,
		},
	}, nil
}

func main() {
	socketPath := flag.String("gateway-socket", "", "agtpd gateway socket path or host:port.")
	flag.Parse()
	if *socketPath == "" {
		fmt.Fprintln(os.Stderr, "[gateway-demo-go] --gateway-socket is required")
		os.Exit(2)
	}

	reg := agtp.NewRegistry()
	if err := reg.Register("QUERY", "/echo", echo); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	if err := reg.Register("BOOK", "/room", bookRoom,
		agtp.WithErrors("room_unavailable")); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}

	c := client.NewGatewayClient(*socketPath, reg)
	c.ModuleID = "gateway-demo-go"

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGTERM, syscall.SIGINT)
	go func() {
		<-sigCh
		c.Stop()
	}()

	if err := c.Run(); err != nil {
		fmt.Fprintf(os.Stderr, "[gateway-demo-go] %v\n", err)
		os.Exit(1)
	}
}
