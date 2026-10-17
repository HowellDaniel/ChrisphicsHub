#!/usr/bin/env python3
"""Prove the off-site book — a private repository that holds the shop's copies — on a throwaway.

The shop's own book is never opened here. The whole run works on a brand-new SQLite file under a
temp folder, with its own support folder and its own backups folder, on a spare port.

The other end of the wire is a stand-in for api.github.com: the same three routes the real one has
(read a repository, read a file's contents, write a file's contents), the same `private` flag, the
same base64 body, the same sha-and-conflict rule an update is checked against, and the same refusal
when the token is not the one the repository expects. It answers on 127.0.0.1 over plain HTTP, which
is why CHRISPHICS_OFFSITE_API exists at all — aimed at GitHub the base is HTTPS and this same code
talks to these same routes. So what is proven below is the shop's half: what it sends, what it
refuses to send, and what it does when a redeploy leaves its own disk blank.

Usage: python3 tools/check-offsite-book.py
"""
import base64
import hashlib
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
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8896
# A fabricated token, never a real one: its only job is to let the checks prove where it goes, and
# that it never appears in a book, a log or a commit message.
TEST_TOKEN = "ghp_ThrowawayTokenNeverReal_0123456789"
WRONG_TOKEN = "ghp_WrongTokenNeverAccepted_0000000000"
PRIVATE = "brownhub/throwaway-book-private"
PUBLIC = "brownhub/throwaway-book-public"
BOOK_NAME = "chrisphics-book.db"

work = tempfile.mkdtemp(prefix="chrisphics-offsite-")
BOOK = os.path.join(work, "chrisphics.db")
SUPPORT = os.path.join(work, "support")
BACKUPS = os.path.join(work, "Backups")
os.makedirs(SUPPORT)
os.makedirs(BACKUPS)

REPOS = {PRIVATE: {"private": True, "files": {}}, PUBLIC: {"private": False, "files": {}}}
AUDIT = []          # every call the stand-in took, so a refusal can be proven by what is absent
CONTENTS = re.compile(r"^/repos/([^/]+/[^/]+)/contents/(.+)$")
NAMED = re.compile(r"^/repos/([^/]+/[^/]+)$")


def heard(method, path, auth="none", message=""):
    AUDIT.append({"method": method, "path": path, "auth": auth, "message": message})


class Door(BaseHTTPRequestHandler):
    """Three routes and a memory. Nothing else on this machine is reachable through it."""

    def _reply(self, code, payload, raw=False):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/vnd.github.raw" if raw else "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _accepted(self):
        head = self.headers.get("Authorization") or ""
        ok = head == "Bearer " + TEST_TOKEN
        heard(self.command, self.path, "bearer" if ok else "refused")
        if not ok:
            self._reply(401, {"message": "Bad credentials"})
        return ok

    def do_GET(self):
        if not self._accepted():
            return
        named = NAMED.match(self.path)
        if named:
            repo = REPOS.get(named.group(1))
            if repo is None:
                return self._reply(404, {"message": "Not Found"})
            return self._reply(200, {"full_name": named.group(1), "private": repo["private"]})
        content = CONTENTS.match(self.path)
        if content:
            files = (REPOS.get(content.group(1)) or {}).get("files", {})
            name = urllib.parse.unquote(content.group(2))
            blob = files.get(name)
            if blob is None:
                return self._reply(404, {"message": "Not Found"})
            if "raw" in (self.headers.get("Accept") or ""):
                return self._reply(200, blob, raw=True)
            return self._reply(200, {"name": name, "path": name, "size": len(blob),
                                     "sha": hashlib.sha1(blob).hexdigest(), "encoding": "base64",
                                     "content": base64.b64encode(blob).decode("ascii")})
        self._reply(404, {"message": "Not Found"})

    def do_PUT(self):
        if not self._accepted():
            return
        content = CONTENTS.match(self.path)
        if not content:
            return self._reply(404, {"message": "Not Found"})
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._reply(422, {"message": "Body must be JSON."})
        files = REPOS.setdefault(content.group(1), {"private": False, "files": {}})["files"]
        name = urllib.parse.unquote(content.group(2))
        message = str(payload.get("message") or "")
        heard("COMMIT", content.group(1) + "/" + name, "bearer", message)
        try:
            blob = base64.b64decode(payload.get("content") or "", validate=True)
        except ValueError:
            return self._reply(422, {"message": "content is not base64."})
        held = files.get(name)
        if held is not None and payload.get("sha") != hashlib.sha1(held).hexdigest():
            # The real route refuses an update that does not carry the sha of what is there, so a
            # build that always created and never updated would be caught on this line.
            return self._reply(409, {"message": "Conflict: requested sha did not match HEAD."})
        files[name] = blob
        self._reply(200, {"content": {"name": name, "sha": hashlib.sha1(blob).hexdigest()},
                          "commit": {"sha": "c" * 40, "message": message}})

    def log_message(self, *_args):
        pass


