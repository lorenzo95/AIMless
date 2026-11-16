//go:build tailcat

package main

import (
	"context"
	"crypto/hkdf"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/tailscale/tailcat"
	"go4.org/mem"
	"tailscale.com/tailcfg"
	"tailscale.com/types/key"
)

// TailcatNode is the tailcat implementation of Transport. Unlike Yggdrasil's
// single datagram PacketConn, tailcat is pairwise: each daemon runs one
// tailcat.Server (its persistent identity) and dials buddies as tailcat.Clients.
// A connection is bidirectional — both ends read and write envelopes — so one
// TCP connection per peer pair carries MSG/ACK/STATUS/PROBE both ways.
//
// The wire frame IS the AIMless envelope: a 20-byte header whose last two bytes
// are the little-endian payload length, followed by the payload. TCP/netstack
// removes Yggdrasil's MTU ceiling, so 32KiB FILE chunks fit unchanged.
//
// Reconnect follows the Phase 0 spike finding: a stale session cannot be revived
// by re-Ping alone, so on any connection error we evict the peer's Client and
// connection; the next Send recreates a fresh Client with the same persistent
// key. The meow handshake and dial run in the background so Send never blocks
// on them: the handshake gets its own budget (tailcat resends the meow every
// second because DERP drops packets sent to a key whose relay connection is
// still coming up), while the dial stays short and mail's retryLoop provides
// the retry cadence.
//
// Our Server and our outbound Clients share one node key, and a DERP relay
// delivers to only the most recently active connection for a key, so on a
// single self-hosted relay the server and a client can shadow each other.
// Tearing the handshake down early (the old 2s budget) restarted that shadowing
// on every attempt, which made a custom relay look permanently offline.

const tailcatKeyFile = "tailcat.key"

// tailcatPort is the TCP port AIMless speaks inside the tunnel.
const tailcatPort = 1

// tailcatDialTimeout bounds the TCP dial inside an already-handshaked tunnel.
// It stays short so a failed attempt is retried quickly (mail's retryLoop and
// presence provide the cadence); a dial that fails means a stale session.
const tailcatDialTimeout = 2 * time.Second

// tailcatDialAttempts is how many times a warm client is re-dialled before the
// whole client is rebuilt. Each dial sends a fresh WireGuard initiation, so we
// retry at our own cadence instead of waiting for WireGuard's 5s retransmit.
const tailcatDialAttempts = 2

// tailcatPingTimeout bounds the meow handshake, separately from the dial.
// tailcat resends the meow every second because DERP drops packets sent to a
// key whose relay connection is still coming up, so the handshake needs more
// than the old 2s all-in-one deadline: cutting it short tore the client down
// just as it became usable, and the peer never came online on a custom relay.
const tailcatPingTimeout = 6 * time.Second

// defaultDERPMapURL is the official Tailscale DERP map (~28 regions). The
// tailcat.dev default is only a 4-region subset; the official relays accept
// tailcat connections too, so we default to the full map.
const defaultDERPMapURL = "https://login.tailscale.com/derpmap/default"

// selfHostedRegionID is the DERP region ID used for a self-hosted relay. It is
// deliberately outside the public ranges (official 1-28, tailcat.dev 301-304).
const selfHostedRegionID tailcfg.DERPRegionID = 900

// regionCacheTTL is how long the DERP region list is cached before a refresh,
// so the picker stays current without a fetch on every open.
const regionCacheTTL = time.Hour

// tailcatKeyset is the persisted identity: node key + pre-shared key + an
// optional fixed DERP region. The address is stable across restarts only when
// all three are restored together (the PSK is part of the address capability).
type tailcatKeyset struct {
	Key    string              `json:"key"`
	PSK    string              `json:"psk"`
	Region *tailcfg.DERPRegion `json:"region,omitempty"`
}

