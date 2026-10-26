package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// A peer URI that the daemon will accept.
func goodPeerURI() string {
	return "tls://ca.us.ygg.informatics.coop:443?key=0000000023fc6ffb62afb79448aefb0943df2e4472cf08bd489f5407378300a2"
}

func TestValidatePeerURI(t *testing.T) {
	good := []string{
		goodPeerURI(),
		"tcp://10.0.0.1:9000",
		"quic://host.example:65535",
		"ws://host.example:80",
		"wss://host.example:443",
		"unix:///run/aimless/peer.sock",
	}
	for _, uri := range good {
		if err := validatePeerURI(uri); err != nil {
			t.Errorf("validatePeerURI(%q) = %v, want nil", uri, err)
		}
	}
	bad := []string{
		"",
		"host.example:443",
		"ftp://host.example:21",
		"tls://host.example",
		"tls://:443",
		"tls://host.example:0",
		"tls://host.example:70000",
		"tls://host.example:notaport",
		"tls://host.example:443/../etc",
		"tls://host.example:443; rm -rf /",
	}
	for _, uri := range bad {
		if err := validatePeerURI(uri); err == nil {
			t.Errorf("validatePeerURI(%q) = nil, want an error", uri)
		}
	}
}

// A peer whose hostname does not resolve must not take the daemon down - and
// the client has to be able to see that it was refused.
func TestUnresolvablePeerIsSkippedNotFatal(t *testing.T) {
	dir := t.TempDir()
	node, err := StartNode(dir, []string{
		"tls://this-host-does-not-exist.invalid:443",
	}, nil, quietLogger())
	if err != nil {
		t.Fatalf("a dead peer must not stop the daemon: %v", err)
	}
	defer node.Stop()

	dialed, refused := node.PeerState()
	if len(dialed) != 0 {
		t.Errorf("dialled %v, want none", dialed)
	}
	if _, ok := refused["tls://this-host-does-not-exist.invalid:443"]; !ok {
		t.Errorf("the refused peer is not reported: %v", refused)
	}
}

func TestConfiguredPeersKeepsTheOrderAndTheRefusedOnes(t *testing.T) {
	dir := t.TempDir()
	node, err := StartNode(dir, []string{
		"tls://this-host-does-not-exist.invalid:443",
	}, nil, quietLogger())
	if err != nil {
		t.Fatalf("start node: %v", err)
	}
	defer node.Stop()

	got := node.ConfiguredPeers()
	if len(got) != 1 || got[0] != "tls://this-host-does-not-exist.invalid:443" {
		t.Errorf("ConfiguredPeers() = %v, want the peer we were given", got)
	}
}

// The peer list is stored in config.json, and an empty list means "no peers"
// rather than "use the built-in list".
func TestConfigPeersEmptyMeansNoPeers(t *testing.T) {
	path := filepath.Join(t.TempDir(), "config.json")
	empty := []string{}
	if err := saveConfig(path, &configFile{Peers: &empty}); err != nil {
		t.Fatalf("saveConfig: %v", err)
	}
	cfg, err := loadConfig(path)
	if err != nil {
		t.Fatalf("loadConfig: %v", err)
	}
	if cfg.Peers == nil {
		t.Fatal("an empty peer list must not be read as 'not set'")
	}
	if len(*cfg.Peers) != 0 {
		t.Errorf("peers = %v, want an empty list", *cfg.Peers)
	}

	// A file without a peers key means "use the built-in list".
	path2 := filepath.Join(t.TempDir(), "config.json")
	if err := os.WriteFile(path2, []byte(`{"listen":["tcp://0.0.0.0:0"]}`), 0o600); err != nil {
		t.Fatalf("write config: %v", err)
	}
	cfg2, err := loadConfig(path2)
	if err != nil {
		t.Fatalf("loadConfig: %v", err)
	}
	if cfg2.Peers != nil {
		t.Errorf("peers = %v, want nil (no peers key)", *cfg2.Peers)
	}
}

func TestSaveConfigWritesThePeerList(t *testing.T) {
	path := filepath.Join(t.TempDir(), "config.json")
	list := []string{goodPeerURI(), "tcp://10.0.0.1:9000"}
	if err := saveConfig(path, &configFile{Peers: &list}); err != nil {
		t.Fatalf("saveConfig: %v", err)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read config: %v", err)
	}
	var raw map[string]interface{}
	if err := json.Unmarshal(data, &raw); err != nil {
		t.Fatalf("config is not json: %v", err)
	}
	peers, ok := raw["peers"].([]interface{})
	if !ok || len(peers) != 2 {
		t.Fatalf("config peers = %v, want two entries", raw["peers"])
	}
}

// The setpeers op: validate, store, apply.
func TestSetPeersStoresAndApplies(t *testing.T) {
	dir := t.TempDir()
	node, err := StartNode(dir, nil, nil, quietLogger())
	if err != nil {
		t.Fatalf("start node: %v", err)
	}
	defer node.Stop()
	mail := newTestMail(t, dir, node)
	presence := NewPresence(mail, 250*time.Millisecond)
	presence.Start()
	configPath := filepath.Join(dir, "config.json")
	sock := filepath.Join(dir, "api.sock")
	api, err := NewAPIServer(node, mail, presence, sock, configPath)
	if err != nil {
		t.Fatalf("start api: %v", err)
	}
	defer api.Close()

	client := dialAPI(t, sock)
	client.send(t, apiMessage{Op: "setpeers", PeerList: []string{goodPeerURI()}})
	resp := client.read(t, 10*time.Second)
	if resp.Op != "peers" {
		t.Fatalf("op = %s (%s), want peers", resp.Op, resp.Error)
	}

	// It is stored, so it survives a restart.
	cfg, err := loadConfig(configPath)
	if err != nil {
		t.Fatalf("loadConfig: %v", err)
	}
	if cfg.Peers == nil || len(*cfg.Peers) != 1 || (*cfg.Peers)[0] != goodPeerURI() {
		t.Fatalf("config peers = %v, want the peer we set", cfg.Peers)
	}

	// And the daemon knows about it.
	found := false
	for _, p := range node.ConfiguredPeers() {
		if p == goodPeerURI() {
			found = true
		}
	}
	if !found {
		t.Errorf("the peer is not in the node's list: %v", node.ConfiguredPeers())
	}
}

func TestSetPeersRefusesABadURI(t *testing.T) {
	dir := t.TempDir()
	node, err := StartNode(dir, nil, nil, quietLogger())
	if err != nil {
		t.Fatalf("start node: %v", err)
	}
	defer node.Stop()
	mail := newTestMail(t, dir, node)
	presence := NewPresence(mail, 250*time.Millisecond)
	presence.Start()
	configPath := filepath.Join(dir, "config.json")
	sock := filepath.Join(dir, "api.sock")
	api, err := NewAPIServer(node, mail, presence, sock, configPath)
	if err != nil {
		t.Fatalf("start api: %v", err)
	}
	defer api.Close()

	client := dialAPI(t, sock)
	client.send(t, apiMessage{Op: "setpeers", PeerList: []string{"not a peer uri"}})
	resp := client.read(t, 10*time.Second)
	if resp.Op != "error" {
		t.Fatalf("op = %s, want error", resp.Op)
	}
	if _, err := os.Stat(configPath); !os.IsNotExist(err) {
		t.Error("a refused peer list must not be written to the config")
	}
}
