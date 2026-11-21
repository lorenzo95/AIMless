#!/bin/bash
# Build the container from THIS checkout and run it on THIS branch's code.
#
# The image is built locally (not pulled from GHCR), and the client pyz + daemon
# are seeded from ./dist - whatever you last built with deploy/package.sh -
# instead of being downloaded from main. Nothing is fetched at startup.
#
#   deploy/package.sh                        # build dist/ from this branch first
#   deploy/docker/build.sh                   # web desktop, yggdrasil
#   AIMLESS_TRANSPORT=tailcat deploy/docker/build.sh
#   deploy/docker/build.sh --daemon          # headless daemon instead
#   AIMLESS_TRANSPORT=tailcat deploy/docker/build.sh --daemon
set -e
cd "$(dirname "$0")"
ROOT="$(cd ../.. && pwd)"

TRANSPORT="${AIMLESS_TRANSPORT:-yggdrasil}"
case "$TRANSPORT" in
    tailcat)   DAEMON=aimlessd-tailcat-linux-amd64 ;;
    yggdrasil) DAEMON=aimlessd-linux-amd64 ;;
    *) echo "unknown AIMLESS_TRANSPORT '$TRANSPORT' (use yggdrasil or tailcat)" >&2; exit 1 ;;
esac

for f in aimless.pyz "$DAEMON"; do
    if [ ! -f "$ROOT/dist/$f" ]; then
        echo "dist/$f is missing - run deploy/package.sh first" >&2
        exit 1
    fi
done

# Seed the bind-mounted bin dir so the container runs this tree's artifacts.
mkdir -p aimless-data/bin
cp -f "$ROOT/dist/aimless.pyz" "$ROOT/dist/$DAEMON" aimless-data/bin/
chmod +x "aimless-data/bin/aimless.pyz" "aimless-data/bin/$DAEMON"
echo "seeded aimless-data/bin from this checkout's dist (transport: $TRANSPORT)"

COMPOSE=docker-compose.yml
if [ "${1:-}" = "--daemon" ]; then
    shift
    COMPOSE=docker-compose.daemon.yml
fi

# Build here, then point compose at the local image via AIMLESS_IMAGE so neither
# compose file pulls the published (main-based) one.
docker build -t aimless-webtop:local .
export AIMLESS_IMAGE=aimless-webtop:local
# never = use the seeded artifacts, do not download from main
export AIMLESS_FETCH=never
export AIMLESS_TRANSPORT="$TRANSPORT"

# --force-recreate: the artifacts are bind-mounted, so a recreated container is
# what actually picks them up. Without it compose sees an unchanged image tag
# and config, leaves the old processes running, and the rebuild silently does
# nothing.
docker compose -f "$COMPOSE" up -d --force-recreate "$@"
