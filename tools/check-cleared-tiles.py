#!/usr/bin/env python3
"""The owner's word, 2 November 2026: "Clear the 'Collected Friday' and 'Week to Friday' figures."
then "Clear the day card figures too."

He cleared them off the SCREEN, not out of the book. Every payment the cashier took on Friday is
still in the shop's file and /api/dashboard still answers it — what changed is that the Dashboard's
two day tiles stop printing those amounts and stand at ₵ 0.00 instead, and the day card's glance line
says the figures are cleared rather than printing them. The month, spent and profit figures still
tally, because they were never the ask, and the clock, calendar, weather and shop hours on the day
card are the shop as it stands right now.

This check exists because the next rebuild can quietly bind the amounts back in. Run it with:

    python3 tools/check-cleared-tiles.py

It boots a throwaway shop server against a COPY of the book, reads the Money band the way a cashier
does — at a desktop width and at a phone width, in the rendering engine the Mac app ships — and
closes by proving the shop's own file is still the bytes it started with.

"""
import datetime
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

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


PORT = free_port(8961)
BASE = "http://127.0.0.1:%d" % PORT

work = tempfile.mkdtemp(prefix="chrisphics-cleared-")
CLONE = os.path.join(work, "clone.db")
src = sqlite3.connect("file:%s?mode=ro" % LIVE, uri=True)
dst = sqlite3.connect(CLONE)
src.backup(dst)
dst.close()
src.close()
# This throwaway copy would otherwise ask the loopback to sign in, and no check here holds the
# shop's word. Cleared on the copy only — the shop's own file is never opened for writing.
wipe = sqlite3.connect(CLONE)
wipe.execute("UPDATE app_state SET value='0' WHERE key='shop_mac_login'")
wipe.commit()
wipe.close()

live_stat = os.stat(LIVE)
live_hash = hashlib.sha256(open(LIVE, "rb").read()).hexdigest()

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


def text(path):
    return open(os.path.join(REPO, path), encoding="utf-8").read()


def get_json(path):
    with urllib.request.urlopen(BASE + path, timeout=8) as res:
        return json.loads(res.read().decode())


def cash(v):
    """The same figure the page prints, worked out here so the browser can never grade its own
    answer: the page's money() is symbol + space + en-US grouping to two places."""
    return "₵ %s" % ("{:,.2f}".format(float(v)))


# ---------------------------------------------------------------- the binding, as written
app = text("public/app.js")
check("the day tile is bound to a cleared figure, not the book's takings",
      "stat('Collected ' + dayTag, money(0)" in app)
check("and so is the week tile", "stat(weekTag, money(0)" in app)
check("while the month tile still reads the book",
      "stat(onCurrentMonth ? 'Month to date' : d.money_month_label, money(k.collected_month)" in app)
# The day card took its glance line from the same two amounts; it now takes it from neither.
check("and the day card is no longer handed the book's day figures at all",
      "function todayCard(when)" in app and "todayCard(d.kpi" not in app)

# ---------------------------------------------------------------- a shop to read
server = subprocess.Popen([sys.executable, os.path.join(REPO, "server.py"),
                           "--port", str(PORT), "--no-browser"],
                          cwd=REPO, env=env, stdout=log, stderr=log)
ready = False
for _ in range(60):
    time.sleep(0.25)
    try:
        if server.poll() is None and "authenticated" in get_json("/api/session"):
            ready = True
            break
    except Exception:
        continue
if not check("a throwaway shop server is answering on 127.0.0.1:%d" % PORT, ready):
    print("\nthe throwaway shop server did not start — no screen checks ran")
    sys.exit(1)

holder = subprocess.run(["lsof", "-nP", "-iTCP:%d" % PORT, "-sTCP:LISTEN"],
                        capture_output=True, text=True).stdout.splitlines()
listeners = {line.split()[1] for line in holder[1:]
             if len(line.split()) > 2 and line.split()[1].isdigit()}
check("and the only thing answering on that port is the server this run started",
      listeners == {str(server.pid)},
      "%d listener(s), pids %s, this run's child is %d"
        % (len(listeners), ",".join(sorted(listeners)) or "-", server.pid))

d = get_json("/api/dashboard")
k = d["kpi"]

# Worked out here rather than read off the page, so a screen that clears the wrong day still fails.
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December")
today = datetime.date.today()
TODAY_WORDS = "%s, %d %s %d" % (DAY_NAMES[today.weekday()], today.day,
                               MONTH_NAMES[today.month - 1], today.year)
anchor = datetime.date.fromisoformat((d.get("money_day") or str(today))[:10])
anchor_name = DAY_NAMES[anchor.weekday()]
on_today = anchor == today
CLEARED_GLANCE = ('Figures cleared — ' +
                  ('the shop has traded today' if on_today
                   else 'the book last moved %s' % anchor_name))
check("the engine still answers the day's takings — nothing was struck from the book",
      float(k["collected_today"]) > 0, cash(k["collected_today"]))
check("and still answers the week's", float(k["collected_week"]) > 0, cash(k["collected_week"]))
check("and the month's, which is the figure the screen keeps showing",
      float(k["collected_month"]) > 0, cash(k["collected_month"]))

PROBE_BIN = os.path.join(work, "wk-probe")
built = subprocess.run(["swiftc", os.path.join(REPO, "tools/wk-probe.swift"), "-o", PROBE_BIN],
                       capture_output=True, text=True, timeout=300)
