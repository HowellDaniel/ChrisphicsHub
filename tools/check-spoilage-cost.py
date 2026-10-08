#!/usr/bin/env python3
"""Prove spoilage is recorded against a job but kept out of that job's cost and profit.

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
PORT = 8895
BASE = "http://127.0.0.1:%d" % PORT

work = tempfile.mkdtemp(prefix="chrisphics-spoilage-")
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


proc = boot()
try:
    code, client = get("POST", "/api/clients", {"name": "Spoilage Check Client"})
    check("a client exists for the test job", code == 201, client.get("name", ""))

    code, job = get("POST", "/api/jobs", {
        "client_id": client["id"], "kind": "Job", "title": "Spoilage check",
        "quantity": 10, "unit": "pcs", "unit_price": 50, "category": "Flyers"})
    jid = job["id"]
    check("the job is booked at 500.00", code == 201 and cents(job["total"]) == 500.0,
          "%s" % cents(job["total"]))

    code, quote = get("POST", "/api/jobs", {
        "client_id": client["id"], "kind": "Quote", "title": "Spoilage check quote",
        "quantity": 10, "unit": "pcs", "unit_price": 50, "category": "Flyers"})
    check("a quote exists to be refused", code == 201 and quote["kind"] == "Quote", quote["kind"])

    month = time.strftime("%Y-%m")

    def month_spent():
        rows = get("GET", "/api/accounts")[1]["spent"]
        row = next((r for r in rows if r["month"] == month), None)
        return cents(row and row["spent"])

    spent_before = month_spent()

    code, expense = get("POST", "/api/expenses", {
        "job_id": jid, "category": "Paper / Stock", "amount": 120, "payee": "Test paper"})
    code, book = get("GET", "/api/jobs/%s" % jid)
    check("a real job expense moves cost and profit",
          cents(book["cost"]) == 120.0 and cents(book["profit"]) == 380.0,
          "cost %s / profit %s" % (cents(book["cost"]), cents(book["profit"])))
    spent_start = month_spent()
    check("the month's money out grew by the paper's 120.00",
          round(spent_start - spent_before, 2) == 120.0, "%s -> %s" % (spent_before, spent_start))

    code, spoil = get("POST", "/api/spoiled", {
        "job_id": jid, "quantity": 25, "amount": 60, "reason": "Colour banding on a batch"})
    sid = spoil["id"]
    check("the spoilage record exists", code == 201 and cents(spoil["amount"]) == 60.0,
          "id %s / %s" % (sid, cents(spoil["amount"])))
    code, book = get("GET", "/api/jobs/%s" % jid)
    check("the spoilage is recorded but not in the job cost",
          cents(book["cost"]) == 120.0 and cents(book["profit"]) == 380.0,
          "cost %s / profit %s" % (cents(book["cost"]), cents(book["profit"])))
    check("it still shows inside the job's spoiled section",
          any(r["id"] == sid for r in book["spoilage"]), str(len(book["spoilage"])))
    flag = {e["id"]: e["is_spoilage"] for e in book["expenses"]}
    check("its expense row is marked as spoilage", flag.get(spoil["expense_id"]) == 1,
          str(flag))
    keep = cents(sum(cents(e["amount"]) for e in book["expenses"] if not e["is_spoilage"]))
    check("the drawer's cost table would drop that row", keep == 120.0, "non-spoilage rows total %s" % keep)

    code, rows = get("GET", "/api/spoiled")
    check("the spoilage screen lists it against the job",
          any(r["id"] == sid and r["job_id"] == jid for r in rows), str(len(rows)))

    spent_after_spoil = month_spent()
    check("the month's money out still counts the 60.00",
          round(spent_after_spoil - spent_start, 2) == 60.0,
          "%s -> %s" % (spent_start, spent_after_spoil))

    code, hand = get("POST", "/api/expenses", {
        "job_id": jid, "category": "Spoilage", "amount": 25, "payee": "Hand-written spoilage"})
    code, book = get("GET", "/api/jobs/%s" % jid)
    check("a hand-picked Spoilage category still counts",
          cents(book["cost"]) == 145.0 and cents(book["profit"]) == 355.0,
          "cost %s / profit %s" % (cents(book["cost"]), cents(book["profit"])))

    book = sqlite3.connect(CLONE)
    direct = book.execute("SELECT round(sum(amount),2) FROM expenses WHERE job_id=?", (jid,)).fetchone()[0]
    view_cost = book.execute("SELECT cost FROM job_accounts WHERE id=?", (jid,)).fetchone()[0]
    book.close()
    check("the ledger still holds all three expense rows", cents(direct) == 205.0, "%s" % cents(direct))
    check("the view's cost is the ledger minus the spoilage", cents(view_cost) == 145.0,
          "%s" % cents(view_cost))

    spent_after_hand = month_spent()
    code, edited = get("PUT", "/api/spoiled/%s" % sid, {
        "job_id": jid, "quantity": 40, "amount": 90, "reason": "Colour banding on two batches"})
    code, book = get("GET", "/api/jobs/%s" % jid)
    check("editing the amount leaves the job cost alone",
          code == 200 and cents(edited["amount"]) == 90.0 and cents(book["cost"]) == 145.0,
          "amount %s / cost %s" % (cents(edited["amount"]), cents(book["cost"])))
    spent_after_edit = month_spent()
    check("the month's money out follows the edit",
          round(spent_after_edit - spent_after_hand, 2) == 30.0,
          "%s -> %s" % (spent_after_hand, spent_after_edit))

    code, removed = get("DELETE", "/api/spoiled/%s" % sid)
    code, book = get("GET", "/api/jobs/%s" % jid)
    check("removing the record drops its linked expense",
          not any(e["id"] == spoil["expense_id"] for e in book["expenses"])
          and not any(r["id"] == sid for r in book["spoilage"]),
          "rows left %s / spoilage %s" % (len(book["expenses"]), len(book["spoilage"])))
    check("the hand-written Spoilage row is still there",
          any(e["id"] == hand["id"] and cents(e["amount"]) == 25.0 for e in book["expenses"]),
          str([cents(e["amount"]) for e in book["expenses"]]))
    check("the job cost is unchanged by the removal",
          cents(book["cost"]) == 145.0 and cents(book["profit"]) == 355.0,
          "cost %s / profit %s" % (cents(book["cost"]), cents(book["profit"])))
    spent_after_delete = month_spent()
    check("the month's money out drops by the 90.00",
          round(spent_after_delete - spent_after_edit, 2) == -90.0,
          "%s -> %s" % (spent_after_edit, spent_after_delete))

    code, listing = get("GET", "/api/jobs")
    row = next((j for j in listing if j["id"] == jid), None)
    check("the job list reports the same cost as the drawer",
          row and cents(row["cost"]) == 145.0 and cents(row["profit"]) == 355.0,
          "%s / %s" % (cents(row["cost"]) if row else "-", cents(row["profit"]) if row else "-"))

    def report_pair():
        totals = get("GET", "/api/report")[1]["totals"]
        return cents(totals["cost"]), cents(totals["spent"])

    cost_before, spent_before_report = report_pair()
    code, again = get("POST", "/api/spoiled", {
        "job_id": jid, "quantity": 30, "amount": 60, "reason": "Second batch, same jam"})
    check("a fresh spoilage can be logged again", code == 201 and cents(again["amount"]) == 60.0,
          "id %s" % again.get("id"))
    cost_after, spent_after_report = report_pair()
    check("the report's job cost ignores it", round(cost_after - cost_before, 2) == 0.0,
          "%s -> %s" % (cost_before, cost_after))
    check("the report's money out counts it", round(spent_after_report - spent_before_report, 2) == 60.0,
          "%s -> %s" % (spent_before_report, spent_after_report))
    code, book = get("GET", "/api/jobs/%s" % jid)
    check("the drawer still prices the job at 145.00 / 355.00",
          cents(book["cost"]) == 145.0 and cents(book["profit"]) == 355.0,
          "cost %s / profit %s" % (cents(book["cost"]), cents(book["profit"])))

    bad = [("a job that does not exist", {"job_id": 0, "quantity": 5, "amount": 10, "reason": "x"}, 404),
           ("a quote instead of a job", {"job_id": quote["id"], "quantity": 5, "amount": 10,
                                         "reason": "x"}, 404),
           ("zero quantity", {"job_id": jid, "quantity": 0, "amount": 10, "reason": "x"}, 400),
           ("no reason", {"job_id": jid, "quantity": 5, "amount": 10, "reason": ""}, 400)]
    for label, payload, want in bad:
        code, out = get("POST", "/api/spoiled", payload)
        check("refuses %s with %d" % (label, want), code == want, str(out.get("error", ""))[:48])
    code, book = get("GET", "/api/jobs/%s" % jid)
    check("none of those touches the job's cost",
          cents(book["cost"]) == 145.0 and cents(book["profit"]) == 355.0,
          "cost %s / profit %s" % (cents(book["cost"]), cents(book["profit"])))

    book = sqlite3.connect(CLONE)
    direct = book.execute("SELECT coalesce(round(sum(amount),2),0) FROM expenses WHERE job_id=?",
                          (jid,)).fetchone()[0]
    cost = book.execute("SELECT cost FROM job_accounts WHERE id=?", (jid,)).fetchone()[0]
    linked = book.execute("SELECT count(*) FROM expenses e WHERE e.job_id=? AND EXISTS "
                          "(SELECT 1 FROM spoiled_work s WHERE s.expense_id=e.id)", (jid,)).fetchone()[0]
    book.close()
    check("one expense row is still linked to a spoilage record", linked == 1, "%s" % linked)
    check("the ledger holds 205.00 while the view prices the job at 145.00",
          cents(direct) == 205.0 and cents(cost) == 145.0, "rows %s / view %s" % (cents(direct), cents(cost)))

    proc.terminate()
    proc.wait(timeout=10)
    proc = boot()
    code, book = get("GET", "/api/jobs/%s" % jid)
    check("the same maths holds after a restart",
          cents(book["cost"]) == 145.0 and cents(book["profit"]) == 355.0,
          "cost %s / profit %s" % (cents(book["cost"]), cents(book["profit"])))
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
