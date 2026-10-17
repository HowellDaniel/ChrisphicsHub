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
  * and — because a queue nobody can look at is not a book — that same figure is on the counter's own
    screens the rest of the day, priced the way the shop will price it, wearing a mark that says the
    shop has not seen it yet; while a screen the device did not write to still shows only what the
    shop sent, and the remembered copy is never rewritten by the showing;
  * the device carries the whole rail while the shop is answering, not only the screen somebody
    happened to open last — and a screen it never opened still paints once the Mac is off;
  * a carry asked for while nobody is answering stamps nothing, so a remembered book can never
    report itself as freshly copied on the Sync screen;
  * the two buttons a cashier presses do what they say on the tin — one writes a checked copy into
    the shop's own folder, the other fills this machine's own memory on the spot instead of leaving
    it to wait on a twenty-minute clock.

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
# The live book mirrors every copy into the shop's own iCloud folder. A throwaway book that pressed a
# copy button would file a fake ledger in there among the real ones, so the second destinations are
# taken out of the clone: every copy this check asks for stays inside its own temp folder.
setup.execute("DELETE FROM app_state WHERE key IN ('backup_mirror', 'backup_mail',"
              " 'last_backup_mail', 'last_backup_offsite')")
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
# read() builds that one expression; extra named JS expressions are appended so a single WebKit load
# can answer several questions about the same painted screen.
def read(*extra):
    body = [
        "ready: !!document.querySelector('[data-action=\"new-job\"]')",
        "status: (document.getElementById('networkStatus')||{}).textContent || \"\"",
        "money: (document.body.innerText.match(/[\\u20B5][\\d,]+\\.?\\d*/g)||[]).slice(0,8)",
        "walk: window.__W || ''", "keys: window.__K || []",
        "carry: (S.offlineCarried||{}).at || 0", "screens: (S.offlineCarried||{}).screens || 0",
        # The dashboard and accounts figures by their own card labels, so a claim about today's
        # takings quotes the number under that name rather than a ₵ string found somewhere on the page.
        "kpi: [].slice.call(document.querySelectorAll('.stat')).reduce(function(a, s) {"
        " a[(s.querySelector('.k')||{}).textContent] = (s.querySelector('.v')||{}).textContent;"
        " return a; }, {})",
        "words: document.body.innerText.replace(/\\s+/g,' ').slice(0,900)",
    ]
    return "say|JSON.stringify({" + ",".join(body + list(extra)) + "})"


def row_of(text, name="row"):
    """The first table row whose own text mentions TEXT, read as the screen shows it — the mark and
    every money cell on that one line, with nothing from a neighbouring row mixed in."""
    return ("%s: (function(){ var r = [].slice.call(document.querySelectorAll('tbody tr'))"
            ".filter(function(x){ return (x.innerText || '').indexOf('%s') >= 0; })[0];"
            " return r ? r.innerText.replace(/\\s+/g, ' ') : 'no such row on this screen'; })()"
            % (name, text.replace("'", "\\'")))


def shows(text, name="shows"):
    """Whether the words appear anywhere on the painted page — absence is the honest form of a
    negative check, and the 900-character preview is too short to prove one."""
    return ("%s: (document.body.innerText || '').indexOf('%s') >= 0"
            % (name, text.replace("'", "\\'")))



def amount(shown):
    """'₵ 350.00' as 350.0 — and None when the card did not draw a figure at all."""
    try:
        return round(float(str(shown).replace("₵", "").replace(",", "").strip()), 2)
    except (TypeError, ValueError):
        return None


