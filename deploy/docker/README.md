# aimless in a Docker web desktop (noVNC)

Runs the aimless GUI + daemon inside a minimal **Alpine** container. No desktop
stack is installed - just Xvfb (`:1`), the tiny `openbox` window manager,
`x11vnc` + `noVNC` for browser access, and `supervisord` supervising every
process with `autorestart`.

Everything runs as the non-root user `aimless` (uid 1000). The entrypoint only
runs as root long enough to fix the ownership of the bind-mounted `/data`
volume.

## Why this shape

Same pattern as `~bitmessage-docker`: a virtual display, the app launched in
the foreground, and supervisord restarts it whenever it exits. Because there is
**no system tray** in the container, aimless falls back to window mode - closing
the window quits the app, and supervisord immediately starts it again. So the
aimless window in your browser is effectively "always on".

The daemon connects to the overlay out of the box: with no `config.json` it
uses the built-in public Yggdrasil relay (see `daemon/main.go` `defaultPeers`).

## Setup (one time)

### Option A - pull from GHCR (recommended)

```bash
docker run -d --name aimless-webtop --restart unless-stopped \
  -p 127.0.0.1:8080:8080 -p 127.0.0.1:5900:5900 \
  -e VNC_PASS="${VNC_PASS:-aimless}" \
  -v "$PWD/aimless-data:/data" \
  ghcr.io/lorenzo95/aimless/aimless-webtop:latest
```

or with compose (the repo's `deploy/docker/docker-compose.yml` already points at
the GHCR image):

```bash
cd deploy/docker
docker compose up -d
```

### Option B - build locally from your checkout (runs your branch, not main)

```bash
deploy/package.sh          # build dist/ from the current branch first
cd deploy/docker
./build.sh                 # web desktop, yggdrasil
```

`build.sh` builds the image here (tagged `aimless-webtop:local`, never pulled
from GHCR) and **seeds `aimless-data/bin/` from your working tree's `dist/`**,
then starts with `AIMLESS_FETCH=never` so nothing is downloaded. That is how you
run an unmerged branch: the code in the container is exactly what you just
built, not whatever is on main.

```bash
AIMLESS_TRANSPORT=tailcat ./build.sh            # web desktop over tailcat
./build.sh --daemon                             # headless daemon
AIMLESS_TRANSPORT=tailcat ./build.sh --daemon   # headless tailcat daemon
```

After changing code, re-run `deploy/package.sh` then `./build.sh`; the artifacts
are re-seeded every time.

With Option A the container is a fixed **environment**: the client
(`aimless.pyz`) and the daemon (a static binary) are **fetched from the git
`dist/` artifacts on first start** into `./aimless-data/bin/` (a bind-mounted
volume), so they persist across restarts and you never rebuild the image for a
new aimless release. Which daemon gets fetched depends on `AIMLESS_TRANSPORT`
(below). Option B skips the download and uses your local `dist/` instead.

No extra steps needed: on first run the browser window shows a **create identity**
dialog (passphrase + confirm + screen name). Fill it in and you're set.

## Choosing the transport

`AIMLESS_TRANSPORT` selects which daemon the container fetches and runs:

| Value | Daemon fetched | State lives in |
| --- | --- | --- |
| `yggdrasil` (default) | `aimlessd-linux-amd64` | `aimless-data/daemon/` |
| `tailcat` | `aimlessd-tailcat-linux-amd64` | `aimless-data/tailcat/daemon/` |

```bash
# web desktop over tailcat (e.g. against your own DERP relay)
AIMLESS_TRANSPORT=tailcat AIMLESS_VERSION=tailcat-transport docker compose up -d --force-recreate

# headless tailcat daemon
AIMLESS_TRANSPORT=tailcat AIMLESS_VERSION=tailcat-transport docker compose -f docker-compose.daemon.yml up -d --force-recreate
```

The client pyz is the same for both. Each transport gets its **own identity,
contacts and history** (a separate `AIMLESS_HOME`), so switching never mixes
keys or addresses, and switching back finds your old identity untouched. In
daemon-only mode the tailcat API socket is
`aimless-data/tailcat/daemon/api.sock`.

Only the selected transport's daemon is downloaded. Fetches use a 30s read
timeout and are retried up to three times, so a stalled transfer fails loudly
instead of hanging startup.

## Updating to a new aimless release

No image rebuild - just fetch a newer client/daemon (and, when a new container
release exists, pull the fresh image):

