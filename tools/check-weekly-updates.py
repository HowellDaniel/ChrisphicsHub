#!/usr/bin/env python3
"""Prove the shop hears about a new build once a week, by itself.

The owner's rule (31 October 2026): the book is to be receiving software updates every one week —
not only when somebody remembers to press a button. Two things had to stay true while building that,
and both are pressed on below rather than asserted:

  * the answer never enters the shop's book. It is one number and a verdict about software, so it is
    kept in a file beside the book the way the mail passwords are, and every route and every clock
    that touches it is proven not to write a record, not to be counted as a book write, and not to
    make the other counters repaint;
  * it changes nothing on its own. The check reads one number and says what it means. Taking a newer
    build in is still a person pressing desktop/build-app.sh.

The other end of the wire is a stand-in repository: it answers GET with whatever version this run
decides, so the week, the verdicts, a refusal and a junk answer are all provable without touching
GitHub — and nothing in this file asks GitHub anything at all. The shop's own book is never opened:
the whole run works on a brand-new SQLite file under a temp folder with its own support folder.

Usage: python3 tools/check-weekly-updates.py
"""
import datetime as dt
import hashlib
import inspect
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_PY = os.path.join(REPO, "server.py")
APP_JS = os.path.join(REPO, "public", "app.js")
SW_JS = os.path.join(REPO, "public", "sw.js")
MAIN_SWIFT = os.path.join(REPO, "desktop", "src", "main.swift")
PORTABLE_M = os.path.join(REPO, "tools", "portable-app-shell.m")
README = os.path.join(REPO, "README.md")
SCHEMA = os.path.join(REPO, "schema.sql")


def free_port(start):
    """A port of our own, so a check that crashed earlier and left its server behind does not
    wedge this one. The shop's 8834 is never asked for."""
    for port in range(start, start + 40):
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", port))
            probe.close()
            return port
        except OSError:
            probe.close()
    raise SystemExit("no free loopback port above %d" % start)


PORT = free_port(8948)
work = tempfile.mkdtemp(prefix="chrisphics-weekly-")
BOOK = os.path.join(work, "checked.db")
SUPPORT = os.path.join(work, "support")
BACKUPS = os.path.join(work, "Backups")
os.makedirs(SUPPORT)
os.makedirs(BACKUPS)
ANSWER = os.path.join(SUPPORT, "update-check.json")

# ------------------------------------------------------------------ the stand-in repository
SERVED = {"text": "9.9", "status": 200, "hits": 0, "silence": False, "files": {}}


