#!/usr/bin/env python3
"""Prove what an installed device does when the shop's own Mac stops answering.

The owner's ask (2026-10-16, 20:48): "When this machine goes off let the dashboard on the assigned
machines stay online." Two different things hide inside that sentence, and this file tells them apart
by measuring both:

  readable   — the page still opens on the phone and shows the shop's figures out of the device's
               own memory, with nothing answering on the network;
  live       — the phone reaches a book that is not on this Mac at all, which only a hosted copy
               can do.

The readable half is what can be proven here, so it is proven: a throwaway engine is started on a
port of its own, a real WebKit view warms its memory from it, and then that engine is killed — the
same thing a powered-off Mac does to the shop Wi-Fi, without the ten other things a powered-off Mac
does to this check. The view is reloaded and the screen it paints is read out.

What is watched for, in order:
  * the dashboard paints at all while nothing is answering;
  * the figures on it are the ones that device saw last, and a payment made to the book after the
    device looked does NOT appear — a stale screen that matches its own cache is correct, and a
    stale screen that quietly shows fresher numbers would prove the reading never came from cache;
  * the page says it is offline, and says when its numbers stopped being live, because a print shop
    decides on "today's takings" and has to tell a live figure from a remembered one at a glance;
  * a figure typed while the Mac is dark is kept on the device and lands in the book by itself when
    the Mac answers again, so nothing said at the counter is lost to a power cut;
  * the device carries the whole rail while the shop is answering, not only the screen somebody
    happened to open last — and a screen it never opened still paints once the Mac is off;
  * a carry asked for while nobody is answering stamps nothing, so a remembered book can never
    report itself as freshly copied on the Sync screen.

Everything is written to a copy of the book in a temp folder; the shop's own file is opened read-only
and compared byte for byte at the end. Loopback is the shop, so no password is needed or used.

    python3 tools/check-mac-offline.py
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
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE = os.path.expanduser("~/Library/Application Support/Chrisphics Hub/chrisphics.db")

# One fixed path for the probe binary: WebKit keys a page's stored data to the app that asked for
# it, so a probe rebuilt into a fresh temp folder each run would remember nothing and the whole
# scenario would read as "the device lost the book".
PROBE_PATH = "/tmp/chrisphics-dark-probe"


def free_port(start):
    """A port of our own, so a check that crashed earlier and left its server behind does not wedge
    this one. The shop's 62434 and 8834 are never asked for."""
    for port in range(start, start + 40):
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", port))
            probe.close()
            return port
        except OSError:
            probe.close()
    raise SystemExit("no free loopback port above %d" % start)


PORT = free_port(8918)
BASE = "http://127.0.0.1:%d" % PORT

work = tempfile.mkdtemp(prefix="chrisphics-dark-")
CLONE = os.path.join(work, "clone.db")
src = sqlite3.connect("file:%s?mode=ro" % LIVE, uri=True)
dst = sqlite3.connect(CLONE)
src.backup(dst)
dst.close()
src.close()

# A counter with one known order on it, so every figure the screen shows is a number this file chose.
setup = sqlite3.connect(CLONE)
for table in ("clients", "jobs", "job_items", "payments", "expenses", "leads", "spoiled_work"):
    setup.execute("DELETE FROM %s" % table)
setup.execute("UPDATE app_state SET value='0' WHERE key='shop_mac_login'")
cid = setup.execute("INSERT INTO clients (name, phone) VALUES ('Dark Room Films', '0535583460')"
                    ).lastrowid
jid = setup.execute("INSERT INTO jobs (ref, client_id, title, category, quantity, unit_price,"
                    " status) VALUES ('CH-DARK-1', ?, 'Window vinyl', 'Other', 1, 250, 'Printing')",
                    (cid,)).lastrowid
setup.commit()
setup.close()

TAKEN = 250.0    # paid while the device is watching
LATER = 90.0     # paid to the book after the device last looked
DARK = 12.0      # an expense typed at the counter while the Mac is off

live_hash = hashlib.sha256(open(LIVE, "rb").read()).hexdigest()
live_stat = os.stat(LIVE)

