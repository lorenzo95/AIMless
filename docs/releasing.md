# Releasing AIMless

Two different things ship, and they are built in different places:

| What | Built by | Where it lands | Contains |
|---|---|---|---|
| **webtop image** | [`.github/workflows/ghcr.yml`](../.github/workflows/ghcr.yml) on push to `main` and on `v*` tags | `ghcr.io/lorenzo95/aimless/aimless-webtop` | Alpine + Xvfb/openbox/x11vnc/noVNC/supervisord/GTK. **No aimless code** |
| **`dist/aimless.pyz`** | [`deploy/package.sh`](../deploy/package.sh), by hand | committed to git | the client, transport-agnostic |
| **`dist/aimlessd-linux-amd64`** | `deploy/package.sh` | committed to git | Yggdrasil daemon |
| **`dist/aimlessd-tailcat-linux-amd64`** | `deploy/package.sh` (`-tags tailcat`) | committed to git | tailcat daemon |

**One image serves both transports.** The entrypoint reads `AIMLESS_TRANSPORT`
(`yggdrasil`, the default, or `tailcat`), fetches only that daemon plus the shared
pyz, and gives each transport its own `AIMLESS_HOME` (`/data` vs `/data/tailcat`)
so the identities never mix. There is no second image and no build matrix.

Because the image is only an environment, **a new aimless release does not need an
image rebuild** - it needs new `dist/` artifacts on the ref the container fetches
from. Tagging is still what produces the versioned image tags.

## Release checklist

1. **Bump the version in three places** (they must agree):
   - `client/aimless/__init__.py` -> `__version__`
   - `client/pyproject.toml` -> `version`
   - `daemon/api.go` -> `buildVersion = "aimlessd/X.Y.Z"`

   Raise `MIN_DAEMON_BUILD` in `client/aimless/__init__.py` only when the daemon
   API changed incompatibly.
2. **Build the artifacts**: `./deploy/package.sh` (needs Go on PATH). Produces the
   pyz and *both* daemons, plus `dist/aimless-dist.tar.gz` (gitignored).
3. **Gate**: `python3 deploy/check_dist.py` must print OK. It compares every
   module in HEAD's pyz against HEAD's client source byte-for-byte, checks the
   module sets match, and checks the version stamp in **both** daemons. Changed
   code with an unchanged version number is caught; so is a stale tailcat binary.
