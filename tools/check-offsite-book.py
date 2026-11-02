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
# Two more machines, for the leg that needs more than one: the shop's own computer, and a laptop that
# walked out of the Wi-Fi. Each has its own book, its own support folder and its own copies folder, so
# nothing one of them writes can be what the other one is answering from.
SHOP_BOOK = os.path.join(work, "shop-machine.db")
SHOP_SUPPORT = os.path.join(work, "shop-support")
SHOP_BACKUPS = os.path.join(work, "shop-Backups")
LAPTOP_BOOK = os.path.join(work, "laptop-machine.db")
LAPTOP_SUPPORT = os.path.join(work, "laptop-support")
LAPTOP_BACKUPS = os.path.join(work, "laptop-Backups")
SHOP_PORT, LAPTOP_PORT = 8897, 8898
for folder in (SHOP_SUPPORT, SHOP_BACKUPS, LAPTOP_SUPPORT, LAPTOP_BACKUPS):
    os.makedirs(folder)
# A fifth machine, for the way the shop's own Mac settles this: nothing in the environment, the
# repository and the token typed at the screen and kept in the mode-600 file beside its book.
SCREEN_BOOK = os.path.join(work, "screen-machine.db")
SCREEN_SUPPORT = os.path.join(work, "screen-support")
SCREEN_BACKUPS = os.path.join(work, "screen-Backups")
SCREEN_PORT = 8899
os.makedirs(SCREEN_SUPPORT)
os.makedirs(SCREEN_BACKUPS)
# A sixth shape of the same machine: the book reached the way a phone on the shop's Wi-Fi reaches it,
# through a proxy that names the visitor. The card is still readable — it carries nothing worth
# stealing — but the repository and the trips are not, because nobody is standing at that Mac.
PHONE_PORT = 8900
SWITCH_PORT = 8901
VISITOR = {"X-Forwarded-For": "192.168.100.88"}
APPJS = os.path.join(REPO, "public", "app.js")

REPOS = {PRIVATE: {"private": True, "files": {}}, PUBLIC: {"private": False, "files": {}}}
AUDIT = []          # every call the stand-in took, so a refusal can be proven by what is absent
CONTENTS = re.compile(r"^/repos/([^/]+/[^/]+)/contents/(.+)$")
NAMED = re.compile(r"^/repos/([^/]+/[^/]+)$")


