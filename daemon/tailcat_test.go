//go:build tailcat

package main

import (
	"bytes"
	"encoding/binary"
	"testing"
	"time"

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
