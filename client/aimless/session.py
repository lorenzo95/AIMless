"""Daemon process supervision and the client session.

Extracted from gtkui.py: pid helpers, DaemonSupervisor (spawn/ensure/restart the
daemon), Session (store + sync + client wiring) and stop_all. No GUI dependency.
"""
import os
import signal
import subprocess
import time

from . import crypto, paths, protocol, tunnel
from .daemon import Client, DaemonClient
from .service import Sync
from .store import Store
from .uibase import cache_path, contacts_path, daemon_binary, identity_path, sock_path


def read_pid(path):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except Exception:
        return None


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def daemon_pid_from_socket():
    d = None
    try:
        d = DaemonClient(sock_path())
        who = d.request("whoami", timeout=3)
        return int(who.get("pid", 0)) or None
    except Exception:
        return None
    finally:
        if d is not None:
            d.close()


def daemon_pid_from_procs():
    """Last-resort stop() fallback: an aimlessd process using our datadir
    (covers daemons still booting, whose API socket is not up yet)."""
    target = paths.daemon_dir()
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/cmdline", "rb") as f:
                    cmdline = f.read().decode(errors="replace").split("\x00")
            except Exception:
                continue
            name = os.path.basename(cmdline[0])
            if not name.startswith("aimlessd"):
                continue
            if "-datadir" in cmdline:
                if target in cmdline:
                    return int(entry)
            elif target == os.path.expanduser("~/.local/share/aimless/daemon"):
                return int(entry)
    except Exception:
        pass
    return None


class DaemonSupervisor:
    def __init__(self):
        self.remote = tunnel.remote_config()
        self.external = bool(self.remote)
        self.tunnel = tunnel.TunnelSupervisor(self.remote) if self.external else None
        self.datadir = None if self.external else paths.daemon_dir()
        self.sock = sock_path()
        self.child = None

    def binary(self):
        return daemon_binary()

    def is_running(self):
        try:
            DaemonClient(self.sock).close()
            return True
        except Exception:
            return False

    def _reachable(self):
        """A full handshake through the client socket (is_running only opens it)."""
        d = None
        try:
            d = DaemonClient(self.sock)
            d.request("whoami", timeout=3)
            return True
        except Exception:
            return False
        finally:
            if d is not None:
                try:
                    d.close()
                except Exception:
                    pass

    def _local_daemon_running(self):
        """True when a *local* daemon occupies <root>/daemon/api.sock, which would
        conflict with a remote daemon sharing our node key."""
        local = os.path.join(paths.daemon_dir(), "api.sock")
        if os.path.abspath(local) == os.path.abspath(self.sock):
            return False
        try:
            DaemonClient(local).close()
            return True
        except Exception:
            return False

    def tunnel_state(self):
        return self.tunnel.state() if self.tunnel is not None else None

    def status(self):
        d = None
        try:
            d = DaemonClient(self.sock)
            who = d.request("whoami", timeout=5)
            st = d.request("status", timeout=5)
            peers = st.get("peers", [])
            return {
                "address": who.get("address", ""),
                "pubkey": who.get("key", ""),
                "peers_up": sum(1 for p in peers if p.get("up")),
                "peers_total": len(peers),
                "peer_list": st.get("peer_list") or [],
                "peer_details": peers,
                "default_peers": st.get("default_peers") or [],
                "build": st.get("build", ""),
                "mtu": st.get("mtu", 0),
                "transport": st.get("transport", "yggdrasil"),
                "relay": st.get("relay") or {},
                "region": st.get("region") or {},
                "regions": st.get("regions") or [],
            }
        except Exception:
            return None
        finally:
            if d is not None:
                d.close()

    def spawn(self):
        if self.external:
            raise RuntimeError("external daemon mode - not spawning a local aimlessd")
        binary = self.binary()
        if not binary:
            raise RuntimeError(
                "aimlessd not found - put aimlessd-linux-amd64 (or aimlessd) next to "
                "aimless.pyz, or add it to PATH")
        try:
            os.makedirs(self.datadir, exist_ok=True)
            os.makedirs(paths.logs_dir(), exist_ok=True)
            daemon_log = open(paths.daemon_log_path(), "ab")
        except OSError:
            daemon_log = subprocess.DEVNULL
        self.child = subprocess.Popen(
            [binary, "-datadir", self.datadir, "-api", self.sock],
            start_new_session=True,
            stdout=daemon_log,
            stderr=daemon_log,
        )
        if daemon_log is not subprocess.DEVNULL:
            daemon_log.close()  # the child inherited its own copy
        try:
            os.makedirs(paths.run_dir(), exist_ok=True)
            with open(paths.daemon_pid_path(), "w") as f:
                f.write(str(self.child.pid))
        except Exception:
            pass
        return self.child.pid

    def ensure(self, log=None):
        if self.external:
            if self._local_daemon_running():
                raise RuntimeError(
                    "a local aimlessd is running at "
                    + os.path.join(paths.daemon_dir(), "api.sock")
                    + " - run `aimless stop` before using the remote daemon")
            if not self.tunnel.ensure(log=log):
                raise RuntimeError(
                    f"could not start the SSH tunnel to {self.tunnel.host}"
                    + (f" ({self.tunnel.last_error})" if self.tunnel.last_error else ""))
            deadline = time.time() + 10
            while time.time() < deadline:
                if self._reachable():
                    if log:
                        log(f"remote daemon ready ({self.tunnel.host})")
                    return True
                time.sleep(0.2)
            raise RuntimeError(
                f"remote daemon not reachable through the tunnel ({self.tunnel.host})")
        if self.is_running():
            return True
        if log:
            log("starting aimlessd …")
        self.spawn()
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.is_running():
                if log:
                    log("daemon running")
                return True
            if self.child and self.child.poll() is not None:
                raise RuntimeError("aimlessd exited immediately (check " + paths.root() + ")")
            time.sleep(0.2)
        raise RuntimeError("daemon did not come up within 20s")

    def stop(self):
        if self.external:
            if self.tunnel is not None:
                self.tunnel.stop()
            return
        pid = daemon_pid_from_socket()
        if pid is None:
            pid = read_pid(paths.daemon_pid_path())
        if pid is None and self.child:
            pid = self.child.pid
        if pid is None:
            pid = daemon_pid_from_procs()
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        deadline = time.time() + 5
        stopped = False
        while time.time() < deadline:
            if not self.is_running():
                stopped = True
                break
            time.sleep(0.2)
        if not stopped and pid:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            time.sleep(0.5)
        try:
            os.remove(paths.daemon_pid_path())
        except OSError:
            pass