type TailcatNode struct {
	datadir        string
	priv           key.NodePrivate
	psk            tailcat.PresharedKey
	server         *tailcat.Server
	logger         *aimlessLogger
	relay          RelayConfig
	derpMapURL     string
	regionsCache   []RegionInfo
	regionByHost   map[string]RegionInfo // DERP node hostname -> region (current-region lookup)
	regionsCacheAt time.Time

	mu        sync.Mutex
	addrs     map[string]string          // peer hex -> tailcat address (dial target)
	clients   map[string]*tailcat.Client // peer hex -> our outbound client
	conns     map[string]net.Conn        // peer hex -> live bidirectional connection
	connects  map[string]chan struct{}   // peer hex -> in-flight/last connection attempt
	hmu       sync.RWMutex
	onPacket  func(from PeerID, payload []byte)
	onPathUp  func(PeerID)
	closeOnce sync.Once
	probeMu   sync.Mutex
	probeUp   bool
	probeErr  string
	stopCh    chan struct{}
}

// NewTransport constructs the tailcat transport. peers/listenURLs are Yggdrasil
// concepts and are ignored; the relay region comes from the keyset or config.
func NewTransport(datadir string, peers, listenURLs []string, log *aimlessLogger) (Transport, error) {
	return newTailcatNode(datadir, nil, log)
}

// newTailcatNode builds a TailcatNode. region overrides the keyset's region
// (tests inject an in-process local DERP); nil means use the keyset's region or
// let tailcat pick the nearest public relay.
func newTailcatNode(datadir string, region *tailcfg.DERPRegion, log *aimlessLogger) (*TailcatNode, error) {
	if err := os.MkdirAll(datadir, 0o700); err != nil {
		return nil, fmt.Errorf("create datadir: %w", err)
	}
	ks, err := loadOrCreateTailcatKey(datadir)
	if err != nil {
		return nil, err
	}
	var priv key.NodePrivate
	if err := priv.UnmarshalText([]byte(ks.Key)); err != nil {
		return nil, fmt.Errorf("parse tailcat key: %w", err)
	}
	var psk tailcat.PresharedKey
	if err := psk.UnmarshalText([]byte(ks.PSK)); err != nil {
		return nil, fmt.Errorf("parse tailcat psk: %w", err)
	}
	relay := loadRelayConfig(datadir)
	derpMapURL := relay.DERPMapURL
	if derpMapURL == "" {
		derpMapURL = defaultDERPMapURL
	}
	regionID := relay.RegionID
	if region == nil {
		if r := regionForRelay(relay); r != nil {
			region = r // self-hosted relay
			regionID = 0
		} else {
			region = ks.Region
		}
	} else {
		derpMapURL = "" // injected region (tests): no DERP map fetch
	}
	t := &TailcatNode{
		datadir:    datadir,
		priv:       priv,
		psk:        psk,
		logger:     log,
		relay:      relay,
		derpMapURL: derpMapURL,
		addrs:      make(map[string]string),
		clients:    make(map[string]*tailcat.Client),
		conns:      make(map[string]net.Conn),
		connects:   make(map[string]chan struct{}),
	}
	if err := t.startServer(region, regionID, derpMapURL); err != nil {
		return nil, err
	}
	// If the region was auto-picked (no explicit region or self-hosted relay),
	// persist it so the address is stable across restarts and can use the short
	// RegionID form. Injected regions (tests) and self-hosted relays are skipped.
	if region == nil && relay.RegionID == 0 {
		t.persistAutoRegion()
	}
	t.stopCh = make(chan struct{})
	t.probeUp = true // optimistic until the first probe completes
	go t.probeRelayLoop()
	return t, nil
}

// startServer creates and starts the tailcat.Server with the given relay. The
// OnTCP handler closes over t, so it survives a server swap.
func (t *TailcatNode) startServer(region *tailcfg.DERPRegion, regionID int, derpMapURL string) error {
	logf := func(format string, args ...any) { t.logger.Printf(format, args...) }
	t.server = &tailcat.Server{
		Key:          t.serverKey(),
		PresharedKey: t.psk,
		Region:       region,
		RegionID:     tailcfg.DERPRegionID(regionID),
		DERPMapURL:   derpMapURL,
		Logf:         logf,
		OnTCP: func(port uint16) func(net.Conn) {
			if port != tailcatPort {
				return nil
			}
			return func(c net.Conn) {
				peerHex := t.peerKeyHex(c)
				if peerHex == "" {
					c.Close()
					return
				}
				t.registerConn(peerHex, c)
				t.readLoop(peerHex, c) // blocks until the connection closes
			}
		},
	}
	if err := t.server.Start(); err != nil {
		return fmt.Errorf("start tailcat server: %w", err)
	}
	return nil
}

