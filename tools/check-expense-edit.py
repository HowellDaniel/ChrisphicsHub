#!/usr/bin/env python3
"""Prove the counter can put right a money-out entry — change it, or take it out — on a COPY of the shop's book.

What the owner said: "The cashier did some double entry on the Expenses, but when she wants to remove
one of them it doesn't respond. So enable editing and removal options." The shop's own log says what
happened at her machine: at 22:35 on 3 November the iMac at 192.168.100.28 opened an expense
(GET /api/expenses/14, 200), saved a change to it (PUT /api/expenses/14, 400) and never sent a remove
at all — no DELETE left that machine, then or at any other time. Nothing was wrong with the engine's
routes. The Remove button asks "Remove this expense … from the book?" through the browser's own
confirm panel, and a window shell that was never told how to show that panel answers it with "no" in
silence. So the press stopped at the counter's own machine and the book never heard about it.

Two things are proven here, and the second is the one that reaches the Macs already handed out:

  * the engine answers all three by-number routes — read one expense, change it, take it out — and
    refuses honestly when the figure is zero, the entry is not there, or the money belongs to a
    spoilage record;
  * the page asks its own question, in its own box, so a press needs no native panel to land. The
    fixture takes that door away on purpose: window.confirm is replaced with exactly what a shell with
    no UI delegate returns ("no", silently, without asking). A flow still leaning on it would leave the
    row on the screen and fail here, rather than passing on a machine that happens to have the panel.

The live book is opened read-only and copied into a temp folder; every record written here goes to that
copy, through the page, by a real WebKit view driving the shipped front end. The shop's own file is
never written to, and nothing here needs the shop's password: loopback is the shop.

Run one of these at a time — the view keeps its browser data per address, and a second concurrent run
wipes the first one's mid-scenario.

    python3 tools/check-expense-edit.py
"""
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
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE = os.path.expanduser("~/Library/Application Support/Chrisphics Hub/chrisphics.db")
SERVER = os.path.join(REPO, "server.py")
APP_JS = os.path.join(REPO, "public", "app.js")
PORTABLE_M = os.path.join(REPO, "tools", "portable-app-shell.m")
MAIN_SWIFT = os.path.join(REPO, "desktop", "src", "main.swift")
SW_JS = os.path.join(REPO, "public", "sw.js")
VERSION_FILE = os.path.join(REPO, "VERSION")

CHECKED = []
fails = []


def text(path):
    return open(path, encoding="utf-8").read()


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


def check(label, ok, detail=""):
    CHECKED.append(label)
    print("%-58s %s   %s" % (label, "ok" if ok else "FAIL", str(detail)[:66]))
    if not ok:
        fails.append(label)
    return ok


def say(label, words):
    print("%-58s      %s" % (label, str(words)[:70]))


PORT = free_port(8921)
BASE = "http://127.0.0.1:%d" % PORT
work = tempfile.mkdtemp(prefix="chrisphics-expense-")
CLONE = os.path.join(work, "clone.db")
src = sqlite3.connect("file:%s?mode=ro" % LIVE, uri=True)
dst = sqlite3.connect(CLONE)
src.backup(dst)
src.close()
dst.close()

# A blank ledger: the shop's own entries would only give these checks rows to mistake for the ones
# under test. And the shop's own Mac asks loopback to sign in before it writes, which no check here can
# answer — that door is switched off on this throwaway copy only, and printed as a fact below.
setup = sqlite3.connect(CLONE)
for table in ("clients", "jobs", "job_items", "payments", "expenses", "leads", "spoiled_work"):
    setup.execute("DELETE FROM %s" % table)
setup.execute("UPDATE app_state SET value='0' WHERE key='shop_mac_login'")
setup.commit()
setup.close()

live_stat = os.stat(LIVE)
live_hash = hashlib.sha256(open(LIVE, "rb").read()).hexdigest()
watch = sqlite3.connect("file:%s?mode=ro" % LIVE, uri=True)
live_expenses = watch.execute("SELECT count(*) FROM expenses").fetchone()[0]
real_door = watch.execute("SELECT value FROM app_state WHERE key='shop_mac_login'").fetchone()
watch.close()

