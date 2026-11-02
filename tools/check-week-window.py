#!/usr/bin/env python3
"""Prove the Dashboard's "This week" counts the shop's working week: Monday opens it, Saturday shuts it.

The owner asked for it on 2 November 2026, the day after November opened. The window before it was
seven days lifted off a date, so a Monday's screen carried the tail of the month gone — last
Saturday's money, and Sunday's, counted again inside the new month's week. What is proved here,
against a throwaway clone of the shop's own book so the figures are the shop's real ones:

  * the week the engine reports is the money day's own Monday through the Saturday of that week —
    worked out here from the money day alone, not read back from the engine's answer, so a window
    that quietly stayed a rolling seven days fails;
  * the week's money equals the book's takings over exactly those six days, and its job count the
    bookings over the same;
  * a Sunday is not working-week money: money written on the Sunday that closed the previous week is
    inserted into the clone and the tile read again — the figure must not stir — while money written
    on a day inside the counted week must move it by exactly its amount. Both halves are forced in
    the book rather than restated from the window;
  * a month pinned in the chooser is read back into a working week of that month, never today's.

The live book is opened read-only and copied with Connection.backup; nothing here writes to the
shop's own file, and loopback needs no shop word.
"""
import datetime as dt
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
PORT = 8953
BASE = "http://127.0.0.1:%d" % PORT
sys.path.insert(0, os.path.join(REPO, "tools"))
import shopcheck  # noqa: E402  (lives beside this check)
shopcheck.require_free(PORT)

SUNDAY_MARK = 7777.77      # booked on the Sunday that closed last week — must change nothing
WEEKDAY_MARK = 5555.55     # booked on a day inside the counted week — must land to the pesewa

work = tempfile.mkdtemp(prefix="chrisphics-week-")
CLONE = os.path.join(work, "clone.db")
src = sqlite3.connect("file:%s?mode=ro" % LIVE, uri=True)
dst = sqlite3.connect(CLONE)
src.backup(dst)
dst.close()
src.close()
tidy = sqlite3.connect(CLONE)
tidy.execute("UPDATE app_state SET value='0' WHERE key='shop_mac_login'")
tidy.commit()
tidy.close()
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
    raise RuntimeError("the throwaway engine never answered")


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
ran = 0


def check(label, ok, detail=""):
    global ran
    ran += 1
    print("%-58s %s %s" % (label, "ok" if ok else "FAIL", detail))
    if not ok:
        fails.append(label)


def dash(month=""):
    code, raw = call("GET", "/api/dashboard" + ("?month=%s" % month if month else ""))
    return json.loads(raw.decode())


def cents(value):
    return round(float(value or 0), 2)


def iso(day):
    return day.isoformat()


def day_of(text):
    return dt.date.fromisoformat(text[:10])


def week_of(day):
    """The working week a day belongs to: its own Monday, and the Saturday before the rest day."""
    monday = day - dt.timedelta(days=day.weekday())
    return monday, monday + dt.timedelta(days=5)


