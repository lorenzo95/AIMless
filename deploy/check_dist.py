#!/usr/bin/env python3
"""Release gate: the committed dist artifacts must match the committed source.

Run after `deploy/package.sh` and after committing. Everything is compared
against HEAD, not the working tree, so this checks exactly what a fresh wget
from raw.githubusercontent.com serves:

  * every module inside dist/aimless.pyz is byte-identical to its source in
    client/aimless/, and the module sets match - so changed code with an
    unchanged version number is caught, and so is a new module that never made
    it into the zipapp;
  * the pyz reports the source __version__;
  * BOTH daemon binaries (yggdrasil and tailcat) carry the source buildVersion.
    We ship two daemons from one image, so a stale tailcat artifact is just as
    broken as a stale yggdrasil one.

Binary bytes are not compared against a rebuild: that would need the exact
toolchain that produced them. The version stamp plus the pyz content check is
what catches the mistakes people actually make (forgetting to repackage).
"""
import io
import os
import re
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAEMON_ARTIFACTS = ("dist/aimlessd-linux-amd64", "dist/aimlessd-tailcat-linux-amd64")


def fail(msg):
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def git(*args):
    out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True)
    if out.returncode != 0:
        fail(f"git {' '.join(args)}: {out.stderr.decode(errors='replace').strip()}")
    return out.stdout


def head_blob(path):
    return git("show", f"HEAD:{path}")


def head_modules():
    """The client modules committed at HEAD, as zipapp-relative paths."""
    listing = git("ls-tree", "-r", "--name-only", "HEAD", "client/aimless").decode()
    return {p[len("client/"):] for p in listing.splitlines() if p.endswith(".py")}


src = open(os.path.join(ROOT, "client/aimless/__init__.py")).read()
m = re.search(r'__version__ = "([^"]+)"', src)
client_ver = m and m.group(1)
src = open(os.path.join(ROOT, "daemon/api.go")).read()
m = re.search(r'buildVersion = "aimlessd/([^"]+)"', src)
daemon_ver = m and m.group(1)

if not client_ver or not daemon_ver:
    fail("could not parse source versions")

pyz = head_blob("dist/aimless.pyz")
z = zipfile.ZipFile(io.BytesIO(pyz))
packed = {n for n in z.namelist() if n.startswith("aimless/") and n.endswith(".py")}
expected = head_modules()

missing = sorted(expected - packed)
extra = sorted(packed - expected)
if missing:
    fail(f"dist/aimless.pyz is missing {len(missing)} module(s): {', '.join(missing)}"
         " - rerun deploy/package.sh and commit")
if extra:
    fail(f"dist/aimless.pyz carries {len(extra)} module(s) not in HEAD: {', '.join(extra)}"
         " - rerun deploy/package.sh and commit")

stale = [n for n in sorted(packed) if z.read(n) != head_blob("client/" + n)]
if stale:
    fail(f"dist/aimless.pyz differs from HEAD in {len(stale)} module(s): {', '.join(stale)}"
         " - rerun deploy/package.sh and commit")

init = z.read("aimless/__init__.py").decode()
m = re.search(r'__version__ = "([^"]+)"', init)
pyz_ver = m and m.group(1)
if pyz_ver != client_ver:
    fail(f"dist/aimless.pyz is {pyz_ver}, source is {client_ver} - rerun deploy/package.sh and commit")

for artifact in DAEMON_ARTIFACTS:
    blob = head_blob(artifact)
    if f"aimlessd/{daemon_ver}".encode() not in blob:
        fail(f"{artifact} is not {daemon_ver} - rerun deploy/package.sh and commit")

print(f"OK: dist artifacts match source (client {client_ver}, daemon {daemon_ver}; "
      f"{len(packed)} modules byte-identical, both daemons stamped)")