env = dict(os.environ, CHRISPHICS_DB=CLONE, CHRISPHICS_SUPPORT=work,
           CHRISPHICS_BACKUP_DIR=os.path.join(work, "Backups"),
           CHRISPHICS_CALLS_PER_MINUTE="4000", PYTHONUNBUFFERED="1")
log = open(os.path.join(work, "server.log"), "wb")

fails = []
counted = [0]


def check(label, ok, detail=""):
    counted[0] += 1
    print("%-58s %s   %s" % (label, "ok" if ok else "FAIL", str(detail)[:66]))
    if not ok:
        fails.append(label)
    return ok


def request(path):
    return urllib.request.urlopen(BASE + path, timeout=6)


def book(sql, args=()):
    """Change the book behind the device's back, the way a second machine at the press would."""
    con = sqlite3.connect(CLONE)
    con.execute(sql, args)
    con.commit()
    con.close()


def scalar(sql, args=()):
    con = sqlite3.connect("file:%s?mode=ro" % CLONE, uri=True)
    try:
        return con.execute(sql, args).fetchone()[0]
    finally:
        con.close()


def figures(payload):
    """The money strings on the screen, as numbers — '₵250.00' and '₵250' both read as 250."""
    out = []
    for item in (payload or {}).get("money") or []:
        try:
            out.append(float(str(item).replace("₵", "").replace(",", "").strip()))
        except ValueError:
            pass
    return out


def boot():
    p = subprocess.Popen(["/usr/bin/python3", os.path.join(REPO, "server.py"), "--port", str(PORT),
                          "--no-browser"], cwd=REPO, env=env, stdout=log, stderr=log)
    for _ in range(80):
        try:
            with request("/healthz") as res:
                if json.loads(res.read().decode())["ok"]:
                    return p
        except Exception:
            time.sleep(0.15)
    raise SystemExit("the test shop never answered; see " + os.path.join(work, "server.log"))


def hush(proc):
    """The Mac going off, as far as this check can stage it: nothing on that address answers again."""
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    for _ in range(60):
        probe = socket.socket()
        probe.settimeout(0.2)
        try:
            probe.connect(("127.0.0.1", PORT))
            probe.close()
            time.sleep(0.15)
        except OSError:
            return True
    return False


def view(cmds, fresh=False, tag=""):
    """One real WebKit load, in its own process, the way a phone opens the installed dashboard."""
    env_run = dict(env, WKPROBE_FRESH_DATA="1") if fresh else dict(env)
    out = subprocess.run([PROBE_PATH, BASE, work] + cmds, capture_output=True, text=True,
                         timeout=600, env=env_run)
    lines = (out.stdout or "").splitlines()
    said = [l.strip() for l in lines if l.strip().startswith("{")]
    if not said:
        print((out.stdout or "")[-1000:])
        print((out.stderr or "")[-500:])
        fails.append("the %s view returned nothing" % tag)
        return {}
    try:
        return json.loads(said[-1])
    except ValueError:
        fails.append("the %s view did not answer in JSON" % tag)
        return {}


# The page reads its own screen out, so every claim below is a number a person would have seen.
READ = ("JSON.stringify({ready: !!document.querySelector('[data-action=\"new-job\"]'),"
        " status: (document.getElementById('networkStatus')||{}).textContent || \"\","
        " money: (document.body.innerText.match(/[\\u20B5][\\d,]+\\.?\\d*/g)||[]).slice(0,8),"
        " walk: window.__W || '', keys: window.__K || [],"
        " carry: (S.offlineCarried||{}).at || 0, screens: (S.offlineCarried||{}).screens || 0,"
        " words: document.body.innerText.replace(/\\s+/g,' ').slice(0,900)})")

built = subprocess.run(["swiftc", "-O", os.path.join(REPO, "tools/wk-probe.swift"),
                        "-o", PROBE_PATH], capture_output=True, text=True, timeout=600)
check("the rendering engine probe is in place", os.path.exists(PROBE_PATH),
      (built.stderr or "")[-160:])
if not os.path.exists(PROBE_PATH):
    raise SystemExit("nothing was rendered")

book("INSERT INTO payments (client_id, job_id, amount, kind, method, note)"
     " VALUES (?,?,?, 'Payment', 'Cash', 'paid while watched')", (cid, jid, TAKEN))

