package main

import (
	"crypto/ed25519"
	"testing"
	"time"
)

// The status the buddy hears has to mean what it says.
//
// An empty payload means "no client attached" - that is the "client offline
// (messages delivered)" marker. So the daemon must not send an empty payload
// when a client IS attached, and it must not send a made-up one either: the
// buddy would try to decrypt it and fail.
func TestPresenceStatusMeansWhatItSays(t *testing.T) {
	dir := t.TempDir()
	node, err := StartNode(dir, nil, nil, quietLogger())
	if err != nil {
		t.Fatalf("start node: %v", err)
	}
	defer node.Stop()
	mail := newTestMail(t, dir, node)
	presence := NewPresence(mail, 50*time.Millisecond)
	presence.Start()

	priv := ed25519.NewKeyFromSeed(make([]byte, 32))
	peer := priv.Public().(ed25519.PublicKey)

	// No client attached: the buddy must hear "no client" - an empty payload.
	seq, payload, send := presence.statusFor(peer)
	if !send {
		t.Fatal("the status has to be sent")
	}
	if len(payload) != 0 {
		t.Errorf("with no client attached the payload should be empty, got %d bytes", len(payload))
	}
	if seq != 0 {
		t.Errorf("seq = %d, want 0", seq)
	}

	// A client is attached but has set no status of its own: there is nothing
	// to say. An empty payload would mean the opposite, and a made-up payload
	// would be a status the buddy cannot decrypt - so the buddy keeps what it
	// last heard until the client pushes a status of its own.
	presence.TouchClient()
	_, payload, send = presence.statusFor(peer)
	if send {
		t.Error("a client without a status of its own has nothing to say")
	}
	if len(payload) != 0 {
		t.Errorf("nothing should have been sent, got %d bytes", len(payload))
	}

	// With a status of its own, that status is what the buddy hears.
	if _, err := presence.SetStatus(peer, []byte("real status")); err != nil {
		t.Fatalf("SetStatus: %v", err)
	}
	seq, payload, send = presence.statusFor(peer)
	if !send {
		t.Fatal("the status has to be sent")
	}
	if string(payload) != "real status" {
		t.Errorf("payload = %q, want the client's own status", payload)
	}
	if seq == 0 {
		t.Error("the status needs a sequence number of its own")
	}
}
