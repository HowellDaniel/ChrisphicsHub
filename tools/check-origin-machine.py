#!/usr/bin/env python3
"""Prove the shop can name one machine as the first, and that what happens there reaches the rest.

The owner's word: "let this machine 192.168.100.19 be the first machine because it carries out the
original and updated versions of the book entries so any changes from its book needs to reflect on
to the other machines."

Two things follow from that, and they are proved differently.

*Most of the promise is already the shape of the book.* A counter that has joined the shop writes the
shop's one book over the Wi-Fi, so its entries are the shop's entries the moment they are saved, and
every other screen repaints when it next asks /api/book-rev. Nothing about naming a machine changes
that — so this check proves it anyway, by writing through a real engine and watching the revision the
other counters read move.

*What naming does change is the one case a stamp cannot settle.* When a book that was kept apart is
brought in, a job stamped later than the shop's copy replaces it and an older one never does. Two
copies stamped to the same second are a tie that rule reads as "not newer" and leaves alone — and the
tie is usually the first machine's own write, made on the shop's book and carried home in a copy. So
a tie goes with the machine the shop named, and only with it: an older copy from that same machine is
still refused, and a stranger's tie is still left alone. The machine that keeps the shop's records
cannot also be quietly overwritten by the copy it handed out.

Everything runs against a throwaway book in a temporary folder — including the engine this check
starts on its own ports — and the shop's live book is hashed before and after to prove none of it
went near it.

    python3 tools/check-origin-machine.py
"""
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE = os.path.expanduser("~/Library/Application Support/Chrisphics Hub/chrisphics.db")
SHOP_NAME = "CRISPprint Ghana"
FIRST = "192.168.100.19"
STRANGER = "192.168.100.14"
STAMP = "2026-10-20 12:00:00"

work = tempfile.mkdtemp(prefix="chrisphics-origin-")
book = os.path.join(work, "shop.db")
support = os.path.join(work, "support")
backup = os.path.join(work, "backups")
for d in (support, backup):
    os.makedirs(d)

live_hash_before = hashlib.sha256(open(LIVE, "rb").read()).hexdigest()

os.environ["CHRISPHICS_DB"] = book
os.environ["CHRISPHICS_SUPPORT"] = support
os.environ["CHRISPHICS_BACKUP_DIR"] = backup
sys.path.insert(0, REPO)
import server  # noqa: E402

server.connect(book)
server.set_state("shop_name", SHOP_NAME)
server.apply_book_profile()

fails = []
PORT = 8947
PROXY_PORT = 8948


def check(name, ok, got=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else "  -> " + str(got)))
    if not ok:
        fails.append(name)


def fresh_book(path):
    """A second book, built the way the engine builds its own, so a copy brought in here is a copy a
    machine could really have kept."""
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    with open(os.path.join(REPO, "schema.sql"), "r", encoding="utf-8") as fh:
        con.executescript(fh.read())
    con.execute("INSERT INTO app_state (key, value) VALUES ('shop_name', ?)", (SHOP_NAME,))
    con.execute("INSERT INTO clients (name, phone) VALUES (?, ?)", ("Ada Print Shop", "0501234567"))
    return con


def source_book(tag, stamp, unit_price, created=STAMP):
    """One client the shop already knows, and one copy of the shop's own job — stamped `stamp`,
    priced `unit_price`. The tag goes into app_state, which is never brought in, so every file this
    makes is a different file to the digest that stops the same copy being added twice."""
    path = os.path.join(work, "counter-%s.db" % tag)
    con = fresh_book(path)
    client = con.execute("SELECT id FROM clients").fetchone()[0]
    con.execute("INSERT INTO jobs (ref, client_id, title, quantity, unit_price, status,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("CH-2026-0900", client, "500 business cards", 500, unit_price, "Printing",
                 created, stamp))
    con.execute("INSERT INTO app_state (key, value) VALUES ('probe_nonce', ?)", (tag,))
    con.commit()
    con.close()
    return path


