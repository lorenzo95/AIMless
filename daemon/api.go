package main

import (
	"bufio"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log"
	"net"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/yggdrasil-network/yggdrasil-go/src/core"
)

const buildVersion = "aimlessd/0.9.11"

// protocolVersion identifies the wire format spoken to other daemons. Peers on a
// different version are ignored rather than misparsed; there is no compat layer.
const protocolVersion = 2

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

type historyMsg struct {
	Seq     uint64 `json:"seq"`
	Ts      int64  `json:"ts"`
	Payload string `json:"payload"`
}

type apiMessage struct {
	Op       string       `json:"op"`
	Id       string       `json:"id,omitempty"`
	Address  string       `json:"address,omitempty"`
	Key      string       `json:"key,omitempty"`
	Pid      int          `json:"pid,omitempty"`
	To       string       `json:"to,omitempty"`
	From     string       `json:"from,omitempty"`
	Payload  string       `json:"payload,omitempty"`
	Bytes    int          `json:"bytes,omitempty"`
	Seq      uint64       `json:"seq,omitempty"`
	Ts       int64        `json:"ts,omitempty"`
	Error    string       `json:"error,omitempty"`
	Peers    []peerStatus `json:"peers,omitempty"`
	PeerList []string     `json:"peer_list,omitempty"`
	// DefaultPeers is the built-in list, so the client can offer to restore
	// it when the configured one is empty or broken.
	DefaultPeers []string          `json:"default_peers,omitempty"`
	Added        []string          `json:"added,omitempty"`
	Removed      []string          `json:"removed,omitempty"`
	Failed       map[string]string `json:"failed,omitempty"`
	MTU          int               `json:"mtu,omitempty"`
	Build        string            `json:"build,omitempty"`
	Msgs         []historyMsg      `json:"msgs,omitempty"`
	Oldest       uint64            `json:"oldest,omitempty"`
	Latest       uint64            `json:"latest,omitempty"`
	Presence     []presenceEntry   `json:"presence,omitempty"`
	Blocked      []string          `json:"blocked,omitempty"`
	Type         string            `json:"type,omitempty"`
	Transfers    []AttachPending   `json:"transfers,omitempty"`
	Chunks       []attachEntry     `json:"chunks,omitempty"`
	Tid          string            `json:"tid,omitempty"`
}

// apiConn is one client connection.
//
// Writes go through its own mutex so that two broadcasts can never interleave
// on the same connection (the client reads line-delimited JSON), and so a
// client that stopped reading cannot block the other clients: the write has a
// deadline, and the server lock is not held while writing.
type apiConn struct {
	net.Conn
	wmu sync.Mutex
}

// write sends one line to the client.
func (c *apiConn) write(data []byte) {
	c.wmu.Lock()
	defer c.wmu.Unlock()
	_ = c.SetWriteDeadline(time.Now().Add(5 * time.Second))
	_, _ = c.Write(data)
}

type APIServer struct {
	node     *Node
	mail     *Mail
	presence *Presence
	ln       net.Listener
	mu       sync.Mutex
	conns    map[*apiConn]struct{}
	closed   bool
	// configPath is where the peer list is persisted, so an edit made from
	// the client survives a restart of the daemon.
	configPath string
}

func NewAPIServer(node *Node, mail *Mail, presence *Presence, socketPath, configPath string) (*APIServer, error) {
	if err := os.Remove(socketPath); err != nil && !os.IsNotExist(err) {
		return nil, fmt.Errorf("remove stale socket: %w", err)
	}
	ln, err := net.Listen("unix", socketPath)
	if err != nil {
		return nil, fmt.Errorf("listen on %s: %w", socketPath, err)
	}
	if err := os.Chmod(socketPath, 0o600); err != nil {
		ln.Close()
		return nil, fmt.Errorf("chmod socket: %w", err)
	}
	s := &APIServer{node: node, mail: mail, presence: presence, ln: ln,
		conns: make(map[*apiConn]struct{}), configPath: configPath}
	go s.acceptLoop()
	return s, nil
}

func (s *APIServer) acceptLoop() {
	for {
		conn, err := s.ln.Accept()
		if err != nil {
			return
		}
		s.mu.Lock()
		if s.closed {
			conn.Close()
			s.mu.Unlock()
			return
		}
		client := &apiConn{Conn: conn}
		s.conns[client] = struct{}{}
		s.mu.Unlock()
		go s.handleConn(client)
	}
}

