#!/usr/bin/env python3
"""Prove that money paid on the mobile money number shows on the dashboard, on a throwaway book.

The owner's ask: those who pay through 0506399641 must reflect on the dashboard. Two things had to
be true and neither was — the notice reader has never given up an alert (macOS denies the shop's
Python the Messages file), and every payment in the book is marked Cash, so there was no MoMo money
to show either. The card therefore now counts what the book itself says, and the figures below are
worked out by hand before any query is read.

    KOFI  MoMo 750.00 (5 Oct, on a job) + MoMo 200.00 (20 Sep)     -> 950.00, 2 payments
    AMA   MoMo 1,000.00 (11 Oct) and a MoMo refund of 50.00 (12 Oct) -> 950.00, 2 payments
    YAW   Cash 400.00 + Bank Transfer 300.00                        -> counts nowhere here

    MoMo all through: 950.00 + 950.00 = 1,900.00 over 4 payments
    MoMo in October:  750.00 + 1,000.00 - 50.00 = 1,700.00 over 3 payments (the 200.00 is September)
    Everything in:    750 + 100 + 1,000 - 50 + 400 + 300 = 2,500.00 this month, 2,700.00 all the time

A pasted alert booked to KOFI for 120.00 arrives today, so the same book afterwards reads
2,020.00 over 5 payments (1,820.00 this month), KOFI top of the name list at 1,070.00, and the
month's takings up to 2,620.00.

The last two boots are the ones that matter most: the identical rows re-marked all Cash, then all
MoMo. The takings must not stir by a pesewa — marking a payment as mobile money changes a label,
never a figure.

    python3 tools/check-momo-ledger.py
"""
import hashlib
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
PORT = 8933
BASE = "http://127.0.0.1:%d" % PORT

work = tempfile.mkdtemp(prefix="chrisphics-momo-")
CLONE = os.path.join(work, "clone.db")
src = sqlite3.connect("file:%s?mode=ro" % LIVE, uri=True)
dst = sqlite3.connect(CLONE)
src.backup(dst)
dst.close()
src.close()

wipe = sqlite3.connect(CLONE)
for table in ("clients", "jobs", "payments", "expenses", "leads", "money_signals"):
    wipe.execute("DELETE FROM %s" % table)
# Loopback is the shop, but the counter's own Mac still asks to sign in. Cleared on this copy only.
wipe.execute("UPDATE app_state SET value='0' WHERE key='shop_mac_login'")
wipe.commit()
wipe.close()

live_stat = os.stat(LIVE)
live_hash = hashlib.sha256(open(LIVE, "rb").read()).hexdigest()

fails = []
log = open(os.path.join(work, "server.log"), "wb")
env = dict(os.environ, CHRISPHICS_DB=CLONE, CHRISPHICS_SUPPORT=work,
           CHRISPHICS_BACKUP_DIR=os.path.join(work, "Backups"), PYTHONUNBUFFERED="1")


def check(label, ok, detail=""):
    print("%-58s %s   %s" % (label, "ok" if ok else "FAIL", str(detail)[:66]))
    if not ok:
        fails.append(label)
    return ok


def expect(label, got, want):
    return check(label, got == want, "got %r, want %r" % (got, want))


