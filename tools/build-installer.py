#!/usr/bin/env python3
"""Build a copy of the shop that installs on any computer, with nothing else installed first.

The book, the screens and the engine are one Python file and one folder already — `run.sh` proves it
runs on this Mac with a double-click. What it does not prove is that it runs on *another* Mac, or on a
Windows PC, or on a Linux box, because `run.sh` asks that machine for Python 3 and most of them have
none: macOS stopped shipping a usable `python3` until somebody installs the command line tools, and
Windows ships no Python at all. So the runtime has to travel with the app.

It can. python-build-standalone publishes relocatable CPython folders — the same interpreter, unpacked
wherever the folder is put, no installer, no registry, no admin rights — and the engine is standard
library only, so nothing else needs to travel. This tool downloads one per machine kind, puts the shop
beside it, and writes a launcher that points the book **inside the folder** instead of
`~/Library/Application Support`. That last choice is the whole product: a copy of Chrisphics Hub that
owns its records can be handed to a second computer, dragged to a USB stick, or deleted by dragging it
to the bin, and it never reads or writes the shop's live book by accident.

Each bundle is checked as it is built — every app file compared byte for byte with this repository, the
interpreter found where the launcher will look for it, and the Windows launcher verified to name the
path separators Windows needs. What cannot be checked from a Mac is whether a `.exe` starts, so this
tool says which bundles it proved by running and which it only proved by shape.

Usage:
    python3 tools/build-installer.py                  # every machine kind
    python3 tools/build-installer.py mac-arm64 win-x64
    python3 tools/build-installer.py --clean          # throw the folders away, keep the downloads
"""
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(REPO, "runtime-cache")
DIST = os.path.join(REPO, "dist")

# One release of one interpreter, chosen for the widest set of machines rather than for the newest
# number: the engine is stdlib-only and this is the release this shop's own book has been migrated on.
PYTHON = "3.12.15"
TAG = "20261021"
BASE = "https://github.com/astral-sh/python-build-standalone/releases/download/%s/" % TAG

# The app is these files and nothing else. `tools/` stays behind on purpose: it is dev checks and the
# launchd service, and a shop computer that owns its own book needs neither.
APP_FILES = ("server.py", "schema.sql")
APP_DIRS = ("public",)

TARGETS = {
    "mac-arm64": {
        "asset": "cpython-%s+%s-aarch64-apple-darwin-install_only.tar.gz" % (PYTHON, TAG),
        "folder": "Chrisphics Hub for Mac (Apple silicon)",
        "zip": "Chrisphics-Hub-Mac-Apple-silicon.zip",
        "python": "runtime/bin/python3",
        "launcher": "Start Chrisphics Hub.command",
        "kind": "sh",
        "runs_here": True,
    },
    "mac-x64": {
        "asset": "cpython-%s+%s-x86_64-apple-darwin-install_only.tar.gz" % (PYTHON, TAG),
        "folder": "Chrisphics Hub for Mac (Intel)",
        "zip": "Chrisphics-Hub-Mac-Intel.zip",
        "python": "runtime/bin/python3",
        "launcher": "Start Chrisphics Hub.command",
        "kind": "sh",
        "runs_here": False,
    },
    "win-x64": {
        "asset": "cpython-%s+%s-x86_64-pc-windows-msvc-install_only.tar.gz" % (PYTHON, TAG),
        "folder": "Chrisphics Hub for Windows",
        "zip": "Chrisphics-Hub-Windows.zip",
        "python": "runtime/python.exe",
        "launcher": "Start Chrisphics Hub.bat",
        "kind": "bat",
        "runs_here": False,
    },
    "linux-x64": {
        "asset": "cpython-%s+%s-x86_64-unknown-linux-gnu-install_only.tar.gz" % (PYTHON, TAG),
        "folder": "Chrisphics Hub for Linux",
        "zip": "Chrisphics-Hub-Linux.zip",
        "python": "runtime/bin/python3",
        "launcher": "Start Chrisphics Hub.sh",
        "kind": "sh",
        "runs_here": False,
    },
}

SCRIPT_SH = """#!/bin/bash
# Open Chrisphics Hub on this computer. Its records live in the "data" folder beside this file.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${CHRISPHICS_PORT:-8712}"
URL="http://127.0.0.1:${PORT}/"
if command -v curl >/dev/null && curl -s -o /dev/null --max-time 2 "${URL}healthz"; then
  echo "Chrisphics Hub is already open on this computer: ${URL}"
else
  echo "Opening Chrisphics Hub at ${URL}"
  echo "Records: ${HERE}/data/shop.db"
  echo "Leave this window open while you work; Ctrl+C closes the shop."
fi
export CHRISPHICS_SUPPORT="${HERE}/data"
# Naming the book, rather than letting the engine reach its default, is what keeps this copy its own
# shop: the engine only ever looks for an older book sitting in ~/Library when it is asked for the
# file it would have chosen itself. On this Mac that older book is the shop's, and a portable copy
# that opened it would show one print shop's records on another computer's screens.
export CHRISPHICS_DB="${HERE}/data/shop.db"
exec "${HERE}/%(python)s" "${HERE}/app/server.py" --port "${PORT}" "$@"
"""