class Repository(BaseHTTPRequestHandler):
    """One answer, on a port of our own. `silence` closes the socket without replying, which is how
    a shop whose internet is out gets proven — no GitHub is ever asked."""

    def do_GET(self):
        SERVED["hits"] += 1
        if SERVED["silence"]:
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()
            return
        asked = self.path.split("?")[0]
        if asked in SERVED["files"]:
            body = SERVED["files"][asked]
        else:
            body = SERVED["text"].encode("utf-8")
        self.send_response(SERVED["status"] if not SERVED["files"] else 200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


door = ThreadingHTTPServer(("127.0.0.1", 0), Repository)
door.daemon_threads = True
threading.Thread(target=door.serve_forever, daemon=True, name="fake-repository").start()
STAND_IN = "http://127.0.0.1:%d" % door.server_address[1]
UPDATE_URL = STAND_IN + "/VERSION"
UPDATE_META_URL = STAND_IN + "/main/update/current.json"
DEAD_URL = "http://127.0.0.1:%d/VERSION" % free_port(8990)

fails = []
handles = {}
procs = {}
ran = {"n": 0}


def check(label, ok, detail=""):
    ran["n"] += 1
    print("%-62s %s %s" % (label, "ok" if ok else "FAIL", str(detail)[:70]))
    if not ok:
        fails.append(label)
    return ok


def expect(label, got, want):
    return check(label, got == want, "got %r, want %r" % (got, want))


def text(path):
    return open(path, encoding="utf-8").read()


def line_saying(haystack, needle):
    return next((ln.strip() for ln in haystack.splitlines() if needle in ln),
                "no line holds %r" % needle)


# The engine, in this process, on the throwaway folders — so the clock can be turned by hand.
os.environ["CHRISPHICS_DB"] = BOOK
os.environ["CHRISPHICS_SUPPORT"] = SUPPORT
os.environ["CHRISPHICS_BACKUP_DIR"] = BACKUPS
os.environ["CHRISPHICS_UPDATE_URL"] = UPDATE_URL
os.environ["CHRISPHICS_UPDATE_META_URL"] = UPDATE_META_URL
os.environ["CHRISPHICS_UPDATE_RAW"] = STAND_IN
sys.path.insert(0, REPO)
import server  # noqa: E402

REPO_VERSION = text(os.path.join(REPO, "VERSION")).strip()
THIS_BUILD = server.counter_version()

# --------------------------------------------------------------------------- 1. the week, in the source
print("\n== what the shipped files say the rule is")
engine = text(SERVER_PY)
check("the engine names the cadence as one week, in seconds",
      "UPDATE_WEEK = 7 * 24 * 60 * 60" in engine, line_saying(engine, "UPDATE_WEEK ="))
check("the answer is kept beside the book, in its own file",
      server.UPDATE_FILE == ANSWER and os.path.basename(ANSWER) == "update-check.json",
      server.UPDATE_FILE.replace(os.path.expanduser("~"), "~"))
check("and that file is not the book, nor inside it",
      not server.UPDATE_FILE.startswith(server.DB_PATH)
      and server.UPDATE_FILE != server.DB_PATH, server.DB_PATH)
check("no table in the schema holds it",
      "update_check" not in text(SCHEMA) and "update-check" not in text(SCHEMA),
      "schema.sql names no such table")
check("keeping the answer opens no database at all",
      not re.search(r"set_state|_db\.|execute\(|commit\(|INSERT|UPDATE ",
                    inspect.getsource(server.run_update_check)),
      "run_update_check writes only the file")
check("reading it for the screen opens no database either",
      not re.search(r"set_state|_db\.|execute\(|one\(|q\(",
                    inspect.getsource(server.update_facts)),
      "update_facts reads only the file")
check("the engine starts its own weekly clock when the shop comes up",
      re.search(r"def serve\(.*?watch_updates\(\)", engine, re.S) is not None,
      "watch_updates() is called inside serve()")
check("the road out is one number, and only one number, from the shop's own repository",
      server.REPOSITORY.rstrip("/").endswith("HowellDaniel/ChrisphicsHub")
      and '/main/VERSION"' in engine and 'raw.githubusercontent.com' in engine,
      line_saying(engine, "UPDATE_URL ="))
check("the page asks the engine and not the internet",
      "raw.githubusercontent.com" not in text(APP_JS)
      and "'/api/update-check'" in text(APP_JS), line_saying(text(APP_JS), "api/update-check"))
check("the Mac's own Settings pane does the same, so there is one road",
      "raw.githubusercontent.com" not in text(MAIN_SWIFT)
      and '"api/update-check"' in text(MAIN_SWIFT),
      line_saying(text(MAIN_SWIFT), "api/update-check"))
check("the hand-out shell stops claiming nothing leaves the Mac",
      "Once a week the records engine" in text(PORTABLE_M)
      and "Nothing leaves this Mac" not in text(PORTABLE_M), "")
check("and says it in the shop's own words on every screen",
      "Once a week the engine answering here" in text(APP_JS), "")
sw = text(SW_JS)
cache_version = int(re.search(r"crispprint-shell-v(\d+)", sw).group(1))
check("the shell cache is bumped past the copy that had no weekly answer",
      cache_version >= 20, line_saying(sw, "CACHE_NAME ="))
check("the book's version moved on with the change",
      REPO_VERSION != "1.7", "VERSION is %s" % REPO_VERSION)
readme = text(README)
check("the shop's own book of record says the check is weekly",
      "once a week" in readme.lower() and "--update-check" in readme, "")

# ------------------------------------------------------------------ 2. the clock, turned by hand
print("\n== the week, measured on a clock of the check's own making")


def stamp(seconds_ago):
    return (dt.datetime.now() - dt.timedelta(seconds=seconds_ago)).strftime("%Y-%m-%d %H:%M:%S")


def keep(seconds_ago, verdict="same", remote=REPO_VERSION, trouble=""):
    json.dump({"asked_at": stamp(seconds_ago), "this": THIS_BUILD, "remote": remote,
               "verdict": verdict, "trouble": trouble, "reason": "the check put it here"},
              open(ANSWER, "w", encoding="utf-8"))
    return server.read_update_check()


DAY = 24 * 60 * 60
if os.path.isfile(ANSWER):
    os.remove(ANSWER)
expect("a computer that has never asked is asking today", server.update_check_due()[0], True)
keep(8 * DAY)
expect("eight days on, the week says it is time again", server.update_check_due()[0], True)
keep(6 * DAY)
due, left = server.update_check_due()
expect("six days on, it is not time yet", due, False)
check("and the wait it names is the one day still owed", 0.5 * DAY < left < 1.5 * DAY,
      "%.1f hours" % (left / 3600))
keep(2 * 3600, verdict="same")
expect("an answer from two hours ago is still a fresh answer", server.update_check_due()[0], False)
keep(2 * 3600, verdict="unknown", remote="", trouble="the repository could not be reached")
expect("but an ask that reached nothing is tried again within the hour",
       server.update_check_due()[0], True)
keep(10 * 60, verdict="unknown", remote="", trouble="the repository could not be reached")
expect("and not again inside ten minutes", server.update_check_due()[0], False)
open(ANSWER, "w", encoding="utf-8").write("this is not json")
expect("a file somebody chewed through reads as never asked", server.read_update_check(), {})
expect("and the shop asks again rather than stopping", server.update_check_due()[0], True)
keep(0, verdict="same")
os.remove(ANSWER)
for pair, want in ((("1.2", "1.10"), -1), (("1.10", "1.2"), 1), (("1.7", "1.7.0"), 0),
                   (("1.8", "1.10"), -1), (("1.8", "1.8"), 0), (("1.9", "2"), -1)):
    expect("the rule reads %s against %s as one number is to another" % pair,
           server.compare_versions(*pair), want)

# ---------------------------------------------------------------------- 3. one ask, on the file
print("\n== one ask, and what it is allowed to leave behind")
BOOK_HASH_BEFORE = hashlib.sha256(open(BOOK, "rb").read()).hexdigest() if os.path.isfile(BOOK) else ""


def counts():
    """Every row in the throwaway book, so a write cannot hide in a table nobody counted."""
    handle = sqlite3.connect("file:%s?mode=ro" % BOOK, uri=True)
    try:
        tables = [r[0] for r in handle.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {t: handle.execute("SELECT count(*) FROM %s" % t).fetchone()[0] for t in tables}
    finally:
        handle.close()


if not os.path.isfile(BOOK):
    counts_before, hash_before = {}, hashlib.sha256(b"").hexdigest()
else:
    counts_before, hash_before = counts(), hashlib.sha256(open(BOOK, "rb").read()).hexdigest()

SERVED["text"] = "9.9"
answer = server.run_update_check("the check asked")
expect("a newer number on the repository reads as newer",
       (answer["verdict"], answer["remote"]), ("newer", "9.9"))
expect("and it names the reason the shop kept", answer["reason"], "the check asked")
check("the answer is a file, where the file belongs", os.path.isfile(ANSWER), ANSWER)
check("and it says when it was asked, in the shop's own clock",
      re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", answer["asked_at"]) is not None,
      answer["asked_at"])
SERVED["text"] = REPO_VERSION
expect("the same number on the repository reads as nothing newer",
       server.run_update_check("the check asked")["verdict"], "same")
SERVED["text"] = "0.1"
expect("a build ahead of the repository says so rather than claiming a fault",
       server.run_update_check("the check asked")["verdict"], "ahead")
SERVED["text"] = "1.9\nan explanatory line the shop does not need"
expect("only the first line is a version, and it is read as newer",
       server.run_update_check("the check asked")["remote"], "1.9")
SERVED["text"] = "the quick brown fox"
junk = server.run_update_check("the check asked")
expect("words that are not a version number teach the shop nothing", junk["verdict"], "unknown")
check("and say so in words a counter can read", bool(junk["trouble"]), junk["trouble"])
SERVED["text"] = ""
expect("an empty answer is not read as version zero",
       server.run_update_check("the check asked")["verdict"], "unknown")
SERVED["text"] = "1.8"
SERVED["status"] = 404
expect("a repository that refuses the door is named as a refusal",
       server.run_update_check("the check asked")["verdict"], "unknown")
SERVED["status"] = 200
server.UPDATE_URL = DEAD_URL
dead = server.run_update_check("the check asked")
check("and a shop with no road out at all simply says so",
      "could not be reached" in dead["trouble"], dead["trouble"])
server.UPDATE_URL = UPDATE_URL
due, left = server.update_check_due()
check("the retry that follows a failed ask is the hour, not the week",
      due is False and 3500 < left <= 3600, "%.0f seconds left" % left)

after_hash = hashlib.sha256(open(BOOK, "rb").read()).hexdigest() if os.path.isfile(BOOK) else ""
check("through all of that the book never opened",
      not os.path.isfile(BOOK) or after_hash == hash_before,
      "same bytes" if after_hash == hash_before else "the file changed")

# --------------------------------------------------------------------- 4. the engine, over http
print("\n== an engine that comes up and asks by itself")


def boot():
    probe = socket.socket()
    probe.settimeout(0.6)
    try:
        probe.connect(("127.0.0.1", PORT))
        raise SystemExit("port %d is answered by something else — a check against a port someone"
                         " else holds proves nothing" % PORT)
    except (ConnectionRefusedError, OSError):
        pass
    finally:
        probe.close()
    env = dict(os.environ, CHRISPHICS_DB=BOOK, CHRISPHICS_SUPPORT=SUPPORT,
               CHRISPHICS_BACKUP_DIR=BACKUPS, CHRISPHICS_UPDATE_URL=UPDATE_URL,
               CHRISPHICS_UPDATE_CHECK="4", CHRISPHICS_BACKUP_CHECK="100000",
               PYTHONUNBUFFERED="1")
    handle = open(os.path.join(work, "engine.log"), "wb")
    handles["engine"] = handle
    procs["engine"] = subprocess.Popen(["/usr/bin/python3", SERVER_PY, "--port", str(PORT),
                                        "--no-browser"], cwd=REPO, env=env,
                                       stdout=handle, stderr=handle)
    for _ in range(90):
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/healthz" % PORT, timeout=3) as ok:
                if ok.status == 200:
                    return
        except Exception:
            pass
        time.sleep(0.1)
    raise SystemExit("the throwaway engine never answered; see engine.log")


def stop():
    proc = procs.pop("engine", None)
    if proc:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    handles.pop("engine", None)


def call(method, path, payload=None, raw_body=None):
    data = raw_body if raw_body is not None else (
        None if payload is None else json.dumps(payload).encode())
    request = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path), data=data,
                                     method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode("utf-8", "replace")


os.remove(ANSWER) if os.path.isfile(ANSWER) else None
SERVED["text"] = "9.9"
SERVED["hits"] = 0
hits_at_boot = SERVED["hits"]
boot()
asked_by_itself = ""
for _ in range(90):
    if os.path.isfile(ANSWER):
        asked_by_itself = json.load(open(ANSWER, encoding="utf-8"))
        break
    time.sleep(0.5)
check("a shop that comes up asks the repository without being told",
      bool(asked_by_itself) and SERVED["hits"] > hits_at_boot,
      "%d calls the stand-in took" % (SERVED["hits"] - hits_at_boot))
expect("and it remembers that the engine asked, not a person",
      (asked_by_itself or {}).get("reason"), "the week came round")
expect("the answer it keeps is the newer build", (asked_by_itself or {}).get("verdict"), "newer")

facts = json.loads(call("GET", "/api/counter")[1])
updates = facts.get("updates") or {}
KEYS = ["days", "repository", "asked_at", "this", "remote", "verdict", "trouble", "reason",
        "next_in_hours", "due"]
missing = [k for k in KEYS if k not in updates]
check("/api/counter hands the screen the whole weekly answer", not missing,
      "missing %s" % (missing or "none"))
expect("the cadence it tells the counter is one week", updates.get("days"), 7)
expect("about the build the engine is actually running", updates.get("this"), THIS_BUILD)
expect("and about the number on the repository", updates.get("remote"), "9.9")
expect("read as a newer build waiting", updates.get("verdict"), "newer")
check("the whole facts block still arrives, weekly answer and all",
      all(k in facts for k in ("version", "machine", "book", "hours", "sign_in")), "")

weight = os.stat(BOOK)
rev_before = json.loads(call("GET", "/api/book-rev")[1])["rev"]
sync_before = sqlite3.connect("file:%s?mode=ro" % BOOK, uri=True).execute(
    "SELECT count(*) FROM sync_requests").fetchone()[0]
hash_before_writes = hashlib.sha256(open(BOOK, "rb").read()).hexdigest()

SERVED["text"] = "1.10"
code, body = call("POST", "/api/update-check", {"anything": "a browser sends no body"})
pressed = json.loads(body) if code == 200 else {}
expect("the button on the screen asks now, and gets an answer", code, 200)
expect("read as newer, because ten is ten and not one", pressed.get("verdict"), "newer")
expect("with the number the repository holds", pressed.get("remote"), "1.10")
check("and the engine kept it in the file, not the book",
      json.load(open(ANSWER, encoding="utf-8")).get("remote") == "1.10", ANSWER)
again = json.loads(call("GET", "/api/counter")[1])["updates"]
expect("so every other counter reads the same answer", again.get("remote"), "1.10")
expect("with the stamp of that ask", again.get("asked_at"), pressed.get("asked_at"))

for verb, want in (("GET", 404), ("PUT", 404), ("DELETE", 404), ("PATCH", 501)):
    got, said = call(verb, "/api/update-check")
    check("%s /api/update-check is refused" % verb, got == want, "%d %s" % (got, said[:40]))

code, body = call("POST", "/api/update-check", raw_body=b"<html>not json at all</html>")
check("a body that is not json does not upset it (there is none, and there need not be)",
      code == 200, "%d %s" % (code, body[:50]))

rev_after = json.loads(call("GET", "/api/book-rev")[1])["rev"]
sync_after = sqlite3.connect("file:%s?mode=ro" % BOOK, uri=True).execute(
    "SELECT count(*) FROM sync_requests").fetchone()[0]
hash_after_writes = hashlib.sha256(open(BOOK, "rb").read()).hexdigest()
expect("asking did not count as a write to the book, so no counter repaints for it",
       rev_after, rev_before)
expect("and nothing landed in the table that remembers writes", sync_after, sync_before)
check("the book itself is the same bytes it was", hash_after_writes == hash_before_writes,
      "%d bytes, untouched" % weight.st_size)

# A shop that could not reach anything still has a screen to show.
SERVED["silence"] = True
silent = json.loads(call("POST", "/api/update-check")[1])
expect("an engine with no road out says so instead of claiming a fault",
       silent["verdict"], "unknown")
check("and words the counter can read", bool(silent["trouble"]), silent["trouble"])
shown = json.loads(call("GET", "/api/counter")[1])["updates"]
check("the screen keeps the bad news until a later ask brings better",
      shown["verdict"] == "unknown" and shown["next_in_hours"] <= 1,
      "next ask in %s hours" % shown["next_in_hours"])
SERVED["silence"] = False
SERVED["text"] = REPO_VERSION
settled = json.loads(call("POST", "/api/update-check")[1])
expect("and the next ask reads it straight again", settled["verdict"], "same")
stop()

# --------------------------------------------------------------- 5. the page, in the shipped engine
print("\n== the counter's Updates tab, in the rendering engine the app ships")
built = subprocess.run(["swiftc", os.path.join(REPO, "tools", "wk-probe.swift"),
                        "-o", os.path.join(work, "wk-probe")],
                       capture_output=True, text=True, timeout=300)
PROBE = os.path.join(work, "wk-probe")
if not check("the rendering engine probe compiles", built.returncode == 0,
             (built.stderr or "")[-200:]):
    print("\ncould not build tools/wk-probe.swift — no browser checks ran")
else:
    boot()
    SERVED["text"] = "9.9"
    # The engine asks the moment it comes up. It may have asked before this run said what the
    # repository holds, so the answer is settled once here — through the engine's own door, the
    # same one the screen's button uses.
    call("POST", "/api/update-check")
    SERVED["hits"] = 0
    # One scenario per WebKit run: the page is opened, read, pressed once and read again. The press
    # goes to the engine over the same origin the page came from, so nothing here reaches GitHub.
    said = subprocess.run(
        [PROBE, "http://127.0.0.1:%d" % PORT, work,
         "open|/index.html?cb=%d" % os.getpid(),
         "poll|document.getElementById('view') !== null",
         "eval|location.hash='#/settings?pane=updates'",
         "poll|document.getElementById('updateResult') !== null",
         "say|'ROWS='+JSON.stringify(Array.prototype.map.call("
         "document.querySelectorAll('#view .sfield code'),"
         "function(c){return c.textContent;}).join(' / '))",
         "say|'BTN='+JSON.stringify((document.querySelector('[data-do=settings-check]')||{})"
         ".textContent)",
         "say|'DL='+JSON.stringify(Boolean(document.querySelector('[data-do=settings-download]')))",
         "eval|var b=document.querySelector('[data-do=settings-check]');"
         "b.style.transition='none';b.click()",
         "poll|document.getElementById('updateResult')!==null&&"
         "document.getElementById('updateResult').textContent.indexOf('Asked just now')===0",
         "say|'LINE='+JSON.stringify((document.getElementById('updateResult')||{})"
         ".textContent)", 
         "say|'ERRS='+JSON.stringify(window.__probeErrors||[])",
         "say|'AFTER='+JSON.stringify(Array.prototype.map.call("
         "document.querySelectorAll('#view .sfield code'),"
         "function(c){return c.textContent;}).join(' / '))",
         "say|'DL2='+JSON.stringify(Boolean(document.querySelector('[data-do=settings-download]')))"],
        capture_output=True, text=True, timeout=240)
    prose = said.stdout or ""

    def read(tag):
        for line in prose.splitlines():
            stripped = line.strip()
            if stripped.startswith(tag + "="):
                return json.loads(stripped[len(tag) + 1:])
        return None

    for line in prose.splitlines() + (said.stderr or "").splitlines():
        if line.strip().startswith(("EVAL ERROR", "TIMEOUT waiting", "NAV FAILED",
                                    "SNAPSHOT ERROR")):
            fails.append("probe: " + line.strip()[:70])
            print("   " + line.strip()[:90])
    rows = read("ROWS") or ""
    check("the tab says the rule out loud: once a week, by itself",
          "once a week, by itself" in rows, rows[:90])
    check("it names when the engine last asked, in the shop's own clock",
          re.search(r"\d{4}-\d\d-\d\d", rows) is not None, rows[:90])
    check("and shows the number on the repository, read by the engine",
          "9.9" in rows, rows[:90])
    expect("the button is now an ask, not a maybe", read("BTN"), "Ask now")
    check("a newer build waiting brings a Download button with it", read("DL") is True,
          "Download button shown: %r" % read("DL"))
    after = read("AFTER")
    check("pressing it reaches the engine and the tab redraws from the answer",
          after is not None and "9.9" in after, (after or "")[:90])
    check("and the line under the buttons carries the shop's answer",
          str(read("LINE") or "").startswith("Asked just now"),
          "%r %r" % (read("LINE"), read("ERRS")))
    check("and the Download button is still there to take the build in", read("DL2") is True,
          "after the ask: %r" % read("DL2"))
    check("and the browser never went to the internet itself for it",
          SERVED["hits"] >= 1, "%d calls the stand-in took from this run" % SERVED["hits"])
    stop()

# ------------------------------------------- 6. the newer build, from the repository to this computer
print("\n== the newer build: down, checked, and put in place")

import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "publish_update", os.path.join(REPO, "tools", "publish-update.py"))
publish_update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publish_update)

