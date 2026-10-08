#!/usr/bin/env python3
"""Prove a client is never shown the shop's spoilage.

Two artefacts leave this Mac for a client: the printed job sheet at /print/<id> and the messages
queued on a job. Both must stay clean while the shop's own record keeps the incident.

The live book is opened read-only, copied with Connection.backup into a temp file, and every
request below goes to a throwaway server on a spare port with its own backup folder. The shop's
own file is never written to, and nothing here needs the shop password: loopback is the shop.
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
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE = os.path.expanduser("~/Library/Application Support/Chrisphics Hub/chrisphics.db")
PORT = 8896
BASE = "http://127.0.0.1:%d" % PORT

work = tempfile.mkdtemp(prefix="chrisphics-client-words-")
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


def html(path):
    code, raw = call("GET", path)
    return code, raw.decode("utf-8", "replace")


def cents(value):
    return round(float(value or 0), 2)


proc = boot()
try:
    code, client = get("POST", "/api/clients", {
        "name": "Sheet Check Client", "phone": "055 111 2222", "whatsapp": "055 111 2222",
        "email": "sheets@example.com", "whatsapp_updates": 1, "email_updates": 1})
    cid = client["id"]
    check("a reachable, consenting client exists", code == 201, client.get("name", ""))

    # A job with a real production cost and a ruined batch on top of it.
    code, job = get("POST", "/api/jobs", {
        "client_id": cid, "kind": "Job", "title": "Conference flyers", "quantity": 10,
        "unit": "pcs", "unit_price": 50, "category": "Flyers"})
    jid = job["id"]
    check("the job is booked at 500.00", code == 201 and cents(job["total"]) == 500.0,
          "%s" % cents(job["total"]))

    code, paper = get("POST", "/api/expenses", {
        "job_id": jid, "category": "Paper / Stock", "amount": 120, "payee": "Makola paper"})
    code, spoil = get("POST", "/api/spoiled", {
        "job_id": jid, "quantity": 25, "amount": 60, "reason": "Colour banding on a batch"})

    code, book = get("GET", "/api/jobs/%s" % jid)
    check("the shop still holds the spoiled batch on the job",
          any(r["id"] == spoil["id"] for r in book["spoilage"]), str(len(book["spoilage"])))
    check("the job's cost is the paper alone",
          cents(book["cost"]) == 120.0 and cents(book["profit"]) == 380.0,
          "cost %s / profit %s" % (cents(book["cost"]), cents(book["profit"])))
    hidden = [e for e in book["expenses"] if e["is_spoilage"]]
    check("the API hands the drawer a spoilage expense row",
          len(hidden) == 1 and cents(hidden[0]["amount"]) == 60.0,
          "%s row(s)" % len(hidden))

    code, sheet = html("/print/%s" % jid)
    lowered = sheet.lower()
    check("the job sheet opens", code == 200 and "Job sheet" in sheet, "%s" % code)
    check("the sheet still shows the client's own money",
          "500.00" in sheet and "120.00" in sheet and "Makola paper" in sheet, "")
    check("the sheet says nothing about spoilage", "spoil" not in lowered,
          lowered[lowered.find("spoil") - 40:lowered.find("spoil") + 40] if "spoil" in lowered else "")
    check("the sheet does not carry the ruined batch's reason",
          "colour banding" not in lowered, "")
    check("the sheet's costs table is still there for the real cost",
          "Costs booked against this job" in sheet, "")

    # Every message a client can be sent, on both channels, for every stage.
    for event in ("Pending", "Printing", "Ready", "Delivered", "Cancelled"):
        get("POST", "/api/jobs/%s/status" % jid, {"status": event})
        get("POST", "/api/jobs/%s/notify" % jid, {"event": event})
    book = sqlite3.connect(CLONE)
    messages = book.execute("SELECT channel, event, subject, body FROM notifications "
                            "WHERE job_id=? ORDER BY channel, event", (jid,)).fetchall()
    book.close()
    check("messages were queued on both channels",
          len({m[0] for m in messages}) == 2 and len(messages) >= 10,
          "%s messages" % len(messages))
    dirty = [m for m in messages if "spoil" in (m[2] + m[3]).lower()
             or "colour banding" in (m[2] + m[3]).lower()
             or "60.00" in m[3]]
    check("no queued message mentions the spoilage", not dirty,
          " | ".join("%s/%s" % (m[0], m[1]) for m in dirty))
    sample = next((m[3] for m in messages if m[0] == "WhatsApp"), "")
    check("the messages still carry what the client needs",
          job["ref"] in sample and "Sheet Check Client" in sample, sample.replace("\n", " ")[:72])

    # A job whose only cost was a ruined batch must not print a costs table at all.
    code, solo = get("POST", "/api/jobs", {
        "client_id": cid, "kind": "Job", "title": "Only a ruined batch", "quantity": 4,
        "unit": "pcs", "unit_price": 25, "category": "Flyers"})
    sid2 = solo["id"]
    get("POST", "/api/spoiled", {"job_id": sid2, "quantity": 4, "amount": 15,
                                 "reason": "Wrong paper weight"})
    code, solo_sheet = html("/print/%s" % sid2)
    check("the second sheet opens", code == 200, "%s" % code)
    check("a spoilage-only job prints no costs table",
          "Costs booked against this job" not in solo_sheet
          and "spoil" not in solo_sheet.lower(), "")
    code, solo_book = get("GET", "/api/jobs/%s" % sid2)
    check("and the shop still sees the cost it was",
          len(solo_book["spoilage"]) == 1 and cents(solo_book["cost"]) == 0.0,
          "spoilage %s / cost %s" % (len(solo_book["spoilage"]), cents(solo_book["cost"])))

    # The counter's card is read out in front of a client, so it too stays clean.
    get("POST", "/api/jobs/%s/status" % jid, {"status": "Ready"})
    get("POST", "/api/payments", {"job_id": jid, "amount": 500, "method": "Cash",
                                  "kind": "Payment"})
    code, hand = get("GET", "/api/handover?q=%s" % job["ref"])
    card_text = json.dumps(hand)
    check("the hand-over card answers for the settled job",
          code == 200 and hand["card"]["can_handover"], "%s" % code)
    check("it carries no spoilage", "spoil" not in card_text.lower(), "")

    # The sheet a quote prints, unchanged.
    code, quote = get("POST", "/api/jobs", {
        "client_id": cid, "kind": "Quote", "title": "Quote with no costs", "quantity": 10,
        "unit": "pcs", "unit_price": 50, "category": "Flyers"})
    code, quote_sheet = html("/print/%s" % quote["id"])
    check("a quote sheet prints no costs table",
          code == 200 and "Costs booked against this job" not in quote_sheet, "")

    code, bad = html("/print/999999")
    check("a made-up sheet id still answers politely", code == 200 and "not found" in bad.lower(),
          "%s" % code)
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
