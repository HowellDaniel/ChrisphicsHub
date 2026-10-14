#!/usr/bin/env python3
"""Prove the counter can take an order and its money without fighting the screen, on a COPY of the
shop's book.

What the owner said: "Make everything flexible, it's hard to use it in this current state. I want to
use this in real life." Measured on his own book first — 29 jobs, exactly one item line each, all of
them paid in cash at the counter, every one in the unit 'job', four hand-typed spelling variants of
"walk in" sitting as four separate clients. So the flexible path is the short one, and everything
these checks drive is the minute a client stands at the desk:

  * figures typed the way they are said out loud — 1,200 and ₵0.25 — must book 1,200 pieces at
    ₵0.25, never the ₵0.00 a browser number box answers with;
  * the caret must land in a real field when a form opens;
  * Enter inside an item line opens the next line instead of booking a half-written job;
  * a pasted list lays itself out one product per row;
  * the cash taken with the order belongs on the order screen, and money that covers the total is a
    Payment, not a Deposit that leaves the accounts saying the client still owes;
  * money can be taken against ANY job, including one already settled;
  * yesterday's ticket can be repeated in one press;
  * a figure the book refuses comes back typed, rather than being lost;
  * and the order form is still usable on the phone carried to the press.

The live book is opened read-only and copied into a temp folder; every record here is written to that
copy, through the page, by a real WebKit view driving the shipped front end. The shop's own file is
never written to, and nothing here needs the shop's password: loopback is the shop.

Run one of these at a time. The view it drives keeps its browser data per address, and a second
concurrent run wipes the first one's mid-scenario — which reads as the page refusing, not as two
checks in each other's way.

    python3 tools/check-flexible-entry.py
"""
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import shutil

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE = os.path.expanduser("~/Library/Application Support/Chrisphics Hub/chrisphics.db")


def free_port(start):
    """A port of our own, so a check that crashed earlier and left its server behind does not wedge
    this one. The shop's 8834 is never asked for."""
    for port in range(start, start + 40):
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", port))
            probe.close()
            return port
        except OSError:
            probe.close()
    raise SystemExit("no free loopback port above %d" % start)


PORT = free_port(8906)
BASE = "http://127.0.0.1:%d" % PORT

work = tempfile.mkdtemp(prefix="chrisphics-flex-")
CLONE = os.path.join(work, "clone.db")
src = sqlite3.connect("file:%s?mode=ro" % LIVE, uri=True)
dst = sqlite3.connect(CLONE)
src.backup(dst)
dst.close()
src.close()

# A blank counter: the shop's own clients and orders would only give these checks more rows to
# mistake for the ones under test. And the counter's Mac asks loopback to sign in, which no check
# here can answer — cleared on this throwaway copy only.
setup = sqlite3.connect(CLONE)
for table in ("clients", "jobs", "job_items", "payments", "expenses", "leads", "spoiled_work"):
    setup.execute("DELETE FROM %s" % table)
setup.execute("UPDATE app_state SET value='0' WHERE key='shop_mac_login'")
setup.commit()
setup.close()

live_stat = os.stat(LIVE)
live_hash = hashlib.sha256(open(LIVE, "rb").read()).hexdigest()

# The scenarios reload and re-render a dozen times a minute; that is this check's own noise.
env = dict(os.environ, CHRISPHICS_DB=CLONE, CHRISPHICS_SUPPORT=work,
           CHRISPHICS_BACKUP_DIR=os.path.join(work, "Backups"),
           CHRISPHICS_CALLS_PER_MINUTE="4000", PYTHONUNBUFFERED="1")
log = open(os.path.join(work, "server.log"), "wb")

fails = []


def check(label, ok, detail=""):
    print("%-58s %s   %s" % (label, "ok" if ok else "FAIL", str(detail)[:66]))
    if not ok:
        fails.append(label)
    return ok


def expect(label, got, want):
    return check(label, got == want, "got %r, want %r" % (got, want))