proc = boot()
try:
    # ── 1. the device meets the shop, and remembers what it saw ─────────────────────────────────
    warm = view(["open|/#/",
                 "poll|!!document.querySelector('[data-action=\"new-job\"]')",
                 "poll|(document.getElementById('networkStatus')||{}).textContent"
                 ".indexOf('onnect')>=0",
                 "say|" + READ], fresh=True, tag="warming")
    check("the device opened the shop's dashboard", warm.get("ready") is True, warm.get("status"))
    check("and it was the live book it read", "Offline" not in (warm.get("status") or ""),
          warm.get("status"))
    check("the figure it was shown is the one this check paid into the book",
          TAKEN in figures(warm), warm.get("money"))

    # ── 2. the device is asked to carry the whole book, not just this screen ─────────────────────
    carried = view(["open|/#/",
                    "poll|!!document.querySelector('[data-action=\"new-job\"]')",
                    "eval|window.__W='pending'; warmOfflineBook().then(function(){window.__W='done';},"
                    " function(){window.__W='threw';});",
                    "poll|window.__W !== 'pending'",
                    "eval|(async function(){ const rows = await offlineAll('responses');"
                    " window.__K = rows.map(function(r){ return r.key; }); })();",
                    "poll|Array.isArray(window.__K)",
                    "say|" + READ], tag="carrying")
    check("while the shop answered, the device copied every screen it can reach",
          carried.get("ready") is True and carried.get("walk") == "done", carried.get("walk"))
    held = carried.get("keys") or []
    want = ["/api/bootstrap", "/api/session", "/api/dashboard", "/api/clients",
            "/api/handover", "/api/expenses", "/api/accounts", "/api/spoiled", "/api/shop",
            "/api/report?"]
    missing = [path for path in want if not any(h.startswith(path) for h in held)]
    check("and what it holds is the whole rail, not the one screen it was showing",
          not missing and len(held) >= 12, "%d held, missing %s" % (len(held), missing))
    # The Sync screen is the only place that tells the owner how much of the shop a device actually
    # carries, so the walk has to report it and stamp the hour it happened.
    check("the device counts the screens it carried and stamps the hour it did it",
          carried.get("carry") != 0 and carried.get("screens") >= len(want),
          "%s screens at %s" % (carried.get("screens"), carried.get("carry")))

    # ── 3. the book moves while the device is not looking ───────────────────────────────────────
    book("INSERT INTO payments (client_id, job_id, amount, kind, method, note)"
         " VALUES (?,?,?, 'Payment', 'Cash', 'after the device looked')", (cid, jid, LATER))
    check("the book now holds more than the device remembers",
          scalar("SELECT COALESCE(SUM(amount),0) FROM payments") == TAKEN + LATER,
          scalar("SELECT COALESCE(SUM(amount),0) FROM payments"))

    # ── 4. the Mac goes off ──────────────────────────────────────────────────────────────────────
    check("the shop's address stopped answering", hush(proc))
    dark = view(["open|/#/",
                 "poll|!!document.querySelector('[data-action=\"new-job\"]')",
                 "say|" + READ], tag="dark")
    check("with nothing answering, the dashboard still paints", dark.get("ready") is True,
          str(dark.get("words") or "")[:80])
    check("and the page knows it is not reaching the shop",
          "Offline" in (dark.get("status") or ""), dark.get("status"))
    shown = figures(dark)
    check("the money on it is what this device saw last", TAKEN in shown, shown)
    check("nothing fresher leaked in — the payment made after it looked is not on the screen",
          LATER not in shown, shown)
    age = (dark.get("status") or "")
    # Not the word "offline" — a clock. A remembered figure worth deciding on has to say when it
    # stopped being live, and "as of" with no time behind it is the same guess in nicer words.
    check("the dark screen says when its numbers stopped being live",
          "as of" in age.lower() and bool(re.search(r"\d{1,2}:\d{2}", age)), age[:90])

    # The whole point of the copy: a screen nobody on this device ever opened still answers, because
    # it was carried while the shop was up. Reports is the one that asks for it hardest — its address
    # carries the date, so the copy has to be found by screen, not by string.
    blind = view(["open|#/reports",
                  "poll|(document.body.innerText||'').indexOf('Reports')>=0",
                  "say|" + READ], tag="a screen never opened")
    shown2 = figures(blind)
    check("a screen this device never opened still paints with the Mac off",
          blind.get("ready") is True and "Reports" in str(blind.get("words") or ""),
          str(blind.get("words") or "")[:70])
    check("and it is carried figures on it, with the payment made after the carry still missing",
          TAKEN in shown2 and LATER not in shown2, shown2)

    # ── 5. a carry asked for while nobody is answering must not be mistaken for a fresh one ──────
    darkwalk = view(["open|/#/",
                     "poll|!!document.querySelector('[data-action=\"new-job\"]')",
                     "eval|window.__W='pending'; warmOfflineBook().then(function(){window.__W='done';},"
                     " function(){window.__W='threw';});",
                     "poll|window.__W !== 'pending'",
                     "say|" + READ], tag="carrying in the dark")
    check("the walk with nobody home finishes instead of hanging on a dead address",
          darkwalk.get("walk") == "done", darkwalk.get("walk"))
    # Nothing was actually read from the shop, so the device must not claim a carry it did not do:
    # a remembered book stamped as if it were fresh is how a stale figure gets decided on twice.
    check("and it claims no fresh carry over the copy it was only re-reading",
          darkwalk.get("carry") == 0 and darkwalk.get("screens") == 0,
          "%s screens at %s" % (darkwalk.get("screens"), darkwalk.get("carry")))

    # ── 6. the counter keeps working through the outage ──────────────────────────────────────────
    wrote = view(["open|/#/",
                  "poll|!!document.querySelector('[data-action=\"new-job\"]')",
                  "eval|window.__D='pending'; api('/api/expenses', {method:'POST', body:"
                  " JSON.stringify({amount: %s, category: 'Other', note: 'typed in the dark'})})"
                  ".then(function(){window.__D='kept';}, function(e){window.__D='threw:'+(e&&e.message);});"
                  % DARK,
                  "poll|window.__D !== 'pending'",
                  "poll|(document.getElementById('networkStatus')||{}).textContent"
                  ".indexOf('waiting')>=0",
                  "say|" + READ], tag="writing in the dark")
    check("a figure typed with the Mac off was accepted at the counter",
          wrote.get("ready") is True and "waiting" in (wrote.get("status") or ""),
          wrote.get("status"))
    check("and nothing reached the book while it was cut off",
          scalar("SELECT count(*) FROM expenses") == 0, scalar("SELECT count(*) FROM expenses"))

    # ── 7. the Mac comes back ───────────────────────────────────────────────────────────────────
    proc = boot()
    back = {}
    for _ in range(20):
        back = view(["open|/#/",
                     "poll|!!document.querySelector('[data-action=\"new-job\"]')",
                     "poll|(document.getElementById('networkStatus')||{}).textContent"
                     ".indexOf('waiting')<0",
                     "say|" + READ], tag="recovered")
        if back.get("ready") and scalar("SELECT count(*) FROM expenses") >= 1:
            break
        time.sleep(1.0)
    check("the change kept in the dark reached the book once the shop answered again",
          scalar("SELECT count(*) FROM expenses WHERE note='typed in the dark'") == 1,
          scalar("SELECT count(*) FROM expenses"))
    check("the screen went back to the live book on its own",
          "Offline" not in (back.get("status") or "") and "waiting" not in (back.get("status") or ""),
          back.get("status"))
    # The dashboard totals money rather than listing every payment, so the proof that the outage
    # payment is now on the screen is the combined takings appearing — a lone ₵90 was never a figure
    # this screen would draw.
    check("and the payment made during the outage is counted on it", TAKEN + LATER in figures(back),
          back.get("money"))
finally:
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass

after_stat = os.stat(LIVE)
check("the shop's own book was never opened for writing",
      after_stat.st_size == live_stat.st_size
      and hashlib.sha256(open(LIVE, "rb").read()).hexdigest() == live_hash,
      "%d bytes" % after_stat.st_size)
print("\n%d checks, %d failed" % (counted[0], len(fails)))
for f in fails:
    print(" ", f)
shutil.rmtree(work, ignore_errors=True)
sys.exit(1 if fails else 0)
