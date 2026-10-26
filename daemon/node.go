package main

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/hex"
	"errors"
	"fmt"
	"math/big"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/Arceliar/ironwood/types"
	"github.com/yggdrasil-network/yggdrasil-go/src/core"
)

const keyFileName = "node.key"

type Node struct {
	Core    *core.Core
	Pub     ed25519.PublicKey
	Address net.IP
	// Handlers are installed after the node is running, so they are read
	// under a lock - a packet arriving mid-install must not race with it.
	hmu      sync.RWMutex
	onPacket func(from ed25519.PublicKey, payload []byte)
	onPathUp func(key ed25519.PublicKey)

	priv      ed25519.PrivateKey
	listeners []*core.Listener
	closeOnce sync.Once
	logger    *aimlessLogger

	// The peers we actually dialled, and the ones we refused (a hostname that
	// does not resolve, a URI we cannot parse). The client shows both, so a
	// parked DNS record is visible instead of a mysterious missing peer.
	peerMu     sync.RWMutex
	peerURIs   []string
	peerErrors map[string]string
	control    chan outbound // small, latency-sensitive: ACKs, status, probes, text
	bulk       chan outbound // large: TypeFile chunks
	writeFn    func(pub ed25519.PublicKey, payload []byte)
}

type outbound struct {
	pub  ed25519.PublicKey
	data []byte
}

func LoadOrCreateKey(datadir string) (ed25519.PrivateKey, error) {
	if err := os.MkdirAll(datadir, 0o700); err != nil {
		return nil, fmt.Errorf("create datadir: %w", err)
	}
	path := filepath.Join(datadir, keyFileName)
	data, err := os.ReadFile(path)
	if err == nil {
		seed, decErr := hex.DecodeString(strings.TrimSpace(string(data)))
		if decErr != nil {
			return nil, fmt.Errorf("decode key file: %w", decErr)
		}
		if len(seed) != ed25519.SeedSize {
			return nil, fmt.Errorf("key file has wrong size: %d", len(seed))
		}
		return ed25519.NewKeyFromSeed(seed), nil
	}
	if !errors.Is(err, os.ErrNotExist) {
		return nil, fmt.Errorf("read key file: %w", err)
	}
	_, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return nil, fmt.Errorf("generate key: %w", err)
	}
	if err := os.WriteFile(path, []byte(hex.EncodeToString(priv.Seed())+"\n"), 0o600); err != nil {
		return nil, fmt.Errorf("write key file: %w", err)
	}
	return priv, nil
}

func selfSignedCert(priv ed25519.PrivateKey) (*tls.Certificate, error) {
	pub, ok := priv.Public().(ed25519.PublicKey)
	if !ok {
		return nil, errors.New("not an ed25519 key")
	}
	template := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: hex.EncodeToString(pub)},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().AddDate(100, 0, 0),
		KeyUsage:              x509.KeyUsageDigitalSignature | x509.KeyUsageCertSign,
		ExtKeyUsage:           []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth, x509.ExtKeyUsageClientAuth},
		BasicConstraintsValid: true,
		IsCA:                  true,
	}
	der, err := x509.CreateCertificate(rand.Reader, template, template, pub, priv)
	if err != nil {
		return nil, fmt.Errorf("create certificate: %w", err)
	}
	return &tls.Certificate{Certificate: [][]byte{der}, PrivateKey: priv}, nil
}