door = ThreadingHTTPServer(("127.0.0.1", 0), Door)
door.daemon_threads = True
threading.Thread(target=door.serve_forever, daemon=True, name="fake-github").start()
API = "http://127.0.0.1:%d" % door.server_address[1]

fails = []
handles = {}
procs = {}


def check(label, ok, detail=""):
    print("%-58s %s %s" % (label, "ok" if ok else "FAIL", detail))
    if not ok:
        fails.append(label)


def ask(method, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path), data=data,
                                     method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", "replace")
    try:
        return json.loads(body or "{}")
    except ValueError:
        return {"error": body[:140]}


def boot(tag, repo="", token=TEST_TOKEN, name=BOOK_NAME, api=None):
    """Start the book the way the hosted box starts it, on a throwaway file and with the off-site
    leg aimed at the stand-in instead of api.github.com."""
    probe = socket.socket()
    probe.settimeout(0.6)
    try:
        # Something answering here means another process holds the shop's number, and a check run
        # against it would be proving that process's behaviour. A port left in TIME_WAIT by the boot
        # that just ended is not that — so this asks whether anything listens, not whether it binds.
        probe.connect(("127.0.0.1", PORT))
        raise SystemExit("port %d is already answered by something else — a check that runs against"
                         " a port someone else is holding proves nothing" % PORT)
    except (ConnectionRefusedError, OSError):
        pass
    finally:
        probe.close()
    env = dict(os.environ, CHRISPHICS_DB=BOOK, CHRISPHICS_SUPPORT=SUPPORT,
               CHRISPHICS_BACKUP_DIR=BACKUPS, PYTHONUNBUFFERED="1", CHRISPHICS_BACKUP_CHECK="600",
               CHRISPHICS_OFFSITE_API=api or API, CHRISPHICS_OFFSITE_REPO=repo,
               CHRISPHICS_OFFSITE_TOKEN=token, CHRISPHICS_OFFSITE_PATH=name)
    handle = open(os.path.join(work, "server-%s.log" % tag), "wb")
    handles[tag] = handle
    procs[tag] = subprocess.Popen(["/usr/bin/python3", os.path.join(REPO, "server.py"),
                                  "--port", str(PORT), "--no-browser"],
                                 cwd=REPO, env=env, stdout=handle, stderr=handle)
    for _ in range(80):
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/healthz" % PORT, timeout=3) as ok:
                if ok.status == 200:
                    return
        except Exception:
            pass
        time.sleep(0.1)
    raise SystemExit("the throwaway book on port %d never answered; see server-%s.log" % (tag, tag))


def stop(tag):
    proc = procs.pop(tag, None)
    if proc:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    handles.pop(tag, None)


def log_text(tag):
    with open(os.path.join(work, "server-%s.log" % tag), "rb") as fh:
        return fh.read().decode("utf-8", "replace")


def offsite_lines(tag):
    said = [line for line in log_text(tag).splitlines() if "off-site" in line.lower()
            or "restored" in line.lower()]
    return said[-1] if said else "no word about it in the boot log"


def wipe_local_book():
    """A redeploy on a free instance is exactly this: the disk comes back with nothing on it."""
    for extra in ("", "-wal", "-shm"):
        try:
            os.remove(BOOK + extra)
        except OSError:
            pass
    for name in os.listdir(BACKUPS):
        os.remove(os.path.join(BACKUPS, name))


def bytes_of(path):
    with open(path, "rb") as fh:
        return fh.read()


def count_rows(table):
    if not os.path.exists(BOOK):
        return -1
    book = sqlite3.connect("file:%s?mode=ro" % BOOK, uri=True)
    try:
        return book.execute("SELECT count(*) FROM %s" % table).fetchone()[0]
    except sqlite3.Error:
        return -1
    finally:
        book.close()


def writes_to(where):
    return [a for a in AUDIT if a["method"] == "PUT"
            and a["path"].startswith("/repos/%s/contents/" % where)]


# ------------------------------------------------------------------ nothing named, nothing sent
print("\n--- a shop that never named a repository (loopback, throwaway book)")
boot("plain")
report = ask("POST", "/api/backups", {})
check("a copy is still written, checked and tidied", "checked" in report.get("note", ""),
      report.get("file"))
