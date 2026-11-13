//go:build tailcat

package main

import (
	"context"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
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
// key. Dials block to their context deadline on offline peers, so Send uses a
// short timeout and mail's retryLoop provides the backoff cadence.

const tailcatKeyFile = "tailcat.key"

// tailcatPort is the TCP port AIMless speaks inside the tunnel.
const tailcatPort = 1

// tailcatDialTimeout bounds a dial to an offline peer (DERP is not a mailbox).
const tailcatDialTimeout = 2 * time.Second

// tailcatKeyset is the persisted identity: node key + pre-shared key + an
// optional fixed DERP region. The address is stable across restarts only when
// all three are restored together (the PSK is part of the address capability).
type tailcatKeyset struct {
	Key    string              `json:"key"`
	PSK    string              `json:"psk"`
	Region *tailcfg.DERPRegion `json:"region,omitempty"`
}

type TailcatNode struct {
	datadir string
	priv    key.NodePrivate
	psk     tailcat.PresharedKey
	server  *tailcat.Server
	logger  *aimlessLogger
	relay   RelayConfig

	mu        sync.Mutex
	addrs     map[string]string          // peer hex -> tailcat address (dial target)
	clients   map[string]*tailcat.Client // peer hex -> our outbound client
	conns     map[string]net.Conn        // peer hex -> live bidirectional connection
	dialMu    sync.Mutex                 // serializes check-and-dial
	hmu       sync.RWMutex
	onPacket  func(from PeerID, payload []byte)
	onPathUp  func(PeerID)
	closeOnce sync.Once
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
	if region == nil {
		if r := regionForRelay(relay); r != nil {
			region = r
		} else {
			region = ks.Region
		}
	} else {
		derpMapURL = "" // injected region (tests): no DERP map fetch
	}
	t := &TailcatNode{
		datadir: datadir,
		priv:    priv,
		psk:     psk,
		logger:  log,
		relay:   relay,
		addrs:   make(map[string]string),
		clients: make(map[string]*tailcat.Client),
		conns:   make(map[string]net.Conn),
	}
	if err := t.startServer(region, derpMapURL); err != nil {
		return nil, err
	}
	return t, nil
}

// startServer creates and starts the tailcat.Server with the given relay. The
// OnTCP handler closes over t, so it survives a server swap.
func (t *TailcatNode) startServer(region *tailcfg.DERPRegion, derpMapURL string) error {
	logf := func(format string, args ...any) { t.logger.Printf(format, args...) }
	t.server = &tailcat.Server{
		Key:          t.priv,
		PresharedKey: t.psk,
		Region:       region,
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

func (t *TailcatNode) Address() string { return string(t.server.TailcatAddr()) }

func (t *TailcatNode) Stop() {
	t.closeOnce.Do(func() {
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
	defer t.mu.Unlock()
	return t.addrs[peerHex]
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

func (t *TailcatNode) evictConn(peerHex string, c net.Conn) {
	t.mu.Lock()
	if c == nil || t.conns[peerHex] == c {
		delete(t.conns, peerHex)
	}
	cl := t.clients[peerHex]
	delete(t.clients, peerHex)
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

func (t *TailcatNode) getOrCreateClient(peerHex string) (*tailcat.Client, error) {
	t.mu.Lock()
	cl := t.clients[peerHex]
	t.mu.Unlock()
	if cl != nil {
		return cl, nil
	}
	addr := t.addrFor(peerHex)
	if addr == "" {
		return nil, fmt.Errorf("no tailcat address for peer %s", peerHex)
	}
	cl = &tailcat.Client{
		Server: tailcat.Addr(addr),
		Key:    t.priv,
		Logf:   func(format string, args ...any) { t.logger.Printf(format, args...) },
	}
	// Establish the meow handshake so the server adds us as a peer before dial.
	ctx, cancel := context.WithTimeout(context.Background(), tailcatDialTimeout)
	defer cancel()
	if _, err := cl.Ping(ctx); err != nil {
		cl.Close()
		return nil, fmt.Errorf("ping peer %s: %w", peerHex, err)
	}
	t.mu.Lock()
	t.clients[peerHex] = cl
	t.mu.Unlock()
	return cl, nil
}

// connFor returns a live connection to a peer, dialing if necessary.
func (t *TailcatNode) connFor(peerHex string) (net.Conn, error) {
	t.mu.Lock()
	c := t.conns[peerHex]
	t.mu.Unlock()
	if c != nil {
		return c, nil
	}
	t.dialMu.Lock()
	defer t.dialMu.Unlock()
	// Re-check after acquiring the dial lock.
	t.mu.Lock()
	c = t.conns[peerHex]
	t.mu.Unlock()
	if c != nil {
		return c, nil
	}
	cl, err := t.getOrCreateClient(peerHex)
	if err != nil {
		return nil, err
	}
	ctx, cancel := context.WithTimeout(context.Background(), tailcatDialTimeout)
	defer cancel()
	c, err = cl.DialTCPPort(ctx, tailcatPort)
	if err != nil {
		t.evictConn(peerHex, nil)
		return nil, fmt.Errorf("dial peer %s: %w", peerHex, err)
	}
	t.registerConn(peerHex, c)
	go t.readLoop(peerHex, c)
	return c, nil
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
	return []peerStatus{{URI: t.relayLabel(), Up: true}}
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
	if rc.Hostname == "" && rc.DERPMapURL == "" {
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
	reg := &tailcfg.DERPRegion{}
	for _, host := range strings.Split(cfg.Hostname, ",") {
		if host = strings.TrimSpace(host); host != "" {
			reg.Nodes = append(reg.Nodes, &tailcfg.DERPNode{HostName: host})
		}
	}
	if len(reg.Nodes) == 0 {
		return nil
	}
	return reg
}
