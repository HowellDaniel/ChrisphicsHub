#!/usr/bin/env python3
"""Prove the Messages reader stops reporting a refusal it did not cause, on a throwaway book.

The owner's ask: "Messages is shut to this app ... fix this error from the dashboard."
The card itself came off the Dashboard on 15 October, but the engine and /api/momo stayed, and the
boot server still reads Messages when watching is on — so what the shop would see is still proved.

Two engines read Messages and both write the one shared line `momo_status` in the one book — the
Mac app's engine and the Wi-Fi server that starts itself at boot. macOS gives the door (Full Disk
Access) to whichever program was granted it, and here that is the signed app, so the boot server is
still shut. Before this change the shut engine re-stamped its refusal over the working reader every
twenty seconds, so the error could never leave the card even after a correct grant. Now the engine
that cannot get in keeps quiet while any read is under MOMO_READ_GRACE seconds old — and it leaves
the working reader's own status and timestamp alone, so its silence cannot renew the proof that
somebody is reading. Two proofs count, because an app built before `momo_read_ok` exists leaves
nothing but a blank status behind: that stamp, or a blank status with a fresh poll behind it. The
card also names the exact entry this engine needs (messages_door) so a grant can be aimed at the
right program.

What is proved here is the shut engine's behaviour, and it is forced rather than trusted to this
Mac: the door was re-granted on 25 October, so `read_messages` now succeeds when this check runs and
the refusal it used to lean on never happens. Every shut-engine case therefore reads Messages from
a file this process cannot open — the same failure a shut app gets, whatever System Settings says
today. Nothing here reads ~/Library/Messages as it is really seen: the only real read is the report
of which way this Mac's door currently stands.

    python3 tools/check-momo-door.py
"""
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE = os.path.expanduser("~/Library/Application Support/Chrisphics Hub/chrisphics.db")

work = tempfile.mkdtemp(prefix="chrisphics-door-")
book = os.path.join(work, "chrisphics.db")
support = os.path.join(work, "support")
backup = os.path.join(work, "backups")
for d in (support, backup):
    os.makedirs(d)

live_hash_before = hashlib.sha256(open(LIVE, "rb").read()).hexdigest()
subprocess.run(["sqlite3", LIVE, ".backup '%s'" % book], check=True)

os.environ["CHRISPHICS_DB"] = book
os.environ["CHRISPHICS_SUPPORT"] = support
os.environ["CHRISPHICS_BACKUP_DIR"] = backup
sys.path.insert(0, REPO)
import server  # noqa: E402

server.connect(book)

REFUSAL = ("Messages is shut to this app. Allow it under System Settings ▸ Privacy & Security "
           "▸ Full Disk Access, then press Check Messages. (DatabaseError)")

# A file that exists but cannot be opened is the shut engine's whole problem, reproduced without
# asking this Mac to lose its grant: sqlite refuses the read-only open and the reader reports it.
SHUT_MESSAGES = os.path.join(work, "shut-chat.db")
with open(SHUT_MESSAGES, "wb") as fh:
    fh.write(b"not a Messages file")
os.chmod(SHUT_MESSAGES, 0)

fails = []


def check(name, ok, got=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else "  -> " + str(got)))
    if not ok:
        fails.append(name)