env = dict(os.environ, CHRISPHICS_DB=CLONE, CHRISPHICS_SUPPORT=work,
           CHRISPHICS_BACKUP_DIR=os.path.join(work, "Backups"),
           CHRISPHICS_CALLS_PER_MINUTE="4000", PYTHONUNBUFFERED="1")
log = open(os.path.join(work, "server.log"), "wb")


def call(method, path, payload=None):
    """Ask the throwaway shop the way the counter's page does, and hand back both the status and the
    body — a refusal is a finding here, and its words are the point."""
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=6) as answer:
            return answer.status, json.loads(answer.read().decode() or "{}")
    except urllib.error.HTTPError as err:
        body = err.read().decode()
        try:
            return err.code, json.loads(body or "{}")
        except ValueError:
            return err.code, {"error": body[:120]}


def boot():
    proc = subprocess.Popen(["/usr/bin/python3", SERVER, "--port", str(PORT), "--no-browser"],
                            cwd=REPO, env=env, stdout=log, stderr=log)
    for _ in range(80):
        try:
            with urllib.request.urlopen(BASE + "/healthz", timeout=3) as ok:
                if ok.status == 200:
                    return proc
        except Exception:
            time.sleep(0.15)
    raise SystemExit("the test shop never answered; see " + os.path.join(work, "server.log"))


