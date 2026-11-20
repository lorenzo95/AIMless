//go:build tailcat

package main

import (
	"bytes"
	"encoding/binary"
	"encoding/hex"
	"net"
	"strings"
	"testing"
	"time"

	"github.com/tailscale/tailcat"
	"tailscale.com/tailcfg"
	"tailscale.com/tstest/integration"
)

// These tests exercise the tailcat Transport through the real Mail layer
// (journal/retry/ACK/attachments) over an in-process local DERP+STUN, mirroring
// the Yggdrasil integration tests that are build-tagged !tailcat.

func tcRegion(t *testing.T) *tailcfg.DERPRegion {
	t.Helper()
	dm := integration.RunDERPAndSTUN(t, func(string, ...any) {}, "127.0.0.1")
	reg := dm.Regions[1]
	if reg == nil {
		t.Fatal("no region 1 in derpmap")
	}
	return reg
}

func tcMail(t *testing.T, dir string, reg *tailcfg.DERPRegion) (*TailcatNode, *Mail) {
	t.Helper()
	node, err := newTailcatNode(dir, reg, &aimlessLogger{})
	if err != nil {
		t.Fatalf("newTailcatNode: %v", err)
	}
	t.Cleanup(node.Stop)
	mail, err := NewMail(dir, 50, 1<<20, 250*time.Millisecond)
	if err != nil {
		t.Fatalf("NewMail: %v", err)
	}
	node.SetHandlers(mail.HandlePacket, mail.PathUp)
	mail.Attach(node)
	t.Cleanup(func() { mail.Close() })
	return node, mail
}

func tcLink(a, b *TailcatNode) {
	a.AddPeerAddr(b.Pub(), b.Address())
	b.AddPeerAddr(a.Pub(), a.Address())
}

func tcWaitPayload(t *testing.T, ch chan []byte, want string, timeout time.Duration) {
	t.Helper()
	deadline := time.After(timeout)
	for {
		select {
		case p := <-ch:
			if string(p) == want {
				return
			}
		case <-deadline:
			t.Fatalf("timeout waiting for payload %q", want)
		}
	}
}

func TestTailcatDeliveryAndAck(t *testing.T) {
	reg := tcRegion(t)
	nodeA, mailA := tcMail(t, t.TempDir(), reg)
	nodeB, mailB := tcMail(t, t.TempDir(), reg)
	tcLink(nodeA, nodeB)

	delivered := make(chan []byte, 8)
	mailB.SetOnDeliver(func(from PeerID, seq uint64, ts int64, payload []byte) { delivered <- payload })
	acked := make(chan uint64, 8)
	mailA.SetOnAcked(func(to PeerID, seq uint64) { acked <- seq })

	seq, err := mailA.SendMsg(nodeB.Pub(), []byte("hello tailcat"))
	if err != nil {
		t.Fatalf("SendMsg: %v", err)
	}
	tcWaitPayload(t, delivered, "hello tailcat", 20*time.Second)
	select {
	case s := <-acked:
		if s != seq {
			t.Fatalf("acked seq %d != sent %d", s, seq)
		}
	case <-time.After(20 * time.Second):
		t.Fatal("timeout waiting for ACK")
	}
}

func TestTailcatBidirectional(t *testing.T) {
	reg := tcRegion(t)
	nodeA, mailA := tcMail(t, t.TempDir(), reg)
	nodeB, mailB := tcMail(t, t.TempDir(), reg)
	tcLink(nodeA, nodeB)

	toB := make(chan []byte, 8)
	toA := make(chan []byte, 8)
	mailB.SetOnDeliver(func(from PeerID, seq uint64, ts int64, p []byte) { toB <- p })
	mailA.SetOnDeliver(func(from PeerID, seq uint64, ts int64, p []byte) { toA <- p })

	if _, err := mailA.SendMsg(nodeB.Pub(), []byte("a->b")); err != nil {
		t.Fatalf("SendMsg a->b: %v", err)
	}
	if _, err := mailB.SendMsg(nodeA.Pub(), []byte("b->a")); err != nil {
		t.Fatalf("SendMsg b->a: %v", err)
	}
	tcWaitPayload(t, toB, "a->b", 20*time.Second)
	tcWaitPayload(t, toA, "b->a", 20*time.Second)
}