def cash(value):
    """The page's own money shape — symbol, a space, two decimals — so a test of a figure drawn on
    screen asks for the string the screen actually paints."""
    return "₵ %s" % format(float(value), ",.2f")



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
                 # Read the screen only once it has actually drawn a figure. The rail paints in
                 # stages after boot resolves, so a read taken between them shows a dashboard with
                 # no money on it at all — which this file would otherwise report as the device
                 # having forgotten the shop's figures.
                 "poll|(document.body.innerText||'').indexOf('\\u20B5')>=0",
                 read()], fresh=True, tag="warming")
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
                    read()], tag="carrying")
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

    # A walk the code calls is not what happens at the counter. The cashier presses a button on the
    # Pending sync screen, and that press has to take the copy on the spot rather than leave the
    # machine waiting on the twenty-minute clock for one more hour of shop answers.
    pressed = view(["open|/#/sync",
                    "poll|!!document.querySelector('[data-action=\"carry-now\"]')",
                    "eval|S.offlineCarried = null;"
                    " document.querySelector('[data-action=\"carry-now\"]').click();",
                    "poll|(S.offlineCarried||{}).screens >= 12",
                    read()], tag="pressing the copy button")
    check("pressing the copy button takes the copy without waiting on the clock",
          pressed.get("screens", 0) >= len(want) and pressed.get("carry", 0) != 0,
          "%s screens at %s" % (pressed.get("screens"), pressed.get("carry")))
    check("and the button is on the screen a cashier already opens to see what is waiting",
          "Pending sync" in str(pressed.get("words") or ""), str(pressed.get("words") or "")[:60])

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
                 # A load starts believing the shop is there and corrects itself once the knock comes
                 # back empty. Reading the screen in between is how a dark dashboard gets reported as
                 # "still claiming to be connected, with no figures drawn at all".
                 "poll|(document.getElementById('networkStatus')||{}).textContent"
                 ".indexOf('Connected')<0",
                 "poll|(document.body.innerText||'').indexOf('\\u20B5')>=0",
                 read()], tag="dark")
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
                  # The word "Reports" is in the navigation on every screen, so waiting for it proves
                  # nothing about this one having rendered. Wait for its own cards and a drawn figure.
                  "poll|(document.body.innerText||'').indexOf('Billed')>=0",
                  "poll|(document.body.innerText||'').indexOf('\\u20B5')>=0",
                  read()], tag="a screen never opened")
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
                     read()], tag="carrying in the dark")
    check("the walk with nobody home finishes instead of hanging on a dead address",
          darkwalk.get("walk") == "done", darkwalk.get("walk"))
    # Nothing was actually read from the shop, so the device must not claim a carry it did not do:
    # a remembered book stamped as if it were fresh is how a stale figure gets decided on twice.
    check("and it claims no fresh carry over the copy it was only re-reading",
          darkwalk.get("carry") == 0 and darkwalk.get("screens") == 0,
          "%s screens at %s" % (darkwalk.get("screens"), darkwalk.get("carry")))

    # The copy is only worth pressing if the counter can see that it is there. The screen reads this
    # machine's own memory rather than this page's, so a reload with the Mac off still says how much
    # of the shop the device holds — and pressing the button with nobody home still stamps nothing.
    darksync = view(["open|/#/sync",
                     "poll|(document.body.innerText||'').indexOf('holds its own copy')>=0",
                     "eval|S.offlineCarried = null;"
                     " var b = document.querySelector('[data-action=\"carry-now\"]');"
                     " if (b) b.click();",
                     "eval|window.__W='wait'; setTimeout(function(){window.__W='ready';}, 3000);",
                     "poll|window.__W === 'ready'",
                     read()], tag="the copy button in the dark")
    held_words = str(darksync.get("words") or "")
    check("with the Mac off, the sync screen still says what this machine holds",
          "holds its own copy" in held_words, held_words[:70])
    check("and pressing the copy button with nobody home stamps no fresh carry",
          darksync.get("carry") == 0 and darksync.get("screens") == 0,
          "%s screens at %s" % (darksync.get("screens"), darksync.get("carry")))

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
                  read()], tag="writing in the dark")
    check("a figure typed with the Mac off was accepted at the counter",
          wrote.get("ready") is True and "waiting" in (wrote.get("status") or ""),
          wrote.get("status"))
    check("and nothing reached the book while it was cut off",
          scalar("SELECT count(*) FROM expenses") == 0, scalar("SELECT count(*) FROM expenses"))

    # ── 6b. the dark counter can see what it has itself written ──────────────────────────────────
    # A queue nobody can look at is not a book. The ask is a shop that keeps trading with the Mac off
    # and no cable anywhere in it, and a cashier who takes money and then cannot find it on any screen
    # has not traded, whatever the machine says about the change being safely kept. So each remembered
    # screen is folded together with this device's own pending writes — folded at read time, line by
    # line, with every borrowed line saying where it came from, and only into lists this device can
    # honestly judge. The ₵12 expense queued above is folded in too, so the day's spending moves as
    # well as its takings.
    DARK_TITLE = "Funeral programmes"
    DARK_QTY, DARK_RATE, DARKPAY = 20, 9.0, 100.0
    DARK_TOTAL = round(DARK_QTY * DARK_RATE, 2)              # ₵180.00, priced the way the shop prices
    DARK_BALANCE = round(DARK_TOTAL - DARKPAY, 2)            # ₵80.00 still owed once the deposit is in

    dark_write = view(["open|/#/",
                       "poll|!!document.querySelector('[data-action=\"new-job\"]')",
                       "eval|window.__D='pending'; (async function(){ try {"
                       " var c = await api('/api/clients'); var t = await api('/api/jobs', {method:'POST',"
                       " body: JSON.stringify({title: '" + DARK_TITLE + "', category: 'Other',"
                       " quantity: " + str(DARK_QTY) + ", unit_price: " + repr(DARK_RATE) + ","
                       " status: 'Pending', client_id: c[0].id})});"
                       " await api('/api/payments', {method:'POST', body: JSON.stringify({"
                       " job_id: t.id, amount: " + repr(DARKPAY) + ", kind: 'Deposit', method: 'Cash'})});"
                       " window.__D = 'queued:' + t.id;"
                       " } catch (e) { window.__D = 'threw:' + (e && e.message); } })();",
                       "poll|window.__D !== 'pending'",
                       read("wrote: window.__D || ''")], tag="booking an order in the dark")
    check("an order taken with the Mac off was put on this device's own queue",
          str(dark_write.get("wrote") or "").startswith("queued:"), dark_write.get("wrote"))
    check("and the shop's book still holds nothing of it",
          scalar("SELECT count(*) FROM jobs WHERE title=?", (DARK_TITLE,)) == 0
          and scalar("SELECT count(*) FROM payments") == 2,
          "%d jobs, %d payments in the book" % (scalar("SELECT count(*) FROM jobs WHERE title=?",
                                                      (DARK_TITLE,)),
                                                scalar("SELECT count(*) FROM payments")))

    dark_jobs = view(["open|#/jobs",
                      "poll|(document.getElementById('networkStatus')||{}).textContent"
                      ".indexOf('Connect')<0",
                      "poll|(document.body.innerText||'').indexOf('\\u20B5')>=0",
                      "eval|window.__G=[listIsPaintable(new URLSearchParams('status=open&kind=Job')),"
                      " listIsPaintable(new URLSearchParams('status=open&kind=Job&q=vinyl')),"
                      " listIsPaintable(new URLSearchParams('status=open&kind=Job&sort=due')),"
                      " listIsPaintable(new URLSearchParams('status=open&kind=Job&paid=due'))];",
                      "poll|Array.isArray(window.__G)",
                      read(row_of(DARK_TITLE), shows(DARK_TITLE),
                           "guard: window.__G")], tag="the jobs screen with the Mac off")
    line = str(dark_jobs.get("row") or "")
    check("the order booked in the dark is on the jobs screen",
          dark_jobs.get("shows") is True and "no such row" not in line, line[:70])
    check("and the line says the shop has not seen it yet", "Held on this device" in line, line[:70])
    check("priced as the shop will price it — %d at %s" % (DARK_QTY, cash(DARK_RATE)),
          cash(DARK_TOTAL) in line, line)
    check("with the money handed over in the dark against it",
          cash(DARKPAY) in line and cash(DARK_BALANCE) in line, line)
    # A search, a sort or a money filter is the shop's own SQL over records that do not exist there
    # yet, so a queued line is never dropped into a list this device cannot judge for it.
    check("and only an unfiltered list is open to a line from this machine",
          dark_jobs.get("guard") == [True, False, False, False], dark_jobs.get("guard"))

    dark_dash = view(["open|/#/",
                      "poll|(document.getElementById('networkStatus')||{}).textContent"
                      ".indexOf('Connect')<0",
                      "poll|(document.body.innerText||'').indexOf('\\u20B5')>=0",
                      read(shows(DARK_TITLE), shows("Held on this device", "mark"))],
                     tag="the dashboard with the Mac off")
    was = amount((dark.get("kpi") or {}).get("Collected today"))
    now = amount((dark_dash.get("kpi") or {}).get("Collected today"))
    check("today's takings moved by exactly the money this device took",
          was is not None and now == round(was + DARKPAY, 2), "%s → %s" % (was, now))
    check("this month's spending by exactly the figure typed at the dark counter",
          amount((dark_dash.get("kpi") or {}).get("Spent this month")) == DARK,
          (dark_dash.get("kpi") or {}).get("Spent this month"))
    check("and the shop is shown as owed the balance of the order booked in the dark",
          amount((dark_dash.get("kpi") or {}).get("Owed to the shop")) == DARK_BALANCE,
          (dark_dash.get("kpi") or {}).get("Owed to the shop"))
    check("the order also shows on the dashboard itself, marked as this device's own",
          dark_dash.get("shows") is True and dark_dash.get("mark") is True,
          str(dark_dash.get("words") or "")[:70])

    dark_accounts = view(["open|#/accounts",
                          "poll|(document.getElementById('networkStatus')||{}).textContent"
                          ".indexOf('Connect')<0",
                          "poll|(document.body.innerText||'').indexOf('Payment ledger')>=0",
                          read(row_of("Deposit", "payrow"), shows(DARK_TITLE))],
                         tag="the accounts book with the Mac off")
    accounts_kpi = dark_accounts.get("kpi") or {}
    check("the accounts book counts the deposit taken in the dark",
          amount(accounts_kpi.get("Received (this filter)")) == round(TAKEN + DARKPAY, 2),
          accounts_kpi.get("Received (this filter)"))
    check("and its ledger carries the money as this device's own, against the job it was taken for",
          "Held on this device" in str(dark_accounts.get("payrow") or "")
          and cash(DARKPAY) in str(dark_accounts.get("payrow") or "")
          and dark_accounts.get("shows") is True,
          str(dark_accounts.get("payrow") or "")[:80])

    # A screen this device has nothing to add to must stay exactly as the shop sent it: the fold is
    # one machine's own writes, not a licence to invent rows. Quotes and Ready are the two tabs the
    # warm walk carries, and the order booked in the dark is neither.
    for tab, label in (("?kind=Quote", "a quote list"), ("?status=Ready", "a ready-for-pickup list")):
        offtab = view(["open|#/jobs" + tab,
                       "poll|(document.getElementById('networkStatus')||{}).textContent"
                       ".indexOf('Connect')<0",
                       read(shows(DARK_TITLE))], tag=label + " with the Mac off")
        check("%s is not given the device's job because it does not belong there" % label,
              offtab.get("shows") is False, "%s on %s" % (offtab.get("shows"), tab))

    # The fold happens as the screen is read. The copy the shop itself sent stays byte for byte what
    # the shop sent, or the next outage would be guessing which figures were ever the shop's.
    dark_raw = view(["open|/#/",
                     "poll|!!document.querySelector('[data-action=\"new-job\"]')",
                     "eval|window.__K='wait'; (async function(){ var rows = await offlineAll('responses');"
                     " window.__K = rows.filter(function(r){ return String(r.key).indexOf('/api/jobs')===0;"
                     " }).map(function(r){ return JSON.stringify(r.value).indexOf('" + DARK_TITLE +
                     "') >= 0; }); })();",
                     "poll|Array.isArray(window.__K)", read()], tag="the copy the shop sent")
    check("and the showing never rewrote the copy the shop itself sent",
          bool(dark_raw.get("keys")) and not any(dark_raw.get("keys")), dark_raw.get("keys"))

    # A cashier who books an order in the dark will want to open it: read the lines back, take more
    # money, move the due date. The shop's copy has no page for an order that only exists on this
    # machine, so the sheet is built here — the same figures the list shows, and the money counted
    # once, not once on the list and once again on the sheet.
    drawer = view(["open|#/jobs",
                   "poll|(document.body.innerText||'').indexOf('" + DARK_TITLE + "')>=0",
                   "eval|window.__E=[]; window.addEventListener('error', function(e){ window.__E"
                   ".push('err:' + (e.message||'?')); });"
                   " window.addEventListener('unhandledrejection', function(e){ window.__E"
                   ".push('rej:' + String(e && e.reason)); });"
                   " var r=[].slice.call(document.querySelectorAll('tbody tr')).filter("
                   "function(x){ return (x.innerText||'').indexOf('" + DARK_TITLE + "')>=0; })[0];"
                   " window.__R = r ? (r.getAttribute('data-open')||'row without data-open') : 'no such row';"
                   " if (r) r.click();",
                   "eval|window.__O='wait'; setTimeout(function(){ var d = document.getElementById('drawer');"
                   # Ten seconds, not one: with nobody home, opening a sheet first has to wait out
                   # the bounded knock (SHOP_WAIT_MS) before the page falls back to this machine's
                   # own copy. A cashier sees the sheet a few seconds late, not never.
                   " window.__O = (d && !d.hidden) ? (d.innerText || '').replace(/\\s+/g, ' ') : 'no drawer'; }, 10000);",
                   "poll|window.__O !== 'wait'",
                   read("sheet: window.__O || ''",
                        "diag: [window.__R, location.hash,"
                        " (document.getElementById('drawer')||{hidden:'no #drawer'}).hidden,"
                        " [].slice.call(document.querySelectorAll('#toasts .toast')).map(function(t){ return t.textContent; }),"
                        " window.__E].join(' | ')")],
                  tag="opening the order booked in the dark")
    sheet = str(drawer.get("sheet") or "")
    print("      the device's own sheet: %s" % (drawer.get("diag") or "nothing"))
    check("the order booked in the dark opens as a job sheet on this device",
          DARK_TITLE in sheet and "Held on this device" in sheet,
          "%s · %s" % (sheet[:70], drawer.get("diag")))
    check("priced once, with the money against it counted once",
          cash(DARK_TOTAL) in sheet and cash(DARKPAY) in sheet
          and cash(round(DARK_TOTAL + DARKPAY, 2)) not in sheet
          and cash(DARK_BALANCE) in sheet, "%s · %s" % (sheet[:90], drawer.get("diag")))

    # ── 7. the Mac comes back ───────────────────────────────────────────────────────────────────
    proc = boot()
    back = {}
    for _ in range(20):
        back = view(["open|/#/",
                     "poll|!!document.querySelector('[data-action=\"new-job\"]')",
                     "poll|(document.getElementById('networkStatus')||{}).textContent"
                     ".indexOf('waiting')<0",
                     read()], tag="recovered")
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
    # this screen would draw. The ₵100 this device took in the dark was replayed in the same pass, so
    # the day's figure is the shop's two payments plus it.
    check("and the payment made during the outage is counted on it",
          TAKEN + LATER + DARKPAY in figures(back), back.get("money"))

    # And the folded line has to hand over to the shop's own row without leaving both behind. Once the
    # queue is empty there is nothing to fold, so the mark goes and the order appears exactly once —
    # the counter must not see its own booking twice, or the day's takings read double.
    check("the order booked in the dark is now in the shop's own book",
          scalar("SELECT count(*) FROM jobs WHERE title=?", (DARK_TITLE,)) == 1,
          "%d rows named %s" % (scalar("SELECT count(*) FROM jobs WHERE title=?", (DARK_TITLE,)),
                                DARK_TITLE))
    merged = view(["open|#/jobs",
                   "poll|!!document.querySelector('[data-action=\"new-job\"]')",
                   "poll|(document.getElementById('networkStatus')||{}).textContent"
                   ".indexOf('Connect')>=0",
                   "poll|(document.body.innerText||'').indexOf('\\u20B5')>=0",
                   read(row_of(DARK_TITLE),
                        "times: (document.body.innerText || '').split('" + DARK_TITLE +
                        "').length - 1",
                        "mark: (document.body.innerText || '').indexOf('Held on this device') >= 0")],
                  tag="the jobs screen once the shop answers")
    check("it shows once on the jobs screen — the device's line and the shop's row are not both there",
          merged.get("times") == 1, merged.get("times"))
    check("and the mark is off it now that the shop has seen the order",
          merged.get("mark") is False, str(merged.get("row") or "")[:70])
    check("the money taken in the dark is on the same line, as the shop's own",
          cash(DARK_TOTAL) in str(merged.get("row") or "")
          and cash(DARKPAY) in str(merged.get("row") or ""),
          str(merged.get("row") or "")[:90])

    # ---------------------------------------------------------------- the cashier's two own presses
    # "Copy it now" is the manual backup the counter asks for: a checked copy on the shop's own disk
    # — and whatever second destination, mailbox or off-site repository the shop settled — rather
    # than a screen that merely claims it copied. Pressed from a machine on the Wi-Fi, not from the
    # Mac's own loopback, because that is where a cashier sits.
    # The card may already name copies taken before this device ever looked at the shop — the clone
    # starts life as a copy of the live book, history and all — so the wait is for a name that was
    # not on the card a moment ago, and the note has to have changed to say so.
    NAMES = "(document.body.innerText.match(/manual-\\d{8}-\\d{6}\\.db/g)||[])"
    asked_before = scalar("SELECT count(*) FROM security_log WHERE kind='book_copied'")
    note_before = scalar("SELECT value FROM app_state WHERE key='last_backup_note'")
    copied = view(["open|#/shop",
                   "poll|!!document.querySelector('[data-action=\"book-copy\"]')",
                   "eval|window.__N0=" + NAMES + ".slice();",
                   "poll|Array.isArray(window.__N0)",
                   "eval|document.querySelector('[data-action=\"book-copy\"]').click()",
                   "poll|" + NAMES + ".filter(function(n){ return window.__N0.indexOf(n) < 0; }).length > 0",
                   "say|JSON.stringify({fresh: " + NAMES +
                   ".filter(function(n){ return window.__N0.indexOf(n) < 0; }).slice(0,3),"
                   " already: window.__N0.slice(0,3)})"],
                  tag="pressing copy it now")
    folder = os.path.join(work, "Backups")
    # The stamped copy file itself, and nothing else that happens to be in the folder: a SQLite
    # journal left lying there for a moment is not a copy of the book, and the shop's own listing
    # says so with the same anchored name (STAMPED_BACKUP in server.py).
    by_hand = sorted(n for n in os.listdir(folder)
                     if re.match(r"^manual-\d{8}-\d{6}\.db$", n)) if os.path.isdir(folder) else []
    on_card = copied.get("fresh") or []
    check("pressing the button wrote a copy asked for by hand into the shop's folder",
          bool(on_card) and bool(by_hand) and on_card[0] in by_hand,
          "%s · %d kB · card's new name %s · %d already listed" % (
              by_hand[-1] if by_hand else "nothing",
              os.path.getsize(os.path.join(folder, by_hand[-1])) // 1024 if by_hand else 0,
              on_card[:1], len(copied.get("already") or [])))
    note_now = scalar("SELECT value FROM app_state WHERE key='last_backup_note'")
    check("and that copy was opened and counted, not just written",
          "checked" in str(note_now or "") and str(note_now) != str(note_before),
          note_now)
    check("and the book wrote down who asked for it",
          scalar("SELECT count(*) FROM security_log WHERE kind='book_copied'") > asked_before,
          "%d asked for, %d before the press" % (
              scalar("SELECT count(*) FROM security_log WHERE kind='book_copied'"), asked_before))
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
