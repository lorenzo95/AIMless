# Phase 0 spike: tailcat as AIMless's transport

**Gate decision: GO.** tailcat v0.7.0 behaves the way the `tailcat-transport`
swap assumes. Every core assumption was validated against a deterministic,
in-process local DERP+STUN — no docker, no TLS certs, no public relays. One
non-obvious reconnect requirement surfaced (below); it is a design constraint
for Phase 2, not a blocker.

## How to run

```sh
cd daemon/cmd/tailcat-poc
go test -v ./...        # measurements are logged as MEASURE / VALIDATED lines
```

The spike is its own Go module (`aimless/tailcat-poc`) so the heavy
`tailscale.com` dependency tree never touches the main `aimless/daemon` module.
It uses tailcat's own test bootstrap, `tailscale.com/tstest/integration.RunDERPAndSTUN`,
which starts a DERP relay + STUN server in-process on 127.0.0.1.

## Environment

| Piece | Version |
|---|---|
| tailcat | v0.7.0 (pinned) |
| tailscale.com | v1.103.0-pre.0.20260916030321-a2263542f260 |
| Go | 1.27.1 linux/amd64 |
| DERP/STUN | in-process, loopback (deterministic) |

## Measurements (loopback, in-process DERP)

| Metric | Value | Note |
|---|---|---|
| First rendezvous (`Ping` meow/meowed) | ~1.0 s | includes DERP connect + handshake interval; first contact only |
| Dial after rendezvous | ~2.4 ms | |
| First-message RTT | ~0.38 ms | sub-millisecond, **direct path** |
| 32 KiB + 20-byte frame RTT | ~3 ms | intact, **no MTU limit** (TCP/netstack) |
| Echo throughput | ~58 MiB/s | 2 MiB round-trip, direct path |
| Offline re-dial | blocks to ctx deadline | 1 s avg with a 1 s timeout; **no fail-fast** |
| Idle (1 server + 1 client + DERP) | ~194 goroutines, ~12 MiB heap | scales ~linearly per buddy client |
| Direct-path upgrade | confirmed | magicsock `via=direct`, `now using 127.0.0.1:…` |

The headline: once rendezvous completes, delivery is sub-millisecond over a
**direct** path. Even on loopback, magicsock hole-punched to a direct UDP path
rather than staying on DERP — the "truly p2p when the network allows" claim holds.
First rendezvous (~1 s) is vastly better than Yggdrasil's multi-minute tree
convergence, though it is not instant.

## Validated behaviors

- **Persistent identity** — same `Key` + `PresharedKey` + `Region` ⇒ a stable
  217-char address across `Server` restarts. `ParseAddr` recovers the stable node
  public key. Rotating the PSK changes the address (confirms invite staleness).
- **Sender identity** — `Server.PeerEnv(local, remote)` yields
  `TAILCAT_PEER_KEY=nodekey:…`, the authenticated client node key. This is how the
  daemon's receive path maps a connection to a sender.
- **Admission control** — a disallowed client cannot establish a tunnel; a client
  with the wrong PSK cannot either (even if allowlisted); an allowed client with
  the correct PSK works. `AllowedClients` / `AddAllowedClient` map to contacts.
- **Large payloads** — a 32 KiB + 20-byte frame (an AIMless FILE chunk) round-trips
  intact over TCP. No Yggdrasil-style MTU ceiling.
- **Offline delivery semantics** — dialing an offline peer fails (DERP is a live
  relay, not a mailbox). When the peer returns on the same identity, delivery
  resumes. Matches the sender-side journal/retry model.

## Design requirements for Phase 2 (surfaced by the spike)

1. **Reconnect = evict + recreate the `Client`, not revive a stale session.**
   A restarted `Server` has an empty peer list, and the client's cached direct
   path is dead. Re-`Ping` alone does **not** revive the old session (the dial
   still times out). The working recovery is: close the stale `Client`, create a
   fresh one with the **same persistent key**, `Ping`, then dial. `TailcatNode`'s
   connection cache must evict on failure and rebuild this way.
2. **Dial blocks to the context deadline on offline peers.** The retry loop must
   use a short dial timeout (~1–2 s) **and exponential backoff** — not the current
   2 s datagram re-send cadence, which would burn a full timeout per attempt.
3. **Sender identity via `PeerEnv`.** v0.7.0 has no exported `Server.PeerKey`
   (that is main-branch). Parse `TAILCAT_PEER_KEY` from `PeerEnv`, or map the
   connection's `RemoteAddr`. Plan accordingly.
4. **Allowlist removal needs checking.** v0.7.0 exposes `AddAllowedClient` but no
   obvious `RemoveAllowedClient`/`DisconnectClient`. The blocklist path (Phase 3)
   must confirm how to drop an already-connected client (rebuild the allowlist
   and/or close the connection from the handler).
5. **Address length ~217 chars** with an embedded region. Invite v2 will be long
   but paste-able; the stable node id is recovered via `ParseAddr`.
6. **Resource cost** ~194 goroutines / ~12 MiB for one server + one client + DERP.
   Measure at buddy-scale in Phase 2; expect roughly linear growth per client.

## API notes (v0.7.0 vs the main-branch README)

- `Server{Key, PresharedKey, Region, Logf, OnTCP, AllowedClients}`;
  `Start`, `Close`, `TailcatAddr`, `Addr`, `AddAllowedClient`, `PeerEnv`, `Status`.
- `Client{Server Addr, Key, Logf}`; `Ping`, `DialTCPPort`, `DialUDPPort`,
  `PublicKey`, `Close`. `PingResult` carries only handshake `Latency`;
  `DiscoPing`/`Status` give direct-vs-relayed path detail.
- `ParseAddr(Addr) (ConnInfo, error)`; `ConnInfo{ServerPublic, ServerDiscoPublic,
  PresharedKey, Region}`; `NewPresharedKey()`; `MaxUDPPayload = 1232`.
- Local DERP bootstrap: `integration.RunDERPAndSTUN(t, logf, "127.0.0.1")` →
  `DERPMap.Regions[1]`. Test helpers `WaitForDERPForTest` / `PingForTest` exist in
  tailcat's own package (not exported for external use); the spike waits via `Ping`.

## Conclusion

Proceed to Phase 1 (Transport interface + Yggdrasil refactor). The transport swap
is feasible; the reconnect/backoff nuances above are bounded engineering work,
and the E2E layer is untouched. The public-DERP rate-limit/revocability and
region-staleness risks remain as documented in the plan; the spike used a local
relay, so those are validated separately in Phase 6 real-world runs.