func loadOrCreateTailcatKey(datadir string) (*tailcatKeyset, error) {
	path := filepath.Join(datadir, tailcatKeyFile)
	data, err := os.ReadFile(path)
	if err == nil {
		var ks tailcatKeyset
		if jerr := json.Unmarshal(data, &ks); jerr != nil || ks.Key == "" || ks.PSK == "" {
			return nil, fmt.Errorf("corrupt %s", path)
		}
		return &ks, nil
	}
	if !os.IsNotExist(err) {
		return nil, fmt.Errorf("read key file: %w", err)
	}
	priv := key.NewNode()
	psk := tailcat.NewPresharedKey()
	kt, err := priv.MarshalText()
	if err != nil {
		return nil, err
	}
	pt, err := psk.MarshalText()
	if err != nil {
		return nil, err
	}
	ks := &tailcatKeyset{Key: string(kt), PSK: string(pt)}
	blob, err := json.MarshalIndent(ks, "", "  ")
	if err != nil {
		return nil, err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, append(blob, '\n'), 0o600); err != nil {
		return nil, fmt.Errorf("write key file: %w", err)
	}
	if err := os.Rename(tmp, path); err != nil {
		return nil, fmt.Errorf("rename key file: %w", err)
	}
	return ks, nil
}

// --- identity ---

func (t *TailcatNode) Pub() PeerID {
	raw := t.priv.Public().Raw32()
	return PeerID(raw[:])
}

// Address returns the tailcat address. For a public DERP region it uses the
// short RegionID-only form (like the tailcat CLI): the dialing peer resolves the
// region from the DERP map once. A self-hosted relay embeds its custom region
// instead (it is not in the public map) - longer, but self-contained.
func (t *TailcatNode) Address() string {
	long := t.server.TailcatAddr()
	t.mu.Lock()
	rc := t.relay
	t.mu.Unlock()
	if rc.Hostname == "" && rc.RegionID != 0 {
		if ci, err := tailcat.ParseAddr(long); err == nil {
			ci.Region = nil
			ci.RegionID = tailcfg.DERPRegionID(rc.RegionID)
			return string(ci.Addr())
		}
	}
	return string(long)
}

// persistAutoRegion records the region the server auto-picked (nearest by
// netcheck) so it is stable across restarts: otherwise a restart could pick a
// different region and change the address, staling invites. It also lets Address
// use the short RegionID form.
func (t *TailcatNode) persistAutoRegion() {
	cur, _ := t.Regions()
	if cur.ID == 0 {
		return // offline or unidentified: keep the long form, retry next start
	}
	t.mu.Lock()
	t.relay.RegionID = cur.ID
	rc := t.relay
	t.mu.Unlock()
	if err := saveRelayConfig(t.datadir, rc); err != nil {
		t.logger.Printf("persist auto region: %v", err)
	}
}

func (t *TailcatNode) Stop() {
	t.closeOnce.Do(func() {
		if t.stopCh != nil {
			close(t.stopCh)
		}
		t.mu.Lock()
		for _, c := range t.conns {
			c.Close()
		}
		for _, cl := range t.clients {
			cl.Close()
		}
		t.conns = map[string]net.Conn{}
		t.clients = map[string]*tailcat.Client{}
		t.mu.Unlock()
		t.server.Close()
	})
}

// --- handlers ---

func (t *TailcatNode) SetHandlers(onPacket func(from PeerID, payload []byte), onPathUp func(PeerID)) {
	t.hmu.Lock()
	t.onPacket = onPacket
	t.onPathUp = onPathUp
	t.hmu.Unlock()
}

