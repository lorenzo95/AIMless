# Networking: peers, listeners and the mesh

`aimlessd` is an embedded [Yggdrasil](https://yggdrasil-network.github.io/) node. It
needs no TUN device, no root and no open inbound ports - it dials *out* to peers
and speaks to other daemons over the overlay, addressed by node key.

This is the full reference for pointing the daemon at the network. For the
one-paragraph version see the "Peers" section of the top-level `README.md`.

## The moving parts

| Thing | What it is | Where it lives |
|---|---|---|
| **node key** | Your identity on the mesh. The Yggdrasil address is derived from it. Changing it changes your address and invalidates old invites. | `daemon/node.key` |
| **peers** | Outbound links the daemon dials to join the mesh. | `daemon/config.json` → `peers`, or `-peers` |
| **listeners** | Inbound links other nodes may dial. Off by default - you do not need them unless you are running a public node. | `daemon/config.json` → `listen`, or `-listen` |
| **routing** | Learned automatically by the embedded node. | in memory |

A daemon with **no peers** is an isolated node: it will run, keep its address and
accept local API calls, but it cannot reach anyone and nobody can reach it.

## Where the settings live

### `daemon/config.json`

The normal way, and the one that survives restarts. Both keys are optional:

```json
{
  "peers": [
    "tls://ca.us.ygg.informatics.coop:443?key=0000000023fc6ffb62afb79448aefb0943df2e4472cf08bd489f5407378300a2",
    "tcp://ip4.nerdvm.mywire.org:8080?key=00000000c61d731961a290d127cd3fc03a4c5f3f35b9083559d4c81d48d65854"
  ],
  "listen": []
}
```

The same list can be edited from the app - **Options → Peers …** - which
writes this file and applies the change without a restart.

### Command-line flags

```sh
aimlessd -datadir ~/.local/share/aimless/daemon -api ~/.local/share/aimless/daemon/api.sock \
         -peers tls://ca.us.ygg.informatics.coop:443,tcp://ip4.nerdvm.mywire.org:8080 \
         -listen tcp://0.0.0.0:9001
```

### Precedence

| Setting | 1st | 2nd | 3rd (fallback) |
|---|---|---|---|
| **peers** | `-peers <uris>` | `config.json` → `peers` | built-in `defaultPeers` in `daemon/main.go` |
| **listeners** | `-listen <uris>` | `config.json` → `listen` | none |

Two gotchas worth knowing:

- **A flag replaces the config, it does not merge with it.** Passing `-peers`
  with one URI throws away whatever `config.json` listed.
- **An empty `peers` array in `config.json` means "no peers"** - the daemon
  runs as an isolated node on purpose. A config file *without* a `peers` key
  means "use the built-in list". `-peers none` says the same thing from the
  command line. (`listen` has no such quirk: an empty list simply means no
  inbound listeners.)

### All daemon flags

| Flag | Default | Meaning |
|---|---|---|
| `-datadir` | `~/.local/share/aimless/daemon` | Node key, config, journal, API socket. |
| `-api` | `<datadir>/api.sock` | Path of the local NDJSON API socket. |
| `-peers` | (see above) | Comma-separated peer URIs, or `none`. |
| `-listen` | (none) | Comma-separated listener URIs. |
| `-retry` | `2s` | How often the outbox re-sends unacknowledged messages. |
| `-probe` | `15s` | Presence probe interval. Also the online/offline window (3x this). |
| `-inbox` | `50` | Messages retained per peer before the oldest are dropped. |
| `-attachcap` | `64MiB` | Per-peer attachment byte budget. |
| `-verbose` | off | Debug logging to `logs/daemon.log`. |

## The built-in peer list

`defaultPeers` in `daemon/main.go` is the fallback when there is no
`peers` key in `config.json`. It is deliberately small:

- **five peers**, from **five different operators** - one operator going down
  should not take you with it;
- **measured**, not copied - each entry was dialled and its round trip time
  recorded, so the set is close by (tens of milliseconds);
- **pinned** with `?key=` - the peer has to present that public key, so an IP
  that gets reassigned cannot pass itself off as the node you meant to reach.

Public peers rot: operators go away, and a lapsed domain gets parked - the
hostname resolves to `0.0.0.0`, which connects to localhost and is refused
instantly. That is how this daemon once ended up with no peers at all. So:

- a peer whose hostname does not resolve is **refused at startup** and shown
  in the peer manager, instead of being counted as a peer;
- the peer list is editable at runtime (**Options → Peers …**), so a dead peer
  can be swapped without editing files and restarting;
- the current public list lives at <https://publicpeers.neilalexander.dev/>,
  and in machine-readable form at
  <https://github.com/Yggdrasil-Unofficial/pubpeers> (`peers.json`). The peer
  manager links to it - there is no need to go looking for it.

Only add a few peers. The upstream guidance is to pick a small number of
nearby ones: every extra peer is another node you relay traffic for.

## Peer URI reference

### Schemes

| Scheme | Transport |
|---|---|
| `tcp://host:port` | Plain TCP. Simple, and what the tests use. |
| `tls://host:port` | TCP + TLS. The usual choice for public peers. |
| `quic://host:port` | QUIC + TLS. |
| `ws://host:port` | WebSocket over plain TCP. |
| `wss://host:port` | WebSocket over TLS. |
| `socks://proxy:port/host:port` | Through a SOCKS5 proxy. The target is the path. |
| `sockstls://proxy:port/host:port` | Through a SOCKS5 proxy, with TLS to the peer. |
| `unix://path` | A local Unix socket. |

### Query options

Applies to both peers and listeners.

| Option | Meaning |
|---|---|
| `key` | Hex Ed25519 public key to pin the link to. Repeatable. The link only accepts that exact key, so a host that changes its key stops being usable. |
| `priority` | `0`-`255`. **Lower is preferred.** The effective value is the higher of the two ends, so setting it on one side only is enough to steer traffic. Default `0`. |
| `password` | Shared password for the link. |
| `maxbackoff` | Go duration, caps the reconnect backoff. |
| `sni` | TLS SNI hostname. Ignored if it is an IP literal; otherwise it defaults to the host in the URI. |
| `origin` | `Origin` header for `ws://` / `wss://`. `origin=*` skips origin checking. |

### Examples

```jsonc
{
  "peers": [
    // plain TLS, the common case
    "tls://23.184.48.86:1443",

    // pinned to a specific key - the peer must present exactly this key
    "tls://yggdrasil.neilalexander.dev:64648?key=ecbbcb3298e7d3b4196103333c3e839cfe47a6ca47602b94a6d596683f6bb358",

    // explicit SNI, when the URI host is an IP but the cert is for a name
    "tls://203.0.113.10:443?sni=peer.example.org",

    // preferred over the others on the same link
    "tls://backup.example.org:443?priority=10",

    // through a SOCKS5 proxy
    "socks://127.0.0.1:1080/peer.example.org:443"
  ]
}
```

## Sourcing live public peers

The built-in list rots. Public peers go offline, and when their domain lapses
the DNS record is parked - the hostname resolves to `0.0.0.0`, which connects
to localhost and is refused instantly. The symptom is the status bar sitting on
*"connecting - no Yggdrasil peers yet"* with `peers 0/N`.

The live list, refreshed hourly, is at
<https://publicpeers.neilalexander.dev/>. When picking from it:

- Take a **small** number of **nearby** peers with good uptime - your own
  country or a neighbour.
- Prefer one protocol per peer; adding the same peer under `tcp://`, `tls://`
  and `quic://` buys nothing.
- Copy the URI exactly, including any `?key=` part.

Then write them to `daemon/config.json` and restart the daemon.

## Checking it worked

The status bar shows `peers N/M` where N is the number currently up. The
daemon log records each successful link:

```
INFO Connected outbound: 203:b00a:1684:860d:880f:ea9c:92dc:fabb@23.184.48.86:1443
```

A peer that is configured but not up shows `up: false` with zero bytes
transferred in the `status` API reply.

## Routing, and why the first message can be slow

Yggdrasil routing is lazy. The embedded node refreshes its routing table every
few minutes, and a route to a node it has never spoken to is worked out on
demand rather than in advance. So the **first** message to a new peer can take
a minute or two to find its way; after that, routes are cached and delivery is
quick.

Nothing is lost while this happens - messages sit in the sender's outbox
journal and are retried until acknowledged, and they are delivered even if the
recipient was offline the whole time.

## Troubleshooting

**Status shows `peers 0/N`.**
Check DNS first: `dig +short <peer-host>` returning `0.0.0.0` means the peer
domain is dead, not that your network is broken. Confirm general egress with
something unrelated (`curl https://example.com`). If DNS is fine, test the port
directly - `connection refused` means nothing is listening there.

**Peers are up but messages never arrive.**
Check that both sides have each other's *node* key (from the invite), not just
the client key - the node key is the routing address. Let the routing settle:
give it a few minutes before concluding it is broken.

**`aimlessd exited immediately`.**
Usually a bad peer URI - the daemon parses every URI at startup and aborts on a
malformed one. `logs/daemon.log` has the parse error.

**Changed the node key and now nobody can reach you.**
Your address is derived from the node key, so it changed with it. Old invites
point at the old address and will never resolve. See "Keys & identity" in the
top-level README.