func TestTailcatFileChunk(t *testing.T) {
	reg := tcRegion(t)
	nodeA, mailA := tcMail(t, t.TempDir(), reg)
	nodeB, mailB := tcMail(t, t.TempDir(), reg)
	tcLink(nodeA, nodeB)

	got := make(chan []byte, 8)
	mailB.SetOnDeliverFile(func(from PeerID, seq uint64, ts int64, p []byte) { got <- p })

	// A valid FILE chunk: 20-byte routing header (tid + index/total) + a 32KiB
	// body, under the 65535 envelope payload cap — no MTU ceiling.
	big := make([]byte, 20+32*1024)
	for i := 0; i < 16; i++ {
		big[i] = 0x01
	}
	binary.LittleEndian.PutUint16(big[16:18], 0) // index
	binary.LittleEndian.PutUint16(big[18:20], 1) // total
	for i := 20; i < len(big); i++ {
		big[i] = 0xAB
	}
	if _, err := mailA.SendFile(nodeB.Pub(), big); err != nil {
		t.Fatalf("SendFile: %v", err)
	}
	select {
	case p := <-got:
		if !bytes.Equal(p, big) {
			t.Fatalf("file chunk mismatch: len %d want %d", len(p), len(big))
		}
	case <-time.After(20 * time.Second):
		t.Fatal("timeout waiting for file chunk")
	}
}

func TestTailcatPersistentIdentity(t *testing.T) {
	reg := tcRegion(t)
	dir := t.TempDir()
	n1, err := newTailcatNode(dir, reg, &aimlessLogger{})
	if err != nil {
		t.Fatalf("first node: %v", err)
	}
	pub1, addr1 := n1.Pub(), n1.Address()
	n1.Stop()

	n2, err := newTailcatNode(dir, reg, &aimlessLogger{})
	if err != nil {
		t.Fatalf("second node: %v", err)
	}
	t.Cleanup(n2.Stop)
	if !bytes.Equal(n2.Pub(), pub1) {
		t.Fatal("node id changed across restart")
	}
	if n2.Address() != addr1 {
		t.Fatalf("address changed across restart:\n %s\n %s", addr1, n2.Address())
	}
}

func TestTailcatReconnect(t *testing.T) {
	reg := tcRegion(t)
	dirA, dirB := t.TempDir(), t.TempDir()
	nodeA, mailA := tcMail(t, dirA, reg)
	nodeB, mailB := tcMail(t, dirB, reg)
	tcLink(nodeA, nodeB)

	delivered := make(chan []byte, 16)
	mailB.SetOnDeliver(func(from PeerID, seq uint64, ts int64, p []byte) { delivered <- p })

	if _, err := mailA.SendMsg(nodeB.Pub(), []byte("first")); err != nil {
		t.Fatalf("SendMsg first: %v", err)
	}
	tcWaitPayload(t, delivered, "first", 20*time.Second)

	// B goes offline; A's next message must queue durably.
	nodeB.Stop()
	mailB.Close()
	if _, err := mailA.SendMsg(nodeB.Pub(), []byte("second")); err != nil {
		t.Logf("SendMsg while offline returned %v (expected; journal retains)", err)
	}

	// B returns on the same identity. A must evict its stale session, recreate
	// a fresh Client, and deliver the queued message via the retry loop.
	nodeB2, mailB2 := tcMail(t, dirB, reg)
	tcLink(nodeA, nodeB2)
	mailB2.SetOnDeliver(func(from PeerID, seq uint64, ts int64, p []byte) { delivered <- p })

	tcWaitPayload(t, delivered, "second", 30*time.Second)
}

func TestTailcatAddrPersistence(t *testing.T) {
	reg := tcRegion(t)
	dirA, dirB := t.TempDir(), t.TempDir()
	nodeA, mailA := tcMail(t, dirA, reg)
	nodeB, mailB := tcMail(t, dirB, reg)

	// Register through the contacts layer (persists addrs to contacts.json),
	// not AddPeerAddr directly — this is the path the watch op uses.
	if err := mailA.RememberAddr(nodeB.Pub(), nodeB.Address()); err != nil {
		t.Fatalf("RememberAddr A: %v", err)
	}
	if err := mailB.RememberAddr(nodeA.Pub(), nodeA.Address()); err != nil {
		t.Fatalf("RememberAddr B: %v", err)
	}
	delivered := make(chan []byte, 16)
	mailB.SetOnDeliver(func(from PeerID, seq uint64, ts int64, p []byte) { delivered <- p })
	if _, err := mailA.SendMsg(nodeB.Pub(), []byte("first")); err != nil {
		t.Fatalf("SendMsg first: %v", err)
	}
	tcWaitPayload(t, delivered, "first", 20*time.Second)

	// Restart A's daemon on the same datadir. A fresh Mail must reload the
	// persisted addresses via Attach and dial B without re-watching.
	nodeA.Stop()
	mailA.Close()
	nodeA2, mailA2 := tcMail(t, dirA, reg)
	_ = nodeA2
	if _, err := mailA2.SendMsg(nodeB.Pub(), []byte("second")); err != nil {
		t.Fatalf("SendMsg after restart: %v", err)
	}
	tcWaitPayload(t, delivered, "second", 20*time.Second)
}

