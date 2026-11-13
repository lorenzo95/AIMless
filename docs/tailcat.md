# The tailcat transport

AIMless can run over [tailcat](https://github.com/tailscale/tailcat) instead of
Yggdrasil. tailcat is Tailscale's data plane (WireGuard + magicsock + DERP)
without the control plane: pairwise, NAT-traversing, end-to-end encrypted
tunnels addressed by public key. This page describes the alternative transport,
how to switch to it, and how it differs from Yggdrasil.

> Status: experimental, on the `tailcat-transport` branch. The Yggdrasil
> transport remains the default. See `docs/tailcat-spike.md` for the Phase 0
> measurements that gated the work.

## Why

Yggdrasil is a mesh with spanning-tree routing and **no NAT traversal**: two
nodes behind NAT talk via the overlay tree, through public peers, and a route to
a never-before-seen peer can take minutes to converge. tailcat instead:

- hole-punches a **direct UDP path** between peers when the network allows
  (magicsock/disco), falling back to a DERP relay only when it cannot;
- rendezvous through DERP in ~a second, not minutes;
- carries the AIMless envelope over TCP (gVisor netstack), so there is **no
  datagram MTU ceiling** — 32 KiB file chunks fit unchanged.

The E2E layer is untouched: the client still makes per-recipient NaCl sealed
boxes and Ed25519 signatures. tailcat adds a second encrypted transport layer
underneath; it never sees plaintext.

## Architecture

The daemon's network boundary is the `Transport` interface (`daemon/transport.go`):

| Implementation | Build tag | File |
|---|---|---|
| `YggNode` (wraps the Yggdrasil `*Node`) | `!tailcat` (default) | `daemon/transport_ygg.go`, `daemon/node.go` |
| `TailcatNode` | `tailcat` | `daemon/tailcat.go` |

Everything above the interface — journals, retry/ACK, attachments, presence,
the local NDJSON API, and the whole Python client — is transport-independent.

`TailcatNode` runs one persistent `tailcat.Server` (the node identity) and a
cache of `tailcat.Client`s (one per buddy). A connection is **bidirectional**:
both ends read and write envelopes, so one TCP connection per peer pair carries
MSG/ACK/STATUS/PROBE both ways. The wire frame **is** the AIMless envelope
(20-byte header, little-endian payload length at bytes 18:20).

### Identity vs address

The two transports diverge on addressing:

- **Stable node ID** — the WireGuard public key (32 bytes, hex). Used for the
  conversation/room id, presence, blocklist and journals. Stable across restarts.
- **Dial address** — the full tailcat address (`tc…`): the node key plus a
  path-discovery key, a pre-shared key, and the DERP region. Needed to connect.

The daemon stores `pubkey → address` in `daemon/contacts.json` (an `addrs` map).
The `watch` op carries the address; `Mail.Attach` re-registers persisted
addresses on startup so buddies are reachable after a restart without re-adding.

### Reconnect

A restarted peer has an empty peer list and the old session's cached direct path
is dead; re-pinging alone does not revive it. On any connection error
`TailcatNode` **evicts the peer's Client and connection and recreates a fresh
Client with the same persistent key**. Dials use a short timeout (offline peers
block to the deadline — DERP is a live relay, not a mailbox); the mail retry loop
provides the backoff cadence and durable queueing.

## Switching transports (model A)

There is **no runtime toggle and no dual-homing**: you run one transport at a
time, selected by which daemon binary the client spawns.

1. Build the tailcat daemon:
   ```sh
   cd daemon && go build -tags tailcat -o aimlessd-tailcat .
   ```
2. Point the client at it with `AIMLESS_DAEMON_BIN`, and use a **separate
   `AIMLESS_HOME` per transport** so keys, contacts and addresses stay distinct:
   ```sh
   AIMLESS_DAEMON_BIN=/path/to/aimlessd-tailcat AIMLESS_HOME=~/.local/share/aimless-tc aimless
   ```
   `daemon_binary()` honors `AIMLESS_DAEMON_BIN`; without it the default
   Yggdrasil binary is used.

Both binaries share the `<datadir>/lock` flock, so only one daemon runs per
datadir. Switching transport on the *same* home changes your address and makes
old invites stale — separate homes avoid that. To talk to a buddy over tailcat,
both of you must be on tailcat and exchange tailcat invites.

## Invites (v2)

A tailcat invite carries the dial address:

```
aimless2:<client-pk-base58>:<node-pubkey-hex>:<tailcat-address>:<screen-name>
```

Yggdrasil keeps the v1 form (`aimless1:<client-pk>:<node-pk>:<screen>`).
`protocol.parse_invite` reads both and keeps its 3-tuple return;
`protocol.invite_addr` extracts the v2 dial address. `make_invite` emits v2
automatically when the daemon's address is a tailcat address (`tc…`).

The tailcat address is a **bearer capability** (it contains the pre-shared key):
treat the invite as a secret, like a key. Rotating the PSK changes the address
and stales old invites.

## Relays (DERP)

By default the daemon uses the full official Tailscale DERP map
(`https://login.tailscale.com/derpmap/default`, ~28 regions). These are free,
best-effort relays - no SLA, and access can be revoked. Text is tiny and sails
through; large files are slow when the path stays relayed. When hole-punching
succeeds the relay leaves the data path entirely. (tailcat's own
`https://tailcat.dev/derpmap.json` default is only a 4-region subset; the official
relays accept tailcat connections too, so we default to the full map.)

**Regions.** Your DERP region is part of your tailcat address, so pick it once
before sharing invites. Choose it in the GUI relay panel (the "Relay …" menu
item), which lists every region in the map and pre-selects the one you are on; or
set `relay.region_id` in `config.json` (e.g. 21 = Toronto). Saving in the GUI
applies right away by restarting the daemon; a hand-edited `config.json` needs a
restart. The route bar shows your current region on hover. Verified: a node on
the official Toronto relay (region 21) connects fine.

Two region facts worth knowing:
- You and a buddy can be on *different* regions and it works: each invite embeds
  that node's own region, and the other side connects to it.
- *Changing your own region* changes your address (the region is embedded in it),
  so buddies who already added you need your new invite. Pick your region before
  sharing invites.

To run your own relay (no rate limit, no revocability risk), run
[`derper`](https://github.com/tailscale/tailscale/tree/main/cmd/derper) on a host
with a TLS certificate and point the daemon's region at it. A self-hosted relay
on a static IP also avoids the public-relay region-staleness issue.

## Building and testing

```sh
cd daemon
go build .                      # default Yggdrasil daemon
go build -tags tailcat .        # tailcat daemon
go test ./...                   # Yggdrasil + transport-agnostic tests
go test -tags tailcat ./...     # tailcat integration + transport-agnostic tests
```

The tailcat integration tests (`daemon/tailcat_test.go`) drive the real Mail
layer over an **in-process local DERP+STUN** (`tailscale.com/tstest/integration`):
delivery + ACK, bidirectional flow, 32 KiB file chunks, persistent identity,
offline→online reconnect, and address persistence across restart. The
Yggdrasil-coupled test files are build-tagged `!tailcat`; the transport-agnostic
unit tests run under both tags.

The `tailcat` build pulls in `tailscale.com` (gVisor netstack, wireguard-go,
magicsock), so the tailcat binary is larger (~36 MB vs ~14 MB). The default
Yggdrasil binary stays lean — the tailcat code is excluded by build tag.

## Group rooms

Rooms are client-side fan-out: one sealed copy per member. The roster carried in
each room message includes each member's dial address (`addr`) alongside
`node`/`pubkey`/`screen`, so a recipient can dial the other members over tailcat.
The room id is still the hash of the member **node ids** (not addresses), so it
stays stable. `send_room`/`send_file_room` register member addresses with the
daemon before fanning out. Yggdrasil rosters carry an empty `addr` and are
unaffected. Verified end-to-end over public DERP by
`deploy/smoke_tailcat_room.py` (three nodes, bidirectional room delivery).

## Limitations / remaining work

- No runtime transport toggle, no dual-homing, no per-contact delivery switching
  (that would require re-keying conversations/rooms on the client E2E key).
- Relay configuration is via the `setrelay` op, the `config.json` `relay` block
  (`region_id`, `hostname`, and/or `derpmap_url`), or the GUI relay panel (region
  picker + self-hosted hostname). The relay is embedded in the address, so applying
  a change needs a daemon restart: the GUI does this automatically for the local
  daemon; a remote (tunnel) daemon must be restarted by hand.
- The GUI is transport-aware (relay panel, relay-based tray/status, v2 invite
  add-buddy); `gtkui.py` is still a monolith and would benefit from extraction.
- tailcat v0.7.0 makes no API/wire stability promises; the version is pinned.
- Sender identity uses `Server.PeerEnv` (`TAILCAT_PEER_KEY`); v0.7.0 has no
  exported `PeerKey`. Allowlist removal for blocking is via connection close.