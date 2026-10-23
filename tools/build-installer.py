#!/usr/bin/env python3
"""Build the shop as an application that installs on a Mac or a Windows PC with nothing else first.

The book, the screens and the engine are one Python file and one folder already, and `run.sh` proves
it runs on this Mac — but only on a Mac that already has Python 3, which most do not: macOS stopped
shipping a usable `python3`, and Windows ships none. So the runtime has to travel with the app, and
python-build-standalone publishes relocatable CPython folders — the same interpreter, unpacked
wherever the folder is put, no installer, no registry, no admin rights. The engine is standard
library only, so nothing else needs to travel with it.

What this tool then produces is not a folder to copy but the thing a person expects from an app:

    Mac      Chrisphics Hub.app — a real window with the shop in it, its own icon in the Dock,
                                   built for one chip or the other from tools/portable-app-shell.m.
                   "Install Chrisphics Hub.command" drops it in /Applications, and a .dmg to drag from
                   and a .pkg to double-click go with it.
    Windows  Install Chrisphics Hub.cmd → one PowerShell script copies the program into
                   %LOCALAPPDATA%\\Programs, puts an entry in the Start Menu and on the Desktop,
                   and registers itself in Settings → Apps, so it uninstalls like anything else.
    Linux    a folder and a shell launcher — no installer, and the report says so.

A Mac gets a window because nothing else about it can be an application: macOS stopped shipping a
usable `python3`, so the shop cannot be a script a person runs, and a browser tab is not what someone
means when they say "the app". The shell starts the bundled engine itself, waits for the book to open,
and closes it again on quit, so no orphan is left holding the port. Windows keeps a console window,
because that is how a Windows folder that needs no admin rights stays explainable — closing the window
closes the shop, and it is written on the window.

The whole point is that each install owns its records. They go in *this computer's* per-user folder
(`~/Library/Application Support/CRISPprint Hub`, `%LOCALAPPDATA%\\CRISPprint Hub`), never the shop's
`Chrisphics Hub` folder, and the launcher names the book file rather than letting the engine choose
it — because the engine only looks for an older book in `~/Library` when it is left to pick its own
path, and a portable copy that found the shop's old records would print one print shop's takings on
another company's screens. Two installs are two books: nothing merges them. That is the fork this
design chose. For one book several machines reach, there is the always-on shop server and the
offline-carry devices instead.

Owning a book is not the same as starting with an empty one, and an empty one is no use to anybody at
the shop. So a build made on the shop's Mac reads that Mac's own book — read-only, the same way
`tools/carry-book.py` and `server.py --backup` read it while the shop is open — and puts a copy of it
inside every bundle. The install then opens with the shop's figures and the shop's settings: name,
address, phone, tagline, clients, jobs, money, expenses. The rows that only mean something on the
shop's Mac are taken out of the copy first — who is signed in, which writes its server already
replayed, the access log, and any client message still waiting to be sent, because a second machine
would send the client the same message a second time. And the email password never travels, because it
was never in the book; it sits in its own file at mode 600, and typing it is the owner's job.

Every Mac bundle is **run** here — the launcher in place, then again after the installer has copied
it into an Applications folder and again out of its own zip — because a copy can lose the symlinks
inside a runtime or the interpreter's executable bit, and only starting it proves it kept them. The
window is proved too, by asking the app to describe its own window (`--window-probe`): what the page
says it is, and how many distinct colours the picture of it holds. Windows and Linux can be checked
by shape from here and the report names which is which.

Usage:
    python3 tools/build-installer.py                  # every kind, from the shop's own book
    python3 tools/build-installer.py mac-arm64 win-x64
    python3 tools/build-installer.py --book-from copy-of-book.db   # start from that copy instead
    python3 tools/build-installer.py --blank          # empty books, for a shop that is not this one
    python3 tools/build-installer.py --clean          # throw the folders away, keep the downloads
"""
import hashlib
import os
import plistlib
import re
import shutil
import sqlite3
import struct
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

# What an installed copy calls itself, and where it keeps its book. The Mac folder is deliberately
# *not* the shop's own "Chrisphics Hub" Application Support folder.
MAC_APP = "Chrisphics Hub.app"
MAC_EXE = "Chrisphics Hub"
MAC_ID = "com.crispprint.hub.portable"
ICON_PNG = os.path.join(REPO, "public", "img", "icon-512.png")
SHELL_SRC = os.path.join(REPO, "tools", "portable-app-shell.m")
# The oldest macOS the shell still compiles against. The command-line tools here link both chips, so
# one source covers an Apple-silicon Mac and an Intel one — which a Swift shell would not.
MAC_MIN = "10.13"

# The app is these files and nothing else. `tools/` stays behind on purpose: it is dev checks and the
# launchd service, and a computer that owns its own book needs neither.
APP_FILES = ("server.py", "schema.sql")
APP_DIRS = ("public",)

# The shop's own records, on this Mac. Everything the shop configures — its name, address, phone,
# tagline, the backup choices, the consent default — lives in this one file, so copying the book is
# what "all its settings" means; there is no separate settings file to hand over.
SHOP_SUPPORT = os.path.expanduser("~/Library/Application Support/Chrisphics Hub")
SHOP_BOOK = os.path.join(SHOP_SUPPORT, "chrisphics.db")
BOOK_COPY = re.compile(r"^(backup|manual)-\d{8}-\d{6}\.db$")

# Where a bundle keeps the copy it installs from, and what the machine's own book is called.
SEED_FOLDER = os.path.join("data", "shop.db")
MAC_SEED = os.path.join(MAC_APP, "Contents", "Resources", "data", "shop.db")

# What never travels with a copy of the book. Who is signed in, which writes the shop's server has
# already replayed and what the shop's access log says are facts about the shop's own Mac; and a
# client message still waiting to be sent would be sent a second time by whichever machine happened
# to install the copy.
LEAVE_AT_HOME = ("DELETE FROM sessions",
                 "DELETE FROM sync_requests",
                 "DELETE FROM security_log",
                 "DELETE FROM notifications WHERE state <> 'Sent'",
                 "DELETE FROM app_state WHERE key IN ('last_backup_at','last_backup_file',"
                 "'last_backup_kind','last_backup_error','last_backup_note','last_backup_mail',"
                 "'momo_status','momo_checked')")
FIGURES = ("jobs", "clients", "payments")
RUNTIME_TABLES = ("sessions", "sync_requests", "security_log")

# Filled in by main(): the one snapshot every bundle in this build is seeded from.
SEEDING = {"path": None, "from": "an empty book", "figures": {t: 0 for t in FIGURES},
           "shop": {}, "settings": "", "when": ""}


