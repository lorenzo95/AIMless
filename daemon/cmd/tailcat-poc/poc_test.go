// Phase 0 spike for the tailcat-transport branch.
//
// This is a go/no-go gate: it validates that tailcat v0.7.0 behaves the way the
// transport swap assumes, using tailcat's own in-process local DERP+STUN
// (tailscale.com/tstest/integration.RunDERPAndSTUN) so it is deterministic and
// needs no docker, TLS certs, or public relays.
//
// Run: go test -v ./...   (measurements are logged)
//
// Validated here: rendezvous + framed delivery, first-message latency, large
// (32KiB+) payloads over TCP, persistent identity across Server restarts,
// sender identity via PeerEnv, AllowClient admission + PSK requirement,
// offline re-dial cost, relayed throughput, idle goroutines/heap.
package main

import (
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/tailscale/tailcat"
	"tailscale.com/tailcfg"
	"tailscale.com/tstest/integration"
	"tailscale.com/types/key"
	"tailscale.com/types/logger"
)

const pocPort = 1

func mkLogger(t testing.TB, name string) logger.Logf {
	return func(format string, args ...any) {
		t.Helper()
		t.Logf("["+name+"] "+format, args...)
	}
}

// writeFrame / readFrame use a 4-byte big-endian length prefix. The production
// transport will reuse AIMless's existing 20-byte envelope header (payload_len)
// instead; the spike only needs to prove TCP framing carries large payloads.
func writeFrame(w io.Writer, b []byte) error {
	var hdr [4]byte
	binary.BigEndian.PutUint32(hdr[:], uint32(len(b)))
	if _, err := w.Write(hdr[:]); err != nil {
		return err
	}
	_, err := w.Write(b)
	return err
}

func readFrame(r io.Reader) ([]byte, error) {
	var hdr [4]byte
	if _, err := io.ReadFull(r, hdr[:]); err != nil {
		return nil, err
	}
	n := binary.BigEndian.Uint32(hdr[:])
	if n > 64<<20 {
		return nil, fmt.Errorf("frame too large: %d", n)
	}
	buf := make([]byte, n)
	if _, err := io.ReadFull(r, buf); err != nil {
		return nil, err
	}
	return buf, nil
}

// echoServer starts a tailcat.Server whose OnTCP handler echoes framed payloads
// and records the authenticated peer key of the last connection.
func echoServer(t testing.TB, reg *tailcfg.DERPRegion, priv key.NodePrivate, psk tailcat.PresharedKey) (*tailcat.Server, *peerCapture) {
	t.Helper()
	cap := &peerCapture{}
	s := &tailcat.Server{Key: priv, PresharedKey: psk, Logf: mkLogger(t, "server"), Region: reg}
	s.OnTCP = func(port uint16) func(net.Conn) {
		if port != pocPort {
			return nil
		}
		return func(c net.Conn) {
			defer c.Close()
			for _, kv := range s.PeerEnv(c.LocalAddr(), c.RemoteAddr()) {
				if strings.HasPrefix(kv, "TAILCAT_PEER_KEY=") {
					cap.set(strings.TrimPrefix(kv, "TAILCAT_PEER_KEY="))
				}
			}
			for {
				msg, err := readFrame(c)
				if err != nil {
					return
				}
				if err := writeFrame(c, msg); err != nil {
					return
				}
			}
		}
	}
	if err := s.Start(); err != nil {
		t.Fatalf("server Start: %v", err)
	}
	t.Cleanup(func() { s.Close() })
	return s, cap
}

type peerCapture struct {
	mu sync.Mutex
	k  string
}

func (c *peerCapture) set(k string) { c.mu.Lock(); c.k = k; c.mu.Unlock() }
func (c *peerCapture) get() string  { c.mu.Lock(); defer c.mu.Unlock(); return c.k }

// localRegion boots an in-process DERP+STUN and returns region 1.
func localRegion(t testing.TB) *tailcfg.DERPRegion {
	t.Helper()
	dm := integration.RunDERPAndSTUN(t, mkLogger(t, "derpstun"), "127.0.0.1")
	reg := dm.Regions[1]
	if reg == nil {
		t.Fatal("no region 1 in derpmap")
	}
	return reg
}