```bash
# latest client+daemon on main, existing image
AIMLESS_FETCH=always docker compose up -d --force-recreate

# pull the newest published container, then latest artifacts
docker compose pull && AIMLESS_FETCH=always docker compose up -d --force-recreate

# or pin to a specific git ref (branch / commit sha)
AIMLESS_VERSION=v0.8.0 docker compose up -d --force-recreate
```

`AIMLESS_FETCH=always` forces a re-fetch on the next start; otherwise the
fetched artifacts are reused until the pinned `AIMLESS_VERSION` changes or you
delete them (`rm aimless-data/bin/*`). The `AIMLESS_VERSION` env is the seam a
future auto-updater can drive.

## Use

1. Open <http://localhost:8080/vnc.html> in a browser.
   Enter the noVNC password (`VNC_PASS`, default `aimless`).
2. First run: the **create identity** dialog appears - pick a passphrase and a
   screen name behind it.
   Later runs show the standard unlock prompt instead.
3. Add buddies in the **Contacts** tab (paste their `aimless1:…` invite).

Restart behaviour (there is no system tray in the container, so aimless runs in
plain window mode):

- **Cancel / Escape** on the identity dialog, and **closing the window**, both
  quit the app; supervisord restarts it within a second. You can never be stuck
  on a black screen - a dialog or window is always up.

Useful while the container runs:

```bash
docker exec -it aimless-webtop /opt/aimless/aimless.pyz list
docker exec -it aimless-webtop /opt/aimless/aimless.pyz away "bbl"
docker exec -it aimless-webtop /opt/aimless/aimless.pyz send <buddy> "hi"
```

## Persistence

Everything lives in `./aimless-data/` (mounted at `/data`, `AIMLESS_HOME=/data`),
split by owner:

```
client/   identity, contacts, cache, prefs, attachments
daemon/   node key, aimless.db, blocklist, the API socket, and config.json
logs/     app.log
bin/      fetched aimless.pyz + aimlessd
```

Custom peers go in `daemon/config.json`:

```json
{"peers": ["tcp://nodea:9001"], "listen": []}
```

Peer URI schemes and options, and the daemon flags: `docs/networking.md` in
the repo root.

> **Upgrading from an older image?** The volume layout changed; old
> `state/`/`config/` directories are ignored. Recreate the volume and restore
> from an encrypted backup via **Import backup…** on the first-run screen.

## Daemon-only mode (always-on)

Run the same image headless - no VNC, no ports - as an always-on daemon your
client reaches over SSH:

```bash
mkdir -p aimless-data
docker compose -f docker-compose.daemon.yml up -d
```

- Just `aimlessd -datadir /data/daemon`. The container runs as the owner of
  `./aimless-data` (detected at start), so the socket is owned by you - no need
  to pass uid/gid. (`mkdir -p aimless-data` first so it's yours, not root.)
- **Seed your node key** so your address is unchanged: copy your existing
  `daemon/node.key` into `./aimless-data/daemon/` (and optionally `aimless.db`
  for queued/inbox continuity). Optional peers live in
  `./aimless-data/daemon/config.json` → `{"peers": [...]}`.
- The daemon's API socket appears on the host at
  `./aimless-data/daemon/api.sock` (container `/data/daemon/api.sock`). On the
  client, add to `client/prefs.json`:

  ```json
  "remote": {
    "host": "user@this-host",
    "socket": "/abs/path/aimless-data/daemon/api.sock"
  }
  ```

  The client forwards that socket over SSH (streamlocal); the socket's `0600`
  permissions are the only auth - no TCP.
- Override with `AIMLESS_UID`/`AIMLESS_GID` only if the detected owner is wrong.
  Unix sockets in bind mounts work on native Linux Docker, not Docker Desktop.
- One client per daemon.

## Stop / remove

```bash
docker compose down
rm -rf aimless-data   # (from deploy/docker)
```

## Notes

- **Architecture**: the fetched daemon is the released `aimlessd-linux-amd64`
  (static, x86-64); the container is therefore x86-64 only. If you need arm64,
  the daemon is a trivial `CGO_ENABLED=0 go build` - see the repo `daemon/`.
- **Internet exposure**: a VPS on the public internet should sit behind a
  reverse proxy with TLS + basic auth (or SSH tunnel), since noVNC + the VNC
  password alone are thin protection for a remote host.