SCRIPT_BAT = """@echo off
rem Open Chrisphics Hub on this computer. Its records live in the "data" folder beside this file.
setlocal
set HERE=%%~dp0
if "%%CHRISPHICS_PORT%%"=="" set CHRISPHICS_PORT=8712
echo Opening Chrisphics Hub at http://127.0.0.1:%%CHRISPHICS_PORT%%/
echo Records: %%HERE%%data\\shop.db
echo Leave this window open while you work; close it to stop the shop.
rem Naming the book keeps this copy its own shop — see the same line in the Mac launcher.
set CHRISPHICS_SUPPORT=%%HERE%%data
set CHRISPHICS_DB=%%HERE%%data\\shop.db
"%%HERE%%%(python)s" "%%HERE%%app\\server.py" --port %%CHRISPHICS_PORT%% %%*
"""

INSTALL_NOTE = """CHRISPPRINT GHANA — CHRISPHICS HUB
A copy of the shop's records book that runs on this computer alone.

START IT
    Double-click "%(launcher)s". A window opens; the shop is then at
    http://127.0.0.1:8712/ in your browser. Keep that window open while you work —
    closing it closes the shop. It opens its own browser page; if none appears, type
    that address into Chrome, Edge, Safari or Firefox yourself.

YOUR RECORDS
    Everything this computer books lives in the "data" folder inside this folder
    ("data/shop.db"). Nothing here reads or writes any other computer's book.
    Back the shop up by copying this folder. To move it to another computer, copy
    this folder and start it there — the records come with it.

MAKE IT FEEL LIKE AN APP
    In the browser, use Install / Add to Dock / Add to Home screen. The screens then
    open from their own icon, and the last figures you were shown still paint when
    this computer is off the network.

IF macOS REFUSES TO OPEN IT
    The folder was downloaded, so macOS wants to check it first and cannot, because it
    is not from the App Store. One time, in Terminal:
        xattr -dr com.apple.quarantine "<this folder>"
    Then double-click "%(launcher)s" again.

IF PORT 8712 IS TAKEN
    Start it on another number instead:
        %(port_hint)s

THE SHOP'S OWN MACHINE
    This is not the shop's Wi-Fi server. It answers only on this computer, and no
    password is asked for because nothing outside this computer can reach it. The
    counter's always-on server on the shop Mac is set up separately (README.md).
"""

fails = []
checks = 0


def note(label, ok, detail=""):
    global checks
    checks += 1
    print("%-56s %s %s" % (label, "ok" if ok else "FAIL", detail))
    if not ok:
        fails.append(label)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rows_in(path):
    """How much the fresh book actually holds — read straight out of the file, because the one
    failure this check exists to catch is a portable copy quietly filling itself with the shop's
    own records."""
    try:
        book = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    except sqlite3.Error:
        return None
    try:
        return {table: book.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
                for table in ("jobs", "clients", "payments")}
    except sqlite3.Error:
        return None
    finally:
        book.close()


def download(asset):
    """Fetch one runtime once and keep it; a rebuild should not cost another 30 MB."""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, asset)
    if os.path.exists(path) and open(path, "rb").read(2) == b"\x1f\x8b":
        return path, os.path.getsize(path)
    url = BASE + asset
    print("    downloading %s" % url)
    with urllib.request.urlopen(url, timeout=600) as response, open(path + ".part", "wb") as fh:
        shutil.copyfileobj(response, fh)
    os.replace(path + ".part", path)
    return path, os.path.getsize(path)


def app_tree():
    """Every file that belongs in the bundle, keyed by its path inside the bundle.

    The engine resolves `public/` and `schema.sql` beside its own file (`ROOT` in server.py), so all
    three live together under `app/` — a bundle that put the screens next to the launcher instead
    would start and answer every API route while serving a 404 for the dashboard.
    """
    wanted = {}
    for name in APP_FILES:
        wanted[os.path.join("app", name)] = os.path.join(REPO, name)
    for folder in APP_DIRS:
        for base, _, names in os.walk(os.path.join(REPO, folder)):
            for name in names:
                if name == ".DS_Store":
                    continue
                source = os.path.join(base, name)
                wanted[os.path.join("app", os.path.relpath(source, REPO))] = source
    return wanted