def gitsha(blob):
    """The name a git repository gives a file's bytes — what its metadata answers as `sha`, and what
    an update has to carry. A stand-in that made this number up would let the shop's half pass here
    and fail against the real thing."""
    return hashlib.sha1(b"blob %d\0" % len(blob) + blob).hexdigest()


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
                                     "sha": gitsha(blob), "encoding": "base64",
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
        if held is not None and payload.get("sha") != gitsha(held):
            # The real route refuses an update that does not carry the sha of what is there, so a
            # build that always created and never updated would be caught on this line.
            return self._reply(409, {"message": "Conflict: requested sha did not match HEAD."})
        files[name] = blob
        self._reply(200, {"content": {"name": name, "sha": gitsha(blob)},
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


def ask(method, path, payload=None, port=PORT, headers=None):
    data = None if payload is None else json.dumps(payload).encode()
    heads = {"Content-Type": "application/json"}
    heads.update(headers or {})
    request = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=data,
                                     method=method, headers=heads)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", "replace")
    try:
        return json.loads(body or "{}")
    except ValueError:
        return {"error": body[:140]}


def free(port):
    """Is this number clear? Something answering here means another process holds it, and a check run
    against that process would be proving its behaviour, not the shop's. A port left in TIME_WAIT by
    the boot that just ended is not that — so this asks whether anything listens, not whether it
    binds."""
    probe = socket.socket()
    probe.settimeout(0.6)
    try:
        probe.connect(("127.0.0.1", port))
        return False
    except (ConnectionRefusedError, OSError):
        return True
    finally:
        probe.close()


def boot(tag, repo="", token=TEST_TOKEN, name=BOOK_NAME, api=None, book=BOOK, support=SUPPORT,
         backups=BACKUPS, port=PORT, role="", minutes="", extra=()):
    """Start the book the way the hosted box starts it, on a throwaway file and with the off-site
    leg aimed at the stand-in instead of api.github.com."""
    if not free(port):
        raise SystemExit("port %d is already answered by something else — a check that runs against"
                         " a port someone else is holding proves nothing" % port)
    env = dict(os.environ, CHRISPHICS_DB=book, CHRISPHICS_SUPPORT=support,
               CHRISPHICS_BACKUP_DIR=backups, PYTHONUNBUFFERED="1", CHRISPHICS_BACKUP_CHECK="600",
               CHRISPHICS_OFFSITE_API=api or API, CHRISPHICS_OFFSITE_REPO=repo,
               CHRISPHICS_OFFSITE_TOKEN=token, CHRISPHICS_OFFSITE_PATH=name,
               CHRISPHICS_OFFSITE_ROLE=role, CHRISPHICS_OFFSITE_MINUTES=minutes,
               CHRISPHICS_OFFSITE_CHECK="5")
    handle = open(os.path.join(work, "server-%s.log" % tag), "wb")
    handles[tag] = handle
    procs[tag] = subprocess.Popen(["/usr/bin/python3", os.path.join(REPO, "server.py"),
                                  "--port", str(port), "--no-browser"] + list(extra),
                                 cwd=REPO, env=env, stdout=handle, stderr=handle)
    for _ in range(80):
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/healthz" % port, timeout=3) as ok:
                if ok.status == 200:
                    return
        except Exception:
            pass
        time.sleep(0.1)
    raise SystemExit("the throwaway book on port %d never answered; see server-%s.log" % (port, tag))


def wait_for(said, seconds=90):
    """Give the clock its turn. The depository moves on its own schedule, so a check that demands the
    answer the moment after the write would be proving nothing about the schedule at all."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if said():
            return True
        time.sleep(1)
    return bool(said())


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


def count_rows(table, book=BOOK):
    """How many records one machine's own file holds — the only honest way to say whether a copy taken
    out of the depository actually arrived."""
    if not os.path.exists(book):
        return -1
    reader = sqlite3.connect("file:%s?mode=ro" % book, uri=True)
    try:
        return reader.execute("SELECT count(*) FROM %s" % table).fetchone()[0]
    except sqlite3.Error:
        return -1
    finally:
        reader.close()


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

# --------------------------------- the shop writes, and a machine that was away catches up
print("\n--- two machines, one book out in the depository, no wire between them")
REPOS[PRIVATE]["files"] = {}
boot("shopdesk", repo=PRIVATE, book=SHOP_BOOK, support=SHOP_SUPPORT, backups=SHOP_BACKUPS,
     port=SHOP_PORT, role="shop", minutes="0")
boot("laptop", repo=PRIVATE, book=LAPTOP_BOOK, support=LAPTOP_SUPPORT, backups=LAPTOP_BACKUPS,
     port=LAPTOP_PORT, role="own", minutes="0")
check("both open on their own empty books, and neither was told where the other one is",
      count_rows("clients", SHOP_BOOK) == 0 and count_rows("clients", LAPTOP_BOOK) == 0,
      "%d client(s) at the shop, %d on the laptop"
      % (count_rows("clients", SHOP_BOOK), count_rows("clients", LAPTOP_BOOK)))
walk_in = ask("POST", "/api/clients", {"name": "Depository Walk-in", "phone": "055 100 2003"},
              port=SHOP_PORT)
walk_job = ask("POST", "/api/jobs", {"client_id": walk_in["id"], "kind": "Job",
                                     "title": "Depository walk-in banners", "quantity": 6,
                                     "unit": "pcs", "unit_price": 40, "category": "Banners"},
              port=SHOP_PORT)
check("the shop's own book holds the entry that was just typed at it",
      bool(walk_job.get("id")) and count_rows("jobs", SHOP_BOOK) == 1,
      "ref %s" % walk_job.get("ref"))
check("the shop machine posted it to the depository by itself, with nobody pressing anything",
      wait_for(lambda: b"Depository Walk-in" in (REPOS[PRIVATE]["files"].get(BOOK_NAME) or b"")),
      "%d commit(s) to the repository" % len(writes_to(PRIVATE)))
check("and the machine that was away took it in out of the depository, with no network between them",
      wait_for(lambda: count_rows("clients", LAPTOP_BOOK) >= 1),
      "%d client(s) on the laptop" % count_rows("clients", LAPTOP_BOOK))
ask("POST", "/api/clients", {"name": "Depository Second", "phone": "055 400 5006"}, port=SHOP_PORT)
check("a later entry arrives too, so the leg is a clock and not a one-off",
      wait_for(lambda: count_rows("clients", LAPTOP_BOOK) >= 2),
      "%d client(s) on the laptop, %d at the shop"
      % (count_rows("clients", LAPTOP_BOOK), count_rows("clients", SHOP_BOOK)))
check("the shop has posted the second entry before the quiet window opens",
      wait_for(lambda: b"Depository Second" in (REPOS[PRIVATE]["files"].get(BOOK_NAME) or b"")),
      "%d commit(s) so far" % len(writes_to(PRIVATE)))
commits = len(writes_to(PRIVATE))
time.sleep(16)     # three turns of the fast clock, on a shop that has typed nothing
check("a shop that has not traded since its last post costs the repository no new commit",
      len(writes_to(PRIVATE)) == commits,
      "%d commit(s) before the quiet, %d after" % (commits, len(writes_to(PRIVATE))))
before = count_rows("clients", LAPTOP_BOOK)
again = ask("POST", "/api/offsite", {"action": "take"}, port=LAPTOP_PORT)
check("asking again when nothing moved says so, and adds nothing",
      again.get("ok") is True and "already taken up" in again.get("note", "")
      and count_rows("clients", LAPTOP_BOOK) == before, again.get("note", "")[:58])
away = ask("POST", "/api/clients", {"name": "Away At Home", "phone": "055 700 8009"},
           port=LAPTOP_PORT)
ask("POST", "/api/jobs", {"client_id": away["id"], "kind": "Job",
                          "title": "Away machine flyers", "quantity": 200, "unit": "pcs",
                          "unit_price": 12, "category": "Flyers"}, port=LAPTOP_PORT)
sent = ask("POST", "/api/offsite", {"action": "send"}, port=LAPTOP_PORT)
check("an away machine may post, and it takes what is already out there before overwriting it",
      sent.get("ok") is True and sent.get("did") == "send" and "posted" in sent.get("note", ""),
      sent.get("note", "")[:58])
check("the shop then holds the record made away from it — once, not twice",
      wait_for(lambda: count_rows("clients", SHOP_BOOK) == 3),
      "%d client(s) in the shop's book" % count_rows("clients", SHOP_BOOK))
check("and the job typed on the laptop came back through the depository too",
      wait_for(lambda: count_rows("jobs", SHOP_BOOK) == 2),
      "%d job(s) in the shop's book" % count_rows("jobs", SHOP_BOOK))
held_blob = REPOS[PRIVATE]["files"].get(BOOK_NAME) or b""
check("what the depository holds is the ledger, and never the word that opens it",
      len(held_blob) > 0 and TEST_TOKEN.encode() not in held_blob,
      "%d KB of book out there" % (len(held_blob) // 1024))
card = ask("GET", "/api/offsite", None, port=SHOP_PORT)
check("the card a screen reads has no token in it, only the fact that one is saved",
      "token" not in card and card.get("token_saved") is True and TEST_TOKEN not in json.dumps(card),
      ", ".join(sorted(key for key in card if "token" in key)) or "no token field")
check("and neither machine wrote the token into its own book",
      TEST_TOKEN.encode() not in bytes_of(SHOP_BOOK) and TEST_TOKEN.encode() not in bytes_of(LAPTOP_BOOK))
stop("shopdesk")
stop("laptop")

# ------------------------------------------- settling it the way the shop's own Mac does
print("\n--- naming the depository at the screen, into the file beside the book")
REPOS[PRIVATE]["files"] = {}
boot("screen", repo="", token="", name="", book=SCREEN_BOOK, support=SCREEN_SUPPORT,
     backups=SCREEN_BACKUPS, port=SCREEN_PORT)
unset = ask("GET", "/api/offsite", None, port=SCREEN_PORT)
check("with nothing typed yet the card says the plain truth, and names the screen to go to",
      unset.get("set") is False and "no off-site repository is named" in unset.get("why", "")
      and "Shop & devices" in unset.get("why", ""), unset.get("why", "")[:58])
wrong = ask("PUT", "/api/offsite", {"repo": "two names deep and a space"}, port=SCREEN_PORT)
check("a repository that does not read as owner/name is refused before anything is written",
      "owner/name" in wrong.get("error", "")
      and not os.path.exists(os.path.join(SCREEN_SUPPORT, "offsite-secrets.json")),
      wrong.get("error", "")[:58])
settled = ask("PUT", "/api/offsite", {"repo": PRIVATE, "token": TEST_TOKEN}, port=SCREEN_PORT)
secrets = os.path.join(SCREEN_SUPPORT, "offsite-secrets.json")
mode = oct(os.stat(secrets).st_mode & 0o777) if os.path.exists(secrets) else "no file at all"
check("the shop's own Mac settles the repository and the word at its own screen",
      settled.get("ready") is True and settled.get("repo") == PRIVATE, str(settled.get("repo")))
check("both go into the mode-600 file beside the book, and the token never lands in the book",
      mode == "0o600" and TEST_TOKEN.encode() not in bytes_of(SCREEN_BOOK), mode)
check("the card says a word is saved without ever repeating it",
      settled.get("token_saved") is True and "token" not in settled
      and TEST_TOKEN not in json.dumps(settled),
      ", ".join(sorted(key for key in settled if "token" in key)) or "no token field")
kept = ask("PUT", "/api/offsite", {"repo": PRIVATE, "token": ""}, port=SCREEN_PORT)
check("a blank word keeps the one already saved, since no screen can read it back to retype it",
      kept.get("token_saved") is True and kept.get("ready") is True, str(kept.get("ready")))
screen_client = ask("POST", "/api/clients", {"name": "Screen Settled Client",
                                             "phone": "055 600 7008"}, port=SCREEN_PORT)
ask("POST", "/api/jobs", {"client_id": screen_client["id"], "kind": "Job",
                          "title": "Settled at the screen", "quantity": 3, "unit": "pcs",
                          "unit_price": 90, "category": "Banners"}, port=SCREEN_PORT)
copies_before = len(os.listdir(SCREEN_BACKUPS))
writes_before = len(writes_to(PRIVATE))
first = ask("POST", "/api/offsite", {"action": "send"}, port=SCREEN_PORT)
check("asking the depository for a trip posts this machine's book",
      first.get("ok") is True and "posted" in first.get("note", ""), first.get("note", "")[:58])
check("a post is a snapshot, not a copy in the shop's copies folder, which half-hourly posts would bury",
      len(os.listdir(SCREEN_BACKUPS)) == copies_before,
      "%d file(s) in the copies folder" % len(os.listdir(SCREEN_BACKUPS)))
check("and the record typed at that screen is the one now held out there",
      b"Screen Settled Client" in (REPOS[PRIVATE]["files"].get(BOOK_NAME) or b""))
second_send = ask("POST", "/api/offsite", {"action": "send"}, port=SCREEN_PORT)
check("a second ask for the same ledger makes no second commit",
      second_send.get("ok") is True and "already" in second_send.get("note", "")
      and len(writes_to(PRIVATE)) == writes_before + 1,
      "%d commit(s) for %d asks" % (len(writes_to(PRIVATE)), writes_before + 2))
ask("PUT", "/api/offsite", {"repo": PUBLIC, "token": TEST_TOKEN}, port=SCREEN_PORT)
public_run = ask("POST", "/api/offsite", {"action": "send"}, port=SCREEN_PORT)
check("a public repository is refused when the leg actually runs, and not one byte goes there",
      public_run.get("ok") is False and "not a private repository" in public_run.get("note", "")
      and not REPOS[PUBLIC]["files"], public_run.get("note", "")[:58])
stop("screen")

boot("envscreen", repo=PRIVATE, book=SCREEN_BOOK, support=SCREEN_SUPPORT,
     backups=SCREEN_BACKUPS, port=SHOP_PORT)
own_env = ask("PUT", "/api/offsite", {"repo": "someone-elses/book"}, port=SHOP_PORT)
check("a machine aimed by its service settings refuses to take the repository from a screen",
      "service settings" in own_env.get("error", ""), own_env.get("error", "")[:70])
check("and the environment's answer is still the one the card reads",
      ask("GET", "/api/offsite", None, port=SHOP_PORT).get("repo") == PRIVATE,
      ask("GET", "/api/offsite", None, port=SHOP_PORT).get("repo"))
stop("envscreen")

# ------------------------------------------------- the card as a screen reads it, and who may work it
print("\n--- the depository on the counter's own screen")
boot("visitor", repo=PRIVATE, book=SCREEN_BOOK, support=SCREEN_SUPPORT,
     backups=SCREEN_BACKUPS, port=PHONE_PORT, extra=("--trust-proxy",))
commits_before = len(writes_to(PRIVATE))
read = ask("GET", "/api/offsite", None, port=PHONE_PORT, headers=VISITOR)
check("a machine reaching the book the way a phone does still reads the card",
      read.get("repo") == PRIVATE and read.get("ready") is True and read.get("token_saved") is True,
      str(read.get("repo")))
check("and what it reads carries no word that opens the repository",
      "token" not in read and TEST_TOKEN not in json.dumps(read),
      ", ".join(sorted(key for key in read if "token" in key)) or "no token field")
refused_put = ask("PUT", "/api/offsite", {"repo": "someone-elses/book"},
                  port=PHONE_PORT, headers=VISITOR)
refused_post = ask("POST", "/api/offsite", {"action": "send"}, port=PHONE_PORT, headers=VISITOR)
check("but that same machine cannot settle the depository, nor send a book through it",
      "whose book it is" in refused_put.get("error", "")
      and "whose book it is" in refused_post.get("error", ""),
      refused_put.get("error", "")[:58])
check("and a screen reading the card costs the repository no commit",
      len(writes_to(PRIVATE)) == commits_before,
      "%d commit(s) before, %d after" % (commits_before, len(writes_to(PRIVATE))))
stop("visitor")

# The card offers a switch, so the switch has to be the thing the clock reads. A counter standing on
# mobile data who said 'only when somebody asks' must not watch this machine dial the repository
# every half hour on their own meter.
print("\n--- the switch on the card stops the clock, and does not merely say it does")
REPOS[PRIVATE]["files"] = {}
boot("switched", repo="", token="", name="", book=SCREEN_BOOK, support=SCREEN_SUPPORT,
     backups=SCREEN_BACKUPS, port=SWITCH_PORT, role="shop", minutes="0")
switched = ask("PUT", "/api/offsite", {"repo": PRIVATE, "token": TEST_TOKEN, "auto": False},
               port=SWITCH_PORT)
ask("POST", "/api/clients", {"name": "Switched Off Client", "phone": "055 700 8009"},
    port=SWITCH_PORT)
calls_before = len(AUDIT)
commits_before = len(writes_to(PRIVATE))
time.sleep(16)     # three turns of a clock told to run every zero minutes
check("a machine told to keep still is read by the card as keeping still",
      switched.get("auto") is False, str(switched.get("auto")))
check("and over three turns of the fast clock it asked the repository for nothing",
      len(AUDIT) == calls_before and len(writes_to(PRIVATE)) == commits_before,
      "%d call(s) and %d commit(s) while switched off"
      % (len(AUDIT) - calls_before, len(writes_to(PRIVATE)) - commits_before))
ask("PUT", "/api/offsite", {"auto": True}, port=SWITCH_PORT)
check("the same switch put back on runs the leg by itself, with nobody pressing anything",
      wait_for(lambda: len(writes_to(PRIVATE)) > commits_before),
      "%d commit(s) after the switch went back" % (len(writes_to(PRIVATE)) - commits_before))
stop("switched")

appjs = open(APPJS, encoding="utf-8").read()
check("Shop & devices shows the depository card",
      "function depositoryCard" in appjs and "depositoryCard(depo, shopMac)" in appjs)
check("it asks the book at /api/offsite", "await api('/api/offsite')" in appjs)
check("it offers both legs by hand, and only to the machine whose book it is",
      'data-action="offsite-send"' in appjs and 'data-action="offsite-take"' in appjs
      and "offsiteTrip('send', el)" in appjs and "offsiteTrip('take', el)" in appjs)
check("its token box is a password box, never filled back in",
      'name="token" type="password"' in appjs
      and not re.search(r'name="token"[^>]*value=', appjs),
      "the box that takes the word cannot show it")
check("the card is built from what the engine answered, not from a guess about the calendar",
      "depo.every_minutes" in appjs and "depo.send_note" in appjs and "depo.take_note" in appjs)
check("and it says plain that the repository has to be private before the book goes there",
      "Make that repository private" in appjs and "refuses to post" in appjs)

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
                                         "garbage", "badtoken", "halffalse", "hygiene",
                                         "shopdesk", "laptop", "screen", "envscreen", "visitor",
                                         "switched"))
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