proc = boot()
try:
    print("== the money-out routes the counter needs, on a throwaway book")
    say("the shop's own book", "%d expenses on %s, unchanged by this check" % (live_expenses, LIVE))
    say("sign-in door on the shop's own Mac", "shop_mac_login=%s (off on the copy only)" %
        (real_door[0] if real_door else "unset"))

    status, client = call("POST", "/api/clients", {"name": "Zephyr Print House"})
    check("a client is on file to hang the records on", status == 201, client)
    status, job = call("POST", "/api/jobs", {"kind": "Job", "client_id": client["id"],
                                             "title": "Zephyr 400 flyers", "quantity": 1,
                                             "unit": "job", "unit_price": 400, "status": "Pending"})
    check("and a job to charge one expense against", status == 201, job.get("ref"))

    def expense(payload):
        made = call("POST", "/api/expenses", payload)
        assert made[0] == 201, made
        return made[1]

    twin = expense({"spent_on": "2026-11-03", "category": "Paper / Stock", "payee": "BOSS",
                    "amount": 50, "method": "Cash", "reference": "NANA", "note": "PAPER"})
    expense({"spent_on": "2026-11-03", "category": "Paper / Stock", "payee": "BOSS",
             "amount": 50, "method": "Cash", "reference": "NANA", "note": "PAPER"})
    onjob = expense({"spent_on": "2026-11-03", "category": "Ink / Toner", "payee": "ZEPHYR",
                     "amount": 40, "method": "Cash", "job_id": job["id"], "note": "INK"})
    status, spoil = call("POST", "/api/spoiled", {"job_id": job["id"], "quantity": 2,
                                                  "reason": "JAMMED", "amount": 120})
    check("a double entry, an entry on a job and a spoilage record all exist",
          status == 201 and spoil.get("expense_id"), spoil.get("id"))
    linked = spoil["expense_id"]

    status, read = call("GET", "/api/expenses/%d" % twin["id"])
    check("one expense reads back by its own number",
          status == 200 and read["id"] == twin["id"] and read["note"] == "PAPER", read)
    status, missing = call("GET", "/api/expenses/999999")
    check("a number that is not in the book says so",
          status == 404 and "not found" in (missing.get("error") or "").lower(), missing)

    status, moved = call("PUT", "/api/expenses/%d" % twin["id"],
                         {"spent_on": "2026-11-02", "category": "Other", "payee": "BOSS",
                          "amount": 77.25, "method": "Cash", "reference": "NANA",
                          "job_id": None, "note": "PAPER, corrected"})
    check("a change written back lands, in the words the counter typed",
          status == 200 and moved["amount"] == 77.25 and moved["spent_on"] == "2026-11-02", moved)
    kept = sqlite3.connect("file:%s?mode=ro" % CLONE, uri=True).execute(
        "SELECT amount, category FROM expenses WHERE id=?", (twin["id"],)).fetchone()
    check("and the book keeps it, not just the answer",
          kept and kept[0] == 77.25 and kept[1] == "Other", kept)

    status, refused = call("PUT", "/api/expenses/%d" % twin["id"],
                           {"amount": 0, "category": "Other", "payee": "BOSS", "method": "Cash"})
    check("an expense changed to nothing is refused, in plain words",
          status == 400 and "greater than zero" in (refused.get("error") or ""), refused)
    status, refused = call("PUT", "/api/expenses/%d" % linked,
                           {"amount": 10, "category": "Spoilage", "payee": "x", "method": "Cash"})
    check("money the spoilage record owns is not edited from here",
          status == 400 and "Spoiled work" in (refused.get("error") or ""), refused)

    cost_before = call("GET", "/api/jobs/%d" % job["id"])[1]["cost"]
    status, moved = call("PUT", "/api/expenses/%d" % onjob["id"],
                         {"spent_on": "2026-11-03", "category": "Ink / Toner", "payee": "ZEPHYR",
                          "amount": 90, "method": "Cash", "job_id": job["id"], "note": "INK"})
    cost_moved = call("GET", "/api/jobs/%d" % job["id"])[1]["cost"]
    check("an expense moved onto a job moves that job's cost with it",
          status == 200 and cost_moved == round(cost_before + 50, 2),
          "cost %s -> %s" % (cost_before, cost_moved))

    total_before = call("GET", "/api/expenses")[1]["total"]
    status, done = call("DELETE", "/api/expenses/%d" % twin["id"])
    gone_answer = call("GET", "/api/expenses/%d" % twin["id"])[0] == 404
    total_after = call("GET", "/api/expenses")[1]["total"]
    check("the second entry of the pair comes out of the book",
          status == 200 and done.get("ok") and gone_answer, done)
    check("and the month's out figure drops by exactly what left it",
          round(total_after, 2) == round(total_before - 77.25, 2),
          "%s -> %s" % (total_before, total_after))

    status, refused = call("DELETE", "/api/expenses/%d" % linked)
    check("the spoilage record's own money-out refuses to be removed here",
          status == 400 and "Spoiled work" in (refused.get("error") or ""), refused)
    check("…and the refusal sends the counter to the screen that does it",
          "Spoiled work" in (refused.get("error") or ""), refused.get("error"))
    status, done = call("DELETE", "/api/expenses/%d" % onjob["id"])
    cost_back = call("GET", "/api/jobs/%d" % job["id"])[1]["cost"]
    check("a job's expense leaving puts that job's cost back",
          done.get("ok") and cost_back == round(cost_before - 40, 2),
          "cost back to %s (spoilage never entered it)" % cost_back)
    status, missing = call("DELETE", "/api/expenses/999999")
    check("removing an entry that is not there says so, rather than pretending",
          status == 404 and "not found" in (missing.get("error") or "").lower(), missing)

    # ---------------------------------------------------------------- the page, with the panel taken away
    print("\n== the counter's press, in the shipped engine, with the native panel gone")
    probe = os.path.join(work, "wk-probe")
    built = subprocess.run(["swiftc", "-O", os.path.join(REPO, "tools/wk-probe.swift"),
                            "-o", probe], capture_output=True, text=True, timeout=600)
    if not check("the rendering engine probe compiles", built.returncode == 0,
                 (built.stderr or "")[-160:]):
        raise SystemExit("nothing was rendered")

    # The hand-out shell's exact behaviour, forced into the fixture: the question is never shown and
    # the answer is always "no". Anything still waiting on it cannot pass below.
    poison = os.path.join(work, "no-panel.js")
    open(poison, "w").write(
        "window.__panelCalls = 0;\n"
        "window.confirm = function () { window.__panelCalls++; return false; };\n"
        "window.alert = function () { window.__panelCalls++; };\n")

    page_pair = [expense({"spent_on": "2026-11-03", "category": "Paper / Stock", "payee": "BOSS",
                          "amount": 50, "method": "Cash", "reference": "NANA", "note": "TWICE"})
                 for _ in range(2)]
    keep, drop = page_pair

    def report(step):
        return ("say|JSON.stringify({step:'%s'," % step + "%s})")

    cmds = [
        "open|/#/expenses",
        "poll|!!document.querySelector('[data-del-expense=\"%d\"]')" % drop["id"],
        report("rows") % ("rows:document.querySelectorAll('[data-del-expense]').length,"
                          "spoilageLink:!!document.querySelector('#view a[href=\"#/spoiled\"]')"),
        "eval|document.querySelector('[data-del-expense=\"%d\"]').click()" % drop["id"],
        "poll|!document.getElementById('modal').hidden",
        report("asked") % ("title:(document.querySelector('#modalCard h2')||{}).textContent||'',"
                           "body:(document.querySelector('#modalCard p')||{}).textContent||'',"
                           "yes:(document.getElementById('askYes')||{}).textContent||'',"
                           "no:(document.getElementById('askNo')||{}).textContent||'',"
                           "panelCalls:window.__panelCalls"),
        "eval|document.getElementById('askNo').click()",
        "poll|document.getElementById('modal').hidden",
        report("cancelled") % ("stillThere:!!document.querySelector('[data-del-expense=\"%d\"]')" % drop["id"]),
        "eval|document.querySelector('[data-del-expense=\"%d\"]').click()" % drop["id"],
        "poll|!!document.getElementById('askYes')",
        "eval|document.getElementById('askYes').click()",
        # The box closes the moment the counter answers, before the book has heard about it — so
        # waiting on the modal would read the row while the delete is still in flight.
        "poll|document.getElementById('modal').hidden &&"
        "!document.querySelector('[data-del-expense=\"%d\"]')" % drop["id"],
        report("removed") % ("gone:!document.querySelector('[data-del-expense=\"%d\"]')," % drop["id"] +
                             "kept:!!document.querySelector('[data-del-expense=\"%d\"]')," % keep["id"] +
                             "toast:(document.querySelector('.toast')||{}).textContent||'',"
                             "panelCalls:window.__panelCalls"),
        "eval|var b=document.querySelector('[data-edit-expense=\"%d\"]');" % keep["id"] +
        "if(!b) throw new Error('no Edit button for the row that must be editable');b.click()",
        "poll|!!document.getElementById('expenseForm')",
        report("opened") % ("editing:document.getElementById('expenseForm').dataset.expenseId,"
                            "wasAt:document.getElementById('expenseForm').amount.value,"
                            "note:document.getElementById('expenseForm').note.value,"
                            "payee:document.getElementById('expenseForm').payee.value"),
        "eval|var f=document.getElementById('expenseForm');f.amount.value='210.50';"
        "f.note.value='INK, re-priced'",
        "eval|document.querySelector('#expenseForm .modal-foot .btn.primary').click()",
        "poll|document.getElementById('modal').hidden &&"
        "document.querySelector('#view').innerText.indexOf('210.50') > -1",
        report("saved") % ("rows:[].map.call(document.querySelectorAll('#view tbody tr'),function(r){"
                           "return r.innerText.replace(/\\s+/g,' ').slice(0,150)})"),
    ]
    out = subprocess.run([probe, BASE, work] + cmds, capture_output=True, text=True,
                         timeout=900, env=dict(env, WKPROBE_SCRIPT=poison, WKPROBE_FRESH_DATA="1"))
    for line in (out.stdout or "").splitlines():
        print("   " + line.strip()[:130])
    found = {}
    for line in (out.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                got = json.loads(line)
                found[got.get("step")] = got
            except ValueError:
                pass
    if (out.stdout or "").count("TIMEOUT"):
        for label in ("asked", "cancelled", "removed", "opened", "saved"):
            if label not in found:
                fails.append("the page never reached the %s step" % label)
                print("%-58s FAIL   the view stopped before it" % ("the page reached the %s step" % label))

    rows = found.get("rows") or {}
    check("the book's rows are on the screen, and the spoilage entry points at its own screen",
          rows.get("rows") == 3 and rows.get("spoilageLink") is True, rows)
    asked = found.get("asked") or {}
    check("the Remove press asks its question on the page itself",
          "sure" in (asked.get("title") or "").lower() and "50" in (asked.get("body") or "") and
          (asked.get("yes") or "").lower().startswith("remove"), asked)
    check("…and it never waited for a panel this machine cannot show",
          asked.get("panelCalls") == 0, asked.get("panelCalls"))
    check("answering Cancel leaves the entry in the book",
          (found.get("cancelled") or {}).get("stillThere") is True, found.get("cancelled"))
    removed = found.get("removed") or {}
    check("answering Remove takes that one entry out, and says so",
          removed.get("gone") is True and removed.get("kept") is True and
          "removed" in (removed.get("toast") or "").lower() and removed.get("panelCalls") == 0, removed)
    opened = found.get("opened") or {}
    check("Edit opens the entry filled with what the book holds",
          opened.get("editing") == str(keep["id"]) and opened.get("wasAt") == "50.00" and
          opened.get("note") == "TWICE" and opened.get("payee") == "BOSS", opened)
    saved = found.get("saved") or {}
    check("a figure typed over the old one is what the row reads afterwards",
          any("210.50" in r for r in (saved.get("rows") or [])), (saved.get("rows") or [""])[0])

    # --------------------------------------------------------------------------- what the shells say
    print("\n== the shells a counter's window is built from")
    shell = text(PORTABLE_M)
    check("the hand-out window shell takes the page's dialogs",
          "WKUIDelegate" in shell and "_web.UIDelegate = self;" in shell, "")
    check("…it answers confirm with a Yes and a No the counter can press",
          re.search(r"runJavaScriptConfirmPanelWithMessage.*?addButtonWithTitle:@\"Yes\".*?"
                    r"addButtonWithTitle:@\"No\".*?completionHandler\(answer == NSAlertFirstButtonReturn\)",
                    shell, re.S) is not None, "")
    check("…and never leaves WebKit waiting for a panel that cannot be shown",
          shell.count("if (!view.window) return completionHandler") >= 3,
          "%d guards" % shell.count("if (!view.window) return completionHandler"))
    app_swift = text(MAIN_SWIFT)
    check("the shop's own Mac app still answers them too",
          "runJavaScriptConfirmPanelWithMessage" in app_swift and
          "runJavaScriptAlertPanelWithMessage" in app_swift, "")

    page = text(APP_JS)
    # Comments are where the shop writes down why confirm() was taken out; only live code can still wait
    # for a panel, so the prose is stripped before the search.
    code = re.sub(r"/\*.*?\*/", "", page, flags=re.S)
    code = re.sub(r"(?m)^[ \t]*//.*$", "", code)
    waiting = [line.strip() for line in code.splitlines() if re.search(r"(?<![\w.$])confirm\(", line)]
    check("no press on the counter's screens waits for a native panel any more",
          not waiting, waiting[0] if waiting else "")
    check("…they all go through the shop's own box",
          page.count("await askYesNo(") >= 10 and "function askYesNo(" in page,
          "%d presses ask in the page" % page.count("await askYesNo("))
    check("a change saved while the shop is dark still replays its own verb",
          "method: entry.method" in page, "")

    version = text(VERSION_FILE).strip()
    parts = [int(x) for x in re.findall(r"\d+", version)]
    parts += [0] * (3 - len(parts))
    cache = re.search(r"crispprint-shell-v(\d+)", text(SW_JS))
    check("the book's version moved on with the change",
          tuple(parts[:3]) >= (1, 19, 0), "VERSION is %s" % version)
    check("and every counter's cached page is replaced for it",
          cache and int(cache.group(1)) >= 35, "cache is v%s" % (cache and cache.group(1)))

    print("\n== the shop's own book, untouched")
    same = hashlib.sha256(open(LIVE, "rb").read()).hexdigest() == live_hash
    check("its bytes are the ones this check opened it with", same,
          "%s -> %s bytes" % (live_stat.st_size, os.stat(LIVE).st_size))
finally:
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()
    shutil.rmtree(work, ignore_errors=True)

print("\n%d checks, %d failed" % (len(CHECKED), len(fails)))
if fails:
    print("failed: " + "; ".join(fails))
    sys.exit(1)
print("all checks passed")