func (t *TailcatNode) packetHandler() func(from PeerID, payload []byte) {
	t.hmu.RLock()
	defer t.hmu.RUnlock()
	return t.onPacket
}

func (t *TailcatNode) firePathUp(peerHex string) {
	t.hmu.RLock()
	h := t.onPathUp
	t.hmu.RUnlock()
	if h != nil {
		if pid, err := peerIDFromHex(peerHex); err == nil {
			h(pid)
		}
	}
}

// --- peer address registry / admission ---

// AddPeerAddr registers a buddy's dial address and admits its key to our server.
// Contacts are mutual in AIMless, so registering a peer both lets us dial it and
// lets it dial us.
func (t *TailcatNode) AddPeerAddr(id PeerID, addr string) {
	peerHex := hex.EncodeToString(id)
	t.mu.Lock()
	t.addrs[peerHex] = addr
	t.mu.Unlock()
	if np, err := nodePublicFromPeerID(id); err == nil {
		t.server.AddAllowedClient(np)
	}
}

func (t *TailcatNode) addrFor(peerHex string) string {
	t.mu.Lock()
	addr := t.addrs[peerHex]
	t.mu.Unlock()
	if addr == "" {
		return ""
	}
	// Addresses published before the server key was derived carry the peer's
	// identity as the server key. Recompute the current form from the PSK the
	// address already carries, so old stored addresses keep working without
	// re-exchanging invites.
	ci, err := tailcat.ParseAddr(tailcat.Addr(addr))
	if err != nil {
		return addr
	}
	pid, err := peerIDFromHex(peerHex)
	if err != nil {
		return addr
	}
	np, err := nodePublicFromPeerID(pid)
	if err != nil || ci.ServerPublic.NodePublic != np {
		return addr
	}
	sk := deriveKey(ci.PresharedKey, serverKeyLabel)
	if sk.IsZero() {
		return addr
	}
	ci.ServerPublic = tailcat.NodePublic{NodePublic: sk.Public()}
	return string(ci.Addr())
}

// peerKeyHex extracts the authenticated peer node key from an accepted conn.
// v0.7.0 exposes this via PeerEnv (TAILCAT_PEER_KEY), not an exported PeerKey.
func (t *TailcatNode) peerKeyHex(c net.Conn) string {
	for _, kv := range t.server.PeerEnv(c.LocalAddr(), c.RemoteAddr()) {
		if strings.HasPrefix(kv, "TAILCAT_PEER_KEY=") {
			s := strings.TrimPrefix(kv, "TAILCAT_PEER_KEY=")
			s = strings.TrimPrefix(s, "nodekey:")
			if raw, err := hex.DecodeString(s); err == nil && len(raw) == 32 {
				return s
			}
		}
	}
	return ""
}

// --- connection lifecycle ---

func (t *TailcatNode) registerConn(peerHex string, c net.Conn) {
	t.mu.Lock()
	old := t.conns[peerHex]
	t.conns[peerHex] = c
	t.mu.Unlock()
	if old != nil && old != c {
		old.Close()
	}
	t.firePathUp(peerHex)
}

// evictConn drops a peer's connection and client. When c is a specific conn
// that is no longer the current one, only that stale conn is closed: its death
// says nothing about the client we have since rebuilt, and closing that would
// kill a freshly completed handshake (the reconnect case).
func (t *TailcatNode) evictConn(peerHex string, c net.Conn) {
	t.mu.Lock()
	stale := c != nil && t.conns[peerHex] != c
	var cl *tailcat.Client
	if !stale {
		delete(t.conns, peerHex)
		cl = t.clients[peerHex]
		delete(t.clients, peerHex)
	}
	t.mu.Unlock()
	if c != nil {
		c.Close()
	}
	if cl != nil {
		cl.Close()
	}
}

func (t *TailcatNode) readLoop(peerHex string, c net.Conn) {
	pid, err := peerIDFromHex(peerHex)
	if err != nil {
		c.Close()
		return
	}
	for {
		env, err := readEnvelope(c)
		if err != nil {
			t.evictConn(peerHex, c)
			return
		}
		if h := t.packetHandler(); h != nil {
			h(pid, env)
		}
	}
}