func TestSpikeRendezvousDeliveryAndLatency(t *testing.T) {
	reg := localRegion(t)
	priv := key.NewNode()
	psk := tailcat.NewPresharedKey()
	s, cap := echoServer(t, reg, priv, psk)

	c := &tailcat.Client{Server: s.TailcatAddr(), Key: key.NewNode(), Logf: mkLogger(t, "client")}
	t.Cleanup(func() { c.Close() })
	s.AddAllowedClient(c.PublicKey())

	// Rendezvous latency (meow/meowed handshake through DERP).
	pingStart := time.Now()
	pi, err := c.Ping(context.Background())
	if err != nil {
		t.Fatalf("Ping: %v", err)
	}
	t.Logf("MEASURE rendezvous: Ping.Latency=%v wall=%v", pi.Latency, time.Since(pingStart))

	// First-message time: dial + send + echo.
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	dialStart := time.Now()
	conn, err := c.DialTCPPort(ctx, pocPort)
	if err != nil {
		t.Fatalf("DialTCPPort: %v", err)
	}
	defer conn.Close()
	t.Logf("MEASURE dial: %v", time.Since(dialStart))

	payload := []byte("hello from the aimless spike")
	msgStart := time.Now()
	if err := writeFrame(conn, payload); err != nil {
		t.Fatalf("writeFrame: %v", err)
	}
	got, err := readFrame(conn)
	if err != nil {
		t.Fatalf("readFrame: %v", err)
	}
	t.Logf("MEASURE first-message RTT: %v", time.Since(msgStart))
	if string(got) != string(payload) {
		t.Fatalf("echo mismatch: got %q want %q", got, payload)
	}

	// Sender identity: the server saw the client's authenticated node key.
	if want := c.PublicKey().String(); cap.get() != want {
		t.Fatalf("PeerEnv key = %q, want %q", cap.get(), want)
	}
	t.Logf("VALIDATED sender identity via PeerEnv: %s", cap.get())
}