def build(key):
    target = TARGETS[key]
    bundle = os.path.join(DIST, target["folder"])
    print("\n== %s" % target["folder"])
    shutil.rmtree(bundle, ignore_errors=True)
    os.makedirs(bundle, exist_ok=True)

    archive, size = download(target["asset"])
    note("%s runtime downloaded" % key, size > 1_000_000, "%.1f MB" % (size / 1048576.0))

    # `tar` rather than Python's tarfile: these runtimes carry symlinks and executable bits, and a
    # bundle whose interpreter lost its +x is a folder that looks complete and does nothing.
    subprocess.run(["tar", "-xzf", archive, "-C", bundle], check=True)
    moved = os.path.join(bundle, "python")
    if os.path.isdir(moved):
        os.rename(moved, os.path.join(bundle, "runtime"))

    wanted = app_tree()
    for relative, source in sorted(wanted.items()):
        destination = os.path.join(bundle, relative)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        shutil.copy2(source, destination)
    note("every app file copied into the bundle", len(wanted) >= 10, "%d files" % len(wanted))

    launcher = target["launcher"]
    body = SCRIPT_BAT if target["kind"] == "bat" else SCRIPT_SH
    # The paths above are written with `/` because this tool runs on a Mac; the launcher a PC double-
    # clicks has to name its own separators, so translate the one line that matters.
    python_in_launcher = target["python"].replace("/", "\\") if target["kind"] == "bat" \
        else target["python"]
    with open(os.path.join(bundle, launcher), "w", newline="\n") as fh:
        fh.write(body % {"python": python_in_launcher})
    if target["kind"] == "sh":
        os.chmod(os.path.join(bundle, launcher), 0o755)
    launcher_text = open(os.path.join(bundle, launcher)).read()
    note("the launcher names the runtime inside the bundle", python_in_launcher in launcher_text,
         launcher)
    if target["kind"] == "bat":
        note("  and names it the way Windows reads a path", "\\" in python_in_launcher
             and "runtime/python.exe" not in launcher_text, "backslashes")

    # The engine reads its book's home from CHRISPHICS_SUPPORT, which both launchers set to the "data"
    # folder beside them; creating it here means a first run never fails on a missing directory.
    os.makedirs(os.path.join(bundle, "data"), exist_ok=True)
    with open(os.path.join(bundle, "README.txt"), "w", newline="\n") as fh:
        fh.write(INSTALL_NOTE % {"launcher": launcher,
                                 "port_hint": ('CHRISPHICS_PORT=8801 "./%s"' % launcher
                                               if target["kind"] == "sh" else
                                               'set CHRISPHICS_PORT=8801 && "%s"' % launcher)})

    interpreter = os.path.join(bundle, *target["python"].split("/"))
    note("the interpreter the launcher calls is there", os.path.exists(interpreter), target["python"])
    if target["kind"] == "sh":
        note("  and it is executable", os.access(interpreter, os.X_OK), "")

    # The engine imports sqlite3, http.server, ssl and smtplib; a runtime that lost part of its
    # standard library unpacks to a folder that looks complete and dies on the first request. The
    # stdlib sits in "Lib" on Windows and "lib/python3.12" elsewhere — and on a Mac's case-insensitive
    # disk "Lib" answers for "lib" too, so a folder is only the stdlib if it holds `encodings`.
    roots = [os.path.join(bundle, "runtime", leaf) for leaf in
             ("Lib", os.path.join("lib", "python" + PYTHON.rsplit(".", 1)[0]))]
    stdlib = next((r for r in roots if os.path.exists(os.path.join(r, "encodings", "__init__.py"))),
                  None)
    missing = [m for m in ("sqlite3/__init__.py", "http/server.py", "ssl.py", "smtplib.py",
                           "json/__init__.py")
               if stdlib is None or not os.path.exists(os.path.join(stdlib, *m.split("/")))]
    note("  and it carries the library the engine imports", stdlib is not None and not missing,
         "%s%s" % (os.path.relpath(stdlib, bundle) if stdlib else "no stdlib folder",
                   (" — missing " + ", ".join(missing)) if missing else ""))
    if target["kind"] == "bat":
        dlls = os.path.join(bundle, "runtime", "DLLs")
        binaries = [n for n in (os.listdir(dlls) if os.path.isdir(dlls) else [])
                    if n.startswith(("_sqlite3", "_ssl", "libssl", "libcrypto", "sqlite3"))]
        note("  and the compiled half Windows needs (sqlite3, ssl) is there",
             any(n.startswith("_sqlite3") for n in binaries)
             and any(n.startswith("_ssl") for n in binaries), ", ".join(sorted(binaries)))

    # A relocatable CPython on Windows can run in "isolated" mode, where the folder holding the
    # script is not put on the search path; the tell is a `python._pth` file beside the exe. Name
    # the app folder in it when it exists, and say plainly when it does not (the install_only
    # archives mostly do not ship one, which is the simpler case: sys.path then follows the script).
    pth = [n for n in sorted(os.listdir(os.path.join(bundle, "runtime"))) if n.endswith("._pth")] \
        if os.path.isdir(os.path.join(bundle, "runtime")) else []
    for name in pth:
        path = os.path.join(bundle, "runtime", name)
        lines = [ln for ln in open(path).read().splitlines() if ln.strip()]
        if not any("app" in ln for ln in lines):
            lines.append("..\\app")
            with open(path, "w", newline="\r\n") as fh:
                fh.write("\n".join(lines) + "\n")
    note("a Windows path file, if the runtime has one, names the app folder",
         all("app" in open(os.path.join(bundle, "runtime", n)).read() for n in pth),
         ", ".join(pth) or "none in this runtime")

    digest = hashlib.sha256()
    for relative, source in sorted(wanted.items()):
        digest.update(relative.encode("utf-8"))
        digest.update(open(source, "rb").read())
    with open(os.path.join(bundle, "app.sha256"), "w") as fh:
        fh.write("%s  %d files  python %s+%s\n" % (digest.hexdigest(), len(wanted), PYTHON, TAG))

    zip_path = os.path.join(DIST, target["zip"])
    if os.path.exists(zip_path):
        os.remove(zip_path)
    parent = os.path.dirname(bundle)
    subprocess.run(["zip", "-qryX", zip_path, os.path.basename(bundle)], cwd=parent, check=True)
    note("zipped for sending", os.path.getsize(zip_path) > 1_000_000,
         "%.1f MB" % (os.path.getsize(zip_path) / 1048576.0))
    names = zipfile.ZipFile(zip_path).namelist()
    note("  and the zip carries the launcher, the engine and the screens",
         any(n.endswith(launcher) for n in names)
         and any(n.endswith("app/server.py") for n in names)
         and any(n.endswith("app/public/app.js") for n in names)
         and any(n.endswith("app/schema.sql") for n in names),
         "%d entries" % len(names))
    return bundle