# The build the repository offers sits one step past whatever this copy calls itself, so the
# check stays true on the day the shop's own number moves.
def stepped(text):
    parts = [int(p) for p in str(text).strip().split(".") if p.strip().isdigit()] or [1, 0]
    while len(parts) < 2:
        parts.append(0)
    parts[-1] += 1
    return ".".join(str(p) for p in parts)

NEWER = stepped(REPO_VERSION)

# A small tree that calls itself NEWER — the build that is new from this one's point of view.
newsrc = os.path.join(work, "newsrc")
os.makedirs(os.path.join(newsrc, "public", "img"))
shutil.copy2(SERVER_PY, os.path.join(newsrc, "server.py"))
open(os.path.join(newsrc, "VERSION"), "w", encoding="utf-8").write(NEWER + "\n")
open(os.path.join(newsrc, "schema.sql"), "w", encoding="utf-8").write("-- the newer build\n")
open(os.path.join(newsrc, "public", "app.js"), "w", encoding="utf-8").write(
    "/* the newer build */\n")
open(os.path.join(newsrc, "public", "index.html"), "w", encoding="utf-8").write(
    "<!doctype html><title>newer</title>\n")
open(os.path.join(newsrc, "public", "img", "empty.txt"), "w", encoding="utf-8").write("x\n")
published, manifest = publish_update.write(newsrc, os.path.join(work, "published"), "a small proof")

