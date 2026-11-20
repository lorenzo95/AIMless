# Reference self-hosted DERP relay

A ready-to-run [Tailscale DERP](https://github.com/tailscale/tailscale/tree/main/cmd/derper)
relay + STUN server for AIMless's **tailcat** transport, with automatic Let's Encrypt TLS.

Why run one: the public DERP fleet is shared and rate-limited, and a public region
can change under you. Your own relay on a static IP removes both, and it is the only
way to be sure your rendezvous point stays reachable.

`derper` terminates TLS itself and answers the ACME HTTP-01 challenge on port 80.
**Do not put an HTTP reverse proxy or load balancer in front of it**: DERP upgrades
from HTTP to a raw binary protocol inside TLS, which a proxy will break.

## Prerequisites

1. A VPS with a public IP, Docker and the compose plugin.
2. DNS: an `A` record for your relay hostname pointing at that IP (add `AAAA` too
   if the VPS has IPv6).
3. Firewall / security group: inbound **80/tcp**, **443/tcp**, **3478/udp**.
   Port 80 must stay open - Let's Encrypt renewals use it.

## Run

```sh
cd derper
cp .env.example .env            # set DERP_DOMAIN to your relay hostname
docker compose up -d
docker compose logs -f derper   # watch the first cert issuance
```

Or without an `.env` file: `DERP_DOMAIN=derp.example.com docker compose up -d`.

Certs land in `./certs`, the server node key in `./state`. Both are gitignored -
keep them around, because re-issuing certs on every restart can hit Let's Encrypt
rate limits.

## Point AIMless at it

AIMless builds its own DERP region **900** from the hostname, so you need no
tailnet, no ACL file and no DERP map URL:

- **GUI**: *Relay …* -> self-hosted hostname -> your relay hostname -> **Save**.
  Applies right away (the local daemon restarts). Several relays: comma-separated
  hostnames.
- **Config**: the `relay` block in the daemon's `config.json`:

  ```json
  { "relay": { "hostname": "derp.example.com" } }
  ```

- **API**: the `setrelay` op with the same block.

This compose publishes DERP on 443 and STUN on 3478, which is exactly what AIMless
dials for a self-hosted region.

Two things to know:

- The region is **part of your address**, so choose it before sharing invites.
  Buddies you already talk to pick up a changed address on their own (every status
  carries it, within ~60s); anyone else needs your new invite.
- A remote (tunnel) daemon is not restarted by the client - restart it by hand. If
  a bad relay config keeps the daemon from starting, the app offers
  **Reset relay & retry**.

Full transport details: [../docs/tailcat.md](../docs/tailcat.md).

## Verify

```sh
python3 probe.py derp.example.com
# or: DERP_DOMAIN=derp.example.com python3 probe.py
```

Run it from **any** machine, not just the relay host, so it proves reachability
from the outside. Stdlib only. It checks STUN over IPv4 and IPv6, `/derp/probe`,
`/generate_204`, and the DERP TLS upgrade including the server's key frame; exit
status 0 means every check passed. The two control STUN checks at the end tell you
whether your own network blocks STUN - if those fail too, the relay is not
necessarily at fault.

Note: derper ignores plain STUN requests by design. The probe sends the
`SOFTWARE`/`FINGERPRINT` attributes Tailscale requires.

The probe's request building and host plumbing also have offline unit tests:
`python3 -m pytest derper -q` (no network needed).

Also useful:
- `docker compose logs derper` - the cert being obtained, no ACME errors.
- The AIMless route bar / tray: the daemon TCP-probes the configured relay every
  ~20s, so a hostname that does not resolve or a dead relay shows as
  *relay not reachable* (reason on hover) instead of a false "online".
- `tailscale netcheck` and `tailscale debug derp 900`, if you also run Tailscale.

## Optional: register it in a tailnet

Only needed if you use Tailscale itself and want your tailnet's clients to prefer
this relay. AIMless does not read this file. Add a `derpMap` to your tailnet policy
file (ACLs) at <https://login.tailscale.com/admin/acls>; custom regions must use
IDs >= 900. Listing the IP explicitly avoids DNS bootstrap problems:

```json
"derpMap": {
  "OmitDefaultRegions": false,
  "Regions": {
    "900": {
      "RegionID": 900,
      "RegionCode": "myrelay",
      "RegionName": "My self-hosted relay",
      "Nodes": [
        {
          "Name": "900a",
          "RegionID": 900,
          "HostName": "derp.example.com",
          "IPv4": "YOUR_VPS_IPV4",
          "DERPPort": 443,
          "STUNPort": 3478
        }
      ]
    }
  }
}
```

Set `"OmitDefaultRegions": true` to use *only* your server instead of Tailscale's
built-in fleet.

## Notes and security

- Tailscale ships no official DERP image. `fredliang/derper` is a widely used
  community build of `tailscale.com/cmd/derper`; the tag here is pinned to a
  Tailscale release. Bump it periodically, roughly in step with your clients.
- **`DERP_VERIFY_CLIENTS=false`** (the default here) means anyone who knows the
  hostname can relay through this server. That is what AIMless needs: your buddies
  are not in your tailnet. Payload traffic stays end-to-end encrypted - the relay
  only ever sees ciphertext, and AIMless's own E2E layer is independent of the
  transport underneath it.
- `DERP_VERIFY_CLIENTS=true` restricts relaying to nodes of your tailnet, but
  requires a `tailscaled` on the VPS (same version as derper) with its socket
  mounted into the container (`/var/run/tailscale/tailscaled.sock`). Only use it if
  every peer that must reach you is in that tailnet.
- What the relay *can* see is metadata: who connects, when, and how many bytes.
  It cannot read messages, screen names, filenames or away status.
- Never commit `certs/`, `state/` or your `.env` - the gitignore here covers them.