proc = boot()
try:
    d = dash()
    k = d["kpi"]
    start, end = day_of(d["week_start"]), day_of(d["week_end"])
    money_day, today = day_of(d["money_day"]), day_of(d["today"])
    should_start, should_end = week_of(money_day)

    print("book: today %s · money day %s · week %s -> %s\n"
          % (iso(today), iso(money_day), iso(start), iso(end)))

    check("the week opens on the money day's own Monday, not seven days back",
          start == should_start and start.weekday() == 0,
          "%s, weekday %d (expected %s)" % (iso(start), start.weekday(), iso(should_start)))
    check("and shuts on that week's Saturday — the shop's working week",
          end == should_end and end.weekday() == 5 and (end - start).days == 5,
          "%s, weekday %d (expected %s)" % (iso(end), end.weekday(), iso(should_end)))
    check("the day the screen headlines sits inside the week it counts",
          start <= money_day <= end, "%s between %s and %s" % (iso(money_day), iso(start), iso(end)))
    check("and the card calls itself today's week only when today really sits in it",
          bool(d["week_current"]) == (should_start <= today <= should_start + dt.timedelta(days=6)),
          "week_current=%s · today %s · this week runs %s -> %s"
          % (d["week_current"], iso(today), iso(should_start),
             iso(should_start + dt.timedelta(days=6))))

    book = sqlite3.connect(CLONE)
    client = book.execute("SELECT id FROM clients WHERE archived=0 ORDER BY id LIMIT 1").fetchone()[0]

    def takings(a, b):
        return cents(book.execute(
            "SELECT coalesce(sum(CASE WHEN kind='Refund' THEN -amount ELSE amount END),0)"
            " FROM payments WHERE paid_at BETWEEN ? AND ?", (iso(a), iso(b))).fetchone()[0])

    def booked(a, b):
        return book.execute("SELECT count(*) FROM jobs WHERE kind='Job'"
                            " AND date(created_at) BETWEEN ? AND ?", (iso(a), iso(b))).fetchone()[0]

    def mark(amount, day, reference):
        book.execute("INSERT INTO payments (client_id, amount, kind, method, reference, paid_at)"
                     " VALUES (?, ?, 'Payment', 'Cash', ?, ?)", (client, amount, reference, iso(day)))
        book.commit()

    check("the week's money is the book's takings over Monday to Saturday",
          cents(k["collected_week"]) == takings(should_start, should_end),
          "%s = %s" % (cents(k["collected_week"]), takings(should_start, should_end)))
    check("and the week's job count is the book's bookings over the same six days",
          k["jobs_week"] == booked(should_start, should_end),
          "%s = %s" % (k["jobs_week"], booked(should_start, should_end)))

    # A Sunday is not a working day, so a Sunday's money is no week's working money — whichever month
    # it falls in. This is the whole of the owner's complaint, forced by booking the money.
    prev_sunday = start - dt.timedelta(days=1)
    before_week = cents(k["collected_week"])
    before_day = cents(k["collected_today"])
    mark(SUNDAY_MARK, prev_sunday, "SUNDAY-OUTSIDE-THE-WEEK")
    after = dash()["kpi"]
    check("the money written on that Sunday reached the book",
          takings(prev_sunday, prev_sunday) >= SUNDAY_MARK,
          "%s on %s" % (takings(prev_sunday, prev_sunday), iso(prev_sunday)))
    check("and the week's figure did not stir by a pesewa",
          cents(after["collected_week"]) == before_week,
          "%s -> %s" % (before_week, cents(after["collected_week"])))
    check("neither did the day's, because that Sunday is not the day being headlined",
          cents(after["collected_today"]) == before_day,
          "%s -> %s" % (before_day, cents(after["collected_today"])))

    # The other half: money on a day inside the counted week must arrive, to the pesewa. A Saturday
    # still ahead is not reached yet, so the marker goes on the day the screen headlines — always
    # inside the week, and never a date that could drag the headline forward.
    inside = end if end <= today else money_day
    in_week_before = cents(dash()["kpi"]["collected_week"])
    day_before = cents(dash()["kpi"]["collected_today"])
    mark(WEEKDAY_MARK, inside, "DAY-INSIDE-THE-WEEK")
    now = dash()["kpi"]
    check("the money written inside the counted week reached the book",
          takings(inside, inside) >= WEEKDAY_MARK,
          "%s on %s" % (takings(inside, inside), iso(inside)))
    check("and the week's figure rose by exactly that money",
          cents(now["collected_week"]) == round(in_week_before + WEEKDAY_MARK, 2),
          "%s -> %s (expected %s)" % (in_week_before, cents(now["collected_week"]),
                                      round(in_week_before + WEEKDAY_MARK, 2)))
    if inside == money_day:
        check("the day tile rose by the same money, since it fell on the headlined day",
              cents(now["collected_today"]) == round(day_before + WEEKDAY_MARK, 2),
              "%s -> %s" % (day_before, cents(now["collected_today"])))
    else:
        print("   the marker fell on %s, that week's own Saturday, so only the week tile moved"
              % iso(inside))

    # A month pinned in the chooser is read back into a working week of that month, never today's.
    months = [m["month"] for m in d["months_available"] if m["month"] != d["money_month"]]
    if months:
        pin = max(months)
        p = dash(pin)
        ps, pe = day_of(p["week_start"]), day_of(p["week_end"])
        pm = day_of(p["money_day"])
        want_s, want_e = week_of(pm)
        check("a month pinned in the chooser headlines Monday to Saturday of that month's week",
              ps == want_s and pe == want_e and ps <= pm <= pe and pm.strftime("%Y-%m") == pin
              and not p["week_current"],
              "%s: %s -> %s, money day %s, week_current=%s"
              % (pin, iso(ps), iso(pe), iso(pm), p["week_current"]))
        check("and the pinned week's money matches the book over those six days",
              cents(p["kpi"]["collected_week"]) == takings(ps, pe),
              "%s = %s" % (cents(p["kpi"]["collected_week"]), takings(ps, pe)))
    else:
        check("a month pinned in the chooser headlines Monday to Saturday of that month's week",
              False, "the book holds only one month, so there is nothing to pin")

    book.close()
finally:
    proc.terminate()
    proc.wait(timeout=10)
    log.close()
    after_stat = os.stat(LIVE)
    check("the shop's own book was never touched",
          (after_stat.st_size, after_stat.st_mtime) == (live_stat.st_size, live_stat.st_mtime),
          "%s bytes, unchanged" % live_stat.st_size)
    shutil.rmtree(work, ignore_errors=True)

print("\n%d checks, %d failed — %s" % (ran, len(fails),
      "every working-week window held" if not fails else "; ".join(fails)))
raise SystemExit(1 if fails else 0)