func (s *APIServer) handleConn(conn *apiConn) {
	defer func() {
		if r := recover(); r != nil {
			log.Printf("recovered in connection handler: %v", r)
		}
		s.mu.Lock()
		delete(s.conns, conn)
		s.mu.Unlock()
		conn.Close()
	}()
	scanner := bufio.NewScanner(conn)
	scanner.Buffer(make([]byte, 64*1024), 1024*1024)
	for scanner.Scan() {
		line := scanner.Bytes()
		if len(line) == 0 {
			continue
		}
		var req apiMessage
		if err := json.Unmarshal(line, &req); err != nil {
			s.replyReq(conn, req, apiMessage{Op: "error", Error: "bad json: " + err.Error()})
			continue
		}
		s.dispatch(conn, req)
	}
}

func (s *APIServer) dispatch(conn *apiConn, req apiMessage) {
	// Any client request counts as a heartbeat: presence only advertises a
	// client while one has been seen recently.
	if s.presence != nil {
		s.presence.TouchClient()
	}
	switch req.Op {
	case "whoami":
		s.replyReq(conn, req, apiMessage{Op: "whoami", Address: s.node.Address.String(), Key: hex.EncodeToString(s.node.Pub), Pid: os.Getpid()})
	case "status":
		// GetPeers reads the peer's RTT sample without holding the lock the
		// peer actor writes it with, so `go test -race` can flag a race inside
		// ironwood when a peer completes its handshake at the same moment.
		// It is upstream (ironwood network/peers.go), harmless in practice -
		// the sample is only used for the latency readout.
		peers := s.node.Core.GetPeers()
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
		// Peers we refused (a hostname that does not resolve) are peers too:
		// the client has to show them, or the list silently shrinks.
		_, refused := s.node.PeerState()
		for uri, reason := range refused {
			out = append(out, peerStatus{URI: uri, Error: reason})
		}
		s.replyReq(conn, req, apiMessage{
			Op:           "status",
			Address:      s.node.Address.String(),
			Key:          hex.EncodeToString(s.node.Pub),
			Peers:        out,
			PeerList:     s.node.ConfiguredPeers(),
			DefaultPeers: defaultPeers,
			MTU:          int(s.node.Core.MTU()),
			Build:        buildVersion,
		})
	case "send":
		s.handleSend(conn, req)
	case "sendfile":
		s.handleSend(conn, req, TypeFile)
	case "history":
		s.handleHistory(conn, req)
	case "pendingattachments":
		s.handlePendingAttachments(conn, req)
	case "fetchattachment":
		s.handleFetchAttachment(conn, req)
	case "ackfile":
		s.handleAckFile(conn, req)
	case "presence":
		s.replyReq(conn, req, apiMessage{Op: "presence", Presence: s.presence.Snapshot()})
	case "watch":
		s.handleWatch(conn, req)
	case "setstatus":
		s.handleSetStatus(conn, req)
	case "block":
		s.handleBlock(conn, req, true)
	case "unblock":
		s.handleBlock(conn, req, false)
	case "setpeers":
		s.handleSetPeers(conn, req)
	case "blocklist":
		s.replyReq(conn, req, apiMessage{Op: "blocklist", Blocked: s.mail.Blocked()})
	case "":
		s.replyReq(conn, req, apiMessage{Op: "error", Error: "missing op"})
	default:
		s.replyReq(conn, req, apiMessage{Op: "error", Error: "unknown op: " + req.Op})
	}
}

func (s *APIServer) handleSend(conn *apiConn, req apiMessage, typ ...EnvelopeType) {
	keyBytes, err := hex.DecodeString(req.To)
	if err != nil {
		s.replyReq(conn, req, apiMessage{Op: "error", Error: "bad key hex: " + err.Error()})
		return
	}
	if len(keyBytes) != ed25519.PublicKeySize {
		s.replyReq(conn, req, apiMessage{Op: "error", Error: fmt.Sprintf("key must be %d hex chars", 2*ed25519.PublicKeySize)})
		return
	}
	payload, err := base64.StdEncoding.DecodeString(req.Payload)
	if err != nil {
		s.replyReq(conn, req, apiMessage{Op: "error", Error: "bad payload base64: " + err.Error()})
		return
	}
	var seq uint64
	if len(typ) > 0 && typ[0] == TypeFile {
		seq, err = s.mail.SendFile(ed25519.PublicKey(keyBytes), payload)
	} else {
		seq, err = s.mail.SendMsg(ed25519.PublicKey(keyBytes), payload)
	}
	if err != nil {
		s.replyReq(conn, req, apiMessage{Op: "error", Error: "send failed: " + err.Error()})
		return
	}
	s.replyReq(conn, req, apiMessage{Op: "queued", To: req.To, Seq: seq, Bytes: len(payload)})
}

