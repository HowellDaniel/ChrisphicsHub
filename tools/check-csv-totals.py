#!/usr/bin/env python3
"""Prove every CSV export ends with a totals line that adds up.

The live book is opened read-only, copied with Connection.backup into a temp file, emptied of the
shop's own records, and given six rows whose worth is worked out by hand below — so the figures
the file prints can be checked against arithmetic rather than against the same code. One pass runs
the export functions in process; another starts a throwaway server on a spare port with its own
backup and support folders and asks it for the seven files over HTTP. The shop's file is never
written to, and nothing here needs the shop password: loopback is the shop.
"""
import csv
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from urllib.parse import quote

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE = os.path.expanduser("~/Library/Application Support/Chrisphics Hub/chrisphics.db")
PORT = 8901
BASE = "http://127.0.0.1:%d" % PORT
import shopcheck  # noqa: E402  (lives beside this check)
shopcheck.require_free(PORT)
sys.path.insert(0, REPO)
import server  # noqa: E402

EXPORTS = ["jobs", "items", "expenses", "leads", "clients", "payments", "accounts"]
WORD = "verify-only-word-not-the-shop"
fails = []

# The seeded records and what each export's money columns are worth over them, worked out by hand
# from the shop's own costing rules so a wrong sum in server.py cannot agree with itself:
#   job 901  10 x 12.50 + 4 x 25.00 = 225.00 billed; lines cost 10 x 4.00 + 4 x 10.00 = 80.00,
#            plus the 33.33 expense booked against it = 113.33 cost; 100.00 paid on it.
#   job 902  100 x 2.50 - 50.00 discount = 200.00 billed, no lines, no cost, nothing paid.
WANT = {"jobs": {"Total": 425.00, "Cost": 113.33, "Profit": 311.67, "Paid": 100.00,
                 "Balance due": 325.00},
        "items": {"Line total": 225.00, "Line cost": 80.00, "Line profit": 145.00},
        "expenses": {"Amount": 233.33},
        "leads": {"Est. value": 450.00},
        "clients": {"Billed": 425.00, "Received": 80.00,
                    # Bravo's 20.00 refund was held on the account with no job to pay it off, so
                    # the shop's own balance due (open job balance minus the credit) is 20.00
                    # higher than the two jobs alone: 125.00 + 220.00.
                    "Balance due": 345.00},
        # the 20.00 refund counts off, exactly as the dashboard's collected figure does
        "payments": {"Amount": 80.00},
        "accounts": {"Total": 425.00, "Paid": 100.00, "Outstanding": 325.00}}

SEED = """
  INSERT INTO clients (id, name, phone) VALUES (901, 'Test Alpha', '0200000001');
  INSERT INTO clients (id, name, phone) VALUES (902, 'Test Bravo', '0200000002');
  INSERT INTO jobs (id, ref, client_id, kind, title, category, status, quantity, unit_price,
                    extras, discount, created_at)
  VALUES (901, 'CH-TEST-001', 901, 'Job', 'Banners', 'Banner', 'Printing', 1, 0, 0, 0,
          '2026-10-01 09:00');
  INSERT INTO job_items (job_id, title, quantity, unit_price, unit_cost, position)
  VALUES (901, 'Front', 10, 12.50, 4.00, 0);
  INSERT INTO job_items (job_id, title, quantity, unit_price, unit_cost, position)
  VALUES (901, 'Back', 4, 25, 10, 1);
  INSERT INTO expenses (id, spent_on, category, amount, job_id)
  VALUES (901, '2026-10-01', 'Paper', 33.33, 901);
  INSERT INTO payments (id, client_id, job_id, amount, kind, method, paid_at)
  VALUES (901, 901, 901, 100, 'Payment', 'Cash', '2026-10-02');
  INSERT INTO jobs (id, ref, client_id, kind, title, category, status, quantity, unit_price,
                    extras, discount, created_at)
  VALUES (902, 'CH-TEST-002', 902, 'Job', 'Flyers', 'Flyer', 'Pending', 100, 2.5, 0, 50,
          '2026-10-03 09:00');
  INSERT INTO payments (id, client_id, job_id, amount, kind, method, paid_at)
  VALUES (902, 902, NULL, 20, 'Refund', 'MoMo', '2026-10-04');
  INSERT INTO expenses (id, spent_on, category, amount, job_id)
  VALUES (902, '2026-10-04', 'Rent', 200, NULL);
  INSERT INTO leads (id, name, source, interest, stage, value)
  VALUES (901, 'Came in', 'Walk-in', 'Shirts', 'Prospect', 0);
  INSERT INTO leads (id, name, source, interest, stage, value)
  VALUES (902, 'On WhatsApp', 'WhatsApp', 'Cards', 'Won', 450);
"""


