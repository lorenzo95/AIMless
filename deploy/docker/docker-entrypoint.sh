#!/bin/sh
# Fix up the bind-mounted /data volume (host dirs are initially root-owned),
# fetch the per-release aimless artifacts if needed, then drop to the non-root
# user and either run the webtop stack (default) or a headless daemon-only
# container (AIMLESS_MODE=daemon).
set -e

mkdir -p /data/logs /data/daemon /data/bin

VERSION="${AIMLESS_VERSION:-main}"
BASE="https://raw.githubusercontent.com/lorenzo95/Aimless/$VERSION/dist"

fetch() {
    target="$1"
    url="$2"
    marker="$target.version"
    if [ "$AIMLESS_FETCH" = "never" ]; then
        # Local-build / offline mode: run what is already in /data/bin (build.sh
        # seeds it from the working tree's dist/) and never download.
        if [ ! -f "$target" ]; then
            echo "AIMLESS_FETCH=never but $(basename "$target") is missing in /data/bin" >&2
            exit 1
        fi
        return 0
    fi
    if [ "$AIMLESS_FETCH" = "always" ] \
       || [ ! -f "$target" ] \
       || [ ! -f "$marker" ] \
       || [ "$(cat "$marker" 2>/dev/null)" != "$VERSION" ]; then
        echo "fetching $(basename "$target") from $VERSION …"
        # -T bounds a stalled read so a hung transfer cannot block startup
        # forever; busybox wget has no retry flag, so loop here.
        n=0
        until wget -T 30 -qO "$target.tmp" "$url"; do
            n=$((n + 1))
            if [ "$n" -ge 3 ]; then
                echo "failed to fetch $url after $n attempts" >&2
                rm -f "$target.tmp"
                exit 1
            fi
            echo "retrying $(basename "$target") (attempt $((n + 1))/3) …" >&2
            sleep 2
        done
        mv -f "$target.tmp" "$target"
        printf '%s\n' "$VERSION" > "$marker"
        chmod +x "$target"
    fi
}

# Transport selection (Model A): one daemon binary per transport, each with its
# own AIMLESS_HOME so keys, contacts and addresses never mix. Yggdrasil keeps
# the historical /data layout; tailcat lives under /data/tailcat.
case "${AIMLESS_TRANSPORT:-yggdrasil}" in
    tailcat)
        DAEMON_ARTIFACT=aimlessd-tailcat-linux-amd64
        export AIMLESS_HOME=/data/tailcat
        ;;
    yggdrasil)
        DAEMON_ARTIFACT=aimlessd-linux-amd64
        export AIMLESS_HOME=/data
        ;;
    *)
        echo "unknown AIMLESS_TRANSPORT '$AIMLESS_TRANSPORT' (use yggdrasil or tailcat)" >&2
        exit 1
        ;;
esac
DAEMON_BIN="/data/bin/$DAEMON_ARTIFACT"
export AIMLESS_DAEMON_BIN="$DAEMON_BIN"
mkdir -p "$AIMLESS_HOME/daemon" "$AIMLESS_HOME/client" "$AIMLESS_HOME/logs"

# static daemon + client pyz - both fetched from the git dist artifacts. The
# pyz is transport-agnostic; only the daemon binary differs.
fetch /data/bin/aimless.pyz "$BASE/aimless.pyz"
fetch "$DAEMON_BIN"        "$BASE/$DAEMON_ARTIFACT"

if [ "$AIMLESS_MODE" = "daemon" ]; then
    # Daemon-only: no X/VNC/supervisord, no ports. The client reaches this
    # daemon's API socket over SSH by bind-mounting /data to the host.
    # Run as the owner of the bind-mounted /data (i.e. your host user) so the
    # socket is readable by your SSH login - no need to pass uid/gid.
    UID_RUN="${AIMLESS_UID:-$(stat -c '%u' /data 2>/dev/null || echo 1000)}"
    GID_RUN="${AIMLESS_GID:-$(stat -c '%g' /data 2>/dev/null || echo 1000)}"
    case "$UID_RUN" in ""|0) UID_RUN=1000 ;; esac
    case "$GID_RUN" in ""|0) GID_RUN=1000 ;; esac
    chown -R "$UID_RUN:$GID_RUN" /data
    echo "starting $DAEMON_ARTIFACT (daemon-only, transport=${AIMLESS_TRANSPORT:-yggdrasil}) as $UID_RUN:$GID_RUN"
    exec su-exec "$UID_RUN:$GID_RUN" "$DAEMON_BIN" -datadir "$AIMLESS_HOME/daemon"
fi

chown -R aimless:aimless /data
exec su-exec aimless:aimless /usr/bin/supervisord -n -c /etc/supervisord.conf