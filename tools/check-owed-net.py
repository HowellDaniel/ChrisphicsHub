#!/usr/bin/env python3
"""Prove what the shop says it can still collect, on a COPY of its book.

The owner's complaint this answers: every client had paid, yet the dashboard and the Accounts
screen still said a large sum was owed. Both figures added up job balances only and ignored money
a client handed over that was left sitting on their account instead of against a job. These checks
plant that exact situation on a throwaway copy and read the figures back over HTTP, the way the
screens do.

The live book is opened read-only and copied into a temp folder; every record here is written to
that copy through the shop's own API. The shop's file is never written to, and nothing here needs
the shop's password: loopback is the shop.

The last section tests the other half of the promise. A page copied into public/ goes live without
a restart, so the screen can be a build ahead of the server answering it — and a build behind knows
nothing about account credit. The Accounts screen is therefore rendered twice, against this build
and against the previous commit's, and must tell the truth about the money in both.

    python3 tools/check-owed-net.py
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
PORT = 8902
BASE = "http://127.0.0.1:%d" % PORT

# Worked out by hand, before looking at any code. Each client: what they were billed, what money
# they handed over that is still on their account, and what the shop can genuinely still collect.
#   ALAN   one job 200.00; 200.00 paid, left on the account       owed 200.00,  held 200.00 -> 0.00
#   BILAL  two jobs 663.00 and 624.00; 1135.00 on the account    owed 1287.00, held 1135.00 -> 152.00
#   CODY   one job 100.00; nothing paid                           owed 100.00,  held 0.00 -> 100.00
#   DORA   one job 150.00 paid against it; 50.00 left over        owed 0.00,  held 50.00 -> 0.00
#   ELSA   one job 100.00; 30.00 refunded off her account         owed 100.00,  held -30.00 -> 130.00
#   FEMI   one job 500.00, archived before any of it was paid      not counted at all
GROSS = 1687.00        # every balance on ALAN..ELSA's jobs: the figure the shop used to see
OLD = 2187.00          # ... plus FEMI's archived job, which the old sum never left out either
COLLECTABLE = 382.00   # 152.00 + 100.00 + 130.00, floored per client
UNFLOORED = 332.00     # the same without the floor: DORA's 50.00 surplus hiding ELSA's debt

work = tempfile.mkdtemp(prefix="chrisphics-owed-")
CLONE = os.path.join(work, "clone.db")
src = sqlite3.connect("file:%s?mode=ro" % LIVE, uri=True)
dst = sqlite3.connect(CLONE)
src.backup(dst)
dst.close()
src.close()

wipe = sqlite3.connect(CLONE)
for table in ("clients", "jobs", "payments", "expenses", "leads"):
    wipe.execute("DELETE FROM %s" % table)
# The counter's own Mac now asks to sign in, which would turn these loopback writes away, and no
# check here holds the shop's word. Cleared on this throwaway copy only.
wipe.execute("UPDATE app_state SET value='0' WHERE key='shop_mac_login'")
wipe.commit()
wipe.close()

live_stat = os.stat(LIVE)
live_hash = hashlib.sha256(open(LIVE, "rb").read()).hexdigest()

env = dict(os.environ, CHRISPHICS_DB=CLONE, CHRISPHICS_SUPPORT=work,
           CHRISPHICS_BACKUP_DIR=os.path.join(work, "Backups"), PYTHONUNBUFFERED="1")
log = open(os.path.join(work, "server.log"), "wb")

fails = []


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


def client(name, phone):
    code, row = get("POST", "/api/clients", {"name": name, "phone": phone})
    assert code == 201, row
    return row["id"]


def job(cid, price, title):
    code, row = get("POST", "/api/jobs", {"client_id": cid, "kind": "Job", "title": title,
                                          "quantity": 1, "unit": "set", "unit_price": price,
                                          "category": "Flyers", "status": "Ready"})
    assert code == 201, row
    assert round(float(row["total"]), 2) == price, (row["ref"], row["total"])
    return row


def pay(client_id=None, amount=0, job_id=None, kind="Payment"):
    body = {"client_id": client_id, "amount": amount, "kind": kind, "method": "Cash"}
    if job_id:
        body["job_id"] = job_id
    code, row = get("POST", "/api/payments", body)
    assert code == 201, row
    return row


def read_book(sql):
    """One figure, straight out of the copy, the way the ledger itself would say it."""
    con = sqlite3.connect("file:%s?mode=ro" % CLONE, uri=True)
    try:
        return round(float(con.execute(sql).fetchone()[0] or 0), 2)
    finally:
        con.close()


BALANCES = "SELECT coalesce(sum(ja.balance),0) FROM job_accounts ja%s WHERE ja.kind='Job' " \
           "AND ja.status<>'Cancelled' AND ja.balance > 0%s"

# What the Accounts screen really shows, read out of the laid-out page rather than off the wire: the
# words under the headline, and every amount the screen is offering to collect or waving away.
PAGE_JS = r"""
JSON.stringify((function () {
  var v = document.getElementById('view');
  if (!v) return { missing: 'view' };
  var stats = v.querySelectorAll('.stat'), head = null, i;
  for (i = 0; i < stats.length; i++) {
    var k = stats[i].querySelector('.k');
    if (k && k.textContent === 'Owed on jobs') {
      var n = stats[i].querySelector('.n');
      head = n ? n.textContent : null;
      break;
    }
  }
  var gets = [], pills = 0, nodes;
  nodes = v.querySelectorAll('[data-pay-job]');
  for (i = 0; i < nodes.length; i++) gets.push(parseFloat(nodes[i].getAttribute('data-pay-amount')));
  nodes = v.querySelectorAll('.pill');
  for (i = 0; i < nodes.length; i++) {
    if (nodes[i].textContent === 'Covered by account credit') pills++;
  }
  return { head: head, gets: gets, pills: pills };
})())
"""

proc = boot()
old_proc = None
old_log = None
try:
    alan, bilal, cody, dora, elsa, femi = ((client("ALAN OC", "0200000101")),
                                           client("BILAL OC", "0200000102"),
                                           client("CODY OC", "0200000103"),
                                           client("DORA OC", "0200000104"),
                                           client("ELSA OC", "0200000105"),
                                           client("FEMI OC", "0200000106"))
    job(alan, 200.00, "Alan's banners")
    job(bilal, 663.00, "Bilal's shirts")
    job(bilal, 624.00, "Bilal's flyers")
    cody_job = job(cody, 100.00, "Cody's cards")
    dora_job = job(dora, 150.00, "Dora's stickers")
    job(elsa, 100.00, "Elsa's books")
    job(femi, 500.00, "Femi's signage")
    pay(alan, 200.00)                      # handed over at the counter, left on the account
    pay(bilal, 1135.00)                    # same, and it does not quite cover both his jobs
    pay(dora, 150.00, dora_job["id"])      # paid against the job, so that one is settled
    pay(dora, 50.00)                       # and 50.00 more, left on the account
    pay(elsa, 30.00, kind="Refund")        # money back out of the shop, off her account
    get("DELETE", "/api/clients/%d" % femi)  # archived, so out of the shop's money entirely

    # 1. the situation the owner was standing in
    expect("the sum of job balances a live client still carries",
           read_book(BALANCES % (" JOIN clients c ON c.id = ja.client_id AND c.archived = 0", "")),
           GROSS)
    expect("and what the old figure said, archived job included",
           read_book(BALANCES % ("", "")), OLD)

    dash = get("GET", "/api/dashboard")[1]
    acct = get("GET", "/api/accounts")[1]
    kpi, totals = dash["kpi"], acct["totals"]

    # 2. both screens now say what can actually be collected
    expect("dashboard 'Owed to the shop'", round(float(kpi["outstanding"]), 2), COLLECTABLE)
    expect("Accounts 'Owed on jobs'", round(float(totals["receivable"]), 2), COLLECTABLE)
    expect("and the two screens agree with each other", kpi["outstanding"], totals["receivable"])
    check("the headline is smaller than the pile of balances it replaced",
          COLLECTABLE < GROSS, "%s against %s" % (COLLECTABLE, GROSS))

    # 3. one client's surplus must never hide another client's debt
    unfloored = read_book("SELECT sum(ca.open_job_balance - ca.account_credit)"
                          " FROM client_accounts ca JOIN clients c ON c.id = ca.id"
                          " WHERE c.archived = 0")
    expect("without the per-client floor DORA's 50.00 would cover part of ELSA's", unfloored, UNFLOORED)
    expect("the headline keeps her surplus out of everybody else's total",
           round(float(kpi["outstanding"]) - unfloored, 2), 50.00)

    # 4. an archived client's unpaid job is out of it
    expect("FEMI's archived 500.00 is nowhere in the headline",
           read_book(BALANCES % (" JOIN clients c ON c.id = ja.client_id AND c.archived = 1", "")),
           500.00)

    # 5. the dashboard's own list of who still owes
    top = {row["name"].split()[0]: round(float(row["owed"]), 2) for row in dash["debtors"]}
    expect("top debtors: BILAL, net of what he handed over", top.get("BILAL"), 152.00)
    expect("top debtors: ELSA owes more than her job after a refund", top.get("ELSA"), 130.00)
    expect("top debtors: CODY untouched, he has paid nothing", top.get("CODY"), 100.00)
    expect("top debtors: ALAN has gone, his work is paid for in all but name", "ALAN" in top, False)
    expect("top debtors: DORA has gone, her surplus is not somebody's debt", "DORA" in top, False)
    expect("top debtors: FEMI is archived", "FEMI" in top, False)
    expect("and the list adds up to the headline", round(sum(top.values()), 2), COLLECTABLE)

    # 6. the Clients screen, which already netted it, still agrees with the new figures
    due = {row["name"].split()[0]: row for row in get("GET", "/api/clients")[1]}
    expect("clients screen: ALAN's balance is cleared", due["ALAN"]["balance_due"], 0.00)
    expect("clients screen: BILAL's matches the headline", due["BILAL"]["balance_due"], 152.00)
    expect("clients screen: DORA reads as money held, not a debt", due["DORA"]["balance_due"], -50.00)

    # 7. the outstanding table shows, job by job, where the difference between it and the headline went
    rows = {row["client"].split()[0] + "/" + row["ref"]: row for row in acct["debtors"]}
    expect("every job still carrying a balance is listed, archived out", len(rows), 5)

    def theirs(name):
        return sorted((r for k, r in rows.items() if k.startswith(name)),
                      key=lambda r: -r["balance"])

    big, small = theirs("BILAL")
    expect("BILAL's bigger job is covered in full by his credit", big["covered_by_credit"], 663.00)
    expect("the rest of his credit lands on his other job", small["covered_by_credit"], 472.00)
    expect("and 152.00 of that one is genuinely still owed", small["still_owed"], 152.00)
    expect("his two rows together still owe the same 152.00",
           round(sum(r["still_owed"] for r in theirs("BILAL")), 2), 152.00)
    expect("ALAN's job is fully covered", theirs("ALAN")[0]["still_owed"], 0.00)
    expect("CODY's job carries no credit", theirs("CODY")[0]["covered_by_credit"], 0.00)
    expect("and ELSA's job shows its own balance", theirs("ELSA")[0]["still_owed"], 100.00)
    expect("the rows add up to the headline once ELSA's refund is put back",
           round(sum(r["still_owed"] for r in acct["debtors"]) + 30.00, 2), COLLECTABLE)

    # 8. the page can be a build ahead of the shop's running server — copying public/ in needs no
    #    restart, and that is how this app ships — so the very same page is loaded against a server
    #    that has never heard of account credit, and asked what it says about money it was not told
    #    about. A row that reads "Covered by account credit" there would be a job the counter stops
    #    chasing because a figure the server never sent happened to be missing.
    old_dir = os.path.join(work, "old-build")
    os.makedirs(old_dir)
    with open(os.path.join(old_dir, "server.py"), "wb") as fh:
        fh.write(subprocess.run(["git", "show", "HEAD:server.py"], cwd=REPO,
                                 capture_output=True).stdout)
    for name in ("public", "schema.sql"):
        os.symlink(os.path.join(REPO, name), os.path.join(old_dir, name))
    OLD_CLONE = os.path.join(work, "old-clone.db")
    src = sqlite3.connect("file:%s?mode=ro" % CLONE, uri=True)
    dst = sqlite3.connect(OLD_CLONE)
    src.backup(dst)
    dst.close()
    src.close()
    old_support = os.path.join(work, "old-support")
    old_backups = os.path.join(work, "old-Backups")
    os.makedirs(old_support)
    os.makedirs(old_backups)
    old_log = open(os.path.join(work, "old-server.log"), "wb")
    old_proc = subprocess.Popen(["/usr/bin/python3", os.path.join(old_dir, "server.py"),
                                 "--port", str(PORT + 1), "--no-browser"], cwd=old_dir,
                                env=dict(env, CHRISPHICS_DB=OLD_CLONE,
                                         CHRISPHICS_SUPPORT=old_support,
                                         CHRISPHICS_BACKUP_DIR=old_backups),
                                stdout=old_log, stderr=old_log)
    old_base = "http://127.0.0.1:%d" % (PORT + 1)
    up = False
    for _ in range(80):
        try:
            with urllib.request.urlopen(old_base + "/healthz", timeout=3) as res:
                up = json.loads(res.read().decode())["ok"]
                break
        except Exception:
            time.sleep(0.15)
    check("a build behind the shop's own is running for the page to be tested against", up,
          old_base)

    probe = os.path.join(work, "wk-probe")
    built = subprocess.run(["swiftc", os.path.join(REPO, "tools/wk-probe.swift"), "-o", probe],
                           capture_output=True, text=True, timeout=300)
    if not check("the rendering engine probe compiles", built.returncode == 0,
                 (built.stderr or "")[-160:]):
        fails.append("the Accounts screen was never rendered, so the skew went untested")
    else:
        def page(base):
            out = subprocess.run(
                [probe, base, work, "open|/#/accounts",
                 "poll|!!document.querySelector('#view .stat .n')", "say|" + PAGE_JS],
                capture_output=True, text=True, timeout=180)
            for line in (out.stdout or "").splitlines():
                line = line.strip()
                if line.startswith("{"):
                    return json.loads(line)
            for line in (out.stdout or "").splitlines() + (out.stderr or "").splitlines():
                if line.strip().startswith(("TIMEOUT", "EVAL ERROR", "NAV FAILED")):
                    fails.append("the probe said: " + line.strip()[:60])
            return {}

        now = page(BASE)
        behind = page(old_base)
        expect("the new server: the headline says what it did with the money",
               now.get("head"), "job balances, less money kept on accounts")
        expect("the new server: two jobs are covered and say so", now.get("pills"), 2)
        expect("the new server: the rest ask for what is genuinely still owed",
               sorted(now.get("gets") or []), sorted([100.00, 100.00, 152.00]))
        expect("a server a build behind: nothing is claimed as covered",
               behind.get("pills"), 0)
        expect("a server a build behind: every job still asks to be collected",
               sorted(behind.get("gets") or []), sorted([100.00, 100.00, 200.00, 624.00, 663.00]))
        expect("a server a build behind: and the headline admits it only added up balances",
               behind.get("head"), "job balances")

    # 9. ordinary business is unaffected: a client with no credit pays, and the number falls
    pay(cody, 100.00, cody_job["id"])
    after = get("GET", "/api/accounts")[1]
    expect("CODY paying against his job takes 100.00 off the headline",
           round(float(after["totals"]["receivable"]), 2), COLLECTABLE - 100.00)

    # 10. an empty shop owes the shop nothing
    empty = sqlite3.connect(CLONE)
    for table in ("clients", "jobs", "payments", "expenses", "leads"):
        empty.execute("DELETE FROM %s" % table)
    empty.commit()
    empty.close()
    zero_dash = get("GET", "/api/dashboard")[1]
    zero_acct = get("GET", "/api/accounts")[1]
    expect("an empty book reports 0.00 on the dashboard",
           round(float(zero_dash["kpi"]["outstanding"]), 2), 0.00)
    expect("an empty book reports 0.00 on Accounts",
           round(float(zero_acct["totals"]["receivable"]), 2), 0.00)
    expect("and no debtor rows", len(zero_acct["debtors"]), 0)
finally:
    if old_proc:
        old_proc.terminate()
        try:
            old_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            old_proc.kill()
    if old_log:
        old_log.close()
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
    shutil.rmtree(work, ignore_errors=True)

print("\n%d failed" % len(fails))
sys.exit(1 if fails else 0)