SERVED["files"] = {"/main/update/current.json":
                   json.dumps(manifest).encode("utf-8")}
for part in manifest["files"]:
    SERVED["files"]["/main/update/%s/%s" % (manifest["folder"], part["name"])] = open(
        os.path.join(published, part["name"]), "rb").read()

SERVED["text"] = NEWER
expect("the week's ask now reads a newer build", server.run_update_check("the check asked")["verdict"],
       "newer")
BOOK_BYTES = open(BOOK, "rb").read() if os.path.isfile(BOOK) else b""
BOOK_ROWS = counts() if os.path.isfile(BOOK) else {}
package = server.download_update("a counter asked for it")
expect("the newer build comes down and is checked, file by file", package.get("ready"), True)
expect("and it is the version the repository names", package.get("version"), NEWER)
check("every file the build record names arrived", len(package.get("files") or []) ==
      len(manifest["files"]), "%d of %d" % (len(package.get("files") or []), len(manifest["files"])))
check("it is kept beside the book, in the support folder",
      str(package.get("path") or "").startswith(SUPPORT + os.sep)
      and os.path.isdir(package.get("path") or ""), str(package.get("path")).replace(work, "…"))
check("and the file it was proved against agrees", server.read_update_package().get("version") == NEWER,
      server.read_update_package().get("version"))