// errConnectPending is the transport's transient "not yet": callers keep their
// short deadlines and the next attempt finds a live connection. API handlers
// recognise it via errTransportPending and must not report it as a failure.
var errConnectPending = errTransportPending

// connFor returns a live connection to a peer, dialing in the background if
// necessary. At most one attempt per peer is in flight, so two clients cannot
// coexist and shadow each other on the relay.
//
// The meow handshake and the dial run back-to-back in the same goroutine. That
// ordering matters: our server and our client share one node key, and a DERP
// relay delivers only to the most recently active connection for a key. The
// client is the last writer right after its meow, so dialling immediately keeps
// the handshake response addressed to it; splitting the two lets our server ack
// a meow in between and steal delivery. A failed attempt is torn down and
// rebuilt, which is what breaks the shadowing on a single self-hosted relay.
func (t *TailcatNode) connFor(peerHex string) (net.Conn, error) {
	t.mu.Lock()
	c := t.conns[peerHex]
	done := t.connects[peerHex]
	t.mu.Unlock()
	if c != nil {
		return c, nil
	}
	if done != nil {
		select {
		case <-done:
			// Attempt finished; fall through and retry if still no conn.
		default:
			return nil, errConnectPending
		}
	}
	addr := t.addrFor(peerHex)
	if addr == "" {
		return nil, fmt.Errorf("no tailcat address for peer %s", peerHex)
	}
	done = make(chan struct{})
	t.mu.Lock()
	if cur := t.connects[peerHex]; cur != nil {
		select {
		case <-cur:
		default:
			t.mu.Unlock()
			return nil, errConnectPending
		}
	}
	if c := t.conns[peerHex]; c != nil {
		t.mu.Unlock()
		return c, nil
	}
	t.connects[peerHex] = done
	t.mu.Unlock()
	go t.connectPeer(peerHex, addr, done)
	return nil, errConnectPending
}

// connectPeer performs the meow handshake and the dial as one sequence, then
// publishes the connection. On failure it drops the client so the next attempt
// rebuilds it: a stale session cannot be revived by re-Ping alone (Phase 0).
func (t *TailcatNode) connectPeer(peerHex, addr string, done chan struct{}) {
	defer close(done)
	if t.stopped() {
		return
	}
	t.mu.Lock()
	cl := t.clients[peerHex]
	t.mu.Unlock()
	if cl == nil {
		cl = t.newClient(addr, t.priv)
	}
	pctx, pcancel := context.WithTimeout(context.Background(), tailcatPingTimeout)
	_, err := cl.Ping(pctx)
	pcancel()
	var c net.Conn
	if err == nil {
		// Re-dial the warm client rather than rebuilding: each dial sends a
		// fresh WireGuard initiation, so we retry at our own cadence instead
		// of waiting for WireGuard's 5s retransmit.
		for attempt := 0; attempt < tailcatDialAttempts; attempt++ {
			dctx, dcancel := context.WithTimeout(context.Background(), tailcatDialTimeout)
			c, err = cl.DialTCPPort(dctx, tailcatPort)
			dcancel()
			if err == nil {
				break
			}
		}
	}
	if err != nil {
		t.logger.Printf("tailcat: connect to %s failed: %v", peerHex[:8], err)
		// A failed dial can return a non-nil interface wrapping a typed-nil
		// conn, so only the client is closed here.
		cl.Close()
		t.mu.Lock()
		if t.clients[peerHex] == cl {
			delete(t.clients, peerHex)
		}
		t.mu.Unlock()
		return
	}
	if t.stopped() {
		cl.Close()
		c.Close()
		return
	}
	t.mu.Lock()
	if t.conns[peerHex] != nil {
		// The peer dialled us while we were connecting; that connection already
		// carries both directions, so keep it. Retain our handshaked client too
		// (its key no longer collides with the server's) so we can dial at once
		// if that connection dies, instead of redoing the handshake.
		old := t.clients[peerHex]
		t.clients[peerHex] = cl
		t.mu.Unlock()
		if old != nil && old != cl {
			old.Close()
		}
		c.Close()
		return
	}
	old := t.clients[peerHex]
	t.clients[peerHex] = cl
	t.mu.Unlock()
	if old != nil && old != cl {
		old.Close()
	}
	t.registerConn(peerHex, c)
	go t.readLoop(peerHex, c)
}