check("with no repository named the off-site leg says nothing",
      "off-site" not in report.get("note", "") and report.get("offsite_set") is False,
      report.get("note", "")[:60])
check("and nothing at all was asked of the repository", not AUDIT, "%d calls" % len(AUDIT))
stop("plain")

# ------------------------------------------------------------------- a clear-text address is refused
print("\n--- an address that would carry the token in the open")
calls = len(AUDIT)
boot("cleartext", repo=PRIVATE, api="http://192.0.2.1")
check("the leg refuses an HTTP address that is not this machine's own",
      "plain text" in log_text("cleartext"), offsite_lines("cleartext")[:74])
check("and it sends nothing, so no token ever left in the clear", len(AUDIT) == calls,
      "%d calls made all together" % len(AUDIT))
stop("cleartext")

# -------------------------------------------------------------------- a public repo is refused
print("\n--- a public repository: the book never goes there")
boot("public", repo=PUBLIC)
report = ask("POST", "/api/backups", {})
check("the copy card says the off-site leg refused",
      "off-site failed" in report.get("note", "")
      and "not a private repository" in report.get("note", ""),
      report.get("note", "").split("off-site")[-1][:58])
check("and no byte of the ledger reached it",
      not writes_to(PUBLIC) and not REPOS[PUBLIC]["files"],
      "%d writes, %d files held" % (len(writes_to(PUBLIC)), len(REPOS[PUBLIC]["files"])))
check("it was looked at first, which is how the flag is known",
      any(a["method"] == "GET" and a["path"] == "/repos/%s" % PUBLIC for a in AUDIT))
check("the copy the shop can still see is on this Mac and checked",
      bool(report.get("file")) and os.path.exists(os.path.join(BACKUPS, report["file"])),
      str(report.get("file")))
check("a refused leg is a note, not a fault in the shop's server",
      report.get("error") == "" and report.get("offsite_note", "").startswith("failed"),
      report.get("offsite_note", "")[:58])
stop("public")

# ------------------------------------------------------------------------ a private repo takes it
print("\n--- a private repository takes the copy")
boot("private", repo=PRIVATE)
client = ask("POST", "/api/clients", {"name": "Off-site Check Client", "phone": "055 000 1122"})
job = ask("POST", "/api/jobs", {"client_id": client["id"], "kind": "Job",
                                "title": "Off-site check banners", "quantity": 4, "unit": "pcs",
                                "unit_price": 75, "category": "Banners"})
payment = ask("POST", "/api/payments", {"job_id": job["id"], "amount": 100, "method": "Cash",
                                        "note": "deposit on the throwaway"})
check("the throwaway holds a client, a job and money to be copied",
      client.get("id") and job.get("id") and payment.get("id"),
      "ref %s · total %s" % (job.get("ref"), job.get("total")))
check("on a first run the repository holds no copy yet, and says so rather than making one up",
      "no copy of the book is in" in log_text("private"), offsite_lines("private")[:74])
report = ask("POST", "/api/backups", {})
held = REPOS[PRIVATE]["files"].get(BOOK_NAME)
check("the private repository took the copy",
      "off-site " in report.get("note", "") and isinstance(held, bytes),
      report.get("note", "").split("off-site")[-1][:56])