def shop_job():
    return one_shop("SELECT id, ref, unit_price, status, updated_at FROM jobs WHERE ref = ?",
                   ("CH-2026-0900",))


def one_shop(sql, args=()):
    with server._lock:
        return server._db.execute(sql, args).fetchone()


def plant_shop_job(unit_price=100.0, stamp=STAMP):
    with server._lock:
        server._db.execute("DELETE FROM job_items WHERE job_id IN"
                           " (SELECT id FROM jobs WHERE ref = 'CH-2026-0900')")
        server._db.execute("DELETE FROM jobs WHERE ref = 'CH-2026-0900'")
        server._db.execute("DELETE FROM clients WHERE phone = '0501234567'")
        server._db.execute("INSERT INTO clients (name, phone) VALUES (?, ?)",
                           ("Ada Print Shop", "0501234567"))
        client = server._db.execute("SELECT id FROM clients WHERE phone = '0501234567'").fetchone()[0]
        server._db.execute("INSERT INTO jobs (ref, client_id, title, quantity, unit_price, status,"
                           " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           ("CH-2026-0900", client, "500 business cards", 500, unit_price,
                            "Printing", STAMP, stamp))
        server._db.commit()


def clear_origin():
    server.set_origin_machine({"address": ""})


print("Nothing named yet — every counter read the same way:")
clear_origin()
check("no first machine is named", server.origin_machine()["named"] is False,
      server.origin_machine())
check("so no address is the first machine", server.is_origin(FIRST) is False)
check("and no name is either", server.is_origin("", "chrisprints-imac-2") is False)

print("\nNaming it, and what is refused:")
answer = server.set_origin_machine({"address": FIRST, "name": "CHRISPRINT's iMac (2)"})
check("the shop's first machine is kept", answer["origin"] == {
    "address": FIRST, "name": "CHRISPRINT's iMac (2)", "named": True,
    "since": answer["origin"]["since"]}, answer["origin"])
check("it survives a reopen", server.origin_machine()["address"] == FIRST, server.origin_machine())
check("the address it was named by is the first machine", server.is_origin(FIRST) is True)
check("a neighbouring iMac is not", server.is_origin(STRANGER) is False)
check("a book introduced by its hostname is not matched by the shop's word for it",
      server.is_origin("", "chrisprints-imac-2") is False,
      "the name kept here is 'CHRISPRINT's iMac (2)'")
check("and by the name the shop typed", server.is_origin("", "CHRISPRINT's iMac (2)") is True)
server.set_origin_machine({"address": STRANGER, "name": "chrisprints-imac-2"})
check("named after the hostname it introduces itself by, that label matches",
      server.is_origin("", "chrisprints-imac-2") is True, server.origin_machine())
check("and the address it was named by still counts", server.is_origin(STRANGER) is True)
server.set_origin_machine({"address": FIRST, "name": "CHRISPRINT's iMac (2)"})
for bad in ("192.168.1.999", "chrisprints-imac-2.local", "10.0.0", "1.2.3.4.5"):
    try:
        server.set_origin_machine({"address": bad, "name": "x"})
        refused = False
        detail = "accepted %r" % bad
    except ValueError:
        refused = True
        detail = ""
    check("a number that is not a Wi-Fi number is refused (%s)" % bad, refused, detail)
check("the refusal left the first machine where it was",
      server.origin_machine()["address"] == FIRST, server.origin_machine())

print("\nThe merge: a tie goes with the first machine, and nowhere else:")
plant_shop_job(unit_price=100.0, stamp=STAMP)
clear_origin()
records = server.bring_over_book(source_book("tie-unnamed", STAMP, 250.0),
                                 "chrisprints-imac-2", STRANGER)
check("an unnamed shop leaves a same-second tie alone",
      shop_job()["unit_price"] == 100.0 and not records["records"]["updated"].get("jobs"),
      records["records"])