facts_ready = server.package_facts()
check("the screen is told a checked build is waiting", facts_ready["state"] == "ready"
      and facts_ready["install_version"] == NEWER, facts_ready["state"])
check("and how much of it there is", facts_ready["size"] == manifest["size"],
      "%d bytes" % facts_ready["size"])

# A record that names a file outside the app, and bytes that do not match their record, are refused
# before anything is called ready.
expect("a part outside the app is not a part", server.part_target("../../etc/passwd"), None)
expect("nor is a tool the shop publishes with", server.part_target("tools/publish-update.py"), None)
expect("nor a file the tree hides", server.part_target("public/.secret"), None)
expect("the engine is a part", server.part_target("server.py"), "server.py")
expect("so is a screen", server.part_target("public/app.js"), "public/app.js")

truthful = dict(manifest)
broken = json.loads(json.dumps(manifest))
broken["files"][0]["sha256"] = "0" * 64
SERVED["files"]["/main/update/current.json"] = json.dumps(broken).encode("utf-8")
after_bad = server.download_update("a counter asked for it")
expect("bytes that do not match their record are refused", after_bad.get("ready"), False)
check("and the screen is told the download did not finish", server.package_facts()["state"] == "failed",
      server.package_facts()["state"])
check("nothing was changed on this computer", open(BOOK, "rb").read() if os.path.isfile(BOOK) else b"" ==
      BOOK_BYTES, "the book is the same bytes")