func StartNode(datadir string, peers []string, listenURLs []string, log *aimlessLogger) (*Node, error) {
	priv, err := LoadOrCreateKey(datadir)
	if err != nil {
		return nil, err
	}
	cert, err := selfSignedCert(priv)
	if err != nil {
		return nil, err
	}
	c, err := core.New(cert, log)
	if err != nil {
		return nil, fmt.Errorf("start yggdrasil core: %w", err)
	}
	n := &Node{
		Core:    c,
		logger:  log,
		priv:    priv,
		Pub:     priv.Public().(ed25519.PublicKey),
		control: make(chan outbound, 64),
		bulk:    make(chan outbound, 512),
	}
	n.Address = c.Address()
	n.writeFn = func(pub ed25519.PublicKey, payload []byte) {
		_, _ = c.WriteTo(payload, types.Addr(pub))
	}
	go n.writeLoop()
	c.SetPathNotify(func(key ed25519.PublicKey) {
		if h := n.pathUpHandler(); h != nil {
			h(key)
		}
	})

	for _, raw := range listenURLs {
		u, err := url.Parse(raw)
		if err != nil {
			n.Stop()
			return nil, fmt.Errorf("parse listen url %q: %w", raw, err)
		}
		l, err := c.Listen(u, "")
		if err != nil {
			n.Stop()
			return nil, fmt.Errorf("listen on %q: %w", raw, err)
		}
		n.listeners = append(n.listeners, l)
	}

	for _, raw := range peers {
		if err := n.addPeer(c, raw); err != nil {
			// One unusable peer must not take the whole daemon down: the
			// node still works with the peers that are left.
			n.logger.Printf("skipping peer %q: %v", raw, err)
			n.notePeerError(raw, err.Error())
			continue
		}
		n.notePeer(raw)
	}

	go n.readLoop()
	return n, nil
}

// addPeer parses and dials one peer.
//
// A hostname that does not resolve is refused up front: a parked DNS record
// resolves to 0.0.0.0 and the link then retries forever against nothing, which
// is exactly how a node ends up on an island with no clue why.
func (n *Node) addPeer(c *core.Core, raw string) error {
	u, err := url.Parse(raw)
	if err != nil {
		return fmt.Errorf("parse peer url: %w", err)
	}
	if u.Scheme == "" || u.Host == "" {
		return fmt.Errorf("peer url needs a scheme and a host")
	}
	if host := u.Hostname(); host != "" && net.ParseIP(host) == nil {
		if _, err := net.LookupHost(host); err != nil {
			return fmt.Errorf("cannot resolve %s: %w", host, err)
		}
	}
	return c.AddPeer(u, "")
}

func (n *Node) notePeer(uri string) {
	n.peerMu.Lock()
	defer n.peerMu.Unlock()
	n.peerURIs = append(n.peerURIs, uri)
}

func (n *Node) notePeerError(uri, reason string) {
	n.peerMu.Lock()
	defer n.peerMu.Unlock()
	if n.peerErrors == nil {
		n.peerErrors = map[string]string{}
	}
	n.peerErrors[uri] = reason
}

// PeerState returns the peers we dialled and the ones we refused.
func (n *Node) PeerState() (dialed []string, refused map[string]string) {
	n.peerMu.RLock()
	defer n.peerMu.RUnlock()
	dialed = append(dialed, n.peerURIs...)
	refused = map[string]string{}
	for k, v := range n.peerErrors {
		refused[k] = v
	}
	return dialed, refused
}

// ConfiguredPeers is the peer list as configured, in order: the ones we
// dialled, then the ones we refused. The client edits this list, so it has to
// be the exact strings that were stored, not the library's normalised form.
func (n *Node) ConfiguredPeers() []string {
	n.peerMu.RLock()
	defer n.peerMu.RUnlock()
	out := append([]string(nil), n.peerURIs...)
	for uri := range n.peerErrors {
		out = append(out, uri)
	}
	return out
}