// stopped reports whether Stop has been called.
func (t *TailcatNode) stopped() bool {
	select {
	case <-t.stopCh:
		return true
	default:
		return false
	}
}

// --- Transport send surface ---

func (t *TailcatNode) Send(to PeerID, payload []byte) (int, error) {
	return t.send(to, payload)
}

func (t *TailcatNode) SendBulk(to PeerID, payload []byte) (int, error) {
	// TCP orders and segments; bulk shares the connection (no separate queue).
	return t.send(to, payload)
}

func (t *TailcatNode) send(to PeerID, payload []byte) (int, error) {
	if len(to) != 32 {
		return 0, fmt.Errorf("bad public key size: %d", len(to))
	}
	peerHex := hex.EncodeToString(to)
	c, err := t.connFor(peerHex)
	if err != nil {
		return 0, err
	}
	if _, err := c.Write(payload); err != nil {
		t.evictConn(peerHex, c)
		return 0, err
	}
	return len(payload), nil
}

// --- peer/relay management surface (relay-shaped; refined in Phase 3) ---

func (t *TailcatNode) PeerState() ([]string, map[string]string) {
	return []string{t.relayLabel()}, map[string]string{}
}

func (t *TailcatNode) ConfiguredPeers() []string { return []string{t.relayLabel()} }

func (t *TailcatNode) SetPeers(peers []string) ([]string, []string, map[string]string) {
	// Relay reconfiguration is handled by the setrelay op (Phase 3).
	return nil, nil, map[string]string{}
}

func (t *TailcatNode) PeerStatuses() []peerStatus {
	t.probeMu.Lock()
	up, errStr := t.probeUp, t.probeErr
	t.probeMu.Unlock()
	ps := peerStatus{URI: t.relayLabel(), Up: up}
	if !up && errStr != "" {
		ps.Error = errStr
	}
	return []peerStatus{ps}
}

// probeRelayLoop periodically checks whether the configured DERP relay is
// reachable, so the UI can show a dead relay instead of a false "online".
func (t *TailcatNode) probeRelayLoop() {
	t.probeRelay()
	ticker := time.NewTicker(20 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-t.stopCh:
			return
		case <-ticker.C:
			t.probeRelay()
		}
	}
}

func (t *TailcatNode) probeRelay() {
	up, errStr := t.relayReachable()
	t.probeMu.Lock()
	t.probeUp = up
	t.probeErr = errStr
	t.probeMu.Unlock()
}

// relayReachable probes the configured relay with a short TCP dial (DERP listens
// on 443): the self-hosted hostname(s), or the current public region's nodes.
// tailcat/magicsock does not expose its DERP connection state, so this is how we
// tell a dead relay from a live one.
func (t *TailcatNode) relayReachable() (bool, string) {
	t.mu.Lock()
	hostname := t.relay.Hostname
	t.mu.Unlock()
	var hosts []string
	if hostname != "" {
		for _, h := range strings.Split(hostname, ",") {
			if h = strings.TrimSpace(h); h != "" {
				hosts = append(hosts, h)
			}
		}
	} else {
		cur, _ := t.Regions()
		hosts = cur.Nodes
	}
	if len(hosts) == 0 {
		return true, "" // nothing to probe (e.g. injected region); assume up
	}
	var lastErr string
	for _, h := range hosts {
		conn, err := net.DialTimeout("tcp", net.JoinHostPort(h, "443"), 3*time.Second)
		if err == nil {
			conn.Close()
			return true, ""
		}
		lastErr = err.Error()
	}
	return false, lastErr
}

func (t *TailcatNode) MTU() int { return 0 } // TCP: no datagram MTU ceiling

func (t *TailcatNode) DefaultPeers() []string { return nil }