server.set_origin_machine({"address": FIRST, "name": "CHRISPRINT's iMac (2)"})
records = server.bring_over_book(source_book("tie-origin", STAMP, 250.0),
                                 "chrisprints-imac-2", FIRST)
check("the first machine's tie replaces the shop's copy",
      shop_job()["unit_price"] == 250.0, shop_job())
check("and it is counted as brought up to date, not as a new job",
      records["records"]["updated"].get("jobs") == 1
      and not records["records"]["added"].get("jobs"), records["records"])

plant_shop_job(unit_price=100.0, stamp=STAMP)
records = server.bring_over_book(source_book("tie-stranger", STAMP, 250.0),
                                 "some-other-mac", STRANGER)
check("a stranger's same-second copy is still left alone",
      shop_job()["unit_price"] == 100.0, records["records"])

plant_shop_job(unit_price=100.0, stamp=STAMP)
older = server.bring_over_book(source_book("older-origin", "2026-10-19 09:00:00", 250.0),
                               "chrisprints-imac-2", FIRST)
check("being first does not let an older copy walk the shop backwards",
      shop_job()["unit_price"] == 100.0, older["records"])
newer = server.bring_over_book(source_book("newer-stranger", "2026-10-21 09:00:00", 60.0),
                               "some-other-mac", STRANGER)
check("and the ordinary rule still stands for everybody else",
      shop_job()["unit_price"] == 60.0, newer["records"])

# The copy the shop handed out comes home unchanged: every stamp ties, so nothing should move — a
# bring-in that rewrote all 146 jobs would look like a success and be a lot of noise.
plant_shop_job(unit_price=100.0, stamp=STAMP)
same = server.bring_over_book(source_book("identical-origin", STAMP, 100.0),
                              "chrisprints-imac-2", FIRST)
check("a tie that holds nothing different rewrites nothing",
      same["records"]["updated"].get("jobs", 0) == 0, same["records"])

print("\nThe counters the shop can see:")
machines = server.machine_activity()["machines"]
check("the first machine is in the list even before it writes",
      any(m["first"] and m["address"] == FIRST and m["last_write"] == "" for m in machines),
      machines)
for address, when in ((FIRST, "2026-10-27 09:14:02"), (STRANGER, "2026-10-27 08:02:55")):
    with server._lock:
        server._db.execute("INSERT INTO security_log (at, kind, address, detail) VALUES (?,?,?,?)",
                           (when, "wrote_into_book", address, "changed 4 records"))
        server._db.commit()
seen = {m["address"]: m for m in server.machine_activity()["machines"]}
check("a machine that has written shows the last time it did",
      seen.get(FIRST, {}).get("last_write") == "2026-10-27 09:14:02", seen)
check("and is marked as the first one", seen.get(FIRST, {}).get("first") is True, seen)
check("the neighbour is listed but not first",
      seen.get(STRANGER, {}).get("first") is False, seen)
check("the newest writer is listed first",
      server.machine_activity()["machines"][0]["address"] == FIRST,
      server.machine_activity()["machines"])

print("\nThrough a real engine, the way a counter actually writes:")
squatted = []
for port in (PORT, PROXY_PORT):
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", port)) == 0:
            squatted.append(port)
check("both ports are free before the check runs", not squatted, squatted)


def api(port, path, method="GET", body=None):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req) as conn:
            return conn.getcode(), json.loads(conn.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"{}")


engines = []


def start(port, extra=()):
    proc = subprocess.Popen([sys.executable, os.path.join(REPO, "server.py"),
                             "--port", str(port), "--no-browser", *extra],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env={**os.environ})
    engines.append(proc)
    for _ in range(80):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return proc
        time.sleep(0.25)
    raise RuntimeError("the engine on %d never answered" % port)