def ago(seconds):
    import datetime as _dt
    return (_dt.datetime.now() - _dt.timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")


def drop(*keys):
    with server._lock:
        for key in keys:
            server._db.execute("DELETE FROM app_state WHERE key = ?", (key,))
        server._db.commit()


def status():
    return server.state_value("momo_status")


def card():
    return server.momo_payload()["status"]


def book_as_the_working_reader_left(read_ok_ago=None, status_text="", checked_ago=5):
    drop("momo_read_ok", "momo_status", "momo_checked")
    if read_ok_ago is not None:
        server.set_state("momo_read_ok", ago(read_ok_ago))
    server.set_state("momo_status", status_text)
    server.set_state("momo_checked", ago(checked_ago))


print("Which way this Mac's own door stands today (a report, not an assertion):")
real_rows, real_problem = server.read_messages(5)
print("  real read: %s" % ("open — %d newest texts" % len(real_rows) if not real_problem
                           else "shut — %s" % real_problem))

print("\nA reader that cannot open Messages says why:")
server.MESSAGES_DB = SHUT_MESSAGES
rows, problem = server.read_messages(5)
check("read_messages refuses for a reason we can name", bool(problem), problem)
check("the refusal is the Full Disk Access one", "Full Disk Access" in problem, problem)

print("\nA shut engine that found a fresh read keeps its peace — and keeps out of the way:")
book_as_the_working_reader_left(read_ok_ago=10, checked_ago=8)
checked_before = server.state_value("momo_checked")
server.poll_messages()
check("momo_status stays empty when a read is 10s old", status() == "", repr(status()))
check("the reader's own timestamp is not taken over by the shut engine",
      server.state_value("momo_checked") == checked_before,
      "%r -> %r" % (checked_before, server.state_value("momo_checked")))
check("the card is clean too", card() == "", repr(card()))

print("\nThe app built before momo_read_ok exists (its only proof is a blank, fresh line):")
book_as_the_working_reader_left(checked_ago=10)
check("no stamp in the book, as that build leaves it",
      server.state_value("momo_read_ok") == "", repr(server.state_value("momo_read_ok")))
server.poll_messages()
check("a blank status with a fresh poll counts as a working read", status() == "", repr(status()))
book_as_the_working_reader_left(checked_ago=server.MOMO_READ_GRACE - 1)
last_word = server.state_value("momo_checked")
server.poll_messages()
check("just inside the grace stays quiet", status() == "", repr(status()))
check("and it does not renew the proof while quiet",
      server.state_value("momo_checked") == last_word,
      "%r -> %r" % (last_word, server.state_value("momo_checked")))
book_as_the_working_reader_left(checked_ago=server.MOMO_READ_GRACE + 30)
server.poll_messages()
check("once the reader stops, the refusal the shop can act on returns",
      "Full Disk Access" in status(), repr(status()))

print("\nAround the grace on the stamp momo_read_ok:")
book_as_the_working_reader_left(read_ok_ago=server.MOMO_READ_GRACE - 1, checked_ago=400)
server.poll_messages()
check("a stamp just inside the grace keeps the shut engine quiet",
      status() == "", repr(status()))
book_as_the_working_reader_left(read_ok_ago=server.MOMO_READ_GRACE + 1, checked_ago=400)
server.poll_messages()
check("past the stamp's grace the refusal returns", "Full Disk Access" in status(), repr(status()))

print("\nThe card answers for itself, whatever another engine last wrote:")
book_as_the_working_reader_left(read_ok_ago=server.MOMO_READ_GRACE - 1, checked_ago=400)
server.set_state("momo_status", REFUSAL)
check("a refusal written by another engine is not repeated while a read is fresh",
      card() == "", repr(card()))
book_as_the_working_reader_left(checked_ago=server.MOMO_READ_GRACE + 30)
server.set_state("momo_status", REFUSAL)
check("with no recent read the refusal is told", "Full Disk Access" in card(), repr(card()))
book_as_the_working_reader_left(checked_ago=10)
server.set_state("momo_status", REFUSAL)
check("a fresh poll that left a refusal in it is not read as a working reader",
      "Full Disk Access" in card(), repr(card()))

print("\nNothing ever read (keys absent, or junk where a time should be):")
drop("momo_read_ok", "momo_status", "momo_checked")
try:
    server.poll_messages()
    check("poll survives an empty book", True)
except Exception as exc:  # noqa: BLE001
    check("poll survives an empty book", False, "%s: %s" % (type(exc).__name__, exc))
check("the refusal is on the card with nothing else to say", "Full Disk Access" in status(),
      repr(status()))
server.set_state("momo_read_ok", "not a timestamp")
server.set_state("momo_checked", "not a timestamp either")
server.set_state("momo_status", "")
check("a junk stamp and a junk poll are not read as a working reader",
      server.messages_are_being_read() is False, repr(server.messages_are_being_read()))
server.poll_messages()
check("junk stamps fall back to the refusal", "Full Disk Access" in status(), repr(status()))

print("\nThe engine that holds the door stamps the shared line:")
real_read = server.read_messages
server.read_messages = lambda limit=60: ([], "")  # what the granted engine sees
try:
    drop("momo_read_ok", "momo_status", "momo_checked")
    server.poll_messages()
    check("working read stamps momo_read_ok", server.state_value("momo_read_ok") != "",
          repr(server.state_value("momo_read_ok")))
    check("working read leaves momo_status blank", status() == "", repr(status()))
    stamped = server.state_value("momo_checked")
    server.read_messages = real_read
    server.poll_messages()
    check("shut engine does not re-stamp over a fresh read", status() == "", repr(status()))
    check("shut engine leaves the reader's timestamp alone",
          server.state_value("momo_checked") == stamped,
          "%r -> %r" % (stamped, server.state_value("momo_checked")))
finally:
    server.read_messages = real_read

print("\nThe card names the entry this engine needs (messages_door):")
door = server.messages_door()
check("repo engine asks for the python that runs it",
      door == os.path.realpath(sys.executable), door)

fake = os.path.join(work, "Chrisphics Hub.app", "Contents", "Resources", "app")
os.makedirs(fake)
shutil.copy(os.path.join(REPO, "server.py"), os.path.join(fake, "server.py"))
probe_src = os.path.join(work, "probe-app-door.py")
with open(probe_src, "w", encoding="utf-8") as fh:
    fh.write("import os,sys,importlib.util\n"
             "sys.path.insert(0, %r)\n"
             "os.environ['CHRISPHICS_DB']=os.path.join(%r,'nothing.db')\n"
             "os.environ['CHRISPHICS_SUPPORT']=os.path.join(%r,'nothing-support')\n"
             "os.environ['CHRISPHICS_BACKUP_DIR']=os.path.join(%r,'nothing-backup')\n"
             "spec=importlib.util.spec_from_file_location('srv_app',os.path.join(%r,'server.py'))\n"
             "m=importlib.util.module_from_spec(spec)\n"
             "spec.loader.exec_module(m)\n"
             "print(m.messages_door())\n" % (REPO, work, work, work, fake))
probe = subprocess.run([sys.executable, probe_src], capture_output=True, text=True)
app_door = (probe.stdout or "").strip().splitlines()[-1] if probe.stdout.strip() else probe.stderr.strip()
check("app-bundle engine asks for the .app, not bare python",
      app_door.endswith(".app") and "Chrisphics Hub.app" in app_door, app_door)

print("\nThe payload the card reads carries the door:")
payload = server.momo_payload()
check("momo_payload has door", "door" in payload, sorted(payload)[:12])
check("momo_payload has ledger", "ledger" in payload, sorted(payload)[:12])

print("\nPressing Check Messages answers with the read that press just made:")
import json  # noqa: E402
import socket  # noqa: E402
import urllib.request  # noqa: E402

PORT = 8941
with socket.socket() as s:
    squatted = s.connect_ex(("127.0.0.1", PORT)) == 0
check("port %d is free before the check runs" % PORT, not squatted)


def post_check():
    req = urllib.request.Request("http://127.0.0.1:%d/api/momo" % PORT,
                                 data=json.dumps({"check": 1}).encode(),
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req) as conn:
        return json.loads(conn.read())


def get_payload():
    with urllib.request.urlopen("http://127.0.0.1:%d/api/momo" % PORT) as conn:
        return json.loads(conn.read())


if not squatted:
    # Watching off: the throwaway engine's own twenty-second poller must not write into the book
    # between these requests and answer for the press.
    server.set_state("momo_watch", "0")
    # The child is started through a runner, not server.py itself, because the press has to be made
    # by an engine that genuinely cannot get into Messages — and this Mac can.
    engine_src = os.path.join(work, "shut-engine.py")
    with open(engine_src, "w", encoding="utf-8") as fh:
        fh.write("import os,sys\n"
                 "sys.path.insert(0, %r)\n"
                 "import server\n"
                 "server.MESSAGES_DB = %r\n"
                 "server.main()\n" % (REPO, SHUT_MESSAGES))
    srv = subprocess.Popen([sys.executable, engine_src,
                            "--port", str(PORT), "--no-browser"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           env={**os.environ, "CHRISPHICS_DB": book,
                                "CHRISPHICS_SUPPORT": support, "CHRISPHICS_BACKUP_DIR": backup})
    for _ in range(60):
        with socket.socket() as probe_port:
            if probe_port.connect_ex(("127.0.0.1", PORT)) == 0:
                break
        time.sleep(0.25)
    try:
        # The whole read history wiped, not just the status: a press must report the read that
        # press made, and a fresh timestamp left by an earlier poll would read as a working reader.
        drop("momo_status", "momo_read_ok", "momo_checked")
        res = post_check()
        check("the press itself reports the refusal it just hit",
              "Full Disk Access" in (res.get("status") or ""), repr(res.get("status")))
        check("what the press said is what the card now says",
              res.get("status") == get_payload()["status"],
              "%r vs %r" % (res.get("status"), get_payload()["status"]))
        check("the press carries the door to grant", bool(res.get("door")), repr(res.get("door")))
        # The granted app reads in between: the press on the shut engine must then go quiet.
        with sqlite3.connect(book) as con:
            for key in ("momo_status", "momo_read_ok", "momo_checked"):
                con.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)",
                            (key, ago(0)))
            con.commit()
        again = post_check()
        check("a press while another engine is reading reports no error",
              again.get("status") == "", repr(again.get("status")))
    finally:
        srv.terminate()
        srv.wait(timeout=10)

live_hash_after = hashlib.sha256(open(LIVE, "rb").read()).hexdigest()
check("the owner's live book is byte for byte as it was",
      live_hash_before == live_hash_after, live_hash_after)

shutil.rmtree(work, ignore_errors=True)
print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
