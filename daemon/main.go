package main

import (
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

func acquireLock(datadir string) error {
	if err := os.MkdirAll(datadir, 0o700); err != nil {
		return fmt.Errorf("create datadir: %w", err)
	}
	path := filepath.Join(datadir, "lock")
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return fmt.Errorf("open lock file: %w", err)
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		f.Close()
		return fmt.Errorf("another aimlessd instance is already using %s", datadir)
	}
	return nil
}

// defaultPeers are the public Yggdrasil nodes we fall back on when the
// datadir has no config.json.
//
// They are picked by measurement, not by copying a list: each one was dialled
// from this machine and its round-trip time recorded, so the set is close by
// (39-60 ms), comes from five different operators, and every entry is pinned
// with ?key= - the peer must present that public key, so an IP that gets
// reassigned cannot pass itself off as the node we meant to reach.
//
// A public peer list rots: operators go away and their DNS records get
// parked. That is what made this daemon unreachable once already (it dialled
// 0.0.0.0 and called it a peer). So:
//   - peers whose hostname does not resolve are refused at startup, and the
//     client shows why instead of counting them;
//   - the peer list is editable at runtime from the client, so a dead peer
//     can be swapped without editing files and restarting;
//   - the current public list lives at https://publicpeers.neilalexander.dev/
//     and in machine-readable form at
//     https://github.com/Yggdrasil-Unofficial/pubpeers (peers.json).
//
// Users are not stuck with these: daemon/config.json {"peers": [...]} overrides
// them entirely, and aimlessd -peers takes a comma-separated list.
var defaultPeers = []string{
	"tls://ca.us.ygg.informatics.coop:443?key=0000000023fc6ffb62afb79448aefb0943df2e4472cf08bd489f5407378300a2",
	"tcp://ip4.nerdvm.mywire.org:8080?key=00000000c61d731961a290d127cd3fc03a4c5f3f35b9083559d4c81d48d65854",
	"tls://ygg8.mk16.de:1338?key=0000000b0683e38d8cb3085cfbf217edf0f0b2de76bb5ac7f02a94cd42a3fed9",
	"tcp://no0.yggdrasil.netralex.com:65533?key=f4b9fe4f9923834b1c20f84e13b14d90ecde443f78613ec54cf484eea6880fea",
	"tls://nl1.route172.de:443?key=000000002643ae27bad63e2a81f1d30554ce9ea634f5990062a9e079e23593cd",
}

const defaultInboxCapacity = 50

const defaultAttachCap = 64 << 20

const defaultRetryInterval = 2 * time.Second

const defaultProbeInterval = 15 * time.Second

// configFile is the daemon's config.json.
//
// Peers is a pointer so that "peers" being absent (use the built-in list) and
// "peers" being an empty list (no peers at all) are different things - the
// same distinction the -peers flag makes with "none".
type configFile struct {
	Peers  *[]string `json:"peers,omitempty"`
	Listen []string  `json:"listen,omitempty"`
}

func saveConfig(path string, cfg *configFile) error {
	data, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, append(data, '\n'), 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

func loadConfig(path string) (*configFile, error) {
	data, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return &configFile{}, nil
	}
	if err != nil {
		return nil, err
	}
	var cfg configFile
	if err := json.Unmarshal(data, &cfg); err != nil {
		return nil, fmt.Errorf("parse %s: %w", path, err)
	}
	return &cfg, nil
}

func main() {
	log.SetFlags(log.LstdFlags)

	home, _ := os.UserHomeDir()
	defaultDatadir := filepath.Join(home, ".local", "share", "aimless", "daemon")

	datadir := flag.String("datadir", defaultDatadir, "data directory for node key and config")
	apiPath := flag.String("api", "", "unix socket path (default: <datadir>/api.sock)")
	peersFlag := flag.String("peers", "", "comma-separated peer URIs, or 'none' (overrides config)")
	listenFlag := flag.String("listen", "", "comma-separated link listener URIs")
	retryFlag := flag.Duration("retry", defaultRetryInterval, "outbox retry interval")
	probeFlag := flag.Duration("probe", defaultProbeInterval, "presence probe interval")
	inboxFlag := flag.Int("inbox", defaultInboxCapacity, "per-peer inbox message capacity")
	attachFlag := flag.Int64("attachcap", defaultAttachCap, "per-peer attachment byte budget")
	verbose := flag.Bool("verbose", false, "debug logging")
	flag.Parse()

	if err := run(*datadir, *apiPath, *peersFlag, *listenFlag, *retryFlag, *probeFlag,
		*inboxFlag, *attachFlag, *verbose); err != nil {
		log.Fatalf("aimlessd: %v", err)
	}
}

func run(datadir, apiPath, peersFlag, listenFlag string, retryInterval, probeInterval time.Duration,
	inboxCapacity int, attachCap int64, verbose bool) error {
	logger := &aimlessLogger{verbose: verbose}

	if err := acquireLock(datadir); err != nil {
		return err
	}

	cfg, err := loadConfig(filepath.Join(datadir, "config.json"))
	if err != nil {
		return err
	}
	var peers []string
	if cfg.Peers != nil {
		peers = *cfg.Peers
	}
	if peersFlag != "" {
		if peersFlag == "none" {
			peers = nil
		} else {
			peers = strings.Split(peersFlag, ",")
		}
	}
	if peers == nil && peersFlag != "none" {
		peers = defaultPeers
	}
	listenURLs := cfg.Listen
	if listenFlag != "" {
		listenURLs = strings.Split(listenFlag, ",")
	}

	node, err := StartNode(datadir, peers, listenURLs, logger)
	if err != nil {
		return err
	}
	defer node.Stop()

	mail, err := NewMail(datadir, inboxCapacity, attachCap, retryInterval)
	if err != nil {
		return err
	}
	defer mail.Close()
	// Order matters: the node's read loop is already running, so a packet can
	// arrive at any moment. Everything the packet handler touches has to be
	// in place *before* the handler is wired up, or the first packet finds a
	// half-built mail (a nil node, no presence).
	mail.Attach(node)
	presence := NewPresence(mail, probeInterval)
	node.SetHandlers(mail.HandlePacket, mail.PathUp)
	presence.Start()

	if apiPath == "" {
		apiPath = filepath.Join(datadir, "api.sock")
	}
	api, err := NewAPIServer(node, mail, presence, apiPath, filepath.Join(datadir, "config.json"))
	if err != nil {
		return err
	}
	defer api.Close()
	mail.SetOnDeliver(api.DeliverMsg)
	mail.SetOnDeliverFile(api.DeliverFileMsg)
	mail.SetOnAcked(api.Acked)

	fmt.Printf("aimlessd %s\n", buildVersion)
	fmt.Printf("  address: %s\n", node.Address.String())
	fmt.Printf("  pubkey:  %s\n", hex.EncodeToString(node.Pub))
	fmt.Printf("  api:     %s\n", apiPath)
	for _, p := range peers {
		fmt.Printf("  peer:    %s\n", p)
	}

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, os.Interrupt, syscall.SIGTERM)
	<-sig
	fmt.Println("shutting down")
	return nil
}