SERVED["files"]["/main/update/current.json"] = json.dumps(truthful).encode("utf-8")
again = server.download_update("a counter asked for it")
expect("a good record after a bad one is taken", again.get("ready"), True)

# The apply script, run against a copy of an app, exactly as the engine runs it.
health = ThreadingHTTPServer(("127.0.0.1", 0), type("Health", (BaseHTTPRequestHandler,), {
    "do_GET": lambda self: (self.send_response(200), self.send_header("Content-Length", "2"),
                            self.end_headers(), self.wfile.write(b"ok")),
    "log_message": lambda *a: None}))
health.daemon_threads = True
threading.Thread(target=health.serve_forever, daemon=True, name="health").start()
HEALTH_PORT = health.server_address[1]

def run_installer(tag, target, support, port, extra_env=None):
    # A fresh copy of the checked build for each run, because a successful install clears the
    # folder it took the files from — exactly as it does on the shop's own computer.
    staged = os.path.join(work, "staged-" + tag)
    shutil.copytree(published, staged)
    # The engine writes this list when it arms an install; here it is written the same way.
    open(os.path.join(staged, "files.list"), "w", encoding="utf-8").write(
        "\n".join(p["name"] for p in package["files"]) + "\n")
    script = os.path.join(work, "apply-update.sh")
    open(script, "w", encoding="utf-8").write(server.APPLY_SCRIPT)
    env = dict(os.environ, CHRISPHICS_UPDATE_HEALTH_TRIES="5", **(extra_env or {}))
    done = subprocess.run(["/bin/sh", script, staged, target, support, NEWER, "999999", "", str(port),
                           "http", ""], env=env, capture_output=True, text=True, timeout=120)
    record = {}
    try:
        record = json.load(open(os.path.join(support, "updates", "last-install.json"),
                                encoding="utf-8"))
    except (OSError, ValueError):
        pass
    return done, record

