package main

import (
	"crypto/ed25519"
	"encoding/base64"
	"encoding/hex"
	"sort"
	"sync"
	"time"
)

type statusOut struct {
	seq     uint64
	payload []byte
}

type statusIn struct {
	seq      uint64
	ts       int64
	payload  []byte
	attached bool
}

type peerPresence struct {
	lastSeen time.Time
	in       *statusIn
	out      *statusOut
}

type Presence struct {
	mu            sync.Mutex
	mail          *Mail
	probeInterval time.Duration
	onlineWindow  time.Duration
	clientIdle    time.Duration
	lastClient    time.Time
	peers         map[string]*peerPresence
}

func NewPresence(mail *Mail, probeInterval time.Duration) *Presence {
	p := &Presence{
		mail:          mail,
		probeInterval: probeInterval,
		onlineWindow:  3 * probeInterval,
		clientIdle:    30 * time.Second,
		peers:         make(map[string]*peerPresence),
	}
	mail.SetPresence(p)
	return p
}

// TouchClient records client activity (any local API request). Presence only
// advertises a client while one has been seen recently, so an always-on daemon
// with no client attached looks offline/away to buddies instead of "available".
func (p *Presence) TouchClient() {
	p.mu.Lock()
	p.lastClient = time.Now()
	p.mu.Unlock()
}

func (p *Presence) clientPresent() bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	return time.Since(p.lastClient) < p.clientIdle
}

func (p *Presence) Start() {
	go p.loop()
}

func (p *Presence) loop() {
	ticker := time.NewTicker(p.probeInterval)
	defer ticker.Stop()
	for range ticker.C {
		p.tick()
	}
}

func (p *Presence) keyFor(pub ed25519.PublicKey) *peerPresence {
	peerHex := hex.EncodeToString(pub)
	pp, ok := p.peers[peerHex]
	if !ok {
		pp = &peerPresence{}
		p.peers[peerHex] = pp
	}
	return pp
}

func (p *Presence) Watch(pub ed25519.PublicKey) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.keyFor(pub)
}

func (p *Presence) Touch(pub ed25519.PublicKey) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.keyFor(pub).lastSeen = time.Now()
}

func (p *Presence) OnProbe(pub ed25519.PublicKey, seq uint64) {
	p.Touch(pub)
}

func (p *Presence) OnStatus(pub ed25519.PublicKey, seq uint64, ts int64, payload []byte) {
	p.mu.Lock()
	defer p.mu.Unlock()
	pp := p.keyFor(pub)
	pp.lastSeen = time.Now()
	// An empty payload is the "no client attached" marker; a real status means
	// a client is present. Always take the latest.
	pp.in = &statusIn{seq: seq, ts: ts, payload: payload, attached: len(payload) > 0}
}

func (p *Presence) SetStatus(pub ed25519.PublicKey, payload []byte) (uint64, error) {
	p.mu.Lock()
	pp := p.keyFor(pub)
	if pp.out == nil {
		pp.out = &statusOut{}
	}
	pp.out.seq++
	seq := pp.out.seq
	pp.out.payload = payload
	p.mu.Unlock()
	err := p.mail.SendStatus(pub, seq, payload)
	return seq, err
}

func (p *Presence) PathUp(pub ed25519.PublicKey) {
	p.probeOne(pub)
}

func (p *Presence) tick() {
	p.mu.Lock()
	targets := make([]ed25519.PublicKey, 0, len(p.peers))
	for peerHex := range p.peers {
		if keyBytes, err := hex.DecodeString(peerHex); err == nil && len(keyBytes) == ed25519.PublicKeySize {
			targets = append(targets, ed25519.PublicKey(keyBytes))
		}
	}
	p.mu.Unlock()
	for _, pub := range p.mail.Buddies() {
		peerHex := hex.EncodeToString(pub)
		p.mu.Lock()
		_, known := p.peers[peerHex]
		p.mu.Unlock()
		if !known {
			targets = append(targets, pub)
		}
	}
	for _, pub := range targets {
		p.probeOne(pub)
	}
}

func (p *Presence) probeOne(pub ed25519.PublicKey) {
	if p.mail.IsBlocked(pub) {
		return
	}
	_ = p.mail.SendProbe(pub)
	seq, payload, send := p.statusFor(pub)
	if send {
		_ = p.mail.SendStatus(pub, seq, payload)
	}
}

// statusFor is what the buddy gets to hear about us.
//
// An empty payload means "no client attached" - that is the "client offline
// (messages delivered)" marker. The client's own status carries the away
// text.
//
// And when a client is attached but has never set a status, we still say
// something: an empty payload would mean the opposite of the truth, and
// sending nothing would leave the buddy with whatever it last heard - a
// stale "client offline" from when the app was closed.
//
// It is re-sent every tick rather than only on a change: a packet sent
// before the route to the peer is up is simply lost, and a state that is
// only announced once may never arrive.
func (p *Presence) statusFor(pub ed25519.PublicKey) (seq uint64, payload []byte, send bool) {
	present := p.clientPresent()
	p.mu.Lock()
	defer p.mu.Unlock()
	pp := p.keyFor(pub)
	st := pp.out
	switch {
	case !present:
		return 0, nil, true
	case st != nil:
		return st.seq, st.payload, true
	default:
		// A client is attached but it has not set a status of its own:
		// there is nothing to say. An empty payload would mean the opposite
		// ("no client"), and a made-up payload would be a status the buddy
		// cannot decrypt.
		return 0, nil, false
	}
}

type presenceEntry struct {
	Key            string `json:"key"`
	Online         bool   `json:"online"`
	ClientAttached bool   `json:"client_attached"`
	StatusTs       int64  `json:"status_ts,omitempty"`
	StatusPayload  string `json:"status_payload,omitempty"`
}

func (p *Presence) Snapshot() []presenceEntry {
	p.mu.Lock()
	defer p.mu.Unlock()
	out := make([]presenceEntry, 0, len(p.peers))
	now := time.Now()
	for peerHex, pp := range p.peers {
		if p.mail.IsBlockedHex(peerHex) {
			continue
		}
		e := presenceEntry{
			Key:            peerHex,
			Online:         now.Sub(pp.lastSeen) < p.onlineWindow,
			ClientAttached: pp.in == nil || pp.in.attached,
		}
		if pp.in != nil {
			e.StatusTs = pp.in.ts
			e.StatusPayload = base64.StdEncoding.EncodeToString(pp.in.payload)
		}
		out = append(out, e)
	}
	sort.Slice(out, func(i, k int) bool { return out[i].Key < out[k].Key })
	return out
}