func TestTailcatAddrForNormalisesLegacyAddress(t *testing.T) {
	// Before the server key was derived, a published address carried the
	// identity as the server key. Such stored addresses must keep working: the
	// dialer recomputes the current server key from the PSK the address holds,
	// so nobody has to re-exchange invites.
	reg := tcRegion(t)
	nodeA, _ := tcMail(t, t.TempDir(), reg)
	nodeB, _ := tcMail(t, t.TempDir(), reg)

	current := nodeB.Address()
	ci, err := tailcat.ParseAddr(tailcat.Addr(current))
	if err != nil {
		t.Fatalf("ParseAddr: %v", err)
	}
	np, err := nodePublicFromPeerID(nodeB.Pub())
	if err != nil {
		t.Fatalf("nodePublicFromPeerID: %v", err)
	}
	if ci.ServerPublic.NodePublic == np {
		t.Fatal("server key must differ from the identity key")
	}
	ci.ServerPublic = tailcat.NodePublic{NodePublic: np}
	legacy := string(ci.Addr())

	nodeA.AddPeerAddr(nodeB.Pub(), legacy)
	if got := nodeA.addrFor(hex.EncodeToString(nodeB.Pub())); got != current {
		t.Errorf("legacy address not normalised:\n got %s\nwant %s", got, current)
	}
	nodeA.AddPeerAddr(nodeB.Pub(), current)
	if got := nodeA.addrFor(hex.EncodeToString(nodeB.Pub())); got != current {
		t.Errorf("current address changed:\n got %s\nwant %s", got, current)
	}
}

func TestTailcatRelayConfig(t *testing.T) {
	reg := tcRegion(t)
	dir := t.TempDir()
	node, err := newTailcatNode(dir, reg, &aimlessLogger{})
	if err != nil {
		t.Fatalf("newTailcatNode: %v", err)
	}
	t.Cleanup(node.Stop)

	if node.RelayConfig() != (RelayConfig{}) {
		t.Fatal("expected empty relay config by default")
	}
	if _, err := node.SetRelay(RelayConfig{Hostname: "derp.example.com"}); err != nil {
		t.Fatalf("SetRelay: %v", err)
	}
	if node.RelayConfig().Hostname != "derp.example.com" {
		t.Fatal("relay not stored in memory")
	}
	if rc := loadRelayConfig(dir); rc.Hostname != "derp.example.com" {
		t.Fatalf("relay not persisted to config.json: %+v", rc)
	}
	r := regionForRelay(RelayConfig{Hostname: "derp.example.com, derp2.example.com"})
	if r == nil || len(r.Nodes) != 2 || r.Nodes[0].HostName != "derp.example.com" {
		t.Fatalf("regionForRelay built wrong region: %+v", r)
	}
	// Clearing removes the relay (back to default public relays).
	if _, err := node.SetRelay(RelayConfig{}); err != nil {
		t.Fatalf("SetRelay clear: %v", err)
	}
	if loadRelayConfig(dir) != (RelayConfig{}) {
		t.Fatal("relay not cleared from config.json")
	}
	if regionForRelay(RelayConfig{}) != nil {
		t.Fatal("empty relay should yield no custom region")
	}
}

// --- silent-path watchdog ---
//
// Regression for the DERP-only reconnect stall: a tunnel can stop delivering
// without ever erroring (the peer restarted and there is no direct UDP path to
// roam to), so readLoop blocks forever and mail keeps writing into a dead
// session. TestTailcatReconnect covers the end-to-end case, but it only fails
// when UDP is unavailable - these pin the mechanism on any host.