func TestSpikeLargePayloadFraming(t *testing.T) {
	reg := localRegion(t)
	s, _ := echoServer(t, reg, key.NewNode(), tailcat.NewPresharedKey())
	c := &tailcat.Client{Server: s.TailcatAddr(), Logf: mkLogger(t, "client")}
	t.Cleanup(func() { c.Close() })
	s.AddAllowedClient(c.PublicKey())
	if _, err := c.Ping(context.Background()); err != nil {
		t.Fatalf("Ping: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	conn, err := c.DialTCPPort(ctx, pocPort)
	if err != nil {
		t.Fatalf("Dial: %v", err)
	}
	defer conn.Close()

	// 32KiB chunk + 20-byte routing header, like an AIMless FILE chunk.
	big := make([]byte, 32*1024+20)
	for i := range big {
		big[i] = byte(i % 251)
	}
	start := time.Now()
	if err := writeFrame(conn, big); err != nil {
		t.Fatalf("write big: %v", err)
	}
	got, err := readFrame(conn)
	if err != nil {
		t.Fatalf("read big: %v", err)
	}
	if len(got) != len(big) {
		t.Fatalf("big length = %d, want %d", len(got), len(big))
	}
	for i := range big {
		if got[i] != big[i] {
			t.Fatalf("big byte %d differs", i)
		}
	}
	t.Logf("VALIDATED 32KiB+20 framing over TCP, RTT=%v (no MTU limit)", time.Since(start))
}

func TestSpikePersistentIdentity(t *testing.T) {
	reg := localRegion(t)
	priv := key.NewNode()
	psk := tailcat.NewPresharedKey()

	s1, _ := echoServer(t, reg, priv, psk)
	addr1 := s1.TailcatAddr()
	pub1 := s1.Addr()
	s1.Close()

	// "Restart": a fresh Server with the same Key + PresharedKey + Region.
	s2, _ := echoServer(t, reg, priv, psk)
	addr2 := s2.TailcatAddr()
	if addr1 != addr2 {
		t.Fatalf("address changed across restart:\n %s\n %s", addr1, addr2)
	}
	t.Logf("VALIDATED persistent identity: same Key+PSK+Region => stable address (%d chars)", len(addr2))
	_ = pub1

	// A different PSK must produce a different address (capability rotates).
	s3, _ := echoServer(t, reg, priv, tailcat.NewPresharedKey())
	if s3.TailcatAddr() == addr2 {
		t.Fatal("rotating PSK did not change the address")
	}
	t.Logf("VALIDATED PSK rotation changes address (invite staleness confirmed)")

	// The address parses back to the same server public key.
	ci, err := tailcat.ParseAddr(addr2)
	if err != nil {
		t.Fatalf("ParseAddr: %v", err)
	}
	if ci.ServerPublic.String() != priv.Public().String() {
		t.Fatalf("parsed server key %s != node key %s", ci.ServerPublic.String(), priv.Public().String())
	}
	t.Logf("VALIDATED ParseAddr recovers stable node id: %s", ci.ServerPublic.String())
}

func TestSpikeAllowClientAndPSK(t *testing.T) {
	reg := localRegion(t)
	s, _ := echoServer(t, reg, key.NewNode(), tailcat.NewPresharedKey())

	// Disallowed client: discovery handshake works but tunnel/dial must fail.
	bad := &tailcat.Client{Server: s.TailcatAddr(), Logf: mkLogger(t, "disallowed")}
	t.Cleanup(func() { bad.Close() })
	ctx, cancel := context.WithTimeout(context.Background(), 700*time.Millisecond)
	defer cancel()
	if conn, err := bad.DialTCPPort(ctx, pocPort); err == nil {
		conn.Close()
		t.Fatal("disallowed client established a tunnel")
	} else {
		t.Logf("VALIDATED disallowed client rejected: %v", err)
	}

	// Wrong PSK (even if allowed) must not establish a tunnel.
	ci, err := tailcat.ParseAddr(s.TailcatAddr())
	if err != nil {
		t.Fatal(err)
	}
	ci.PresharedKey = tailcat.NewPresharedKey()
	wrong := &tailcat.Client{Server: ci.Addr(), Logf: mkLogger(t, "wrong-psk")}
	t.Cleanup(func() { wrong.Close() })
	s.AddAllowedClient(wrong.PublicKey())
	ctx2, cancel2 := context.WithTimeout(context.Background(), 700*time.Millisecond)
	defer cancel2()
	if conn, err := wrong.DialTCPPort(ctx2, pocPort); err == nil {
		conn.Close()
		t.Fatal("wrong-PSK client established a tunnel")
	} else {
		t.Logf("VALIDATED wrong-PSK client rejected: %v", err)
	}

	// Allowed + correct PSK works.
	good := &tailcat.Client{Server: s.TailcatAddr(), Logf: mkLogger(t, "allowed")}
	t.Cleanup(func() { good.Close() })
	s.AddAllowedClient(good.PublicKey())
	if _, err := good.Ping(context.Background()); err != nil {
		t.Fatalf("allowed client Ping: %v", err)
	}
	ctx3, cancel3 := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel3()
	conn, err := good.DialTCPPort(ctx3, pocPort)
	if err != nil {
		t.Fatalf("allowed client Dial: %v", err)
	}
	conn.Close()
	t.Logf("VALIDATED AllowClient admission + PSK capability")
}

func TestSpikeOfflineRedialCost(t *testing.T) {
	reg := localRegion(t)
	priv := key.NewNode()
	psk := tailcat.NewPresharedKey()
	s, _ := echoServer(t, reg, priv, psk)
	addr := s.TailcatAddr()

	// Persistent client identity (the daemon uses its own node key here).
	clientKey := key.NewNode()
	c := &tailcat.Client{Server: addr, Key: clientKey, Logf: mkLogger(t, "client")}
	s.AddAllowedClient(c.PublicKey())
	if _, err := c.Ping(context.Background()); err != nil {
		t.Fatalf("Ping: %v", err)
	}
	// Take the server offline.
	s.Close()

	// Re-dial against an offline peer must fail (DERP is not a mailbox).
	// NOTE: dial blocks until the context deadline; it does not fail fast. The
	// daemon's retry loop must use a short dial timeout + exponential backoff.
	var elapsed time.Duration
	for i := 0; i < 3; i++ {
		ctx, cancel := context.WithTimeout(context.Background(), 1*time.Second)
		start := time.Now()
		conn, err := c.DialTCPPort(ctx, pocPort)
		elapsed += time.Since(start)
		cancel()
		if err == nil {
			conn.Close()
			t.Fatal("dial to offline server succeeded")
		}
	}
	t.Logf("MEASURE offline re-dial: 3 attempts total=%v avg=%v (blocks to ctx deadline; retry must back off)", elapsed, elapsed/3)

	// Bring a server back on the same identity. Recovery = evict the stale
	// Client and create a fresh one with the SAME persistent key. A stale
	// session's cached direct path is dead; re-Ping alone does not revive it.
	c.Close()
	s2, _ := echoServer(t, reg, priv, psk)
	c2 := &tailcat.Client{Server: addr, Key: clientKey, Logf: mkLogger(t, "client2")}
	t.Cleanup(func() { c2.Close() })
	s2.AddAllowedClient(c2.PublicKey())
	pctx, pcancel := context.WithTimeout(context.Background(), 10*time.Second)
	if _, err := c2.Ping(pctx); err != nil {
		pcancel()
		t.Fatalf("fresh-client Ping after server return: %v", err)
	}
	pcancel()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	conn, err := c2.DialTCPPort(ctx, pocPort)
	if err != nil {
		t.Fatalf("fresh-client dial after server return: %v", err)
	}
	defer conn.Close()
	if err := writeFrame(conn, []byte("back")); err != nil {
		t.Fatalf("write after return: %v", err)
	}
	got, err := readFrame(conn)
	if err != nil || string(got) != "back" {
		t.Fatalf("echo after return: %q %v", got, err)
	}
	t.Logf("VALIDATED delivery resumes when peer returns (evict + fresh Client, same key)")
}

func TestSpikeRelayedThroughput(t *testing.T) {
	reg := localRegion(t)
	s, _ := echoServer(t, reg, key.NewNode(), tailcat.NewPresharedKey())
	c := &tailcat.Client{Server: s.TailcatAddr(), Logf: mkLogger(t, "client")}
	t.Cleanup(func() { c.Close() })
	s.AddAllowedClient(c.PublicKey())
	if _, err := c.Ping(context.Background()); err != nil {
		t.Fatalf("Ping: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	conn, err := c.DialTCPPort(ctx, pocPort)
	if err != nil {
		t.Fatalf("Dial: %v", err)
	}
	defer conn.Close()

	chunk := make([]byte, 32*1024)
	const chunks = 64 // 2 MiB total
	start := time.Now()
	for i := 0; i < chunks; i++ {
		if err := writeFrame(conn, chunk); err != nil {
			t.Fatalf("write chunk %d: %v", i, err)
		}
		if _, err := readFrame(conn); err != nil {
			t.Fatalf("read chunk %d: %v", i, err)
		}
	}
	d := time.Since(start)
	mib := float64(chunks*len(chunk)) / (1 << 20)
	t.Logf("MEASURE echo throughput: %.2f MiB in %v = %.2f MiB/s (round-trip, in-process DERP)", mib, d, mib/d.Seconds())
}

func TestSpikeIdleResources(t *testing.T) {
	reg := localRegion(t)
	s, _ := echoServer(t, reg, key.NewNode(), tailcat.NewPresharedKey())
	c := &tailcat.Client{Server: s.TailcatAddr(), Logf: mkLogger(t, "client")}
	t.Cleanup(func() { c.Close() })
	s.AddAllowedClient(c.PublicKey())
	if _, err := c.Ping(context.Background()); err != nil {
		t.Fatalf("Ping: %v", err)
	}
	// Let it settle idle.
	time.Sleep(2 * time.Second)
	var ms runtime.MemStats
	runtime.ReadMemStats(&ms)
	t.Logf("MEASURE idle (1 server + 1 client, in-process DERP): goroutines=%d heapAlloc=%.1f MiB",
		runtime.NumGoroutine(), float64(ms.HeapAlloc)/(1<<20))
}

// silence unused-import guards if a build trims helpers.
var _ = errors.Is