def call(method, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def get(method, path, payload=None):
    code, raw = call(method, path, payload)
    return code, (json.loads(raw.decode()) if raw else {})


def boot():
    p = subprocess.Popen(["/usr/bin/python3", os.path.join(REPO, "server.py"), "--port", str(PORT),
                          "--no-browser"], cwd=REPO, env=env, stdout=log, stderr=log)
    for _ in range(80):
        try:
            if json.loads(call("GET", "/healthz")[1].decode())["ok"]:
                return p
        except Exception:
            time.sleep(0.15)
    raise SystemExit("the test shop never answered; see " + os.path.join(work, "server.log"))


def stop(p):
    p.terminate()
    for _ in range(60):
        if p.poll() is not None:
            return
        time.sleep(0.1)
    p.kill()


def relabel(method):
    """The same rows, only the payment label changed."""
    con = sqlite3.connect(CLONE)
    con.execute("UPDATE payments SET method=?", (method,))
    con.commit()
    con.close()


def client(name, phone):
    code, row = get("POST", "/api/clients", {"name": name, "phone": phone})
    assert code == 201, row
    return row["id"]


def job(cid, price, title):
    code, row = get("POST", "/api/jobs", {"client_id": cid, "kind": "Job", "title": title,
                                          "quantity": 1, "unit": "set", "unit_price": price,
                                          "category": "Flyers", "status": "Ready"})
    assert code == 201, row
    return row


def pay(client_id, amount, method="Cash", paid_at=None, job_id=None, kind="Payment"):
    body = {"client_id": client_id, "amount": amount, "kind": kind, "method": method}
    if paid_at:
        body["paid_at"] = paid_at
    if job_id:
        body["job_id"] = job_id
    code, row = get("POST", "/api/payments", body)
    assert code == 201, row
    return row


proc = boot()
try:
    print("--- plant the book the figures above were worked out from")
    kofi = client("KOFI MENSA", "0244000001")
    ama = client("AMA SERWAH", "0244000002")
    yaw = client("YAW BOATENG", "0244000003")
    flyer = job(kofi, 1500.00, "5,000 flyers")
    pay(kofi, 750.00, "MoMo", "2026-10-05", job_id=flyer["id"])
    pay(kofi, 200.00, "MoMo", "2026-09-20")
    pay(kofi, 100.00, "Cash", "2026-10-06")
    pay(ama, 1000.00, "MoMo", "2026-10-11")
    pay(ama, 50.00, "MoMo", "2026-10-12", kind="Refund")
    pay(yaw, 400.00, "Cash", "2026-10-02")
    pay(yaw, 300.00, "Bank Transfer", "2026-10-03")

    print("\n--- the number's money read off the book")
    L = get("GET", "/api/momo")[1]["ledger"]
    expect("mobile money counted over every month", [L["n"], L["total"]], [4, 1900.0])
    expect("mobile money counted this month only", [L["month_n"], L["month_total"]], [3, 1700.0])
    expect("the month window is the book's own", L["month_start"], "2026-10-01")
    expect("the money's own first and last day", [L["first"], L["last"]], ["2026-09-20", "2026-10-12"])
    names = [(p["client"], p["n"], p["total"], p["last"]) for p in L["payers"]]
    expect("who paid on the number, biggest first", names,
           [("AMA SERWAH", 2, 950.0, "2026-10-12"), ("KOFI MENSA", 2, 950.0, "2026-10-05")])
    check("YAW's cash and bank never appear", all(p["client"] != "YAW BOATENG" for p in L["payers"]))
    dash = get("GET", "/api/dashboard")[1]["kpi"]
    expect("the takings are the same with or without MoMo",
           [dash["collected_month"], dash["collected_today"]], [2500.0, 0.0])

    print("\n--- an alert the shop pasted in feeds the same figure")
    code, res = get("POST", "/api/momo", {"text": "MTN MoMo: GHS 120.00 received from KOFI MENSA"})
    notice = res["notice"]
    check("the pasted alert read a figure and a name",
          [notice["amount"], notice["payer"], notice["direction"]], [120.0, "KOFI MENSA", "Credit"])
    code, res = get("POST", "/api/momo/%d/book" % notice["id"], {"client_id": kofi, "job_id": "credit"})
    expect("booking it wrote a MoMo payment", [code, res["payment"]["method"], res["payment"]["amount"]],
           [200, "MoMo", 120.0])
    L = get("GET", "/api/momo")[1]["ledger"]
    expect("the number's money grew by the alert", [L["n"], L["total"], L["month_n"], L["month_total"]],
           [5, 2020.0, 4, 1820.0])
    expect("KOFI now leads the name list",
           [(p["client"], p["n"], p["total"]) for p in L["payers"]][0], ("KOFI MENSA", 3, 1070.0))
    dash = get("GET", "/api/dashboard")[1]["kpi"]
    expect("and the month's takings grew with it", dash["collected_month"], 2620.0)
    after_momo = [dash["collected_month"], dash["collected_today"], dash["collected_week"]]

    print("\n--- the same rows marked Cash, then all marked MoMo")
    stop(proc)
    relabel("Cash")
    proc = boot()
    dash = get("GET", "/api/dashboard")[1]["kpi"]
    L = get("GET", "/api/momo")[1]["ledger"]
    expect("nothing was paid on the number", [L["n"], L["total"], L["month_total"]], [0, 0.0, 0.0])
    check("the payers list is empty, not missing", L["payers"] == [])
    expect("and the takings did not stir",
           [dash["collected_month"], dash["collected_today"], dash["collected_week"]], after_momo)
    stop(proc)
    relabel("MoMo")
    proc = boot()
    dash = get("GET", "/api/dashboard")[1]["kpi"]
    L = get("GET", "/api/momo")[1]["ledger"]
    expect("every payment now reads as mobile money", [L["n"], L["total"]], [8, 2820.0])
    expect("every one of them this month", [L["month_n"], L["month_total"]], [7, 2620.0])
    expect("the takings still did not stir",
           [dash["collected_month"], dash["collected_today"], dash["collected_week"]], after_momo)
    expect("three names hold the eight payments",
           [(p["client"], p["total"]) for p in L["payers"]],
           [("KOFI MENSA", 1170.0), ("AMA SERWAH", 950.0), ("YAW BOATENG", 700.0)])
finally:
    stop(proc)
    log.close()

print("\n--- the owner's book")
now_stat = os.stat(LIVE)
expect("the live file was not written to", hashlib.sha256(open(LIVE, "rb").read()).hexdigest(), live_hash)
check("its size and time are untouched",
      (now_stat.st_size, now_stat.st_mtime) == (live_stat.st_size, live_stat.st_mtime),
      "%d bytes" % now_stat.st_size)
print("work kept in %s" % work)
print("server log tail:")
print("".join(open(os.path.join(work, "server.log")).readlines()[-6:]))
shutil.rmtree(work, ignore_errors=True)
print("\n%s" % ("ALL CHECKS PASSED" if not fails else "FAILED: " + ", ".join(fails)))
sys.exit(0 if not fails else 1)