// SetPeers replaces the peer list at runtime: peers that are gone are
// dropped, new ones are dialled. It is what the client's peer manager uses.
func (n *Node) SetPeers(c *core.Core, peers []string) (added, removed []string, failed map[string]string) {
	n.peerMu.Lock()
	old := append([]string(nil), n.peerURIs...)
	n.peerURIs = nil
	n.peerErrors = map[string]string{}
	n.peerMu.Unlock()

	wanted := map[string]bool{}
	for _, raw := range peers {
		wanted[raw] = true
	}
	// Peers we already have stay as they are: only the ones that are gone
	// are dropped, and only the ones that are new are dialled.
	kept := map[string]bool{}
	for _, raw := range old {
		if wanted[raw] {
			kept[raw] = true
			continue
		}
		u, err := url.Parse(raw)
		if err != nil {
			continue
		}
		if err := c.RemovePeer(u, ""); err != nil {
			n.logger.Printf("could not remove peer %q: %v", raw, err)
			continue
		}
		removed = append(removed, raw)
	}
	failed = map[string]string{}
	for _, raw := range peers {
		if kept[raw] {
			// Keep it in the list, it is already dialled.
			n.notePeer(raw)
			continue
		}
		if err := n.addPeer(c, raw); err != nil {
			n.logger.Printf("skipping peer %q: %v", raw, err)
			n.notePeerError(raw, err.Error())
			failed[raw] = err.Error()
			continue
		}
		n.notePeer(raw)
		added = append(added, raw)
	}
	return added, removed, failed
}

// SetHandlers installs the packet and path-up handlers.
func (n *Node) SetHandlers(onPacket func(from ed25519.PublicKey, payload []byte), onPathUp func(key ed25519.PublicKey)) {
	n.hmu.Lock()
	n.onPacket = onPacket
	n.onPathUp = onPathUp
	n.hmu.Unlock()
}

func (n *Node) packetHandler() func(from ed25519.PublicKey, payload []byte) {
	n.hmu.RLock()
	defer n.hmu.RUnlock()
	return n.onPacket
}

func (n *Node) pathUpHandler() func(key ed25519.PublicKey) {
	n.hmu.RLock()
	defer n.hmu.RUnlock()
	return n.onPathUp
}

func (n *Node) readLoop() {
	mtu := int(n.Core.MTU())
	buf := make([]byte, mtu)
	for {
		nRead, from, err := n.Core.ReadFrom(buf)
		if err != nil {
			return
		}
		handler := n.packetHandler()
		if handler == nil || nRead == 0 {
			continue
		}
		fromAddr, ok := from.(types.Addr)
		if !ok {
			continue
		}
		payload := make([]byte, nRead)
		copy(payload, buf[:nRead])
		sender := make(ed25519.PublicKey, len(fromAddr))
		copy(sender, fromAddr)
		handler(sender, payload)
	}
}

// Send enqueues a latency-sensitive packet (text, ACK, status, probe) on the
// control queue.
func (n *Node) Send(pub ed25519.PublicKey, payload []byte) (int, error) {
	return n.enqueue(pub, payload, n.control)
}

// SendBulk enqueues a TypeFile chunk on the bulk queue - file traffic yields to
// control traffic and is paced by the link through the single writer.
func (n *Node) SendBulk(pub ed25519.PublicKey, payload []byte) (int, error) {
	return n.enqueue(pub, payload, n.bulk)
}

func (n *Node) enqueue(pub ed25519.PublicKey, payload []byte, q chan outbound) (int, error) {
	if len(pub) != ed25519.PublicKeySize {
		return 0, fmt.Errorf("bad public key size: %d", len(pub))
	}
	// Core is always set by StartNode in production; the nil check exists so the
	// priority-ordering test can construct a bare Node with a stub writer.
	if n.Core != nil && len(payload) > int(n.Core.MTU()) {
		return 0, fmt.Errorf("payload %d exceeds mtu %d", len(payload), n.Core.MTU())
	}
	select {
	case q <- outbound{pub: pub, data: payload}:
		return len(payload), nil
	default:
		return 0, fmt.Errorf("outbound queue full")
	}
}

func (n *Node) writeLoop() {
	for {
		// Drain latency-sensitive control traffic before touching bulk.
		select {
		case out := <-n.control:
			n.writeFn(out.pub, out.data)
			continue
		default:
		}
		select {
		case out := <-n.control:
			n.writeFn(out.pub, out.data)
		case out := <-n.bulk:
			n.writeFn(out.pub, out.data)
		}
	}
}

func (n *Node) Stop() {
	n.closeOnce.Do(func() {
		n.Core.Stop()
	})
}
