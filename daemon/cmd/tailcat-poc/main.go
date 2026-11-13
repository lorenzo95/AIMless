// Command tailcat-poc is the Phase 0 spike for the tailcat-transport branch.
//
// The spike itself is test-based (poc_test.go): it boots an in-process local
// DERP+STUN via tailscale.com/tstest/integration and validates rendezvous,
// framed delivery, persistent identity, admission control, reconnect recovery,
// throughput and idle resource use. Run it with:
//
//	go test -v ./...
//
// This isolated module keeps the heavy tailscale.com dependency tree out of the
// main aimless/daemon module. Findings: docs/tailcat-spike.md.
package main

import "fmt"

func main() {
	fmt.Println("tailcat-poc is a test-based spike. Run: go test -v ./...")
	fmt.Println("See docs/tailcat-spike.md for the Phase 0 findings (gate: GO).")
}