def say(label, ok, detail=""):
    print("%-58s %s   %s" % (label, "ok " if ok else "FAIL", str(detail)[:74]))
    if not ok:
        fails.append(label)
    return ok


def expect(label, got, want):
    return say(label, got == want, "got %r, want %r" % (got, want))


def clone_to(dest):
    src = sqlite3.connect("file:%s?mode=ro" % quote(LIVE), uri=True)
    dst = sqlite3.connect(dest)
    src.backup(dst)
    dst.close()
    src.close()


def seed(db):
    con = sqlite3.connect(db)
    con.executescript(SEED)
    con.commit()
    con.close()


def clear(db):
    con = sqlite3.connect(db)
    con.executescript("DELETE FROM clients; DELETE FROM jobs; DELETE FROM payments;"
                      " DELETE FROM expenses; DELETE FROM leads;")
    con.commit()
    con.close()


def wipe_and_seed(db):
    # the copy carries tonight's real records; the by-hand figures above only hold if the file
    # being checked contains exactly the rows this check plants
    clear(db)
    seed(db)


def parse(text):
    return list(csv.reader(io.StringIO(text)))


def show(name, text):
    rows = parse(text)
    print("\n--- %s ---" % name)
    for line in rows[1:]:
        print(("  > " if line[0].startswith("Total (") else "    ") + " | ".join(line))
    return rows


def foot(name, count):
    """The line a finished export is supposed to end with."""
    tally = "%d row%s" % (count, "" if count == 1 else "s")
    if name == "payments" and count:
        tally += ", refunds counted off"
    return "Total (%s)" % tally


def check_seeded_export():
    work = tempfile.mkdtemp(prefix="chrisphics-csv-totals-")
    db = os.path.join(work, "counter.db")
    clone_to(db)
    server.connect(db)
    wipe_and_seed(db)
    for name in EXPORTS:
        text, filename, count = server.export_rows(name, {})
        rows = show(name, text)
        header, data, total = rows[1], rows[2:-1], rows[-1]
        expect("%s: the file keeps its name" % name, filename.startswith(name + "-"), True)
        expect("%s: the foot of the file is the totals line" % name, total[0], foot(name, count))
        expect("%s: as many cells as the header" % name, len(total), len(header))
        expect("%s: %d records above it" % (name, count), len(data), count)
        cells = dict(zip(header, total))
        # (a) the arithmetic, against the figures worked out by hand
        for label, expected in WANT[name].items():
            expect("%s: %s totals to %.2f" % (name, label, expected),
                   round(float(cells.get(label, "nan")), 2), expected)
        # (b) nothing else gets a number: unit prices, quantities, discounts are not sums
        expect("%s: only the money columns are filled" % name,
               sorted(h for i, (h, v) in enumerate(zip(header, total)) if v != "" and i > 0),
               sorted(WANT[name]))
        # (c) a shop-owner adding up the rows above lands on the same figures
        for label in WANT[name]:
            index = header.index(label)
            signs = ([-1 if line[header.index("Type")] == "Refund" else 1 for line in data]
                     if name == "payments" else [1] * len(data))
            manual = round(sum(float(line[index]) * s
                               for line, s in zip(data, signs) if line[index]), 2)
            expect("%s: the rows above add up to the foot (%s)" % (name, label),
                   float(total[index]), manual)
    shutil.rmtree(work, ignore_errors=True)


def check_empty_book():
    work = tempfile.mkdtemp(prefix="chrisphics-csv-empty-")
    db = os.path.join(work, "counter.db")
    clone_to(db)
    server.connect(db)
    wipe_and_seed(db)
    clear(db)
    print("\n--- an empty shop ---")
    for name in EXPORTS:
        total = show(name, server.export_rows(name, {})[0])[-1]
        expect("%s: the empty book foots with zero rows" % name, total[0], foot(name, 0))
        expect("%s: and every money column reads 0.00" % name,
               [c for c in total[1:] if c != ""], ["0.00"] * len(server.EXPORT_TOTAL_COLUMNS[name]))
    shutil.rmtree(work, ignore_errors=True)


