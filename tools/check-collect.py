#!/usr/bin/env python3
"""Prove the counter's Collect screen on a COPY of the shop's book.

The live book is opened read-only, copied with Connection.backup into a temp file, and every
request below goes to a throwaway server on a spare port with its own backup folder. The shop's
own file is never written to, and nothing here needs the shop password: loopback is the shop.

Collect itself only reads. The one write it offers is the stage move the job drawer already
made, so the last checks prove a hand-over closes the book the same way the drawer does.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE = os.path.expanduser("~/Library/Application Support/Chrisphics Hub/chrisphics.db")
PORT = 8896
BASE = "http://127.0.0.1:%d" % PORT

work = tempfile.mkdtemp(prefix="chrisphics-collect-")
CLONE = os.path.join(work, "clone.db")
src = sqlite3.connect("file:%s?mode=ro" % LIVE, uri=True)
dst = sqlite3.connect(CLONE)
src.backup(dst)
dst.close()
src.close()
live_stat = os.stat(LIVE)

env = dict(os.environ, CHRISPHICS_DB=CLONE, CHRISPHICS_SUPPORT=work,
           CHRISPHICS_BACKUP_DIR=os.path.join(work, "Backups"), PYTHONUNBUFFERED="1")
log = open(os.path.join(work, "server.log"), "wb")


def boot():
    p = subprocess.Popen(["/usr/bin/python3", os.path.join(REPO, "server.py"), "--port", str(PORT),
                          "--no-browser"], cwd=REPO, env=env, stdout=log, stderr=log)
    for _ in range(80):
        try:
            if json.loads(call("GET", "/healthz")[1].decode())["ok"]:
                return p
        except Exception:
            time.sleep(0.15)
    raise SystemExit("the test server never answered; see " + os.path.join(work, "server.log"))


def call(method, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


fails = []


def check(label, ok, detail=""):
    print("%-52s %s %s" % (label, "ok" if ok else "FAIL", detail))
    if not ok:
        fails.append(label)


def get(method, path, payload=None):
    code, raw = call(method, path, payload)
    return code, json.loads(raw.decode()) if raw else {}


def cents(value):
    return round(float(value or 0), 2)


def shelf():
    return get("GET", "/api/handover")[1]


def ask(q):
    return get("GET", "/api/handover?q=" + urllib.parse.quote(str(q)))[1]


def verdict(q):
    return ask(q).get("card") or {}


def fingerprint():
    """Every row the book holds. A lookup that writes nothing cannot change it."""
    con = sqlite3.connect("file:%s?mode=ro" % CLONE, uri=True)
    try:
        parts = []
        for table in ("jobs", "job_items", "payments", "expenses", "notifications", "job_events",
                      "spoiled_work", "clients"):
            rows = con.execute("SELECT * FROM %s ORDER BY 1" % table).fetchall()
            parts.append("%s=%r" % (table, rows))
        return "|".join(parts)
    finally:
        con.close()


proc = boot()
try:
    code, client = get("POST", "/api/clients", {"name": "Collect Check Client", "phone": "0590000000"})
    check("a client exists for the test jobs", code == 201, client.get("name", ""))
    cid = client["id"]
    today = time.strftime("%Y-%m-%d")

    def job(title, kind="Job", price=20, quantity=10):
        code, row = get("POST", "/api/jobs", {
            "client_id": cid, "kind": kind, "title": title, "quantity": quantity, "unit": "pcs",
            "unit_price": price, "category": "Flyers", "due_date": today})
        assert code == 201, row
        return row

    def move(row, status):
        code, out = get("POST", "/api/jobs/%s/status" % row["id"], {"status": status})
        assert code == 200, out
        return out

    def pay(row, amount):
        code, out = get("POST", "/api/payments", {"job_id": row["id"], "amount": amount,
                                                 "kind": "Payment", "method": "Cash"})
        assert code == 201, out
        return out

    ready_paid = job("Box of flyers, paid in full")
    owing = job("Banner, still owing")
    on_press = job("Notepads, on the press")
    gone = job("Bills already collected")
    late = job("Collected, money still owed")
    called_off = job("Order called off")
    quote = job("Collect check quote", kind="Quote")
    both = job("Two things wrong")

    for row in (ready_paid, owing, gone, both):
        move(row, "Ready")
    move(on_press, "Printing")
    move(both, "Printing")
    move(late, "Delivered")
    pay(ready_paid, cents(ready_paid["total"]))
    pay(owing, 100)
    pay(on_press, cents(on_press["total"]))
    pay(both, 50)
    pay(gone, cents(gone["total"]))
    pay(late, 60)
    move(gone, "Delivered")
    move(called_off, "Cancelled")

    # ------------------------------------------------------------------- the shelf
    out = shelf()
    check("with no number the screen still answers", "waiting" in out, str(list(out))[:60])
    check("the shelf holds what is ready and paid",
          any(r["id"] == ready_paid["id"] for r in out["waiting"])
          and not any(r["id"] == owing["id"] for r in out["waiting"]),
          "%s on the shelf" % len(out["waiting"]))
    check("a job already handed over is off it",
          not any(r["id"] == gone["id"] for r in out["waiting"]), "")
    check("and nothing has been looked up yet",
          out["found"] is None and out["card"] is None, "")

    # ----------------------------------------------------------------- loose typing
    ref = ready_paid["ref"]
    loose = [ref, ref.lower(), ref.replace("-", ""), ref.lower().replace("-", ""),
             ref.replace("-", " "), ref.split("-", 1)[1], ref.split("-", 1)[1].lower()]
    for typed in loose:
        card = verdict(typed)
        check("typed as %-16s lands on the job" % typed,
              (card.get("job") or {}).get("id") == ready_paid["id"],
              str((card.get("job") or {}).get("ref") or "nothing found"))
    check("the bare sequence on its own finds nothing",
          ask(ref.rsplit("-", 1)[1])["found"] is None, ref.rsplit("-", 1)[1])
    check("a number not in the book is refused politely",
          ask("CH-2099-4242")["found"] is None, "")
    check("a quote is not something to collect",
          ask(quote["ref"])["found"] is None, quote["ref"])

    # -------------------------------------------------------------------- verdicts
    card = verdict(ref)
    check("ready and paid may go out", card["can_handover"] is True, card["reason"])
    check("and has nothing left to clear", card["doors"] == [], str(card["doors"]))
    check("while it says the money is in", "Paid in full" in card["money_line"], card["money_line"])

    card = verdict(owing["ref"])
    check("ready but owing may not go out", card["can_handover"] is False, "")
    check("and says how much is short",
          "still owed" in card["reason"] and cents(card["balance"]) == 100.0,
          "%s / %s" % (card["reason"], card["balance"]))
    check("with one door: take the payment",
          [d["action"] for d in card["doors"]] == ["payment"], str(card["doors"]))

    card = verdict(on_press["ref"])
    check("paid but still printing may not go out", card["can_handover"] is False, "")
    check("and says it is not off the press yet", "Still on the press" in card["reason"],
          card["reason"])
    check("with one door: move the stage",
          [d["action"] for d in card["doors"]] == ["status"]
          and card["doors"][0]["status"] == "Ready", str(card["doors"]))

    card = verdict(called_off["ref"])
    check("a called-off order is refused", card["can_handover"] is False
          and "cancelled" in card["reason"], card["reason"])
    check("with no way round it offered", card["doors"] == [], str(card["doors"]))
    check("and no money talk about a job nobody is owed",
          "owed" not in card["reason"], card["reason"])

    card = verdict(gone["ref"])
    check("a job already collected is refused", card["can_handover"] is False
          and "Already handed over" in card["reason"], card["reason"])

    card = verdict(late["ref"])
    check("a job collected on credit still says the money is short",
          "still owed" in card["reason"] and "Already handed over" in card["reason"],
          card["reason"])
    check("and opens the door to take it",
          [d["action"] for d in card["doors"]] == ["payment"], str(card["doors"]))

    card = verdict(both["ref"])
    check("an owing, unready job names both reasons",
          "still owed" in card["reason"] and "Still on the press" in card["reason"], card["reason"])
    check("and opens both doors", [d["action"] for d in card["doors"]] == ["payment", "status"],
          str([d["action"] for d in card["doors"]]))

    over = job("Paid twice over")
    move(over, "Ready")
    pay(over, cents(over["total"]) + 40)
    card = verdict(over["ref"])
    check("a client in credit may still collect", card["can_handover"] is True, card["reason"])
    check("and the card says whose credit it is", "in credit" in card["money_line"],
          card["money_line"])

    # ------------------------------------------------------------------- read only
    before = fingerprint()
    for typed in [ref, owing["ref"], "junk", "%", "2026-0001", "'; DROP TABLE jobs; --",
                  "CH-" + "9" * 200]:
        ask(typed)
    out = shelf()
    check("a lookup writes nothing to the book", fingerprint() == before, "")
    check("and odd input is answered, not a fault",
          out["found"] is None and ask("'; DROP TABLE jobs; --")["found"] is None, "")

    # ------------------------------------------------------------------ hand over
    code, hand = get("POST", "/api/jobs/%s/status" % ready_paid["id"], {"status": "Delivered"})
    check("the hand-over is the stage move the drawer already made", code == 200, str(code))
    check("the job is closed with a time on it",
          hand["status"] == "Delivered" and bool(hand.get("closed_at")), str(hand.get("closed_at")))
    check("and the move is written in the job's own history",
          any(e["type"] == "status" and "Delivered" in (e["detail"] or "") for e in hand["events"]),
          str([e["detail"] for e in hand["events"][:2]]))
    con = sqlite3.connect("file:%s?mode=ro" % CLONE, uri=True)
    queued = con.execute("SELECT event, state FROM notifications WHERE job_id = ? AND event = 'Delivered'",
                         (ready_paid["id"],)).fetchall()
    con.close()
    check("with the client's Delivered news in the queue", bool(queued), str(queued))
    card = verdict(ref)
    check("after it goes out the card says already handed over",
          card["can_handover"] is False and "Already handed over" in card["reason"], card["reason"])
    check("and it is off the shelf",
          not any(r["id"] == ready_paid["id"] for r in shelf()["waiting"]), "")

    # ------------------------------------------------------------------- screens
    for path in ("/", "/app.js", "/styles.css", "/index.html"):
        code, raw = call("GET", path)
        body = raw.decode("utf-8", "replace")
        check("%s answers" % path, code == 200, str(code))
        if path == "/index.html":
            check("the rail carries Collect", 'href="#/collect" data-view="collect"' in body, "")
        if path == "/app.js":
            check("the shipped bundle has the screen",
                  "async function viewCollect" in body and "collect-handover" in body
                  and "'collect-pay'" in body and "'collect-ready'" in body, "")
        if path == "/styles.css":
            check("and its own look", ".collect-q" in body and ".collect-verdict" in body, "")
    code, out = get("POST", "/api/handover", {"q": ref})
    check("nothing may be written through the lookup door", code != 200, str(code))

    proc.terminate()
    proc.wait(timeout=10)
    proc = boot()
    card = verdict(gone["ref"])
    check("the same answers hold after a restart",
          card.get("can_handover") is False and bool(card), card.get("reason", ""))
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()
    after_stat = os.stat(LIVE)
    same = (after_stat.st_size == live_stat.st_size and after_stat.st_mtime == live_stat.st_mtime)
    print("\nLive book: %d bytes, last written %s -- %s" % (
        after_stat.st_size, time.ctime(after_stat.st_mtime),
        "untouched" if same else "CHANGED, WHICH MUST NOT HAPPEN"))
    if not same:
        fails.append("the shop's own book was written to")
    shutil.rmtree(work, ignore_errors=True)

print("\n%s" % ("all checks passed" if not fails else "FAILED: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