4. **Test**: `cd daemon && go test ./...` and `go test -tags tailcat ./...`;
   `cd client && pytest tests`; `python3 deploy/smoke.py`,
   `deploy/smoke_tailcat.py`, `deploy/smoke_tailcat_room.py`.
   [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs the gate, both
   daemon test sets and the client suite on pushes to `main`/`tailcat-transport`,
   on every pull request, and on demand (`workflow_dispatch`). The public-DERP
   smokes are manual (they need internet and are timing-sensitive).
5. **Commit source and `dist/` together.** Never commit a source change without
   repackaging - the gate exists precisely to catch that.
6. **Merge to `main`, then tag**: `git tag v0.9.23 && git push origin v0.9.23`.
   `ghcr.yml` publishes `0.9.23`, `0.9`, `latest` and `sha-<short>`.
7. **Verify the release** (below). Do not assume the publish worked.

## How users actually get the new code

The entrypoint downloads `dist/` artifacts into `/data/bin` on start, keyed by
`AIMLESS_VERSION` (default `main`), and records the ref in a `.version` marker.

> **Caveat worth knowing.** With the default `AIMLESS_FETCH=missing`, the marker
> stores the *ref name*. A container pinned to `main` writes `main` once and then
> matches forever - so it does **not** pick up new commits on `main`, and
> republishing `latest` changes nothing for it (the image has no aimless code).

Two ways to get updates, in order of preference:

```sh
# 1. pin a release: the marker no longer matches, so the artifacts are re-fetched
docker run -d ... -e AIMLESS_VERSION=v0.9.23 ghcr.io/lorenzo95/aimless/aimless-webtop:0.9.23

# 2. or track a moving ref and force a download on every start
docker run -d ... -e AIMLESS_VERSION=main -e AIMLESS_FETCH=always ghcr.io/lorenzo95/aimless/aimless-webtop:latest
```

`AIMLESS_FETCH=never` is the offline/local-build mode: run what is already in
`/data/bin`, download nothing, fail if it is missing.

**Rollback** is the same mechanism: point `AIMLESS_VERSION` at an older tag or
commit SHA and restart. The marker mismatch triggers a re-fetch of that release.

## Testing an unmerged branch in the container

```sh
deploy/package.sh                              # build this tree's dist/
AIMLESS_TRANSPORT=tailcat deploy/docker/build.sh   # local image, seeded artifacts
```

`build.sh` builds `aimless-webtop:local`, seeds `aimless-data/bin/` from your
working tree's `dist/`, and starts with `AIMLESS_FETCH=never`, so the container
runs exactly what you just built - not whatever is on `main`. `--daemon` gives the
headless variant. See [`deploy/docker/README.md`](../deploy/docker/README.md).

## Verifying a published release

```sh
# the image tags exist
docker pull ghcr.io/lorenzo95/aimless/aimless-webtop:0.9.23

# the served artifacts are the ones you committed
sha256sum dist/aimless.pyz dist/aimlessd-tailcat-linux-amd64
curl -sS https://raw.githubusercontent.com/lorenzo95/Aimless/v0.9.23/dist/aimless.pyz | sha256sum

# a pinned container really fetches and runs that release
docker run -d --name aimless-release-check -p 127.0.0.1:8083:8080 \
  -e AIMLESS_VERSION=v0.9.23 -e AIMLESS_TRANSPORT=tailcat \
  -v "$PWD/release-check-data:/data" \
  ghcr.io/lorenzo95/aimless/aimless-webtop:0.9.23
docker logs aimless-release-check        # "fetching ... from v0.9.23", then supervisord
```

Open <http://localhost:8083/vnc.html> and check *Keys & Identity* / the relay
panel. Remove the container and its volume when done.

## Limitations

- **One known load-sensitive test.** `test_gtk.py::test_gui_fetched_transfer_sweep`
  occasionally fails on a busy machine and passes in isolation; the daemon's
  `TestTailcatReconnect` has the same character. If CI goes red on *only* one of
  those, re-run the job - a real regression fails consistently. Everything else
  in the suite is deterministic.

- **The tailcat integration tests need a real network stack.** They start a local
  DERP + STUN and drive magicsock. On a VM (or `docker run --network host`) they
  pass in ~1.5-2.5s; inside a default docker bridge - no IPv6, restricted UDP
  buffer sizes - `TestTailcatAddrPersistence` fails deterministically after a
  sender restart, and raising its timeout does not help. If you reproduce CI in a
  container, use host networking.

- **x86-64 only.** The published artifacts are built on amd64 and the image is
  built for the runner's architecture, so on arm64 (Apple Silicon, Raspberry Pi)
  the fetched daemon will not execute. Workaround: build natively with
  `deploy/package.sh` + `deploy/docker/build.sh`. Note that `package.sh` builds
  for the *host* architecture while the artifact names still say `-linux-amd64` -
  the names are labels, not a promise. Proper arm64 releases would need a second
  Go build, arch-aware artifact names, an arch-aware entrypoint and
  `platforms: linux/amd64,linux/arm64` in `ghcr.yml`.
- The headless daemon image ([`deploy/Dockerfile`](../deploy/Dockerfile), one
  binary per image via the `TRANSPORT` build arg) is **not published**; build it
  yourself if you want it.
- Binary bytes are not reproducible across Go toolchains, so the gate checks the
  version stamp rather than a rebuild hash. Build releases with the toolchain
  pinned in `daemon/go.mod`.