disk = bytes_of(os.path.join(BACKUPS, report["file"]))
check("the bytes it holds are the bytes it was given",
      isinstance(held, bytes) and hashlib.sha256(held).digest() == hashlib.sha256(disk).digest(),
      "%d KB of book" % (len(held or b"") // 1024))
check("and the book keeps where the copy went",
      report.get("offsite_note", "").startswith("sent to") and bool(report.get("offsite_at")),
      report.get("offsite_note", "")[:56])
second = ask("POST", "/api/backups", {})
check("a second copy updates the same file instead of colliding with it",
      len(writes_to(PRIVATE)) == 2 and second.get("offsite_note", "").startswith("sent"),
      "%d writes · %s" % (len(writes_to(PRIVATE)), second.get("note", "")[-42:]))
stop("private")

# ---------------------------------------------------------------------------- a redeploy, proven
print("\n--- what a redeploy does, and what the leg does about it")
wipe_local_book()
check("the throwaway book really is gone", count_rows("jobs") == -1)
boot("restore", repo=PRIVATE)
check("a blank disk is filled from the repository before the counter sees it",
      "Book restored from the off-site repository" in log_text("restore"),
      offsite_lines("restore")[:74])
back = ask("GET", "/api/jobs/%s" % job["id"])
check("the job booked before the wipe is back, with its client and its money",
      back.get("ref") == job["ref"] and back.get("client") == "Off-site Check Client"
      and float(back.get("paid") or 0) >= 100.0,
      "%s · %s · paid %s" % (back.get("ref"), back.get("client"), back.get("paid")))
card = ask("GET", "/api/backups")
check("the restored book says so on the copy card",
      "from " in (card.get("offsite_restore") or "") and bool(card.get("offsite_restore_at")),
      card.get("offsite_restore", ""))
stop("restore")

boot("keep", repo=PRIVATE)
check("booting again onto a book that now has records refuses to pour over it",
      "already holds records" in log_text("keep"), offsite_lines("keep")[:74])
check("and the records that were restored are still there",
      count_rows("jobs") == 1, "%d job(s) in the book" % count_rows("jobs"))
stop("keep")

# --------------------------------------------------------------- what it will not swallow
print("\n--- a copy that is not a book, and a token that is not accepted")
good = REPOS[PRIVATE]["files"][BOOK_NAME]
REPOS[PRIVATE]["files"][BOOK_NAME] = b"this is a print shop's ledger in name only"
wipe_local_book()
boot("garbage", repo=PRIVATE)
check("a file that is not a SQLite book is refused, not poured in",
      "not a SQLite book" in log_text("garbage"), offsite_lines("garbage")[:74])
check("and the shop still opens to an empty book rather than not opening",
      ask("POST", "/api/backups", {}).get("file") is not None,
      "the book answered and made a copy of itself")
stop("garbage")

REPOS[PRIVATE]["files"][BOOK_NAME] = good
wipe_local_book()
boot("badtoken", repo=PRIVATE, token=WRONG_TOKEN)
check("a token the repository refuses does not stop the shop starting",
      "Bad credentials" in log_text("badtoken"), offsite_lines("badtoken")[:74])
check("and the book stays empty rather than half-filled",
      count_rows("jobs") == 0, "%d job(s) after a refused pull" % count_rows("jobs"))
check("the refused word is written as a refusal in the log, not as a crash",
      "Traceback" not in log_text("badtoken")
      and bool([a for a in AUDIT if a["auth"] == "refused"]))
stop("badtoken")

boot("halffalse", repo=PRIVATE, token="")
check("a named repository with no token is reported as a missing word",
      "no token is saved" in log_text("halffalse"), offsite_lines("halffalse")[:74])
check("and the book still starts, with its own copy card live",
      ask("GET", "/api/backups").get("offsite_set") is True, "the health route answered")
stop("halffalse")

# --------------------------------------------------------------------- where the token never goes
print("\n--- where the token never goes")
wipe_local_book()
spaced = "shop copies/chrisphics book.db"
boot("hygiene", repo=PRIVATE, name=spaced)
ask("POST", "/api/backups", {})
stop("hygiene")
check("the token never rides in a URL or a path",
      not [a for a in AUDIT if TEST_TOKEN in a["path"] or TEST_TOKEN in a["message"]])
check("the token is only ever carried as a bearer header",
      AUDIT and all(a["auth"] in ("bearer", "refused") for a in AUDIT),
      "%d calls, %d with the header" % (len(AUDIT),
                                        len([a for a in AUDIT if a["auth"] == "bearer"])))
commit = [a for a in AUDIT if a["method"] == "COMMIT"]
check("a commit message names the copy and nothing of the shop's records",
      commit and all("book copy" in a["message"] for a in commit),
      commit[-1]["message"][:60] if commit else "no commit seen")
check("a file name with spaces and a folder in it reaches the contents route",
      spaced in REPOS[PRIVATE]["files"], sorted(REPOS[PRIVATE]["files"])[-1][:52])
logs = "".join(log_text(tag) for tag in ("plain", "public", "private", "restore", "keep",
                                         "garbage", "badtoken", "halffalse", "hygiene"))
check("the token appears in no server log", TEST_TOKEN not in logs)
check("the token is not inside the book either", TEST_TOKEN.encode() not in bytes_of(BOOK))
rows = sqlite3.connect("file:%s?mode=ro" % BOOK, uri=True).execute(
    "SELECT key, value FROM app_state").fetchall()
check("no off-site answer was written into the book as a secret",
      not any(TEST_TOKEN in str(value) for _key, value in rows),
      " · ".join(sorted(key for key, _v in rows if "offsite" in key))[:60])
check("and the repository was asked nothing outside its own routes",
      all(a["path"].startswith("/repos/") or a["method"] == "COMMIT" for a in AUDIT),
      "%d calls in all" % len(AUDIT))

print("\nWork folder: %s" % work)
shutil.rmtree(work, ignore_errors=True)
door.shutdown()
print("%s" % ("all checks passed" if not fails else "FAILED: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