func (s *APIServer) handlePendingAttachments(conn *apiConn, req apiMessage) {
	keyBytes, err := hex.DecodeString(req.From)
	if err != nil || len(keyBytes) != ed25519.PublicKeySize {
		s.replyReq(conn, req, apiMessage{Op: "error", Error: "bad from key"})
		return
	}
	transfers, err := s.mail.PendingAttachments(hex.EncodeToString(keyBytes))
	if err != nil {
		s.replyReq(conn, req, apiMessage{Op: "error", Error: "pending failed: " + err.Error()})
		return
	}
	s.replyReq(conn, req, apiMessage{Op: "pendingattachments", From: req.From, Transfers: transfers})
}

func (s *APIServer) handleFetchAttachment(conn *apiConn, req apiMessage) {
	keyBytes, err := hex.DecodeString(req.From)
	if err != nil || len(keyBytes) != ed25519.PublicKeySize {
		s.replyReq(conn, req, apiMessage{Op: "error", Error: "bad from key"})
		return
	}
	if req.Tid == "" {
		s.replyReq(conn, req, apiMessage{Op: "error", Error: "missing tid"})
		return
	}
	chunks, err := s.mail.FetchAttachment(hex.EncodeToString(keyBytes), req.Tid)
	if err != nil {
		s.replyReq(conn, req, apiMessage{Op: "error", Error: "fetch failed: " + err.Error()})
		return
	}
	s.replyReq(conn, req, apiMessage{Op: "fetchattachment", From: req.From, Tid: req.Tid, Chunks: chunks})
}

func (s *APIServer) handleAckFile(conn *apiConn, req apiMessage) {
	keyBytes, err := hex.DecodeString(req.From)
	if err != nil || len(keyBytes) != ed25519.PublicKeySize {
		s.replyReq(conn, req, apiMessage{Op: "error", Error: "bad from key"})
		return
	}
	if req.Tid == "" {
		s.replyReq(conn, req, apiMessage{Op: "error", Error: "missing tid"})
		return
	}
	if err := s.mail.AckAttachment(hex.EncodeToString(keyBytes), req.Tid); err != nil {
		s.replyReq(conn, req, apiMessage{Op: "error", Error: "ack failed: " + err.Error()})
		return
	}
	s.replyReq(conn, req, apiMessage{Op: "ackfile", From: req.From, Tid: req.Tid})
}

func (s *APIServer) handleHistory(conn *apiConn, req apiMessage) {
	keyBytes, err := hex.DecodeString(req.From)
	if err != nil {
		s.replyReq(conn, req, apiMessage{Op: "error", Error: "bad key hex: " + err.Error()})
		return
	}
	if len(keyBytes) != ed25519.PublicKeySize {
		s.replyReq(conn, req, apiMessage{Op: "error", Error: fmt.Sprintf("key must be %d hex chars", 2*ed25519.PublicKeySize)})
		return
	}
	msgs, oldest, latest, err := s.mail.History(hex.EncodeToString(keyBytes), req.Seq)
	if err != nil {
		s.replyReq(conn, req, apiMessage{Op: "error", Error: "history failed: " + err.Error()})
		return
	}
	out := make([]historyMsg, 0, len(msgs))
	for _, e := range msgs {
		out = append(out, historyMsg{Seq: e.Seq, Ts: e.Ts, Payload: e.Payload})
	}
	s.replyReq(conn, req, apiMessage{Op: "history", From: req.From, Msgs: out, Oldest: oldest, Latest: latest})
}

func (s *APIServer) handleWatch(conn *apiConn, req apiMessage) {
	keyBytes, err := hex.DecodeString(req.To)
	if err != nil {
		s.replyReq(conn, req, apiMessage{Op: "error", Error: "bad key hex: " + err.Error()})
		return
	}
	if len(keyBytes) != ed25519.PublicKeySize {
		s.replyReq(conn, req, apiMessage{Op: "error", Error: fmt.Sprintf("key must be %d hex chars", 2*ed25519.PublicKeySize)})
		return
	}
	if err := s.mail.Watch(ed25519.PublicKey(keyBytes)); err != nil {
		s.replyReq(conn, req, apiMessage{Op: "error", Error: "watch failed: " + err.Error()})
		return
	}
	s.replyReq(conn, req, apiMessage{Op: "watching", To: req.To})
}