if not check("the rendering engine probe compiles", built.returncode == 0,
             (built.stderr or "")[-200:]):
    print("\ncould not build tools/wk-probe.swift — no screen checks ran")
    sys.exit(1)

READ = """(function () {
  var tiles = Array.prototype.slice.call(document.querySelectorAll('#view .kpis.money .stat'));
  var glance = document.querySelector('#todayGlance');
  var band = document.querySelector('.bookband');
  var clock = document.querySelector('#todayClock');
  var date = document.querySelector('#todayDate');
  return JSON.stringify({
    tiles: tiles.map(function (t) { return {
      k: (t.querySelector('.k') || {}).textContent,
      v: (t.querySelector('.v') || {}).textContent,
      n: (t.querySelector('.n') || {}).textContent,
      hint: t.getAttribute('title'),
      hero: t.classList.contains('hero')
    }; }),
    glance: glance ? glance.textContent : null,
    band: band ? band.textContent.replace(/\\s+/g, ' ') : null,
    clock: clock ? clock.textContent.replace(/\\s+/g, ' ') : null,
    date: date ? date.textContent : null,
    scrollH: document.documentElement.scrollHeight
  });
})()"""


def read_band(width, height, label):
    """Load the Dashboard in the shipped engine at one size and hand back what the Money band says."""
    args = [PROBE_BIN, BASE, work, "size|%dx%d" % (width, height),
            "open|/?mode=light#/dashboard",
            "poll|!document.querySelector('#view .loading')",
            "say|" + READ]
    out = subprocess.run(args, capture_output=True, text=True, timeout=180)
    raw = out.stdout or ""
    for line in raw.splitlines() + (out.stderr or "").splitlines():
        if line.strip().startswith(("EVAL ERROR", "SNAPSHOT ERROR", "TIMEOUT waiting", "NAV FAILED")):
            fails.append("%s: %s" % (label, line.strip()[:70]))
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError as bad:
                fails.append("%s: the probe said something this check could not read: %s"
                             % (label, str(bad)[:40]))
    fails.append("%s: the probe answered with no report at all" % label)
    print("%s: raw output was:\n%s" % (label, raw[:1200]))
    return {}


seen = {}
for (w, h, who) in ((1440, 900, "on a Mac"), (390, 844, "on a phone")):
    r = read_band(w, h, who)
    seen[who] = r
    tiles = r.get("tiles") or []
    if not check("%s: the Money band still carries its four tiles" % who, len(tiles) == 4,
                 "%d tiles" % len(tiles)):
        continue
    print("   %s: %s" % (who, " | ".join("%s = %s (%s)" % (t["k"], t["v"], t["n"]) for t in tiles)))
    expect("%s: the day tile reads zero" % who, tiles[0]["v"], cash(0))
    expect("%s: and says its figure was cleared, not that the day took nothing" % who,
           tiles[0]["n"], "cleared from this screen")
    expect("%s: the week tile reads zero" % who, tiles[1]["v"], cash(0))
    expect("%s: and the same word" % who, tiles[1]["n"], "cleared from this screen")
    check("%s: neither cleared tile is dressed as the hero" % who,
          not tiles[0]["hero"] and not tiles[1]["hero"])
    # The hint is the one place the day is still named on the tile, so a cashier can ask which day
    # the blank stands for without the amount ever coming back.
    check("%s: the day tile still names the day it stands in for" % who,
          (tiles[0]["hint"] or "").startswith("Money in and out, counted from "),
          tiles[0]["hint"])
    # What the owner did NOT clear: the month, and the book band above the tiles.
    expect("%s: the month tile still tallies the book" % who, tiles[2]["v"], cash(k["collected_month"]))
    check("%s: and the band still carries the month's billing" % who,
          cash(k["billed_month"]) in (r.get("band") or ""), r.get("band"))
    # The day card, on the same word a minute later. It was the last place on this screen printing the
    # day's takings; its clock, date, weather and shop hours are the shop right now and stay live.
    g = r.get("glance") or ""
    check("%s: the day card prints no amount" % who, "₵" not in g, g)
    check("%s: ...and no job count either" % who, "booked" not in g, g)
    expect("%s: the line says the figures are cleared, and names the day the book stopped on" % who,
           g, CLEARED_GLANCE)
    check("%s: while the card's clock is still ticking" % who,
          bool((r.get("clock") or "").strip()), r.get("clock"))
    expect("%s: and its date is still today, not the anchored day" % who, r.get("date"), TODAY_WORDS)

print("\nthe day card's glance line, as the shipped engine paints it: %r"
      % ((seen.get("on a Mac") or {}).get("glance"),))

# ---------------------------------------------------------------- the book is still the shop's
after = os.stat(LIVE)
expect("the live book was not written to",
       hashlib.sha256(open(LIVE, "rb").read()).hexdigest(), live_hash)
check("the live book is the size it was", after.st_size == live_stat.st_size,
      "%d vs %d" % (after.st_size, live_stat.st_size))

server.terminate()
for _ in range(40):
    if server.poll() is not None:
        break
    time.sleep(0.25)
log.close()

print("live book: %d bytes, sha256 %s (untouched)" % (after.st_size, live_hash[:16]))
print("\n%d checks failed" % len(fails))
sys.exit(1 if fails else 0)