const transportName = "tailcat"

func (t *TailcatNode) relayLabel() string {
	return "tailcat:" + string(t.server.TailcatAddr())
}

// --- helpers ---

// readEnvelope reads one length-delimited AIMless envelope from a stream.
func readEnvelope(r io.Reader) ([]byte, error) {
	var hdr [envelopeHeaderSize]byte
	if _, err := io.ReadFull(r, hdr[:]); err != nil {
		return nil, err
	}
	if hdr[0] != envelopeVersion {
		return nil, ErrBadVersion
	}
	n := int(binary.LittleEndian.Uint16(hdr[18:20]))
	buf := make([]byte, envelopeHeaderSize+n)
	copy(buf, hdr[:])
	if _, err := io.ReadFull(r, buf[envelopeHeaderSize:]); err != nil {
		return nil, err
	}
	return buf, nil
}

// newClient builds an outbound client to a peer's address with the given node key.
func (t *TailcatNode) newClient(addr string, k key.NodePrivate) *tailcat.Client {
	return &tailcat.Client{
		Server:     tailcat.Addr(addr),
		Key:        k,
		DERPMapURL: t.derpMapURL, // resolve short RegionID addresses against our map
		Logf:       func(format string, args ...any) { t.logger.Printf(format, args...) },
	}
}

func peerIDFromHex(peerHex string) (PeerID, error) {
	b, err := hex.DecodeString(peerHex)
	if err != nil {
		return nil, err
	}
	if len(b) != 32 {
		return nil, fmt.Errorf("bad peer id length %d", len(b))
	}
	return PeerID(b), nil
}

func nodePublicFromPeerID(id PeerID) (key.NodePublic, error) {
	if len(id) != 32 {
		return key.NodePublic{}, fmt.Errorf("bad peer id length %d", len(id))
	}
	return key.NodePublicFromRaw32(mem.B(id)), nil
}

// serverKeyLabel domain-separates the server's listening key from the identity
// key and from any other use of the pre-shared key.
const serverKeyLabel = "aimless-tc-server-v1"

// deriveKey expands a pre-shared key into a distinct node key. The PSK is
// already a secret carried inside the tailcat address, so this exposes nothing
// new, and it is stable across restarts so the published address stays usable.
func deriveKey(psk tailcat.PresharedKey, label string) key.NodePrivate {
	raw, err := hkdf.Key(sha256.New, psk[:], []byte(label), label, 32)
	if err != nil || len(raw) != 32 {
		return key.NodePrivate{}
	}
	var b [32]byte
	copy(b[:], raw)
	return key.NodePrivateFromRaw32(mem.B(b[:]))
}

// serverKey is the node key our tailcat.Server listens on.
//
// It must differ from the identity key our outbound clients use: a DERP relay
// delivers only to the most recently active connection for a key, so a server
// sharing its key with our clients keeps stealing delivery from the client
// waiting for the meowed reply and the WireGuard handshake response. Listening
// on a derived key leaves the identity key to the clients, and inbound
// connections still carry the peer's identity key, so attribution is unchanged.
func (t *TailcatNode) serverKey() key.NodePrivate {
	if k := deriveKey(t.psk, serverKeyLabel); !k.IsZero() {
		return k
	}
	return t.priv
}

// --- relay configuration ---

func (t *TailcatNode) RelayConfig() RelayConfig {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.relay
}

// SetRelay persists a new relay configuration. It applies on the next daemon
// restart: the relay is embedded in the tailcat address, so changing it changes
// the address (and stales old invites). Returns the current address.
func (t *TailcatNode) SetRelay(cfg RelayConfig) (string, error) {
	if err := saveRelayConfig(t.datadir, cfg); err != nil {
		return "", err
	}
	t.mu.Lock()
	t.relay = cfg
	t.mu.Unlock()
	return t.Address(), nil
}

func loadRelayConfig(datadir string) RelayConfig {
	cfg, err := loadConfig(filepath.Join(datadir, "config.json"))
	if err != nil || cfg.Relay == nil {
		return RelayConfig{}
	}
	return *cfg.Relay
}