func (s *APIServer) handleSetStatus(conn *apiConn, req apiMessage) {
	keyBytes, err := hex.DecodeString(req.To)
	if err != nil {
		s.replyReq(conn, req, apiMessage{Op: "error", Error: "bad key hex: " + err.Error()})
		return
	}
	if len(keyBytes) != ed25519.PublicKeySize {
		s.replyReq(conn, req, apiMessage{Op: "error", Error: fmt.Sprintf("key must be %d hex chars", 2*ed25519.PublicKeySize)})
		return
	}
	if s.mail.IsBlocked(ed25519.PublicKey(keyBytes)) {
		s.replyReq(conn, req, apiMessage{Op: "error", Error: "peer is blocked"})
		return
	}
	payload, err := base64.StdEncoding.DecodeString(req.Payload)
	if err != nil {
		s.replyReq(conn, req, apiMessage{Op: "error", Error: "bad payload base64: " + err.Error()})
		return
	}
	seq, err := s.presence.SetStatus(ed25519.PublicKey(keyBytes), payload)
	if err != nil {
		s.replyReq(conn, req, apiMessage{Op: "error", Error: "setstatus failed: " + err.Error()})
		return
	}
	s.replyReq(conn, req, apiMessage{Op: "statusset", To: req.To, Seq: seq})
}

// handleSetPeers replaces the peer list at runtime.
//
// The list is validated first (a bad URI is refused, not dialled), written to
// config.json so it survives a restart, and only then applied - so the file and
// the running daemon never disagree.
func (s *APIServer) handleSetPeers(conn *apiConn, req apiMessage) {
	peers := make([]string, 0, len(req.PeerList))
	for _, uri := range req.PeerList {
		uri = strings.TrimSpace(uri)
		if uri == "" {
			continue
		}
		if err := validatePeerURI(uri); err != nil {
			s.replyReq(conn, req, apiMessage{Op: "error", Error: err.Error()})
			return
		}
		peers = append(peers, uri)
	}
	if err := s.savePeers(peers); err != nil {
		s.replyReq(conn, req, apiMessage{Op: "error", Error: err.Error()})
		return
	}
	added, removed, failed := s.node.SetPeers(s.node.Core, peers)
	s.replyReq(conn, req, apiMessage{
		Op:      "peers",
		Peers:   s.peerStatuses(),
		Added:   added,
		Removed: removed,
		Failed:  failed,
	})
}

// validatePeerURI checks a peer URI before it is dialled or stored.
//
// The scheme has to be one Yggdrasil speaks, and the host has to look like a
// host: a URI with a shell metacharacter or a path in it never reaches the
// network, it just becomes a way to confuse the config file.
func validatePeerURI(uri string) error {
	u, err := url.Parse(uri)
	if err != nil {
		return fmt.Errorf("bad peer %q: %v", uri, err)
	}
	switch u.Scheme {
	case "tcp", "tls", "quic", "ws", "wss", "unix":
	default:
		return fmt.Errorf("bad peer %q: unknown scheme %q", uri, u.Scheme)
	}
	if u.Scheme == "unix" {
		if u.Path == "" {
			return fmt.Errorf("bad peer %q: no socket path", uri)
		}
		return nil
	}
	if u.Hostname() == "" {
		return fmt.Errorf("bad peer %q: no host", uri)
	}
	if p := u.Port(); p != "" {
		n, err := strconv.Atoi(p)
		if err != nil || n < 1 || n > 65535 {
			return fmt.Errorf("bad peer %q: bad port %q", uri, p)
		}
	} else {
		return fmt.Errorf("bad peer %q: no port", uri)
	}
	if u.Path != "" && u.Path != "/" {
		return fmt.Errorf("bad peer %q: a peer URI cannot have a path", uri)
	}
	if strings.Contains(uri, "..") {
		return fmt.Errorf("bad peer %q: a peer URI cannot contain ..", uri)
	}
	return nil
}

// lastPeerError is why a peer is not up, if we know.
func lastPeerError(p core.PeerInfo) string {
	if p.LastError != nil {
		return p.LastError.Error()
	}
	return ""
}