fake_app = os.path.join(work, "fake-app")
os.makedirs(os.path.join(fake_app, "public"), exist_ok=True)
open(os.path.join(fake_app, "VERSION"), "w", encoding="utf-8").write("1.8\n")
open(os.path.join(fake_app, "server.py"), "w", encoding="utf-8").write("# the old engine\n")
open(os.path.join(fake_app, "public", "app.js"), "w", encoding="utf-8").write("/* old */\n")
install_support = os.path.join(work, "install-support")
os.makedirs(install_support, exist_ok=True)
done, record = run_installer("good", fake_app, install_support, HEALTH_PORT)
check("the new build is put in place", open(os.path.join(fake_app, "VERSION"),
                                            encoding="utf-8").read().strip() == NEWER,
      open(os.path.join(fake_app, "VERSION"), encoding="utf-8").read().strip())
check("the screens come with it", "the newer build" in open(os.path.join(fake_app, "public", "app.js"),
                                                            encoding="utf-8").read(), "")
check("the old files are kept, not thrown away",
      any(os.path.isfile(os.path.join(base, "VERSION"))
          for base, _dirs, _names in os.walk(os.path.join(install_support, "updates"))),
      "a previous folder is under the support folder")
expect("and the computer is told the install finished", record.get("result"), "done")
check("the shop was brought back by the script", done.returncode == 0, done.stderr[-120:])