def run_bundle(key, bundle, port, prefix=(), says=None):
    """Start a bundle the way its own launcher does, ask it for the shop, and read the book it made.
    `prefix` runs it through Rosetta when the bundle is for the other kind of Mac chip.
    """
    target = TARGETS[key]
    folder = says or target["folder"]
    launcher_path = os.path.join(bundle, target["launcher"])
    book = os.path.join(bundle, "data", "shop.db")
    legacy = os.path.expanduser("~/Library/Application Support/Chriphics Hub/chriphics.db")
    legacy_before = sha256_file(legacy) if os.path.exists(legacy) else None
    if subprocess.run(["lsof", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN"],
                      capture_output=True).stdout.strip():
        note("%s: a spare port to prove it on" % folder, False, "%d is held" % port)
        return
    # The bundle ships an empty "data" folder; take it away so this run starts the way a machine
    # that has never seen the shop does.
    shutil.rmtree(os.path.join(bundle, "data"), ignore_errors=True)
    child = subprocess.Popen(list(prefix) + ["bash", launcher_path, "--no-browser",
                                             "--port", str(port)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    code = ""
    for _ in range(60):
        time.sleep(0.5)
        code = subprocess.run(["curl", "-s", "--max-time", "2",
                               "http://127.0.0.1:%d/healthz" % port],
                              capture_output=True, text=True).stdout
        if code:
            break
    note("%s: runs on its own bundled Python and answers" % folder, '"ok"' in code,
         code[:58])
    page = subprocess.run(["curl", "-s", "--max-time", "3", "http://127.0.0.1:%d/" % port],
                          capture_output=True, text=True).stdout
    note("  and the dashboard it hands over is the shop's", "Chrisphics Hub" in page,
         "%d bytes" % len(page))
    boot = subprocess.run(["curl", "-s", "--max-time", "3",
                           "http://127.0.0.1:%d/api/bootstrap" % port],
                          capture_output=True, text=True).stdout
    note("  the API answers on the same open door", '"shop"' in boot or '"clients"' in boot,
         "%d bytes" % len(boot))
    sheet = subprocess.run(["curl", "-s", "--max-time", "3",
                            "http://127.0.0.1:%d/app.js" % port],
                           capture_output=True, text=True).stdout
    note("  the screens come from inside the bundle too", "paintNetworkStatus" in sheet,
         "%d bytes" % len(sheet))
    child.terminate()
    try:
        child.wait(timeout=15)
    except subprocess.TimeoutExpired:
        child.kill()
    note("  the book it made is inside the bundle", os.path.exists(book), "data/shop.db")
    rows = rows_in(book) if os.path.exists(book) else None
    note("  and it is an empty book, not this Mac's shop records",
         rows == {"jobs": 0, "clients": 0, "payments": 0},
         "no book" if rows is None else str(rows))
    if legacy_before is not None:
        note("  the shop's own old book was never read", sha256_file(legacy) == legacy_before,
             os.path.basename(os.path.dirname(legacy)))
    shutil.rmtree(os.path.join(bundle, "data"), ignore_errors=True)
    os.makedirs(os.path.join(bundle, "data"), exist_ok=True)


def prove(key, bundle, port, prefix=()):
    run_bundle(key, bundle, port, prefix)
    # A folder that runs where it was built is not yet something you can send. Unpack the zip a
    # second computer will receive into a path this repository has never seen and run it from there:
    # that is the test which catches a bundle whose interpreter arrived as a broken symlink or
    # without its executable bit, both of which a zip can quietly do.
    work = tempfile.mkdtemp(prefix="chrisphics-install-")
    try:
        # The folder inside the zip is renamed on arrival, because the name a person gives it is
        # theirs, not ours.
        subprocess.run(["unzip", "-q", "-o", os.path.join(DIST, TARGETS[key]["zip"]), "-d", work],
                       check=True)
        landed = os.path.join(work, "Chrisphics Hub")
        os.rename(os.path.join(work, TARGETS[key]["folder"]), landed)
        interpreter = os.path.join(landed, *TARGETS[key]["python"].split("/"))
        note("  unpacked elsewhere, the interpreter survived the trip",
             os.path.exists(interpreter) and os.access(interpreter, os.X_OK),
             os.path.realpath(interpreter).replace(work, "…"))
        run_bundle(key, landed, port + 10, prefix,
                   says="%s (out of the zip)" % TARGETS[key]["folder"])
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    wanted = [a for a in sys.argv[1:] if not a.startswith("--")]
    unknown = [a for a in wanted if a not in TARGETS]
    if unknown:
        print("unknown machine kind: %s — pick from %s" % (", ".join(unknown), ", ".join(TARGETS)))
        return 2
    if "--clean" in sys.argv:
        shutil.rmtree(DIST, ignore_errors=True)
        print("removed dist/ (runtime-cache/ kept)")
        return 0
    keys = wanted or list(TARGETS)
    os.makedirs(DIST, exist_ok=True)
    built = {}
    for key in keys:
        built[key] = build(key)

    # A Mac bundle can be proved by running it: this machine's own chip natively, the other chip
    # through Rosetta when this Mac has it. Windows and Linux cannot be started from here, so the
    # report names which bundles were run and which were only ever checked by shape.
    native = other = None
    if sys.platform == "darwin":
        native = "mac-arm64" if os.uname().machine == "arm64" else "mac-x64"
        other = "mac-x64" if native == "mac-arm64" else "mac-arm64"
    elif sys.platform.startswith("linux"):
        native = "linux-x64"
    proved = []
    if not fails:
        port = 8798
        # The other Mac chip only runs here through Rosetta, and only an Apple silicon Mac has it:
        # a machine that cannot start x86 code says so and does not call the bundle broken.
        cross = []
        if other and shutil.which("arch") and subprocess.run(
                ["arch", "-x86_64", "/usr/bin/true"], capture_output=True).returncode == 0:
            cross = [(other, ("arch", "-x86_64"))]
        for key, prefix in [(native, ())] + cross:
            if key not in built:
                continue
            prove(key, built[key], port, prefix)
            proved.append(key)
            port += 1
        if other and other not in proved and other in built:
            print("%s: this Mac cannot start that chip's code, so it was checked by shape only"
                  % TARGETS[other]["folder"])

    print("\n%s" % ("BUILT: " + ", ".join(TARGETS[k]["folder"] for k in keys)))
    print("%d checks, %d failed" % (checks, len(fails)))
    for f in fails:
        print("  failed: %s" % f)
    if proved:
        print("proved by running: %s" % ", ".join(TARGETS[k]["folder"] for k in proved))
    rest = [k for k in keys if k not in proved]
    if rest:
        print("proved by shape only: %s" % ", ".join(TARGETS[k]["folder"] for k in rest))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