// savePeers writes the peer list to the daemon's config.json.
//
// The file is the source of truth: the daemon reads it at startup, so what the
// client sees in the peer manager is what the daemon will use next time.
func (s *APIServer) savePeers(peers []string) error {
	if s.configPath == "" {
		return fmt.Errorf("this daemon has no config file to write")
	}
	cfg, err := loadConfig(s.configPath)
	if err != nil {
		return err
	}
	// An empty list must stay an empty list: "peers": null would read back
	// as "not set" and fall through to the built-in peers.
	list := make([]string, 0, len(peers))
	list = append(list, peers...)
	cfg.Peers = &list
	return saveConfig(s.configPath, cfg)
}

// peerStatuses is the status op's peer list.
func (s *APIServer) peerStatuses() []peerStatus {
	peers := s.node.Core.GetPeers()
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
	_, refused := s.node.PeerState()
	for uri, reason := range refused {
		out = append(out, peerStatus{URI: uri, Error: reason})
	}
	return out
}

func (s *APIServer) handleBlock(conn *apiConn, req apiMessage, block bool) {
	keyBytes, err := hex.DecodeString(req.To)
	if err != nil {
		s.replyReq(conn, req, apiMessage{Op: "error", Error: "bad key hex: " + err.Error()})
		return
	}
	if len(keyBytes) != ed25519.PublicKeySize {
		s.replyReq(conn, req, apiMessage{Op: "error", Error: fmt.Sprintf("key must be %d hex chars", 2*ed25519.PublicKeySize)})
		return
	}
	pub := ed25519.PublicKey(keyBytes)
	if block {
		if err := s.mail.Block(pub); err != nil {
			s.replyReq(conn, req, apiMessage{Op: "error", Error: "block failed: " + err.Error()})
			return
		}
	} else {
		if err := s.mail.Unblock(pub); err != nil {
			s.replyReq(conn, req, apiMessage{Op: "error", Error: "unblock failed: " + err.Error()})
			return
		}
	}
	if block {
		s.replyReq(conn, req, apiMessage{Op: "blocked", To: req.To})
	} else {
		s.replyReq(conn, req, apiMessage{Op: "unblocked", To: req.To})
	}
}

func (s *APIServer) reply(conn *apiConn, msg apiMessage) {
	data, err := json.Marshal(msg)
	if err != nil {
		return
	}
	conn.write(append(data, '\n'))
}

// replyReq echoes the request's correlation id back on the reply (and on error
// replies) so a multiplexing client can match the response to its request.
// Events (recv/acked) are broadcast separately and never carry ids.
func (s *APIServer) replyReq(conn *apiConn, req apiMessage, msg apiMessage) {
	msg.Id = req.Id
	s.reply(conn, msg)
}

func (s *APIServer) DeliverMsg(from ed25519.PublicKey, seq uint64, ts int64, payload []byte) {
	s.broadcast(apiMessage{
		Op:      "recv",
		From:    hex.EncodeToString(from),
		Seq:     seq,
		Ts:      ts,
		Payload: base64.StdEncoding.EncodeToString(payload),
	})
}

func (s *APIServer) DeliverFileMsg(from ed25519.PublicKey, seq uint64, ts int64, payload []byte) {
	s.broadcast(apiMessage{
		Op:      "recv",
		Type:    "file",
		From:    hex.EncodeToString(from),
		Seq:     seq,
		Ts:      ts,
		Payload: base64.StdEncoding.EncodeToString(payload),
	})
}

func (s *APIServer) Acked(to ed25519.PublicKey, seq uint64) {
	s.broadcast(apiMessage{
		Op:  "acked",
		To:  hex.EncodeToString(to),
		Seq: seq,
	})
}

func (s *APIServer) broadcast(msg apiMessage) {
	data, err := json.Marshal(msg)
	if err != nil {
		return
	}
	data = append(data, '\n')
	// Snapshot the connections under the lock, then write outside it: a
	// client that stopped reading must not block every other client (and
	// must not hold the server lock while it blocks).
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return
	}
	conns := make([]*apiConn, 0, len(s.conns))
	for conn := range s.conns {
		conns = append(conns, conn)
	}
	s.mu.Unlock()
	for _, conn := range conns {
		conn.write(data)
	}
}

func (s *APIServer) Close() error {
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return nil
	}
	s.closed = true
	conns := make([]*apiConn, 0, len(s.conns))
	for conn := range s.conns {
		conns = append(conns, conn)
	}
	s.conns = make(map[*apiConn]struct{})
	s.mu.Unlock()
	for _, conn := range conns {
		conn.Close()
	}
	return s.ln.Close()
}