func tcWatchNode() *TailcatNode {
	return &TailcatNode{
		logger:     &aimlessLogger{},
		conns:      map[string]net.Conn{},
		clients:    map[string]*tailcat.Client{},
		awaitReply: map[string]int64{},
		stopCh:     make(chan struct{}),
	}
}

func tcEnvelope(t *testing.T, kind EnvelopeType) []byte {
	t.Helper()
	e := &Envelope{Version: envelopeVersion, Type: kind, Seq: 1, Ts: 1, Payload: []byte("x")}
	b, err := e.Encode()
	if err != nil {
		t.Fatalf("encode: %v", err)
	}
	return b
}

func TestTailcatWatchdogArmsOnlyOnReliableSends(t *testing.T) {
	n := tcWatchNode()
	peer := strings.Repeat("ab", 32)
	for _, kind := range []EnvelopeType{TypeStatus, TypeProbe, TypeAck} {
		n.noteReliableSend(peer, tcEnvelope(t, kind))
	}
	if len(n.awaitReply) != 0 {
		t.Fatal("one-way traffic must not arm the watchdog: a quiet peer never replies")
	}
	n.noteReliableSend(peer, tcEnvelope(t, TypeMsg))
	first := n.awaitReply[peer]
	if first == 0 {
		t.Fatal("MSG must arm the watchdog")
	}
	n.noteReliableSend(peer, tcEnvelope(t, TypeFile))
	if n.awaitReply[peer] != first {
		t.Fatal("the oldest unanswered send must be kept, not reset by later sends")
	}
	n.noteInbound(peer)
	if len(n.awaitReply) != 0 {
		t.Fatal("any inbound packet is proof of life")
	}
}

func TestTailcatWatchdogRebuildsASilentPath(t *testing.T) {
	n := tcWatchNode()
	peer := strings.Repeat("cd", 32)
	c, other := net.Pipe()
	defer other.Close()
	n.conns[peer] = c
	n.noteReliableSend(peer, tcEnvelope(t, TypeMsg))
	since := n.awaitReply[peer]

	n.reapSilentPeers(since + tailcatSilenceTimeout.Milliseconds() - 1)
	if n.conns[peer] == nil {
		t.Fatal("evicted before the silence timeout elapsed")
	}

	n.reapSilentPeers(since + tailcatSilenceTimeout.Milliseconds())
	if n.conns[peer] != nil {
		t.Fatal("a silent path must be evicted so the next send rebuilds the client")
	}
	if len(n.awaitReply) != 0 {
		t.Fatal("the wait must be cleared once the path is torn down")
	}
	if _, err := c.Write([]byte("x")); err == nil {
		t.Fatal("the evicted connection must be closed")
	}
}

func TestTailcatWatchdogLeavesAReplyingPeerAlone(t *testing.T) {
	n := tcWatchNode()
	peer := strings.Repeat("ef", 32)
	c, other := net.Pipe()
	defer other.Close()
	n.conns[peer] = c
	n.noteReliableSend(peer, tcEnvelope(t, TypeMsg))
	n.noteInbound(peer) // the ACK came back
	n.reapSilentPeers(time.Now().UnixMilli() + 60000)
	if n.conns[peer] == nil {
		t.Fatal("a peer that replied must keep its connection")
	}
}

func TestTailcatWatchdogClearsWhenThePeerDialsUs(t *testing.T) {
	n := tcWatchNode()
	peer := strings.Repeat("12", 32)
	n.noteReliableSend(peer, tcEnvelope(t, TypeMsg))
	c, other := net.Pipe()
	defer other.Close()
	n.registerConn(peer, c)
	if len(n.awaitReply) != 0 {
		t.Fatal("an inbound connection proves the path works")
	}
	n.reapSilentPeers(time.Now().UnixMilli() + 60000)
	if n.conns[peer] != c {
		t.Fatal("the inbound connection must survive")
	}
}

func TestTailcatWatchdogToleratesAnAlreadyTornDownPeer(t *testing.T) {
	n := tcWatchNode()
	peer := strings.Repeat("34", 32)
	n.awaitReply[peer] = time.Now().Add(-time.Minute).UnixMilli()
	n.reapSilentPeers(time.Now().UnixMilli()) // no conn: a read error already evicted it
	if len(n.awaitReply) != 0 {
		t.Fatal("the stale wait must be cleared without a connection to close")
	}
}