func saveRelayConfig(datadir string, rc RelayConfig) error {
	path := filepath.Join(datadir, "config.json")
	cfg, err := loadConfig(path)
	if err != nil {
		return err
	}
	if rc.Hostname == "" && rc.DERPMapURL == "" && rc.RegionID == 0 {
		cfg.Relay = nil
	} else {
		cfg.Relay = &rc
	}
	return saveConfig(path, cfg)
}

// regionForRelay builds a custom DERP region from a self-hosted relay hostname
// (or comma-separated list). Empty hostname means no custom region.
func regionForRelay(cfg RelayConfig) *tailcfg.DERPRegion {
	if strings.TrimSpace(cfg.Hostname) == "" {
		return nil
	}
	reg := &tailcfg.DERPRegion{
		RegionID:   selfHostedRegionID,
		RegionCode: "custom",
		RegionName: "Self-hosted relay",
	}
	for _, host := range strings.Split(cfg.Hostname, ",") {
		if host = strings.TrimSpace(host); host != "" {
			reg.Nodes = append(reg.Nodes, &tailcfg.DERPNode{
				HostName: host,
				Name:     host,
				RegionID: selfHostedRegionID,
			})
		}
	}
	if len(reg.Nodes) == 0 {
		return nil
	}
	return reg
}

// Regions reports the current DERP region and the available regions from the
// DERP map (fetched once and cached). The current region is identified by
// matching the DERP node hostnames embedded in our address against the map:
// tailcat zeroes the numeric region ID when encoding an address, so ParseAddr
// returns a placeholder ID (index+1), not the real DERP region.
func (t *TailcatNode) Regions() (RegionInfo, []RegionInfo) {
	t.mu.Lock()
	cached := t.regionsCache
	cacheAt := t.regionsCacheAt
	byHost := t.regionByHost
	url := t.derpMapURL
	relayRegion := t.relay.RegionID
	hostname := t.relay.Hostname
	t.mu.Unlock()
	if (cached == nil || time.Since(cacheAt) > regionCacheTTL) && url != "" {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		dm, err := tailcat.FetchDERPMap(ctx, tailcat.DERPMapURL(url))
		cancel()
		if err == nil {
			byHost = make(map[string]RegionInfo)
			for _, r := range dm.Regions {
				ri := RegionInfo{ID: int(r.RegionID), Code: r.RegionCode, Name: r.RegionName}
				for _, n := range r.Nodes {
					if n.HostName != "" {
						ri.Nodes = append(ri.Nodes, n.HostName)
					}
				}
				cached = append(cached, ri)
				for _, h := range ri.Nodes {
					byHost[h] = ri
				}
			}
			sort.Slice(cached, func(i, j int) bool { return cached[i].ID < cached[j].ID })
			t.mu.Lock()
			t.regionsCache = cached
			t.regionByHost = byHost
			t.regionsCacheAt = time.Now()
			t.mu.Unlock()
		}
	}
	if hostname != "" {
		// Self-hosted relay: not in the public map; report it directly.
		var nodes []string
		for _, h := range strings.Split(hostname, ",") {
			if h = strings.TrimSpace(h); h != "" {
				nodes = append(nodes, h)
			}
		}
		return RegionInfo{ID: int(selfHostedRegionID), Code: "custom", Name: "Self-hosted relay", Nodes: nodes}, cached
	}
	var current RegionInfo
	if ci, err := tailcat.ParseAddr(t.server.TailcatAddr()); err == nil {
		for _, r := range ci.Region {
			for _, n := range r.Nodes {
				if ri, ok := byHost[n.HostName]; ok {
					current = ri
					break
				}
			}
			if current.ID != 0 {
				break
			}
		}
	}
	// Fall back to the explicitly configured region when the map is unavailable
	// (offline) or the address nodes aren't in the public map (self-hosted relay).
	if current.ID == 0 && relayRegion != 0 {
		current = RegionInfo{ID: relayRegion}
		for _, r := range cached {
			if r.ID == relayRegion {
				current = r
				break
			}
		}
	}
	return current, cached
}
