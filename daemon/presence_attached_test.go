package main

import (
	"crypto/ed25519"
	"testing"
	"time"
)

// The buddy's view of "is a client attached to me" must not go stale.
//
// The presence protocol reads an empty status payload as "no client
// attached", so a daemon with a client attached but no status of its own
// (it is not away) has to say something - otherwise the buddy keeps the
// "client offline" it heard while the app was closed.
func TestPresenceSaysAClientIsAttachedEvenWithoutAStatus(t *testing.T) {
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

	// No client attached: the buddy must hear "no client".
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

	// A client is attached but has not set a status: the buddy must not be
	// told "no client attached" either.
	presence.TouchClient()
	_, payload, send = presence.statusFor(peer)
	if !send {
		t.Fatal("the status has to be sent")
	}
	if len(payload) == 0 {
		t.Error("an empty payload means 'no client attached' - the buddy would " +
			"keep a stale 'client offline'")
	}
}
