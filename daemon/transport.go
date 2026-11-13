package main

import "crypto/ed25519"

// PeerID is the 32-byte node identifier used throughout the daemon (mail,
// presence, api, attachments). It is an alias for ed25519.PublicKey so existing
// code keeps compiling unchanged. The Yggdrasil transport uses an Ed25519 node
// key; the tailcat transport maps its Curve25519 WireGuard node key to/from
// these 32 bytes. mail/presence/api use a PeerID purely as a routing/identity
// token — the E2E layer signs with the client's separate identity key.
type PeerID = ed25519.PublicKey

// peerStatus is one entry of the status op's peer/relay list. The shape is
// Yggdrasil's today; the tailcat transport reports relay/path state through the
// same fields (refined when the API ops change).
type peerStatus struct {
	URI     string `json:"uri"`
	Up      bool   `json:"up"`
	Latency int64  `json:"latency_ms"`
	Inbound bool   `json:"inbound"`
	TXBytes uint64 `json:"tx_bytes"`
	RXBytes uint64 `json:"rx_bytes"`
	// Error is why the peer is not usable: the last dial error, or the
	// reason it was refused at startup (an unresolvable hostname).
	Error string `json:"error,omitempty"`
}

// Transport is the daemon's network boundary. mail.go sends and receives
// through it, api.go reads identity and peer/relay state from it, and main.go
// constructs it. Two implementations: YggNode (default, build tag !tailcat) and
// TailcatNode (build tag tailcat). Everything above this interface — journals,
// retry/ACK, attachments, presence, the E2E client — is transport-independent.
type Transport interface {
	// Send enqueues a latency-sensitive packet (text, ACK, status, probe).
	Send(to PeerID, payload []byte) (int, error)
	// SendBulk enqueues a large TypeFile chunk.
	SendBulk(to PeerID, payload []byte) (int, error)
	// SetHandlers installs the packet and path-up callbacks. They may be
	// installed after the transport is running, so implementations must
	// guard them against a concurrent first packet.
	SetHandlers(onPacket func(from PeerID, payload []byte), onPathUp func(PeerID))
	// Pub is the stable node identifier.
	Pub() PeerID
	// Address is the display address (a Yggdrasil IPv6, or a tailcat address).
	Address() string
	// Stop shuts the transport down.
	Stop()

	// Peer/relay management surface used by the status and setpeers ops.
	PeerState() (dialed []string, refused map[string]string)
	ConfiguredPeers() []string
	SetPeers(peers []string) (added, removed []string, failed map[string]string)
	PeerStatuses() []peerStatus
	MTU() int
	DefaultPeers() []string
	// AddPeerAddr registers a buddy's dial address (tailcat) and admits its
	// key to our server. The Yggdrasil transport ignores it (routing is by
	// node key, so no address is needed).
	AddPeerAddr(id PeerID, addr string)
}