def check_a_file_without_refunds():
    """The refund wording belongs only to a file that actually has one, and one row is one row."""
    work = tempfile.mkdtemp(prefix="chrisphics-csv-singular-")
    db = os.path.join(work, "counter.db")
    clone_to(db)
    server.connect(db)
    wipe_and_seed(db)
    con = sqlite3.connect(db)
    con.execute("DELETE FROM payments WHERE id = 902")
    con.commit()
    con.close()
    print("\n--- a payments file with no refund in it ---")
    rows = show("payments", server.export_rows("payments", {})[0])
    header, total = rows[1], rows[-1]
    expect("one row is not counted as several", total[0], "Total (1 row)")
    expect("and nothing is counted off it", total[header.index("Amount")], "100.00")
    shutil.rmtree(work, ignore_errors=True)


def check_the_sum_guard():
    print("\n--- a column holding something that is not money ---")
    expect("a blank and a word are skipped, the number is kept",
           server.export_totals("jobs", [{"total": None}, {"total": "to discuss"},
                                         {"total": 5}]),
           {"total": 5.0, "cost": 0.0, "profit": 0.0, "paid": 0.0, "balance": 0.0})
    expect("a refund counts off, a payment counts in",
           server.export_totals("payments", [{"amount": 100, "kind": "Payment"},
                                             {"amount": 20, "kind": "Refund"}]), {"amount": 80.0})
    expect("no rows still gives every money column a figure",
           server.export_totals("clients", []),
           {"billed": 0.0, "received": 0.0, "balance_due": 0.0})
    expect("an export with no money columns of its own is not totalled",
           server.export_totals("nonsense", [{"anything": 1}]), {})


def check_over_http(db):
    server_work = tempfile.mkdtemp(prefix="chrisphics-csv-server-")
    env = dict(os.environ, CHRISPHICS_DB=db, CHRISPHICS_AUTH_PASSWORD=WORD,
               CHRISPHICS_SUPPORT=os.path.join(server_work, "support"),
               CHRISPHICS_BACKUP_DIR=os.path.join(server_work, "Backups"),
               PYTHONUNBUFFERED="1")
    proc = subprocess.Popen([sys.executable, os.path.join(REPO, "server.py"), "--host", "127.0.0.1",
                             "--port", str(PORT), "--no-browser"], cwd=REPO, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        up = False
        for _ in range(60):
            try:
                with urllib.request.urlopen(BASE + "/healthz", timeout=2) as r:
                    up = bool(json.load(r).get("ok"))
                    if up:
                        break
            except Exception:
                pass
            if proc.poll() is not None:
                break
            time.sleep(0.25)
        say("the test shop answers on port %d" % PORT, up)
        if not up:
            return
        for name in EXPORTS:
            with urllib.request.urlopen(BASE + "/api/export/%s.csv" % name, timeout=8) as res:
                raw = res.read()
                disposition = res.headers.get("Content-Disposition") or ""
            expect("%s: the spreadsheet byte-order mark is still first" % name,
                   raw[:3], b"\xef\xbb\xbf")
            say("%s: offered as a download, named for today" % name,
                re.search(r'filename="%s-\d{4}-\d\d-\d\d\.csv"' % name, disposition) is not None,
                disposition)
            rows = parse(raw.decode("utf-8-sig"))
            expect("%s: the totals line arrived over the wire" % name,
                   rows[-1][0], foot(name, len(rows) - 3))
            for label, expected in WANT[name].items():
                expect("%s over the wire: %s" % (name, label),
                       round(float(dict(zip(rows[1], rows[-1]))[label]), 2), expected)
    finally:
        proc.terminate()
        for _ in range(40):
            if proc.poll() is not None:
                break
            time.sleep(0.15)
        shutil.rmtree(server_work, ignore_errors=True)


def main():
    digest_before = hashlib.sha256(open(LIVE, "rb").read()).hexdigest()
    check_seeded_export()
    check_empty_book()
    check_a_file_without_refunds()
    check_the_sum_guard()
    work = tempfile.mkdtemp(prefix="chrisphics-csv-wire-")
    db = os.path.join(work, "counter.db")
    clone_to(db)
    server.connect(db)
    wipe_and_seed(db)
    check_over_http(db)
    clear(db)
    shutil.rmtree(work, ignore_errors=True)
    expect("the shop's own book was not written",
           hashlib.sha256(open(LIVE, "rb").read()).hexdigest(), digest_before)
    print("\n%d failed" % len(fails))
    for f in fails:
        print("  - %s" % f)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