def git_stamp():
    """The build the person installs is traceable to the commit it came from."""
    return subprocess.run(["git", "-C", REPO, "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True).stdout.strip() or "unknown"


TARGETS = {
    "mac-arm64": {
        "asset": "cpython-%s+%s-aarch64-apple-darwin-install_only.tar.gz" % (PYTHON, TAG),
        "folder": "Chrisphics Hub for Mac (Apple silicon)",
        "zip": "Chrisphics-Hub-Mac-Apple-silicon.zip",
        "kind": "mac", "arch": "aarch64-apple-darwin", "triple": "arm64-apple-macosx" + MAC_MIN,
    },
    "mac-x64": {
        "asset": "cpython-%s+%s-x86_64-apple-darwin-install_only.tar.gz" % (PYTHON, TAG),
        "folder": "Chrisphics Hub for Mac (Intel)",
        "zip": "Chrisphics-Hub-Mac-Intel.zip",
        "kind": "mac", "arch": "x86_64-apple-darwin", "triple": "x86_64-apple-macosx" + MAC_MIN,
    },
    "win-x64": {
        "asset": "cpython-%s+%s-x86_64-pc-windows-msvc-install_only.tar.gz" % (PYTHON, TAG),
        "folder": "Chrisphics Hub for Windows",
        "zip": "Chrisphics-Hub-Windows.zip",
        "kind": "win", "arch": "x86_64-pc-windows-msvc",
    },
    "linux-x64": {
        "asset": "cpython-%s+%s-x86_64-unknown-linux-gnu-install_only.tar.gz" % (PYTHON, TAG),
        "folder": "Chrisphics Hub for Linux",
        "zip": "Chrisphics-Hub-Linux.zip",
        "kind": "linux", "arch": "x86_64-unknown-linux-gnu",
    },
}

# -------------------------------------------------------------------------- Mac: the app itself
# The engine lives inside the bundle, so `ROOT` (which server.py resolves beside its own file) finds
# `public/` and `schema.sql`, and the interpreter is the one that travelled with it.
MAC_LAUNCHER = """#!/bin/bash
# Open Chrisphics Hub on this computer. Its own records are in
# ~/Library/Application Support/CRISPprint Hub — not the shop's book, this machine's.
set -euo pipefail
CONTENTS="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SUPPORT="$HOME/Library/Application Support/CRISPprint Hub"
mkdir -p "$SUPPORT"
PORT="${CHRISPHICS_PORT:-8712}"
# Double-clicked while it is already open, this does the one thing a person means: show the shop.
if [ "${1:-}" != "--no-browser" ] && command -v curl >/dev/null \\
   && curl -s --max-time 2 "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
  open "http://127.0.0.1:${PORT}/"
  exit 0
fi
export CHRISPHICS_SUPPORT="$SUPPORT"
# Naming the book, instead of letting the engine reach its default, is what keeps this copy its own
# shop: see legacy_book_path() in server.py.
export CHRISPHICS_DB="$SUPPORT/shop.db"
# The first time this app is opened on a computer, it takes the copy of the shop's book that came
# inside it and puts it where this machine keeps its own records. After that it works only from this
# machine's book, so reinstalling the app never overwrites what this computer has written since.
if [ ! -f "$SUPPORT/shop.db" ] && [ -f "$CONTENTS/Resources/data/shop.db" ]; then
  cp "$CONTENTS/Resources/data/shop.db" "$SUPPORT/shop.db"
fi
exec "$CONTENTS/Resources/runtime/bin/python3" "$CONTENTS/Resources/app/server.py" \\
     --port "$PORT" "$@"
"""

MAC_PLIST = {
    "CFBundleName": "Chrisphics Hub",
    "CFBundleDisplayName": "Chrisphics Hub",
    "CFBundleIdentifier": MAC_ID,
    "CFBundleExecutable": MAC_EXE,
    "CFBundlePackageType": "APPL",
    "CFBundleInfoDictionaryVersion": "6.0",
    "CFBundleIconFile": "AppIcon",
    "LSMinimumSystemVersion": "10.13",
    "NSHighResolutionCapable": True,
    "NSPrincipalClass": "NSApplication",
    "LSApplicationCategoryType": "public.app-category.business",
    "NSHumanReadableCopyright": "CRISPprint Ghana — printing records and accounts",
}

RUN_SH_MAC = """#!/bin/bash
# Open Chrisphics Hub straight out of this folder, without installing it. Its records still live in
# ~/Library/Application Support/CRISPprint Hub, so installing it later keeps the same book.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -n "${CHRISPHICS_PORT:-}" ]; then
  exec open "$HERE/%(app)s" --args --port "$CHRISPHICS_PORT"
else
  exec open "$HERE/%(app)s"
fi
"""

INSTALL_SH_MAC = """#!/bin/bash
# Install Chrisphics Hub on this Mac: put the app in /Applications (or ~/Applications when that is
# not open to you) and open it. The old copy, if there is one, is kept beside it and named.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${CHRISPHICS_INSTALL_DIR:-/Applications}"
if [ ! -w "$DEST" ]; then DEST="$HOME/Applications"; mkdir -p "$DEST"; fi
if [ -d "$DEST/%(app)s" ]; then
  mv "$DEST/%(app)s" "$DEST/.before-%(app)s-$(date '+%%Y%%m%%d-%%H%%M%%S')"
fi
ditto "$HERE/%(app)s" "$DEST/%(app)s"
chmod +x "$DEST/%(app)s/Contents/MacOS/%(exe)s"
codesign --force --deep --sign - "$DEST/%(app)s" >/dev/null 2>&1 || true
echo "Installed: $DEST/%(app)s"
echo "Records:   $HOME/Library/Application Support/CRISPprint Hub/shop.db"
# The build check installs into a folder that is not this Mac's Applications and does not want a shop
# window appearing on the screen while it runs; a person double-clicking gets the open.
if [ -z "${CHRISPHICS_NO_OPEN:-}" ]; then open "$DEST/%(app)s"; fi
"""

# -------------------------------------------------------------------------- Windows
RUN_BAT_WIN = """@echo off
rem Open Chrisphics Hub on this computer. Its own records are in %%LOCALAPPDATA%%\\CRISPprint Hub.
setlocal
set HERE=%%~dp0
if "%%CHRISPHICS_PORT%%"=="" set CHRISPHICS_PORT=8712
set SUPPORT=%%LOCALAPPDATA%%\\CRISPprint Hub
if not exist "%%SUPPORT%%" mkdir "%%SUPPORT%%"
rem The first time this is started on the PC, the copy of the shop's book that came in the program
rem folder is put where this machine keeps its own records. After that only this machine's book is
rem opened, so reinstalling the program never overwrites what this PC has written since.
if not exist "%%SUPPORT%%\\shop.db" if exist "%%HERE%%data\\shop.db" copy /b "%%HERE%%data\\shop.db" "%%SUPPORT%%\\shop.db" >nul
echo Opening Chrisphics Hub at http://127.0.0.1:%%CHRISPHICS_PORT%%/
echo Records: %%SUPPORT%%\\shop.db
echo Leave this window open while you work; close it to stop the shop.
rem Naming the book, instead of letting the engine reach its default, keeps this copy its own shop.
set CHRISPHICS_SUPPORT=%%SUPPORT%%
set CHRISPHICS_DB=%%SUPPORT%%\\shop.db
"%%HERE%%runtime\\python.exe" "%%HERE%%app\\server.py" --port %%CHRISPHICS_PORT%% %%*
"""

INSTALL_CMD_WIN = """@echo off
rem Install Chrisphics Hub on this PC: the program in your own App Data folder, an entry in the
rem Start Menu and on the Desktop, and a line in Settings > Apps so it uninstalls like anything else.
rem No admin rights, because nothing here goes in Program Files or the machine-wide registry.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" install
"""

UNINSTALL_CMD_WIN = """@echo off
rem Take Chrisphics Hub off this PC. Your records stay where they are, in
rem %LOCALAPPDATA%\\CRISPprint Hub, until you delete that folder yourself.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" uninstall
"""

INSTALL_PS1_WIN = """param([string]$Action = "install")
$Name = "Chrisphics Hub"
$Root = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$Program = Join-Path $env:LOCALAPPDATA "Programs\\$Name"
$Data = Join-Path $env:LOCALAPPDATA "CRISPprint Hub"
$Exe = Join-Path $Program "Start Chrisphics Hub.bat"
$Ico = Join-Path $Program "Chrisphics Hub.ico"
$StartMenu = Join-Path $env:APPDATA "Microsoft\\Windows\\Start Menu\\Programs"
$Desktop = [Environment]::GetFolderPath("Desktop")
$Uninstall = "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\ChrisphicsHub"

function Make-Shortcut([string]$Where) {
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($Where)
    $link.TargetPath = $Exe
    $link.WorkingDirectory = $Program
    $link.IconLocation = $Ico
    $link.Description = "Chrisphics Hub - printing records and accounts"
    $link.Save()
}

if ($Action -eq "install") {
    if (Test-Path $Program) {
        $Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
        Move-Item -Path $Program -Destination "$Program.before-$Stamp"
    }
    New-Item -ItemType Directory -Force -Path $Program | Out-Null
    foreach ($Item in @("app", "runtime", "data", "Chrisphics Hub.ico", "Start Chrisphics Hub.bat",
                        "install.ps1", "README.txt")) {
        Copy-Item -Recurse -Force (Join-Path $Root $Item) $Program
    }
    Make-Shortcut (Join-Path $StartMenu "$Name.lnk")
    Make-Shortcut (Join-Path $Desktop "$Name.lnk")
    New-Item -Force -Path $Uninstall | Out-Null
    Set-ItemProperty -Path $Uninstall -Name DisplayName -Value $Name
    Set-ItemProperty -Path $Uninstall -Name DisplayVersion -Value "%(stamp)s"
    Set-ItemProperty -Path $Uninstall -Name Publisher -Value "CRISPprint Ghana"
    Set-ItemProperty -Path $Uninstall -Name DisplayIcon -Value $Ico
    Set-ItemProperty -Path $Uninstall -Name UninstallString -Value `
        ("powershell -NoProfile -ExecutionPolicy Bypass -File `"{0}\\install.ps1`" uninstall" -f $Program)
    Write-Host "Installed: $Program"
    Write-Host "Records:   $Data\\shop.db"
    Write-Host "Start it from the Start Menu, the Desktop, or $Exe"
    Start-Process -FilePath $Exe
} else {
    Remove-Item -Recurse -Force $Program -ErrorAction SilentlyContinue
    Remove-Item -Force (Join-Path $StartMenu "$Name.lnk") -ErrorAction SilentlyContinue
    Remove-Item -Force (Join-Path $Desktop "$Name.lnk") -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force $Uninstall -ErrorAction SilentlyContinue
    Write-Host "Removed the program. Your records stayed in $Data"
}
"""

# -------------------------------------------------------------------------- Linux
RUN_SH_LINUX = """#!/bin/bash
# Open Chrisphics Hub on this computer. Its records live in the "data" folder beside this file.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${CHRISPHICS_PORT:-8712}"
export CHRISPHICS_SUPPORT="${HERE}/data"
# Naming the book, instead of letting the engine reach its default, keeps this copy its own shop.
export CHRISPHICS_DB="${HERE}/data/shop.db"
echo "Opening Chrisphics Hub at http://127.0.0.1:${PORT}/ — records in ${HERE}/data/shop.db"
echo "Leave this window open while you work; Ctrl+C closes the shop."
exec "${HERE}/runtime/bin/python3" "${HERE}/app/server.py" --port "${PORT}" "$@"
"""

README_TXT = """CHRISPPRINT GHANA — CHRISPHICS HUB
A copy of the printing shop's records book that runs on this computer alone.

INSTALL IT
    %(install)s

RUN IT
    %(run)s
%(opening)s

YOUR RECORDS
%(records)s
%(starts)s
    Nothing here reads or writes any other computer's book. Two computers running this keep two books
    and nothing merges them: what you write on one does not appear on the other, and it never will.
    That is the deal a standalone copy makes so it can work with the network down and the shop Mac
    switched off. To move records, copy that file. To back this computer up, copy that folder.

MAKE IT FEEL LIKE AN APP ON THE PHONE
    The browser can keep this address as an installed app, and the last figures you were shown still
    paint when the computer is off the network. On the shop's Wi-Fi the counter is instead the
    always-on server on the shop Mac, which phones and tablets reach over HTTPS; that is a different
    setup and it is not this folder.

IF PORT 8712 IS TAKEN
    %(port_hint)s

This is not the shop's Wi-Fi server: it answers only on this computer, and no password is asked for
because nothing outside this computer can reach it.
"""

MAC_RECORDS = """    They live in "~/Library/Application Support/CRISPprint Hub/shop.db" — the folder this
    computer's own copy keeps. It is NOT the shop's "Chrisphics Hub" folder, and the app never opens
    that one: the shop's book was copied into this app when it was built, and this file is the only
    book it reads or writes."""

WIN_RECORDS = """    They live in "%LOCALAPPDATA%\\CRISPprint Hub\\shop.db" — this user's own copy.
    Uninstalling leaves that file where it is, so the records outlive the program."""

LINUX_RECORDS = """    They live in the "data" folder inside this one ("data/shop.db"). Copy the folder and the
    records come with it."""

# What a person sees when they start it, because it is not the same thing on each machine: a Mac gets
# a window of its own, the other two get a browser tab and a console that says what it is doing.
MAC_OPENING = """    The shop opens in its own window — the app carries the engine and starts it, so nothing else has
    to be running and no browser is involved. Closing the window closes the shop. If the window does
    not appear, the shop is still there: open <http://127.0.0.1:8712/> in a browser, and read
    "~/Library/Application Support/CRISPprint Hub/engine.log" for what it said."""

WIN_OPENING = """    A console window opens and the shop appears in your browser at <http://127.0.0.1:8712/>. Keep the
    console open while you work — closing it closes the shop. If no page appears, type that address
    into Chrome, Edge or Firefox."""

LINUX_OPENING = WIN_OPENING.replace("console", "terminal")

# What the install opens with. A build made on the shop's Mac hands over the shop's own book, so the
# first screen shows the shop's figures instead of an empty ledger; a build with nothing to copy — or
# one asked for with --blank — starts a shop of its own from zero, which is what somebody outside the
# shop needs.
SEEDED_START = """    It starts already full of the shop's records and the shop's settings — name, address, phone and
    tagline, clients, jobs and money — as they stood on %s. The figures are in the file,
    not on a server: this copy works with no network at all, and it needs nothing from the shop
    Mac to run.
    Where this book came from: %s.
    Two things do not travel, and cannot. The shop's email password is never in the book, so this
    computer cannot send the shop's mail until somebody types it in here; and the money alerts are
    read off the shop Mac's own Messages app, so those stay with that machine.
    Anything written on this computer stays on this computer. If a job is also written on the shop
    Mac, there are then two records of it and nothing joins them up."""
BLANK_START = """    It starts with an empty book, and everything you write belongs to this computer alone."""

fails = []
checks = 0
_SCRATCH = []


def note(label, ok, detail=""):
    global checks
    checks += 1
    print("%-56s %s %s" % (label, "ok" if ok else "FAIL", detail))
    if not ok:
        fails.append(label)


def write(path, text, newline="\n", mode=None):
    with open(path, "w", newline=newline) as fh:
        fh.write(text)
    if mode is not None:
        os.chmod(path, mode)
    return path


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def book_says(path):
    """What a book actually holds, read straight out of the file. These checks exist to catch a copy
    that quietly opens somebody else's records, and a running engine is a poor witness; the file is."""
    try:
        book = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    except sqlite3.Error:
        return None
    try:
        tables = {r[0] for r in book.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "jobs" not in tables or "app_state" not in tables:
            return None
        got = {"integrity": book.execute("PRAGMA integrity_check").fetchone()[0],
               "figures": {t: book.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
                           for t in FIGURES if t in tables},
               "settings": dict(book.execute("SELECT key, value FROM app_state "
                                             "WHERE key LIKE 'shop_%'").fetchall())}
        got["runtime"] = {t: (book.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
                              if t in tables else 0) for t in RUNTIME_TABLES}
        got["waiting"] = (book.execute("SELECT COUNT(*) FROM notifications "
                                       "WHERE state <> 'Sent'").fetchone()[0]
                          if "notifications" in tables else 0)
        return got
    except sqlite3.Error:
        return None
    finally:
        book.close()


def shop_book_source(argv):
    """Which copy of the shop's book the installs start from.

    The live book, read read-only, is the one that answers "give the app all its settings" best: the
    figures are tonight's, not last night's. `server.py --backup` and `tools/carry-book.py` already
    read that same file this way while the shop is running, so a build joining the read is nothing new.
    `--book-from FILE` names a copy, and `--blank` builds empty ones for handing the app to somebody
    who is not this shop.
    """
    if "--blank" in argv:
        return None
    for index, word in enumerate(argv):
        if word == "--book-from":
            return argv[index + 1] if index + 1 < len(argv) else None
        if word.startswith("--book-from="):
            return word.split("=", 1)[1]
    if os.path.isfile(SHOP_BOOK):
        return SHOP_BOOK
    try:
        names = [n for n in os.listdir(os.path.join(SHOP_SUPPORT, "Backups")) if BOOK_COPY.match(n)]
    except OSError:
        return None
    return os.path.join(SHOP_SUPPORT, "Backups", max(names)) if names else None


def seed_book(source):
    """One clean copy of the shop's book, made once per build and handed to every bundle.

    The shop's file is opened `mode=ro`, which is the guarantee the last note in this function
    checks: SQLite refuses a write through that URI, so a build cannot scribble on the records it is
    copying. The copy is left in delete-journal mode with no sidecar files, because a book that needs
    a `-wal` beside it is not a book a launcher can `cp` into place.
    """
    destination = os.path.join(scratch(), "shop-seed.db")
    for extra in (destination, destination + "-wal", destination + "-shm"):
        try:
            os.remove(extra)
        except OSError:
            pass
    before = sha256_file(source)
    shop = sqlite3.connect("file:%s?mode=ro" % source, uri=True)
    copy = sqlite3.connect(destination)
    try:
        if shop.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            return None, "the shop's book did not pass its own check"
        shop.backup(copy)
        for statement in LEAVE_AT_HOME:
            copy.execute(statement)
        copy.commit()
        copy.execute("PRAGMA journal_mode=DELETE")
        copy.execute("VACUUM")
        copy.commit()
    except sqlite3.Error as err:
        copy.close()
        shop.close()
        return None, "the copy could not be made: %s" % err
    copy.close()
    shop.close()
    after = sha256_file(source)
    if after != before and os.path.getmtime(source) < time.time() - 120:
        os.remove(destination)
        return None, "the build changed the shop's own book — stop and look"
    for extra in (destination + "-wal", destination + "-shm"):
        try:
            os.remove(extra)
        except OSError:
            pass
    said = book_says(destination)
    if not said or said["integrity"] != "ok":
        return None, "the copy did not pass its own check"
    note("the shop's own book came out of the read unchanged",
         after == before, "same fingerprint before and after" if after == before
         else "it moved while the build read it, which only the shop can do: the build holds it "
              "mode=ro and SQLite refuses a write through that")
    return destination, ""


def prepare_seed(argv):
    """Decide once, for the whole build, what every installed copy starts its book with."""
    source = shop_book_source(argv)
    if not source:
        SEEDING["from"] = "an empty book — this Mac holds no copy of the shop's book to start from"
        note("a build with no book to copy makes empty installs", True, "--blank, or no shop book here")
        return
    path, why = seed_book(source)
    if not path:
        SEEDING["from"] = "an empty book — %s" % why
        note("the shop's book copied into the installs", False, why)
        return
    said = book_says(path)
    SEEDING.update(path=path, figures=said["figures"], shop=said["settings"],
                   settings=", ".join(sorted(said["settings"])),
                   when=time.strftime("%Y-%m-%d"))
    SEEDING["from"] = ("the shop's own book, read as the build ran" if source == SHOP_BOOK
                       else "the shop's copy %s" % os.path.basename(source))



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


def app_tree(prefix):
    """Every file of the shop, keyed by where it goes inside this bundle.

    The engine resolves `public/` and `schema.sql` beside its own file (`ROOT` in server.py), so the
    two always sit together under one prefix — here `Chrisphics Hub.app/Contents/Resources/app`,
    where a bundle that put the screens elsewhere starts fine and 404s the dashboard on every route.
    """
    wanted = {}
    for name in APP_FILES:
        wanted[os.path.join(prefix, name)] = os.path.join(REPO, name)
    for folder in APP_DIRS:
        for base, _, names in os.walk(os.path.join(REPO, folder)):
            for name in names:
                if name == ".DS_Store":
                    continue
                source = os.path.join(base, name)
                wanted[os.path.join(prefix, os.path.relpath(source, REPO))] = source
    return wanted


def stdlib_of(runtime):
    """The standard library of an unpacked runtime. Windows keeps it in `Lib`, everyone else in
    `lib/python3.12` — and on a Mac's case-insensitive disk `Lib` answers for `lib` too, so a folder
    is only the library when it actually holds `encodings`."""
    for leaf in ("Lib", os.path.join("lib", "python" + PYTHON.rsplit(".", 1)[0])):
        root = os.path.join(runtime, leaf)
        if os.path.exists(os.path.join(root, "encodings", "__init__.py")):
            return root
    return None


def scratch():
    """A throwaway folder to work in. The icon sizes are built with `sips`, and `sips` insists on
    writing beside what it reads — which here is `public/img/`, a folder the live shop serves. A build
    that left resized copies in it would put them on the shop's own screens."""
    if not _SCRATCH:
        _SCRATCH.append(tempfile.mkdtemp(prefix="chrisphics-icon-"))
    return _SCRATCH[0]


def png_bytes(side):
    """The shop's own icon resized to one exact square."""
    out = os.path.join(scratch(), "icon-%d.png" % side)
    if not os.path.exists(out):
        subprocess.run(["sips", "-z", str(side), str(side), ICON_PNG, "--out", out], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return out


# The names `iconutil` accepts, and how many pixels behind each. The 1024 is an upscale of the shop's
# 512 mark; without it a Retina Dock tile shows a soft icon.
ICONSET = [(16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2), (256, 1), (256, 2),
           (512, 1), (512, 2)]


def make_icns(folder):
    """An .icns for the app, from the shop's install icon — the same four-diamond mark the phones get."""
    iconset = os.path.join(scratch(), "AppIcon.iconset")
    shutil.rmtree(iconset, ignore_errors=True)
    os.makedirs(iconset)
    for side, scale in ICONSET:
        name = "icon_%dx%d.png" % (side, side) + ("@2x" if scale == 2 else "")
        shutil.copy(png_bytes(side * scale), os.path.join(iconset, name))
    icns = os.path.join(folder, "AppIcon.icns")
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", icns], check=True)
    shutil.rmtree(iconset, ignore_errors=True)
    return icns


def make_ico(png, out, side=256):
    """A Windows .ico from a PNG. The format is a header plus whole PNG files — no icon compiler,
    and no Windows, to make one: 256x256 is stored as a size of zero."""
    data = open(png, "rb").read()
    with open(out, "wb") as fh:
        fh.write(struct.pack("<HHH", 0, 1, 1))
        fh.write(struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(data), 22))
        fh.write(data)
    return out


def compile_shell(path, triple):
    """The Mac app's executable: a window with the shop in it, over the engine inside the same
    bundle. clang from the command-line tools links AppKit and WebKit for both chips, which a Swift
    shell does not — this machine's Swift runtime ships arm64 slices only, so an Intel copy could
    never be built from Swift. Returns nothing when there is no compiler, and the bundle falls back
    to a shell script that opens the shop in a browser tab instead."""
    if not shutil.which("clang"):
        return None
    done = subprocess.run(["clang", "-O2", "-target", triple, "-fobjc-arc",
                           "-framework", "AppKit", "-framework", "WebKit", SHELL_SRC, "-o", path],
                          capture_output=True, text=True)
    if done.returncode:
        print("    the window shell did not compile: %s" % (done.stderr.strip()[:300] or done.stdout[:300]))
        return None
    os.chmod(path, 0o755)
    return path


def build(key):
    target = TARGETS[key]
    bundle = os.path.join(DIST, target["folder"])
    kind = target["kind"]
    print("\n== %s" % target["folder"])
    shutil.rmtree(bundle, ignore_errors=True)
    os.makedirs(bundle, exist_ok=True)

    archive, size = download(target["asset"])
    note("%s runtime downloaded" % key, size > 1_000_000, "%.1f MB" % (size / 1048576.0))

    # `tar` rather than Python's tarfile: these runtimes carry symlinks and executable bits, and a
    # bundle whose interpreter lost its +x is a folder that looks complete and does nothing.
    unpacked = os.path.join(bundle, "_runtime")
    os.makedirs(unpacked, exist_ok=True)
    subprocess.run(["tar", "-xzf", archive, "-C", unpacked], check=True)
    runtime = os.path.join(bundle, "runtime")
    shutil.move(os.path.join(unpacked, "python"), runtime)
    os.rmdir(unpacked)
    # Every bundle holds the engine under one prefix, beside `public/` and `schema.sql`, because
    # server.py resolves those beside its own file (`ROOT` in server.py). A bundle that put the
    # screens anywhere else starts fine and 404s the dashboard on every route.
    if kind == "mac":
        app = os.path.join(bundle, MAC_APP)
        macos = os.path.join(app, "Contents", "MacOS")
        resources = os.path.join(app, "Contents", "Resources")
        os.makedirs(macos, exist_ok=True)
        os.makedirs(resources, exist_ok=True)
        shutil.move(runtime, os.path.join(resources, "runtime"))
        runtime = os.path.join(resources, "runtime")
        prefix = os.path.join(MAC_APP, "Contents", "Resources", "app")
        exe = os.path.join(macos, MAC_EXE)
        chip = target["triple"].split("-")[0]
        if compile_shell(exe, target["triple"]):
            command = [exe, "--engine"]
            launcher_text = open(SHELL_SRC).read()
            note("the app's executable is a native window shell for this chip",
                 chip in subprocess.run(["file", exe], capture_output=True, text=True).stdout,
                 "clang -target %s, %d KB" % (target["triple"], os.path.getsize(exe) / 1024))
        else:
            write(exe, MAC_LAUNCHER, mode=0o755)
            command = ["bash", exe, "--no-browser"]
            launcher_text = open(exe).read()
            note("the app's executable is a shell script that opens a browser tab", True,
                 "no clang here, so the window shell could not be built")
        plist = dict(MAC_PLIST, CFBundleShortVersionString=git_stamp(), CFBundleVersion=git_stamp())
        with open(os.path.join(app, "Contents", "Info.plist"), "wb") as fh:
            plistlib.dump(plist, fh)
        note("the app's Info.plist names its executable and identifier",
             plistlib.load(open(os.path.join(app, "Contents", "Info.plist"), "rb"))
             == plist, MAC_ID)
        icns = make_icns(resources)
        note("the app carries the shop's own icon", os.path.getsize(icns) > 10_000,
             "%.0f KB .icns" % (os.path.getsize(icns) / 1024.0))
        run_name, install_name = "Start Chrisphics Hub.command", "Install Chrisphics Hub.command"
        write(os.path.join(bundle, run_name), RUN_SH_MAC % {"app": MAC_APP}, mode=0o755)
        write(os.path.join(bundle, install_name),
              INSTALL_SH_MAC % {"app": MAC_APP, "exe": MAC_EXE}, mode=0o755)
        records, install_hint = MAC_RECORDS, (
            "Double-click \"Install Chrisphics Hub.command\". It copies the app into /Applications "
            "(or into your own Applications folder when that one is not open to you) and opens it.\n"
            "    \"Start Chrisphics Hub.command\" runs it out of this folder instead, with the same "
            "records, if you would rather not install it.\n"
            "    Beside this folder are two more shapes of the same app: \"%s\" to drag the icon "
            "onto Applications, and \"%s\" to double-click an installer. Use whichever one the "
            "person receiving it prefers."
            % (target["zip"].replace(".zip", ".dmg"), target["zip"].replace(".zip", ".pkg")))
        opening = MAC_OPENING
        port_hint = 'CHRISPHICS_PORT=8801 "./%s"' % run_name
    elif kind == "win":
        prefix = "app"
        run_name, install_name = "Start Chrisphics Hub.bat", "Install Chrisphics Hub.cmd"
        command = [os.path.join(bundle, run_name)]
        write(os.path.join(bundle, run_name), RUN_BAT_WIN % {}, newline="\r\n")
        write(os.path.join(bundle, install_name), INSTALL_CMD_WIN, newline="\r\n")
        write(os.path.join(bundle, "Uninstall Chrisphics Hub.cmd"), UNINSTALL_CMD_WIN, newline="\r\n")
        write(os.path.join(bundle, "install.ps1"), INSTALL_PS1_WIN % {"stamp": git_stamp()},
              newline="\r\n")
        make_ico(png_bytes(256), os.path.join(bundle, "Chrisphics Hub.ico"))
        records, install_hint = WIN_RECORDS, (
            "Double-click \"Install Chrisphics Hub.cmd\". No admin rights are needed: it puts the "
            "program in your own App Data folder, an icon in the Start Menu and on the Desktop, and "
            "a line in Settings ▸ Apps so it uninstalls like anything else.")
        opening = WIN_OPENING
        port_hint = 'set CHRISPHICS_PORT=8801 && "Start Chrisphics Hub.bat"'
    else:
        prefix = "app"
        run_name, install_name = "Start Chrisphics Hub.sh", None
        command = ["bash", os.path.join(bundle, run_name), "--no-browser"]
        write(os.path.join(bundle, run_name), RUN_SH_LINUX, mode=0o755)
        os.makedirs(os.path.join(bundle, "data"), exist_ok=True)
        records, install_hint = LINUX_RECORDS, "There is no installer for Linux — run it from here."
        opening = LINUX_OPENING
        port_hint = 'CHRISPHICS_PORT=8801 "./%s"' % run_name

    wanted = app_tree(prefix)
    for relative, source in sorted(wanted.items()):
        destination = os.path.join(bundle, relative)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        shutil.copy2(source, destination)
    note("every app file copied into the bundle", len(wanted) >= 10, "%d files" % len(wanted))

    # The copy of the shop's book this install opens with. For a Mac it travels *inside* the .app, so
    # dragging the icon to Applications takes the records with it; for Windows and Linux it sits in a
    # `data` folder beside the launcher. A Linux copy works straight out of that file, because that
    # folder is its support folder; Mac and Windows copy it out to their own per-user book the first
    # time they are started, so the app can be reinstalled without losing what the machine wrote.
    if SEEDING["path"]:
        where = os.path.join(bundle, MAC_SEED if kind == "mac" else SEED_FOLDER)
        os.makedirs(os.path.dirname(where), exist_ok=True)
        shutil.copy2(SEEDING["path"], where)
        os.chmod(where, 0o644)
        said = book_says(where)
        note("  and it starts with a copy of the shop's book", bool(said),
             "%s — taken from %s" % (", ".join("%s %d" % kv for kv in sorted(said["figures"].items()))
                                     if said else "unreadable", SEEDING["from"]))
        note("    with the shop's own settings in it", bool(said and said["settings"]),
             ", ".join(sorted(said["settings"])) if said else "none")
        note("    and none of the shop's sign-ins, replays or access log",
             bool(said) and not any(said["runtime"].values()) and not said["waiting"],
             str(said["runtime"]) + (", %d client messages still waiting to send" % said["waiting"]
                                     if said and said["waiting"] else "") if said else "unreadable")
        note("    and it passes its own check as a book", bool(said) and said["integrity"] == "ok",
             said["integrity"] if said else "unreadable")
        note("    and it is one file, with no write-ahead sidecars",
             not any(os.path.exists(where + s) for s in ("-wal", "-shm")),
             os.path.getsize(where) // 1024 and "%d KB" % (os.path.getsize(where) // 1024))

    server = os.path.join(bundle, prefix, "server.py")
    interpreter = os.path.join(runtime, *RUNTIME_BIN(kind))
    launcher_path = (os.path.join(macos, MAC_EXE) if kind == "mac"
                     else os.path.join(bundle, run_name))
    if kind != "mac":
        launcher_text = open(launcher_path).read()
    note("the launcher names the runtime inside the bundle",
         "runtime" in launcher_text and "server.py" in launcher_text,
         os.path.relpath(launcher_path, bundle))
    note("  and it names this computer's own book", "CHRISPHICS_DB" in launcher_text,
         "CHRISPHICS_SUPPORT too" if "CHRISPHICS_SUPPORT" in launcher_text else "SUPPORT missing")
    if kind == "win":
        note("  with the separators Windows reads",
             "runtime\\python.exe" in launcher_text and "app\\server.py" in launcher_text,
             "backslashes")
    if kind == "mac":
        note("  and ad-hoc signs the app so macOS will run what was copied in",
             subprocess.run(["codesign", "--force", "--deep", "--sign", "-",
                             os.path.join(bundle, MAC_APP)], capture_output=True).returncode == 0
             and subprocess.run(["codesign", "--verify", "--verbose",
                                 os.path.join(bundle, MAC_APP)], capture_output=True).returncode == 0,
             "codesign --verify")
    note("the interpreter the launcher calls is there", os.path.exists(interpreter),
         os.path.relpath(interpreter, bundle))
    if kind != "win":
        note("  and it is executable", os.access(interpreter, os.X_OK), "")
    note("the engine the launcher calls is there too", os.path.exists(server),
         os.path.relpath(server, bundle))
    library = stdlib_of(runtime)
    missing = [m for m in ("sqlite3/__init__.py", "http/server.py", "ssl.py", "smtplib.py",
                           "json/__init__.py")
               if library is None or not os.path.exists(os.path.join(library, *m.split("/")))]
    note("  and it carries the library the engine imports", library is not None and not missing,
         "%s%s" % (os.path.relpath(library, bundle) if library else "no stdlib folder",
                   (" — missing " + ", ".join(missing)) if missing else ""))
    if kind == "win":
        dlls = os.path.join(runtime, "DLLs")
        got = [n for n in (os.listdir(dlls) if os.path.isdir(dlls) else [])]
        note("  and the compiled half Windows needs (sqlite3, ssl) is there",
             any(n.startswith("_sqlite3") for n in got) and any(n.startswith("_ssl") for n in got),
             "python.exe + %d files in DLLs" % len(got))
        ico = os.path.join(bundle, "Chrisphics Hub.ico")
        head = open(ico, "rb").read(22)
        note("  and the icon Windows shows is a real .ico with a PNG in it",
             head[:4] == b"\x00\x00\x01\x00" and open(ico, "rb").read()[22:26] == b"\x89PNG",
             "%d bytes" % os.path.getsize(ico))
        ps = open(os.path.join(bundle, "install.ps1")).read()
        note("  and the installer script reaches Start Menu, Desktop and Apps",
             all(needle in ps for needle in ("Start Menu", "GetFolderPath(\"Desktop\")",
                                             "CurrentVersion\\Uninstall", "LOCALAPPDATA")),
             "install.ps1")
    # A relocatable CPython on Windows can run "isolated", where the folder holding the script is not
    # on the search path; the tell is a `python._pth` beside the exe. Name the app folder in it when
    # one exists (these install_only archives mostly ship none, which is the simpler case).
    pth = [n for n in sorted(os.listdir(runtime)) if n.endswith("._pth")]
    for name in pth:
        path = os.path.join(runtime, name)
        lines = [ln for ln in open(path).read().splitlines() if ln.strip()]
        if not any("app" in ln for ln in lines):
            lines.append("..\\app")
            write(path, "\n".join(lines) + "\n", newline="\r\n")
    note("a Windows path file, if the runtime has one, names the app folder",
         all("app" in open(os.path.join(runtime, n)).read() for n in pth),
         ", ".join(pth) or "none in this runtime")

    run_hint = ('"Start Chrisphics Hub.command" runs it from this folder, or open "%s" once it is '
                'installed.' % MAC_APP) if kind == "mac" else '"%s"' % run_name
    starts = (SEEDED_START % (SEEDING["when"], SEEDING["from"])) if SEEDING["path"] else BLANK_START
    write(os.path.join(bundle, "README.txt"), README_TXT % {
        "install": install_hint, "run": run_hint, "records": records, "port_hint": port_hint,
        "opening": opening, "starts": starts}, newline="\n")
    told = open(os.path.join(bundle, "README.txt")).read()
    note("the README a person reads says where the book came from",
         "Where this book came from" in told if SEEDING["path"] else "empty book" in told.lower(),
         SEEDING["from"])

    digest = hashlib.sha256()
    for relative, source in sorted(wanted.items()):
        digest.update(relative.encode("utf-8"))
        digest.update(open(source, "rb").read())
    write(os.path.join(bundle, "app.sha256"),
          "%s  %d files  python %s+%s  commit %s\n"
          % (digest.hexdigest(), len(wanted), PYTHON, TAG, git_stamp()))

    if kind == "mac":
        for extra in (mac_dmg(bundle, target), mac_pkg(bundle, target)):
            note("  %s" % extra[0], extra[1], extra[2])

    zip_path = os.path.join(DIST, target["zip"])
    if os.path.exists(zip_path):
        os.remove(zip_path)
    subprocess.run(["zip", "-qryX", zip_path, os.path.basename(bundle)], cwd=DIST, check=True)
    note("zipped for sending", os.path.getsize(zip_path) > 1_000_000,
         "%.1f MB" % (os.path.getsize(zip_path) / 1048576.0))
    names = zipfile.ZipFile(zip_path).namelist()
    inside = os.path.relpath(launcher_path, bundle)
    note("  and the zip carries the launcher, the engine and the screens",
         any(n.endswith(inside) for n in names) and any(n.endswith("app/server.py") for n in names)
         and any(n.endswith("public/app.js") for n in names)
         and any(n.endswith("app/schema.sql") for n in names)
         and any(n.endswith("runtime/bin/python3") or n.endswith("runtime/python.exe")
                 for n in names), "%d entries" % len(names))
    return {"bundle": bundle, "kind": kind, "launcher": launcher_path, "server": server,
            "interpreter": interpreter, "runtime": runtime, "command": command}


def RUNTIME_BIN(kind):
    return ("python.exe",) if kind == "win" else ("bin", "python3")


def mac_dmg(bundle, target):
    """A disk image to drag the app onto Applications, the way a Mac app is usually handed over."""
    stage = tempfile.mkdtemp(prefix="chrisphics-dmg-")
    try:
        shutil.copytree(os.path.join(bundle, MAC_APP), os.path.join(stage, MAC_APP), symlinks=True)
        os.symlink("/Applications", os.path.join(stage, "Applications"))
        dmg = os.path.join(DIST, target["zip"].replace(".zip", ".dmg"))
        subprocess.run(["hdiutil", "create", "-volname", "Chrisphics Hub", "-srcfolder", stage,
                        "-ov", "-format", "UDZO", dmg], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        mount = tempfile.mkdtemp(prefix="chrisphics-mount-")
        try:
            subprocess.run(["hdiutil", "attach", "-nobrowse", "-mountpoint", mount, dmg],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            inside = sorted(os.listdir(mount))
        finally:
            subprocess.run(["hdiutil", "detach", "-quiet", mount], stdout=subprocess.DEVNULL)
        return ("the .dmg mounts and holds the app", MAC_APP in inside,
                "%.1f MB — %s" % (os.path.getsize(dmg) / 1048576.0, ", ".join(inside)))
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def mac_pkg(bundle, target):
    """A .pkg installer: double-click, Authenticate, and it is in /Applications by itself."""
    pkg = os.path.join(DIST, target["zip"].replace(".zip", ".pkg"))
    subprocess.run(["pkgbuild", "--root", os.path.join(bundle, MAC_APP), "--identifier", MAC_ID,
                    "--version", git_stamp(), "--install-location",
                    "/Applications/" + MAC_APP, pkg], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    listed = subprocess.run(["pkgutil", "--payload-files", pkg],
                            capture_output=True, text=True).stdout
    ok = ("Contents/MacOS/%s" % MAC_EXE in listed and "Resources/runtime/bin/python3" in listed)
    return ("the .pkg holds the launcher and the runtime", ok,
            "%.1f MB, %d files inside" % (os.path.getsize(pkg) / 1048576.0,
                                          len(listed.splitlines())))


def ask(command, port, prefix=(), env_extra=None):
    """Start one built copy through its own launcher and ask it for the shop."""
    child = subprocess.Popen(list(prefix) + list(command) + ["--port", str(port)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             env=env_extra or os.environ)
    code = ""
    for _ in range(60):
        time.sleep(0.5)
        code = subprocess.run(["curl", "-s", "--max-time", "2",
                               "http://127.0.0.1:%d/healthz" % port],
                              capture_output=True, text=True).stdout
        if code:
            break
    yield ("runs on its own bundled Python and answers", '"ok"' in code, code[:56])
    page = subprocess.run(["curl", "-s", "--max-time", "3", "http://127.0.0.1:%d/" % port],
                          capture_output=True, text=True).stdout
    yield ("the dashboard it hands over is the shop's", "Chrisphics Hub" in page,
           "%d bytes" % len(page))
    sheet = subprocess.run(["curl", "-s", "--max-time", "3", "http://127.0.0.1:%d/app.js" % port],
                           capture_output=True, text=True).stdout
    yield ("the screens come from inside the bundle too", "paintNetworkStatus" in sheet,
           "%d bytes" % len(sheet))
    boot = subprocess.run(["curl", "-s", "--max-time", "3",
                           "http://127.0.0.1:%d/api/bootstrap" % port],
                          capture_output=True, text=True).stdout
    yield ("the API answers on the same open door", '"shop"' in boot or '"clients"' in boot,
           "%d bytes" % len(boot))
    # The shop's name is a setting, and it lives in the book. So a copy that shows it is reading the
    # records the build put inside it, and not the empty schema it would have had to make itself.
    named = SEEDING["shop"].get("shop_name", "")
    yield ("  and it shows the shop's own name, from the settings it was given",
           not SEEDING["path"] or (named and named in boot),
           named or "no shop name to look for")
    on = re.search(r'"db": ?"([^"]*)"', boot)
    yield ("  and the book it has open is this computer's own",
           bool(on) and "Chrisphics Hub/chrisphics.db" not in on.group(1),
           on.group(1) if on else "it did not say which book")
    child.terminate()
    try:
        child.wait(timeout=15)
    except subprocess.TimeoutExpired:
        child.kill()
    # The launcher's job ends with the shop it started: a copy that leaves its engine listening is the
    # one that silently joins a process nobody meant to leave running — and the one that squats a port
    # the next check then has to work around.
    for _ in range(20):
        if not held(port):
            break
        time.sleep(0.25)
    yield ("closing it let go of the port and the shop inside it", not held(port),
           "%d free" % port if not held(port) else "%d still held" % port)


def held(port):
    return bool(subprocess.run(["lsof", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN"],
                               capture_output=True).stdout.strip())


def run_bundle(key, built, port, prefix=(), says=None, home=None):
    """Start one built copy for real, on a spare port, under a HOME that is not this Mac's: what it
    writes is its own brand-new book, and this machine's shop records stay out of reach."""
    folder = says or TARGETS[key]["folder"]
    if held(port):
        note("%s: a spare port to prove it on" % folder, False, "%d is held" % port)
        return
    own_home = home is None
    shop_home = home or tempfile.mkdtemp(prefix="chrisphics-home-")
    env = dict(os.environ, HOME=shop_home)
    book = book_path(key, shop_home, built)
    for index, (label, ok, detail) in enumerate(ask(built["command"], port, prefix, env)):
        note("%s: %s" % (folder, label) if index == 0 else "  %s" % label, ok, detail)
        if not ok:
            fails.append("%s: %s" % (folder, label))
    if book:
        note("  the book it made is this computer's own", os.path.exists(book),
             os.path.relpath(book, shop_home))
        said = book_says(book) if os.path.exists(book) else None
        note("  and it opened holding the records the build put in it",
             bool(said) and said["figures"] == SEEDING["figures"],
             "no book" if said is None else "%s, expected %s"
             % (said["figures"], SEEDING["figures"]))
        # The shop's live book holds who is signed in, which writes its server has replayed and its own
        # access log. A copy that has those rows in front of it is not reading a copy — it is reading
        # the shop, which is the failure this whole branch of checks exists to catch.
        note("  and it is a copy, not the shop's live book",
             bool(said) and not any(said["runtime"].values()) and not said["waiting"],
             str(said["runtime"]) if said and any(said["runtime"].values()) else
             "no sign-ins, no replay rows, no audit trail" if said else "no book")
    if own_home:
        shutil.rmtree(shop_home, ignore_errors=True)


def book_path(key, home, built):
    if TARGETS[key]["kind"] == "mac":
        return os.path.join(home, "Library", "Application Support", "CRISPprint Hub", "shop.db")
    if TARGETS[key]["kind"] == "linux":
        return os.path.join(built["bundle"], "data", "shop.db")
    return None


def relocate(built, launcher):
    """The same command that runs the bundle as built, pointed at the copy that landed somewhere
    else. The launcher is the one path inside the command that changes when a person installs or
    unpacks the app; `bash` and `--engine` stay where they were."""
    return [launcher if part == built["launcher"] else part for part in built["command"]]


def prove_installed(key, built, port):
    """The Mac installer is a script that copies the app into an Applications folder; run it into a
    throwaway one and start the copy that landed there — a bundle that only runs where it was built
    is not an install. `CHRISPHICS_NO_OPEN=1` keeps it from popping a shop window on this Mac while
    the check runs; the copy is then started on its own spare port instead."""
    stage = tempfile.mkdtemp(prefix="chrisphics-apps-")
    home = tempfile.mkdtemp(prefix="chrisphics-home-")
    folder = TARGETS[key]["folder"]
    try:
        installer = os.path.join(built["bundle"], "Install Chrisphics Hub.command")
        env = dict(os.environ, HOME=home, CHRISPHICS_INSTALL_DIR=stage, CHRISPHICS_NO_OPEN="1")
        landed = os.path.join(stage, MAC_APP)
        inner = os.path.join(landed, "Contents", "MacOS", MAC_EXE)
        first = subprocess.run(["bash", installer], capture_output=True, text=True, env=env)
        note("%s: the installer put the app in an Applications folder" % folder,
             first.returncode == 0 and os.path.isdir(landed),
             first.stdout.strip().splitlines()[0] if first.stdout.strip() else first.stderr[:60])
        second = subprocess.run(["bash", installer], capture_output=True, text=True, env=env)
        kept = [n for n in os.listdir(stage) if n.startswith(".before-")]
        note("  and installing over itself kept the copy it replaced",
             second.returncode == 0 and len(kept) == 1, ", ".join(kept) or "nothing kept")
        note("  and the copy kept its executable launcher", os.access(inner, os.X_OK), MAC_EXE)
        note("  and the copy is still a valid signed bundle",
             subprocess.run(["codesign", "--verify", "--verbose", landed],
                            capture_output=True).returncode == 0, "codesign --verify")
        run_bundle(key, dict(built, bundle=stage, launcher=inner, command=relocate(built, inner)),
                   port, home=home, says="%s (installed)" % folder)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        shutil.rmtree(home, ignore_errors=True)


def prove_window(key, built, port, prefix=()):
    """The one question every check above still leaves open: when a person double-clicks the app, does
    a window open and show the shop in it. The app answers about its own window (`--window-probe`),
    because a grab of the whole display comes back black whenever the display has slept — which says
    nothing about the app — and because a page that arrived over loopback is not yet a page that
    painted. The picture is then measured, not admired: an unpainted rectangle has one colour and no
    spread of light. And the port has to be free again afterwards, since an app that leaves its engine
    behind when it closes is how the next copy silently joins a process nobody started."""
    folder = TARGETS[key]["folder"]
    if built["command"][1:] != ["--engine"]:
        note("%s: the window itself" % folder, False, "no native shell in this bundle to open one")
        return
    # Under dist/, not the scratch folder: the build throws the scratch away at the end, and a picture
    # of the shop in a window is the one part of this check a person may want to look at themselves.
    where = os.path.join(DIST, "window-proof-%s" % key)
    home = tempfile.mkdtemp(prefix="chrisphics-home-")
    shutil.rmtree(where, ignore_errors=True)
    child = subprocess.Popen(list(prefix) + [built["launcher"], "--window-probe", where,
                                             "--port", str(port)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             env=dict(os.environ, HOME=home))
    said = None
    for _ in range(80):
        time.sleep(0.25)
        report = os.path.join(where, "window.txt")
        if os.path.exists(report):
            said = open(report).read()
            break
        if child.poll() is not None:
            break
    try:
        page = re.search(r'"title": ?"([^"]*)".*?"screens": ?(\d+)', said or "")
        note("%s: double-clicking the app opens a window on the shop" % folder,
             bool(page) and "Chrisphics Hub" in page.group(1) and int(page.group(2)) >= 8,
             "%s, %s screens in the rail" % (page.group(1)[:24], page.group(2)) if page
             else (said or "the window never reported in").strip()[:70])
        painted = re.search(r"(\d+) distinct colours, light from (\d+) to (\d+)", said or "")
        note("  and the window painted, rather than a black rectangle",
             bool(painted) and int(painted.group(1)) > 100
             and int(painted.group(3)) - int(painted.group(2)) > 100,
             "%s colours, light %s–%s" % painted.groups() if painted
             else "no picture came back")
        picture = os.path.join(where, "window.png")
        landed = os.path.getsize(picture) if os.path.exists(picture) else 0
        note("  the picture of it is kept for a person to look at", landed > 20_000,
             "%.0f KB at %s" % (landed / 1024.0, os.path.relpath(picture, REPO)) if landed else "missing")
        for _ in range(20):
            time.sleep(0.25)
            if child.poll() is not None and not held(port):
                break
        note("  and closing the window let go of the port it opened",
             child.poll() is not None and not held(port),
             "exit %s, port %d %s" % (child.returncode, port, "free" if not held(port) else "still held"))
        if child.poll() is None:
            child.kill()
    finally:
        shutil.rmtree(home, ignore_errors=True)


def prove_from_zip(key, built, port, prefix):
    """Unpack the zip a second computer will receive, put it somewhere this repository has never
    seen and run it from there: the only real test of the symlinks and the executable bit a zip
    can quietly lose."""
    work = tempfile.mkdtemp(prefix="chrisphics-install-")
    try:
        subprocess.run(["unzip", "-q", "-o", os.path.join(DIST, TARGETS[key]["zip"]), "-d", work],
                       check=True)
        landed = os.path.join(work, "Chrisphics Hub")
        os.rename(os.path.join(work, TARGETS[key]["folder"]), landed)
        relative = os.path.relpath(built["interpreter"], built["bundle"])
        interpreter = os.path.join(landed, *relative.split(os.sep))
        note("  unpacked elsewhere, the interpreter survived the trip",
             os.path.exists(interpreter) and (os.access(interpreter, os.X_OK)
                                              or TARGETS[key]["kind"] == "win"),
             os.path.realpath(interpreter).replace(work, "…"))
        runner = os.path.join(landed, os.path.relpath(built["launcher"], built["bundle"]))
        run_bundle(key, dict(built, bundle=landed, launcher=runner, command=relocate(built, runner)),
                   port, prefix, says="%s (out of the zip)" % TARGETS[key]["folder"])
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    argv = sys.argv[1:]
    after = [word for index, word in enumerate(argv) if index and argv[index - 1] == "--book-from"]
    wanted = [a for a in argv if not a.startswith("--") and a not in after]
    unknown = [a for a in wanted if a not in TARGETS]
    if unknown:
        print("unknown machine kind: %s — pick from %s" % (", ".join(unknown), ", ".join(TARGETS)))
        return 2
    if "--clean" in argv:
        shutil.rmtree(DIST, ignore_errors=True)
        print("removed dist/ (runtime-cache/ kept)")
        return 0
    keys = wanted or list(TARGETS)
    os.makedirs(DIST, exist_ok=True)
    prepare_seed(argv)
    print("each install starts from %s" % SEEDING["from"])
    built = {key: build(key) for key in keys}

    # A Mac bundle can be run here: this machine's own chip natively, the other kind through Rosetta
    # when this Mac has it. Windows and Linux cannot be started from a Mac, and the report says which
    # bundles were run and which were only ever looked at.
    native = other = None
    if sys.platform == "darwin":
        native = "mac-arm64" if os.uname().machine == "arm64" else "mac-x64"
        other = "mac-x64" if native == "mac-arm64" else "mac-arm64"
    elif sys.platform.startswith("linux"):
        native = "linux-x64"
    proved = []
    if not fails:
        cross = [(other, ("arch", "-x86_64"))] if (other and shutil.which("arch") and subprocess.run(
            ["arch", "-x86_64", "/usr/bin/true"], capture_output=True).returncode == 0) else []
        for index, (key, prefix) in enumerate([(native, ())] + cross):
            if key not in built:
                continue
            base = 8798 + index * 4
            run_bundle(key, built[key], base, prefix)
            prove_from_zip(key, built[key], base + 1, prefix)
            if TARGETS[key]["kind"] == "mac":
                prove_installed(key, built[key], base + 2)
                prove_window(key, built[key], base + 3, prefix)
            proved.append(key)
        if other and other not in proved and other in built:
            print("%s: this Mac cannot start that chip's code, so it was checked by shape only"
                  % TARGETS[other]["folder"])
    for key in keys:
        if key not in proved:
            print("%s: checked by shape only — a %s bundle cannot be started from this machine"
                  % (TARGETS[key]["folder"], TARGETS[key]["kind"]))

    shutil.rmtree(scratch(), ignore_errors=True)
    print("\n%s" % ("BUILT: " + ", ".join(TARGETS[k]["folder"] for k in keys)))
    print("%d checks, %d failed" % (checks, len(fails)))
    for f in fails:
        print("  failed: %s" % f)
    if proved:
        print("proved by running: %s" % ", ".join(TARGETS[k]["folder"] for k in proved))
    print("in dist/: " + ", ".join(sorted(os.listdir(DIST))))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