if not squatted:
    start(PORT)
    rev_before = api(PORT, "/api/book-rev")[1]["rev"]
    client_id = one_shop("SELECT id FROM clients WHERE phone='0501234567'")[0]
    code, made = api(PORT, "/api/jobs", "POST",
                     {"client_id": client_id, "title": "A4 posters, 20", "quantity": 20,
                      "unit_price": 12.5, "status": "Pending", "category": "Poster"})
    check("a job typed at a counter is taken", code == 201 and made.get("ref"), (code, made))
    code, again = api(PORT, "/api/jobs", "POST",
                      {"client_id": client_id, "title": "Two roll-up stands", "quantity": 2,
                       "unit_price": 180.0, "status": "Pending", "category": "Banner"})
    check("and a second one is taken too", code == 201 and again.get("ref"), (code, again))
    rev_after = api(PORT, "/api/book-rev")[1]["rev"]
    check("the revision every other counter watches moved", rev_after != rev_before,
          "%s -> %s" % (rev_before, rev_after))
    code, shop_payload = api(PORT, "/api/shop")
    origin_seen = shop_payload.get("origin") or {}
    listed = shop_payload.get("machines") or []
    check("the shop's screen is told which machine is first",
          code == 200 and origin_seen.get("address") == FIRST, (code, origin_seen))
    check("the machine that just wrote is in the list",
          any(m["address"] == "127.0.0.1" and m["last_write"] for m in listed), listed)
    check("and it is named as this Mac, not as a number",
          any(m["address"] == "127.0.0.1" and m["name"] == "This Mac" for m in listed), listed)
    check("the first machine is still listed though it has not written here",
          any(m["first"] and m["address"] == FIRST for m in listed), listed)

    # One line per machine per hour, not one per save: a cashier typing all morning is one story.
    trail = [r for r in server.security_trail(200)
             if r["kind"] == "wrote_into_book" and r["address"] == "127.0.0.1"]
    check("two saves in a row leave one line in the trail", len(trail) == 1,
          [t["detail"] for t in trail])
    check("and that line says how many records it held", "2 records" in trail[0]["detail"],
          trail[0]["detail"])

    # The door: a counter across the Wi-Fi must not be able to crown itself.
    start(PROXY_PORT, extra=("--trust-proxy",))
    code, refused = api(PROXY_PORT, "/api/shop-origin", "PUT",
                        {"address": "10.0.0.1", "name": "me"})
    check("a machine that is not the shop's own computer cannot name the first machine",
          code == 403, (code, refused))
    check("and nothing was written while it tried",
          server.origin_machine()["address"] == FIRST, server.origin_machine())
else:
    check("the shop's own ports are free for the engine this check starts", not squatted,
          "port(s) %s already answering — stop whatever holds them and run again" % squatted)

for proc in engines:
    proc.terminate()
    proc.wait(timeout=10)

print("\nWhat the shipped page says:")
app_js = open(os.path.join(REPO, "public", "app.js"), encoding="utf-8").read()
check("the Shop & devices screen has the card", "firstMachineCard(shop, shopMac)" in app_js)
check("the card is written in the shop's own words", "The shop’s first machine" in app_js)
check("and it is the only place that can change it",
      "function firstMachineCard(shop, shopMac) {" in app_js
      and app_js.count("firstMachineCard(") == 2)
check("the page posts to the route the engine guards",
      "api('/api/shop-origin', { method: 'PUT'" in app_js)
sw = open(os.path.join(REPO, "public", "sw.js"), encoding="utf-8").read()
check("the shell cache was bumped so the new card reaches every device",
      "crispprint-shell-v17" in sw, [l for l in sw.splitlines() if "CACHE_NAME =" in l])

live_hash_after = hashlib.sha256(open(LIVE, "rb").read()).hexdigest()
check("the owner's live book is byte for byte as it was",
      live_hash_before == live_hash_after, live_hash_after)

shutil.rmtree(work, ignore_errors=True)
print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