class Session:
    def __init__(self, passphrase):
        self.identity = crypto.load_identity(identity_path(), passphrase)
        self.cache_recovered = None
        try:
            self.store = Store(cache_path(), passphrase)
        except Exception as e:
            try:
                os.replace(cache_path(), cache_path() + ".bad")
            except OSError:
                pass
            self.cache_recovered = f"corrupted state recovered as .bad ({e})"
            self.store = Store(cache_path(), passphrase)
        # Transitional alias: the GTK layer still speaks the cache vocabulary.
        self.cache = self.store
        self.daemon = DaemonClient(sock_path())
        self.self_node = self.daemon.request("whoami")["key"]
        contacts = protocol.load_contacts(contacts_path())
        for info in contacts.values():
            node = info.get("node")
            if node and self.store.is_muted(node):
                self.store.unmute(node)
        self.self_screen = contacts.get("_self", {}).get("screen", "anonymous")
        self.client = Client(self.daemon, self.identity, self.self_screen)
        self.sync = Sync(self.client, self.store)
        self.pubkey_hex = self.client.pubkey_hex

    def contacts(self):
        allc = protocol.load_contacts(contacts_path())
        return {k: v for k, v in allc.items() if k != "_self"}

    def save_contacts(self, contacts, self_info):
        contacts["_self"] = self_info
        protocol.save_contacts(contacts_path(), contacts)

    def my_invite(self):
        who = self.client.whoami()
        return protocol.make_invite(self.identity, who["key"], self.self_screen, addr=who.get("address"))

    def refresh_node(self):
        """Re-read the daemon's node key after a restart (address may change)."""
        self.self_node = self.daemon.request("whoami")["key"]
        return self.self_node


def stop_all():
    stopped = []
    pid = read_pid(paths.app_pid_path())
    if pid and pid != os.getpid() and pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            stopped.append(f"app (pid {pid})")
        except OSError:
            pass
        deadline = time.time() + 10
        while time.time() < deadline and pid_alive(pid):
            time.sleep(0.1)
    legacy_sup = read_pid(os.path.join(paths.run_dir(), "tray.pid"))
    if legacy_sup and legacy_sup != pid and pid_alive(legacy_sup):
        try:
            os.kill(legacy_sup, signal.SIGTERM)
            stopped.append(f"tray supervisor (pid {legacy_sup})")
        except OSError:
            pass
    for legacy in ("tray.pid", "gui.pid", "session.json"):
        try:
            os.remove(os.path.join(paths.run_dir(), legacy))
        except OSError:
            pass
    supervisor = DaemonSupervisor()
    if supervisor.external:
        supervisor.stop()
        stopped.append("remote tunnel")
    else:
        if supervisor.is_running():
            supervisor.stop()
            stopped.append("aimlessd")
        subprocess.run(["pkill", "-x", "aimlessd"], capture_output=True)
    subprocess.run(["pkill", "-f", "aimless.cli gui"], capture_output=True)
    subprocess.run(["pkill", "-f", "aimless.cli tray"], capture_output=True)
    return stopped