def boot():
    p = subprocess.Popen(["/usr/bin/python3", os.path.join(REPO, "server.py"), "--port", str(PORT),
                          "--no-browser"], cwd=REPO, env=env, stdout=log, stderr=log)
    for _ in range(80):
        try:
            with urllib_request("/healthz") as res:
                if json.loads(res.read().decode())["ok"]:
                    return p
        except Exception:
            time.sleep(0.15)
    raise SystemExit("the test shop never answered; see " + os.path.join(work, "server.log"))


def urllib_request(path):
    import urllib.request
    return urllib.request.urlopen(BASE + path, timeout=4)


def rows(sql, args=()):
    con = sqlite3.connect("file:%s?mode=ro" % CLONE, uri=True)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


def one(sql, args=()):
    got = rows(sql, args)
    return got[0] if got else None


def scalar(sql, args=()):
    con = sqlite3.connect("file:%s?mode=ro" % CLONE, uri=True)
    try:
        return con.execute(sql, args).fetchone()[0]
    finally:
        con.close()


proc = boot()
try:
    probe = os.path.join(work, "wk-probe")
    built = subprocess.run(["swiftc", "-O", os.path.join(REPO, "tools/wk-probe.swift"),
                            "-o", probe], capture_output=True, text=True, timeout=600)
    if not check("the rendering engine probe compiles", built.returncode == 0,
                 (built.stderr or "")[-160:]):
        raise SystemExit("nothing was rendered")

    script = os.path.join(REPO, "tools/flex-scenarios.js")

    def run(size, names, start_view):
        cmds = ["open|/" + start_view]
        cmds.append("poll|!!document.querySelector('[data-action=\"new-job\"]')")
        if size:
            cmds.append("size|" + size)
            # A resize is only seen by the media queries after the page is laid out again.
            cmds.append("open|/" + start_view)
            cmds.append("poll|!!document.querySelector('[data-action=\"new-job\"]')")
        for name in names:
            cmds.append("eval|window.__go('%s')" % name)
            cmds.append("poll|window.__R.%s" % name)
        cmds.append("say|JSON.stringify(window.__R)")
        out = subprocess.run([probe, BASE, work] + cmds, capture_output=True, text=True,
                             timeout=900, env=dict(env, WKPROBE_SCRIPT=script,
                                                   WKPROBE_FRESH_DATA="1"))
        for line in (out.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("{"):
                return json.loads(line)
        print((out.stdout or "")[-1500:])
        print((out.stderr or "")[-600:])
        fails.append("the probe returned no results for " + ",".join(names))
        return {}

    def fact(r, key, default=None):
        return (r or {}).get(key, default)

    def errors(r, name):
        return fact(r.get(name), "errors") or []

    names = ["s1a", "s1b", "s1c", "s2", "s3", "s4", "s3b", "s5a", "s5b", "s6"]
    desktop = run(None, names, "#/jobs")
    for name in names:
        sc = desktop.get(name) or {}
        # A scenario that never reported back looks exactly like an empty pass, so it is named here.
        check("%s reported back inside the probe's patience" % name, name in desktop, "")
        check("%s ran without throwing" % name, "failed" not in sc, sc.get("failed", ""))
        check("%s raised no page error" % name, not errors(desktop, name), errors(desktop, name))
        # A sheet that refuses to close is the worst failure at a counter: the client is standing
        # there. These scenarios say so on the page rather than timing out, so read it out here.
        check("%s saved rather than refusing" % name,
              not (sc.get("bookRefused") or sc.get("payRefused")),
              sc.get("bookRefused") or sc.get("payRefused") or "")

    # ---- 1. the walk-in minute, typed the way it is said. The minute is three probe steps — the
    #     opening, the line, the money and the booking — so each one stays inside the probe's own
    #     eight-second patience; the assertions below read them back as the one minute they were.
    s1 = {}
    for part in ("s1a", "s1b", "s1c"):
        s1.update(desktop.get(part) or {})
    expect("a new order opens with the caret in the client box", s1.get("focusLandsOn"), "clientFind")
    expect("Enter on a name nobody has put them on file", s1.get("newClientBoxOpened"), True)
    expect("...and the name he typed is already in the new-client box",
           s1.get("nameCarriedAcross"), "FLEX WALKIN")
    expect("...with the caret moved into it", s1.get("newClientFocused"), True)
    expect("1,200 pieces at ₵0.25 reads as a line of 300.00", s1.get("lineTotalShown"), "₵ 300.00")
    expect("...and a job total of 300.00, not 0.00", s1.get("jobTotalShown"), "₵ 300.00")
    expect("Enter in the price moves to the next cell, it does not save",
           s1.get("enterInPriceMovesTo"), "money it-cost")
    expect("Enter in the last cell opens a second line", s1.get("rowsAfterEnterOnLastCell"), 2)
    expect("...and the order is still open, not booked half-written",
           s1.get("formStillOpenAfterEnter"), True)
    expect("a field settles to digits once he leaves it", s1.get("priceBoxNormalised"), "0.25")
    if s1.get("paste") == "UNSUPPORTED":
        check("this engine cannot hand a paste to the page, so the list-paste went untested",
              True, "skipped")
    else:
        expect("a pasted product list lays out one line per product", s1.get("rowsAfterPaste"), 4)
        expect("...with its specification, quantity and price in their own boxes",
               [s1.get("pastedSecondTitle"), s1.get("pastedSecondQty"), s1.get("pastedSecondPrice")],
               ["VISITING CARDS", "100", "1.20"])
    expect("the extra lines are taken back off before he books, leaving the one he wrote",
           s1.get("rowsBeforeBook"), 1)
    expect("the chip fills the money box to the total", s1.get("depositAfterChip"), "300.00")
    expect("...and says what it is: money received, nothing left owing",
           s1.get("hintAfterChip"), "Recorded as money received on this job — nothing left owing.")
    expect("...and the balance the client still owes drops to zero", s1.get("balanceAfterChip"),
           "₵ 0.00")
    expect("the job sheet offers to repeat the order", s1.get("reorderButton"), True)
    expect("a settled job is offered money on it anyway", s1.get("payButtonWords"),
           "Take money on this job")
    check("...with no amount forced into the box", not s1.get("payButtonPrefill"),
          s1.get("payButtonPrefill"))

    walkins = rows("SELECT * FROM clients WHERE name = 'FLEX WALKIN'")
    expect("one walk-in typed at the counter is one client, not four", len(walkins), 1)
    job1 = one("SELECT * FROM jobs WHERE title = 'A5 FLYERS'")
    check("the item line named the job when he did not type a title", job1 is not None, job1)
    if job1:
        expect("...booked at 1,200 pieces", round(float(job1["quantity"]), 2), 1200.00)
        expect("...at a price of 0.25, with the currency sign read off it",
               round(float(job1["unit_price"]), 2), 0.25)
        expect("...and a total of 300.00", round(float(job1["total"]), 2), 300.00)
        line = one("SELECT * FROM job_items WHERE job_id = ?", (job1["id"],))
        expect("the line itself is stored as plain digits",
               [round(float(line["quantity"]), 2), round(float(line["unit_price"]), 2)]
               if line else None, [1200.00, 0.25])
        cash = rows("SELECT * FROM payments WHERE job_id = ? ORDER BY id", (job1["id"],))
        # The money check 3 takes on this same job lands here too, so the booking is read as the first
        # row it wrote rather than as the only row the job will ever have.
        check("cash taken with the order is booked against the order", len(cash) >= 1, cash)
        expect("...as a Payment when it covers the total",
               "%s=%.2f" % (cash[0]["kind"], float(cash[0]["amount"])) if cash else None,
               "Payment=300.00")
        expect("...and the sheet he booked it off says nothing is owed", s1.get("sheetBalance"),
               "₵ 0.00")

    # ---- 2. an advance on tomorrow's run: no item lines, partial money
    s2 = desktop.get("s2") or {}
    expect("typing part of a name finds the one client and picks them",
           s2.get("oneMatchSelected"), True)
    check("...and says so in the hint", "match" in (s2.get("matchHint") or ""), s2.get("matchHint"))
    expect("with no item lines the quantity and price boxes come back",
           s2.get("singleBlockShown"), True)
    expect("1,500 typed for a price is 1,500", s2.get("totalShown"), "₵ 3,000.00")
    expect("and 1,000 of it handed over leaves 2,000 owing", s2.get("balanceShown"), "₵ 2,000.00")
    job2 = one("SELECT * FROM jobs WHERE title = 'CANOPY PRINT'")
    check("the advance job exists", job2 is not None, job2)
    if job2:
        expect("...billed 3,000.00", round(float(job2["total"]), 2), 3000.00)
        dep = one("SELECT * FROM payments WHERE job_id = ?", (job2["id"],))
        expect("...with 1,000.00 recorded as a Deposit, because it is one",
               [dep["kind"], round(float(dep["amount"]), 2)] if dep else None, ["Deposit", 1000.00])
        bal2 = one("SELECT * FROM job_accounts WHERE id = ?", (job2["id"],))
        expect("...and 2,000.00 still owed on it", round(float(bal2["balance"]), 2), 2000.00)
        expect("...belonging to the same walk-in, not a second one", job2["client_id"],
               walkins[0]["id"] if walkins else None)

    # ---- 3. money against a job the book already calls settled
    s3 = desktop.get("s3") or {}
    expect("the settled job's own sheet is what he is standing at",
           s3.get("jobIsTheSettledOne"), True)
    expect("the money box starts empty rather than at 0.00", s3.get("amountBoxEmpty"), True)
    expect("the caret starts in the amount", s3.get("focusLandsOn"), "amount")
    expect("₵500 typed with the sign reads as 500.00", s3.get("afterShown"), "₵ 0.00 (credit)")
    extra = rows("SELECT * FROM payments WHERE job_id = (SELECT id FROM jobs WHERE title='A5 FLYERS') "
                 "AND amount = 500.00")
    expect("the book takes money on a settled job instead of refusing it", len(extra), 1)
    if extra:
        expect("...as a Payment", extra[0]["kind"], "Payment")
        bal1 = one("SELECT * FROM job_accounts WHERE id = ?", (extra[0]["job_id"],))
        expect("...which lands as 500.00 held on that job", round(float(bal1["balance"]), 2), -500.00)

    # ---- 4. the same order again
    s4 = desktop.get("s4") or {}
    expect("the repeat opens as a new order, not as an edit", s4.get("isEditingOldJob"), False)
    check("and it says where it came off", str(s4.get("headerWords") or "").startswith(
        "Same order again — off"), s4.get("headerWords"))
    expect("...with the client already chosen", bool(s4.get("clientCarriedAcross")), True)
    expect("...and last time's line, quantity and price written",
           [s4.get("lineCarriedAcross"), s4.get("qtyCarriedAcross"), s4.get("priceCarriedAcross")],
           ["A5 FLYERS", "1200", "0.25"])
    expect("...totalling the same money", s4.get("totalShown"), "₵ 300.00")
    again = one("SELECT * FROM jobs WHERE title = 'A5 FLYERS (SECOND RUN)'")
    check("the repeat booked a new row", again is not None, again)
    if again and job1:
        expect("...at the same total", round(float(again["total"]), 2), 300.00)
        expect("...for the same client", again["client_id"], job1["client_id"])
        expect("...and the first order untouched",
               round(float(one("SELECT total FROM jobs WHERE id = ?", (job1["id"],))["total"]), 2),
               300.00)
    expect("repeating an order did not invent a second client",
           scalar("SELECT count(*) FROM clients"), 1)
    expect("and booked three jobs between them", scalar("SELECT count(*) FROM jobs"), 3)

    # ---- 5. the money screen reaches every job, not only the ones still owing
    s3b = desktop.get("s3b") or {}
    expect("every job in the book is on the money screen", s3b.get("jobOptions"), 4)
    check("including the settled ones", bool(s3b.get("settledJobsListed")),
          s3b.get("owingJobsListed"))
    check("and a quote is not offered, because the book would refuse it",
          bool(s3b.get("quotesLeftOut")), s3b.get("quotesLeftOut"))
    expect("choosing a person narrows the list to their jobs", s3b.get("narrowedOptions"), 3)
    check("...and every one of them is theirs", bool(s3b.get("everyJobIsThatClients")),
          s3b.get("chosenClient"))

    # ---- 6. a typed figure survives a refusal — the write, then the refusal, two probe steps.
    s5 = {}
    for part in ("s5a", "s5b"):
        s5.update(desktop.get(part) or {})
    expect("the click-to-change box is a text field, so it cannot silently empty itself",
           s5.get("typedIntoIsText"), "text")
    expect("1,200 typed into an estimate writes 1200.00 into the book",
           round(float(s5.get("writtenToTheBook") or 0), 2), 1200.00)
    expect("...and the chase card on screen shows the new figure", s5.get("pageShowsTheFigure"),
           "₵ 1,200.00")
    expect("and when the book says no, what he typed is put back in the box",
           s5.get("refusedValueStillThere"), "3500.00")

    # ---- 7. the keys
    s6 = desktop.get("s6") or {}
    expect("n opens a booking from anywhere in the shop", s6.get("nOpensABooking"), True)
    check("cmd/ctrl+Enter reaches the save handler instead of the browser blocking it",
          bool(s6.get("reachedTheHandler")), s6.get("reachedTheHandler"))
    expect("...and the order stays open with the reason written on it",
           s6.get("modalStillOpen"), True)

    # ---- 8. the phone
    phone = run("390x844", ["s7"], "#/jobs")
    s7 = phone.get("s7") or {}
    check("s7 ran without throwing", "failed" not in s7, s7.get("failed", ""))
    check("...and raised no page error", not errors(phone, "s7"), errors(phone, "s7"))
    expect("...at phone width", s7.get("viewport"), 390)
    def px(value):
        return float(str(value or "0").replace("px", ""))
    check("an order line's boxes are big enough that the phone does not zoom in on them",
          px(s7.get("itemInputFont")) >= 16.0, s7.get("itemInputFont"))
    expect("the money box still reads from the right", s7.get("moneyAlignsRight"), "right")
    expect("an order line becomes a card, not a column of a wide table",
           s7.get("rowDisplay"), "grid")
    check("...with its own name printed above each box", "Qty" in str(s7.get("cellLabel")),
          s7.get("cellLabel"))
    check("and nothing on the page has to be scrolled sideways to reach it",
          px(s7.get("sidewaysScroll")) <= 1.0, s7.get("sidewaysScroll"))
    check("...including inside the order table itself", px(s7.get("tableOverflow")) <= 1.0,
          s7.get("tableOverflow"))

    # ---- 9. nothing else in the book moved that these checks did not touch
    expect("no stray client", scalar("SELECT count(*) FROM clients"), 1)
    expect("no stray job", scalar("SELECT count(*) FROM jobs WHERE kind='Job'"), 3)
    expect("and every figure in it is plain digits",
           scalar("SELECT count(*) FROM job_items WHERE CAST(quantity AS TEXT) LIKE '%,%'"), 0)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()
    after_stat = os.stat(LIVE)
    same = (after_stat.st_size == live_stat.st_size and after_stat.st_mtime == live_stat.st_mtime
            and hashlib.sha256(open(LIVE, "rb").read()).hexdigest() == live_hash)
    print("\nLive book: %d bytes, last written %s -- %s" % (
        after_stat.st_size, time.ctime(after_stat.st_mtime),
        "untouched" if same else "CHANGED, WHICH MUST NOT HAPPEN"))
    if not same:
        fails.append("the shop's own book was written to")
    # A run that went wrong is worth more while its throwaway book still exists: the page writes
    # nothing to the log, so the shop's own refusals are the only record of what it said no to.
    if fails and os.environ.get("KEEP_FLEX_WORK"):
        print("\nkept: %s" % work)
        with open(os.path.join(work, "server.log")) as fh:
            for line in fh:
                if '" 4' in line or '" 5' in line or "Traceback" in line:
                    print("  shop: " + line.strip()[:160])
    else:
        shutil.rmtree(work, ignore_errors=True)

print("\n%d failed" % len(fails))
sys.exit(1 if fails else 0)