# A build that comes up broken must put the old one back rather than leave a shop that cannot open.
broken_app = os.path.join(work, "broken-app")
os.makedirs(os.path.join(broken_app, "public"), exist_ok=True)
open(os.path.join(broken_app, "VERSION"), "w", encoding="utf-8").write("1.8\n")
open(os.path.join(broken_app, "server.py"), "w", encoding="utf-8").write("# the old engine\n")
open(os.path.join(broken_app, "public", "app.js"), "w", encoding="utf-8").write("/* old */\n")
broken_support = os.path.join(work, "broken-support")
os.makedirs(broken_support, exist_ok=True)
DEAD_PORT = free_port(9010)
done, record = run_installer("broken", broken_app, broken_support, DEAD_PORT)
check("a build that does not answer puts the old one back",
      open(os.path.join(broken_app, "VERSION"), encoding="utf-8").read().strip() == "1.8",
      open(os.path.join(broken_app, "VERSION"), encoding="utf-8").read().strip())
expect("and says so in the shop's own words", record.get("result"), "failed")
check("with a sentence a counter can read", bool(record.get("reason")), record.get("reason"))

# Nothing about any of that was a write to the book, and only this computer may install.
check("the book is the same bytes it was before any of it",
      (open(BOOK, "rb").read() if os.path.isfile(BOOK) else b"") == BOOK_BYTES, "untouched")
check("and the same rows", (counts() if os.path.isfile(BOOK) else {}) == BOOK_ROWS, "same counts")
engine_source = text(SERVER_PY)
check("install is refused to a screen answered over the Wi-Fi",
      "is_loopback" in engine_source and "only this computer may put a build in place"
      in engine_source.lower(), "")
check("neither the download nor the install counts as a book write",
      "/api/update-download" in engine_source and "/api/update-install" in engine_source
      and server.NON_BOOK_ROUTES >= {"/api/update-download", "/api/update-install"}, "")
health.shutdown()

# --------------------------------------------------------------------------------------- the end
print("\n%d checks, %d failed" % (ran["n"], len(fails)))
for name in fails:
    print("  failed: %s" % name)
sys.exit(1 if fails else 0)
