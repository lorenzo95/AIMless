#!/bin/bash
# aimless app. Waits for X, then launches the GUI in the foreground so supervisord
# restarts it whenever the window is closed (no tray in the container, so
# closing the window quits the app - close-to-tray is just a desktop behaviour).
export DISPLAY=:1
export HOME=/home/aimless
# The entrypoint resolves AIMLESS_TRANSPORT into AIMLESS_HOME + AIMLESS_DAEMON_BIN
# (a separate home per transport, so the two identities never mix). Honour those;
# fall back to the same resolution when this script is run directly.
case "${AIMLESS_TRANSPORT:-yggdrasil}" in
    tailcat) : "${AIMLESS_HOME:=/data/tailcat}"
             : "${AIMLESS_DAEMON_BIN:=/data/bin/aimlessd-tailcat-linux-amd64}" ;;
    *)       : "${AIMLESS_HOME:=/data}"
             : "${AIMLESS_DAEMON_BIN:=/data/bin/aimlessd-linux-amd64}" ;;
esac
export AIMLESS_HOME AIMLESS_DAEMON_BIN

for i in $(seq 1 60); do
    [ -S /tmp/.X11-unix/X1 ] && break
    sleep 1
done
sleep 2   # let openbox take the root window first

cd /data/bin
exec /data/bin/aimless.pyz gui