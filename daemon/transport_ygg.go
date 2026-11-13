//go:build !tailcat

package main

import "github.com/yggdrasil-network/yggdrasil-go/src/core"

// YggNode adapts the Yggdrasil *Node to the Transport interface. Node itself is
// unchanged; this wrapper only adapts field access to methods and drops the
// explicit *core.Core argument from SetPeers.
type YggNode struct{ n *Node }

// NewTransport constructs the default (Yggdrasil) transport. The tailcat build
// provides its own NewTransport returning a TailcatNode.
func NewTransport(datadir string, peers, listenURLs []string, log *aimlessLogger) (Transport, error) {
	n, err := StartNode(datadir, peers, listenURLs, log)
	if err != nil {
		return nil, err
	}
	return &YggNode{n}, nil
}

func (y *YggNode) Send(to PeerID, payload []byte) (int, error)     { return y.n.Send(to, payload) }
func (y *YggNode) SendBulk(to PeerID, payload []byte) (int, error) { return y.n.SendBulk(to, payload) }
func (y *YggNode) SetHandlers(onPacket func(from PeerID, payload []byte), onPathUp func(PeerID)) {
	y.n.SetHandlers(onPacket, onPathUp)
}
func (y *YggNode) Pub() PeerID     { return y.n.Pub }
func (y *YggNode) Address() string { return y.n.Address.String() }
func (y *YggNode) Stop()           { y.n.Stop() }

func (y *YggNode) PeerState() ([]string, map[string]string) { return y.n.PeerState() }
func (y *YggNode) ConfiguredPeers() []string                { return y.n.ConfiguredPeers() }
func (y *YggNode) SetPeers(peers []string) ([]string, []string, map[string]string) {
	return y.n.SetPeers(y.n.Core, peers)
}
func (y *YggNode) MTU() int               { return int(y.n.Core.MTU()) }
func (y *YggNode) DefaultPeers() []string { return defaultPeers }

const transportName = "yggdrasil"

// AddPeerAddr is a no-op for Yggdrasil: peers are routed by node key.
func (y *YggNode) AddPeerAddr(id PeerID, addr string) {}

// PeerStatuses is the status op's peer list. GetPeers reads a peer's RTT sample
// without holding the lock the peer actor writes it with, so `go test -race` can
// flag a race inside ironwood when a peer completes its handshake at the same
// moment. It is upstream (ironwood network/peers.go), harmless in practice — the
// sample is only used for the latency readout.
func (y *YggNode) PeerStatuses() []peerStatus {
	peers := y.n.Core.GetPeers()
	out := make([]peerStatus, 0, len(peers))
	for _, p := range peers {
		out = append(out, peerStatus{
			URI:     p.URI,
			Up:      p.Up,
			Latency: p.Latency.Milliseconds(),
			Inbound: p.Inbound,
			TXBytes: p.TXBytes,
			RXBytes: p.RXBytes,
			Error:   lastPeerError(p),
		})
	}
	// Peers we refused (a hostname that does not resolve) are peers too: the
	// client has to show them, or the list silently shrinks.
	_, refused := y.n.PeerState()
	for uri, reason := range refused {
		out = append(out, peerStatus{URI: uri, Error: reason})
	}
	return out
}

// lastPeerError is why a peer is not up, if we know.
func lastPeerError(p core.PeerInfo) string {
	if p.LastError != nil {
		return p.LastError.Error()
	}
	return ""
}
