#!/usr/bin/env python3
"""Prove the shop's own hours and its limit of devices at a time are enforced where it counts.

The counter's rule, written down 2026-10-12: three devices signed in at once, and new sign-ins
only Monday to Saturday, 8:00am to 7:00pm. Two things make that worth a verifier rather than a
glance at the code.

  * A device already inside must keep working when the window shuts — nobody is thrown out of the
    shop's own book at closing time. Only a *fresh* sign-in is turned away.
  * A clock on a phone must not be able to open the book early, so the decision is made on the
    server's clock and the dashboard is handed the seconds that are left.

Nothing here touches the shop's live book. It is read only and copied with Connection.backup into
two temp files, each served on a spare port with its own throwaway word handed over in the
environment — never the owner's, never printed, never sent anywhere but 127.0.0.1. Every session
row is cleared off the copies first, so the device counts below are this run's own and nothing the
shop signed in last week.

  * server A runs with --trust-proxy, which is how it learns to tell a visitor from the counter:
    behind a proxy loopback proves nothing, so each request arrives as a made-up address and is
    judged as a visiting device.
  * server B runs plain on loopback with the shop's-own-Mac switch on, which is the only way to
    prove the counter is never turned away by the hour, nor by a full house.

The server marks its cookie Secure, which an http:// test client would drop, so the session token
is carried by hand from Set-Cookie to Cookie. That examines the server's answer, not the browser's
manners.
"""
import atexit
import datetime as dt
import hashlib
import json
import os
import secrets
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
WORD = secrets.token_urlsafe(14)

work = tempfile.mkdtemp(prefix="chrisphics-shop-hours-")
REMOTE = os.path.join(work, "remote.db")
COUNTER = os.path.join(work, "counter.db")
fails = []
servers = []
visitor = [0]
LAST = {"cookies": []}
live_before = os.stat(LIVE)


def free_port():
    """Ask the machine for a port nobody is holding. A fixed number looks tidier in the output and
    hides the worst possible failure: a leftover server from a crashed run answers the health check,
    every request goes to its stale copy of the book, and the checks then disagree for reasons that
    have nothing to do with the code."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def cleanup():
    for proc in servers:
        if proc.poll() is None:
            proc.terminate()
    for _ in range(50):
        if all(proc.poll() is not None for proc in servers):
            return
        time.sleep(0.1)


atexit.register(cleanup)


def check(label, ok, detail=""):
    print("%-58s %s %s" % (label, "ok" if ok else "FAIL", detail))
    if not ok:
        fails.append(label)


def expect(label, got, want):
    check(label, got == want, "" if got == want else "got %r, wanted %r" % (got, want))


def clone(path):
    src = sqlite3.connect("file:%s?mode=ro" % LIVE, uri=True)
    dst = sqlite3.connect(path)
    src.backup(dst)
    dst.close()
    src.close()
    empty = sqlite3.connect(path)
    empty.execute("DELETE FROM sessions")
    empty.commit()
    empty.close()


def seed(path, values):
    con = sqlite3.connect(path)
    for key, value in values.items():
        con.execute("INSERT INTO app_state (key, value, updated_at)"
                    " VALUES (?, ?, datetime('now','localtime'))"
                    " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
                    " updated_at = excluded.updated_at", (key, str(value)))
    con.commit()
    con.close()


def scrape(path, sql):
    con = sqlite3.connect(path)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def start(port, book, extra=()):
    """One throwaway server with its own support and backup folders, so nothing it writes can
    reach the shop's real ones."""
    env = dict(os.environ, CHRISPHICS_DB=book,
               CHRISPHICS_SUPPORT=os.path.join(work, "support-%d" % port),
               CHRISPHICS_BACKUP_DIR=os.path.join(work, "backups-%d" % port),
               CHRISPHICS_AUTH_PASSWORD=WORD, PYTHONUNBUFFERED="1")
    log_path = os.path.join(work, "server-%d.log" % port)
    log = open(log_path, "wb")
    proc = subprocess.Popen([sys.executable, os.path.join(REPO, "server.py"),
                             "--host", "127.0.0.1", "--port", str(port), "--no-browser"] + list(extra),
                            cwd=REPO, env=env, stdout=log, stderr=log)
    servers.append(proc)
    for _ in range(140):
        if proc.poll() is not None:
            print("  the server on port %d died: %s" % (port, tail(log_path)))
            return False
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/healthz" % port, timeout=3) as res:
                if json.loads(res.read().decode())["ok"]:
                    # Answered, but answered by *this* process? Give it a moment to fall over on a
                    # port it could not bind, then ask again.
                    time.sleep(0.4)
                    if proc.poll() is not None:
                        print("  something else held port %d: %s" % (port, tail(log_path)))
                        return False
                    return True
        except Exception:
            time.sleep(0.15)
    print("  port %d never answered: %s" % (port, tail(log_path)))
    return False


def tail(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readlines()[-3:]).strip()[:200]
    except OSError:
        return "no log yet"


def call(port, path, method="GET", body=None, token="", as_visitor=True, ua="Chrome on Android",
         machine=""):
    """One request. Behind --trust-proxy the address is whatever the header says, so each call can
    arrive as a different device. The machine key a named computer is given travels as its own
    cookie, so it is carried by hand here the same way the session token is."""
    visitor[0] += 1
    headers = {"User-Agent": ua}
    if as_visitor:
        headers["X-Forwarded-For"] = "10.20.%d.%d" % (visitor[0] // 250, visitor[0] % 250)
    held = []
    if token:
        held.append("crispprint_session=%s" % token)
    if machine:
        held.append("crispprint_machine=%s" % machine)
    if held:
        headers["Cookie"] = "; ".join(held)
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=data,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=8) as res:
            cookies = res.headers.get_all("Set-Cookie") or []
            LAST["cookies"] = cookies
            return res.status, parse(res.read()), token_of(cookies) or token
    except urllib.error.HTTPError as err:
        LAST["cookies"] = err.headers.get_all("Set-Cookie") or []
        return err.code, parse(err.read()), ""


def parse(raw):
    try:
        return json.loads(raw.decode())
    except Exception:
        return {}


def cookie_named(lines, name):
    for line in lines or []:
        head = line.split(";")[0]
        if head.startswith(name + "="):
            return head.split("=", 1)[1]
    return ""


def token_of(cookies):
    return cookie_named(cookies, "crispprint_session")


def cookie_max_age(lines, name):
    """The Max-Age one of these Set-Cookie lines carries, so how long a device may stay inside can
    be read off the answer its browser was given rather than off the book."""
    for line in lines or []:
        head, _, rest = line.partition(";")
        if head.startswith(name + "="):
            for bit in rest.split(";"):
                key, _, value = bit.strip().partition("=")
                if key.lower() == "max-age":
                    return int(value)
    return -1


def machine_of():
    """The long key the last answer left in the browser of a machine the counter had named."""
    return cookie_named(LAST["cookies"], "crispprint_machine")


def clock(minutes):
    return "%02d:%02d" % divmod(minutes, 60)


def open_window():
    """A window with two hours either side of this minute, so the check never lands on a boundary.
    The day before today cannot be today, so the shut one needs no such care."""
    now = dt.datetime.now()
    at = now.hour * 60 + now.minute
    return {"open_days": "", "open_from": clock(max(0, at - 120)),
            "open_to": clock(min(23 * 60 + 59, at + 120))}


def shut_window():
    back = (dt.date.today() - dt.timedelta(days=1)).weekday()
    return {"open_days": str(back), "open_from": "00:00", "open_to": "23:59"}


# ------------------------------------------------------------------ 1. the rules themselves
print("\n— the window and the cap, as the book reads them —")
os.environ["CHRISPHICS_DB"] = os.path.join(work, "rules.db")
os.environ["CHRISPHICS_SUPPORT"] = os.path.join(work, "support-rules")
os.environ["CHRISPHICS_BACKUP_DIR"] = os.path.join(work, "backups-rules")
sys.path.insert(0, REPO)
import server  # noqa: E402

server.connect(os.path.join(work, "rules.db"))
HOURS_KEYS = ("open_days", "open_from", "open_to", "device_limit")


def clear_rules():
    for key in HOURS_KEYS:
        server.set_state(key, "")


def at(y, m, d, hh, mm=0):
    return dt.datetime(y, m, d, hh, mm)


clear_rules()
h = server.shop_hours(now=at(2026, 10, 18, 12))
expect("nothing written: open at any hour", h["open"], True)
expect("nothing written: nothing to count down", h["seconds_to_change"], None)
expect("nothing written: the counter says around the clock", h["when_text"], "around the clock")
expect("nothing written: nobody is capped", h["limit"], 0)

# 2026-10-12 is a Monday, so 17 is Saturday and 18 Sunday.
server.set_state("open_days", "0-5")
server.set_state("open_from", "08:00")
server.set_state("open_to", "19:00")
server.set_state("device_limit", "3")
for label, when, want_open, want_secs, want_change in [
    ("Monday 10:00", at(2026, 10, 12, 10), True, 9 * 3600, "close"),
    ("Monday 07:59", at(2026, 10, 12, 7, 59), False, 60, "open"),
    ("Monday 08:00", at(2026, 10, 12, 8), True, 11 * 3600, "close"),
    ("Monday 19:00", at(2026, 10, 12, 19), False, 13 * 3600, "open"),
    ("Saturday 18:30", at(2026, 10, 17, 18, 30), True, 1800, "close"),
    ("Saturday 19:00, shut until Monday", at(2026, 10, 17, 19), False, 37 * 3600, "open"),
    ("Sunday midday", at(2026, 10, 18, 12), False, 20 * 3600, "open"),
    ("Sunday 23:30", at(2026, 10, 18, 23, 30), False, 8 * 3600 + 1800, "open"),
]:
    hour = server.shop_hours(now=when)
    expect("%s: open?" % label, hour["open"], want_open)
    expect("%s: seconds left" % label, hour["seconds_to_change"], want_secs)
    expect("%s: counting toward" % label, hour["changes"], want_change)

hour = server.shop_hours(now=at(2026, 10, 18, 12))
expect("the window in the counter's words", hour["when_text"],
       "Monday to Saturday, 8:00am to 7:00pm")
expect("a shut Sunday says when it opens", hour["state_text"], "Closed · opens Monday at 8:00am")
expect("the cap is read back", hour["limit"], 3)

# A day list with no clock means the whole of those days.
server.set_state("open_from", "")
server.set_state("open_to", "")
expect("days only: a Saturday at 3am is open",
       server.shop_hours(now=at(2026, 10, 17, 3))["open"], True)
expect("days only: a Sunday is still shut",
       server.shop_hours(now=at(2026, 10, 18, 12))["open"], False)

# Garbage is ignored rather than obeyed: a typo must never cost a shop its counter.
for bad in ("mon-tue", "8am", "abc", ""):
    server.set_state("open_days", bad)
    expect("unreadable days %r fall back to every day" % bad,
           server.shop_hours(now=at(2026, 10, 18, 12))["open"], True)
server.set_state("open_days", "0-5")
server.set_state("open_from", "25:00")
expect("a bad time falls back to all day",
       server.shop_hours(now=at(2026, 10, 12, 3))["all_day"], True)
expect("but the open days are still honoured",
       server.shop_hours(now=at(2026, 10, 18, 12))["open"], False)
clear_rules()


def refuses(patch, why):
    try:
        server.set_security_settings(patch, "10.0.0.1")
        check("the pane refuses %s" % why, False, "it saved it instead")
    except ValueError as exc:
        check("the pane refuses %s" % why, True, str(exc)[:50])


def accepts(patch, why):
    try:
        server.set_security_settings(patch, "10.0.0.1")
        check("the pane accepts %s" % why, True)
    except ValueError as exc:
        check("the pane accepts %s" % why, False, str(exc))


refuses({"open_days": "mon,thu"}, "weekday names")
refuses({"open_days": "8"}, "a day that does not exist")
refuses({"open_from": "8:00am"}, "am written into a time")
refuses({"open_from": "08:00"}, "an opening time with no closing one")
refuses({"open_from": "19:00", "open_to": "08:00"}, "closing before opening")
refuses({"device_limit": "21"}, "more devices than the pane will hold")
refuses({"device_limit": "three"}, "a cap spelled out")
refuses({"device_limit": "-1"}, "a negative cap")
accepts({"open_days": "0-5"}, "a range of days")
accepts({"open_days": "0,1,2,3,4,5"}, "the same days listed out")
accepts({"open_from": "08:00", "open_to": "19:00"}, "both times together")
accepts({"device_limit": "3"}, "three places")
accepts({"device_limit": "0"}, "nobody capped")
accepts({"open_from": "", "open_to": ""}, "both times cleared")
expect("the open days alone still count as a window", server.shop_hours()["written"], True)
accepts({"open_days": ""}, "the days cleared too")
expect("and the window is written out again", server.shop_hours()["written"], False)
silent = server.public_hours(server.shop_hours(with_devices=True))
check("a stranger's /api/session never sees who is inside",
      "devices" not in silent and "device_names" not in silent)
check("a signed-in device does get the occupancy",
      "devices" in server.public_hours(server.shop_hours(with_devices=True), True))

# ------------------------------------------------------------------ 1b. named machines
print("\n— a machine the counter writes down as its own —")
own = server.name_shop_computer("CHRISPRINT’s iMac", "127.0.0.1")
check("naming a machine hands back a plate", len(own["plate"]) == 8, own["name"])
expect("and the plate reads in two halves", own["plate_words"].count("-"), 1)
waiting = server.shop_computer_view()
expect("the book now holds one machine", len(waiting), 1)
expect("still waiting for the machine it was written for", waiting[0]["waiting"], True)
check("and the plate is not repeated where the book lists", own["plate"] not in json.dumps(waiting))
claimed, key = server.claim_shop_computer(own["plate"])
check("the plate finds its machine", bool(claimed), own["name"])
expect("by the name the counter wrote", (claimed or {}).get("name"), "CHRISPRINT’s iMac")
check("and is left a long key of its own", len(key) > 30, "%d characters" % len(key))
expect("the key alone says which machine this is", server.computer_by_key(key)["name"],
       "CHRISPRINT’s iMac")
spent, reason = server.claim_shop_computer(own["plate"])
check("a plate is only good for one machine", spent is None, reason[:52])
wrong, reason = server.claim_shop_computer("QQQQ-QQQQ")
check("a plate never written out is turned away", wrong is None, reason[:52])
none, reason = server.claim_shop_computer("")
check("and an empty box says nothing at all", none is None and reason == "")

second = server.name_shop_computer("CHRISPRINT’s iMac (2)", "127.0.0.1")
also, key2 = server.claim_shop_computer(second["plate"].lower()[:4] + "-" + second["plate"][4:])
check("a plate typed small and with a dash still lands", bool(also), (also or {}).get("name"))

stale = server.name_shop_computer("The old press Mac", "127.0.0.1")
with server._lock:
    server._db.execute("UPDATE shop_computers SET expires_at = ? WHERE id = ?",
                       (time.time() - 60, stale["id"]))
    server._db.commit()
late, reason = server.claim_shop_computer(stale["plate"])
check("an out-of-date plate is refused, in words", late is None, reason[:52])
try:
    server.name_shop_computer("   ", "127.0.0.1")
    check("a machine has to be given a name", False, "it was named anyway")
except ValueError as exc:
    check("a machine has to be given a name", True, str(exc)[:52])

clear_rules()
server.set_state("open_days", "0-5")
server.set_state("open_from", "08:00")
server.set_state("open_to", "19:00")
server.set_state("device_limit", "1")
press = server.open_session("Mac · Safari")
server.keep_shop_computer(claimed["id"], press)
inside = server.signed_in_devices()
expect("a named machine's session carries its name",
       [row["shop_computer"] for row in inside if row["shop_computer"]], ["CHRISPRINT’s iMac"])
busy = server.shop_hours(now=at(2026, 10, 12, 10), with_devices=True)
expect("and holds none of the places kept for visitors", busy["devices"], 0)
expect("while the counter is still told it is inside", busy["own_devices"], 1)
stranger = server.open_session("Chrome on Android")
full = server.shop_hours(now=at(2026, 10, 12, 10), with_devices=True)
expect("one visiting device fills the one place", full["devices"], 1)
check("so the next visitor is turned away", server.sign_in_refusal(False, False) is not None)
check("and a named machine is not", server.sign_in_refusal(True, False) is None,
      server.sign_in_refusal(True, False))
shut = server.shop_hours(now=at(2026, 10, 18, 12), with_devices=True)
expect("a Sunday counts the visitors only", shut["devices"], 1)
server.revoke_shop_computer(claimed["id"], "127.0.0.1")
check("taking the name off signs that machine straight out",
      server.one("SELECT id FROM sessions WHERE token_hash = ?",
                 (server.token_digest(press),)) is None)
expect("and the book stops naming it", len(server.shop_computer_view()), 2)
check("the key it kept opens nothing now", server.computer_by_key(key) is None)

# ------------------------------------------- 1c. how long each kind of device is allowed to stay
print("\n— a named machine is not on a countdown —")
server.set_state("session_days", "1")
wanderer = server.open_session("Chrome on Android")
resident = server.open_session("Mac · Chrome", forever=True)
server.keep_shop_computer(also["id"], resident)
hold = server.one("SELECT expires_at FROM sessions WHERE token_hash = ?",
                  (server.token_digest(resident),))
brief = server.one("SELECT expires_at FROM sessions WHERE token_hash = ?",
                   (server.token_digest(wanderer),))
check("a visiting device is given the shop's one day",
      0 < brief["expires_at"] - time.time() < 2 * 86400,
      "%d hours left" % round((brief["expires_at"] - time.time()) / 3600))
check("the named machine is given a date no countdown reaches",
      hold["expires_at"] >= server.SESSION_NEVER,
      dt.datetime.fromtimestamp(hold["expires_at"]).strftime("%d %b %Y"))
expect("and it stays inside when the book is touched", server.touch_session(resident), True)
expect("the shop shortening everyone's stay to a day does not pull it back",
       server.one("SELECT expires_at FROM sessions WHERE token_hash = ?",
                  (server.token_digest(resident),))["expires_at"], hold["expires_at"])
with server._lock:
    server._db.execute("UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
                       (time.time() - 60, server.token_digest(wanderer)))
    server._db.commit()
expect("while a visiting device that ran out is asked again", server.touch_session(wanderer), False)
keeper = [row for row in server.shop_computer_view() if row["name"] == also["name"]][0]
expect("the counter is told this one keeps its sign-in", keeper["stays_forever"], True)
inside = [(row["shop_computer"], row["stays_forever"])
          for row in server.signed_in_devices() if row["shop_computer"]]
expect("and so is the list of devices", inside, [("CHRISPRINT’s iMac (2)", 1)])
server.revoke_shop_computer(also["id"], "127.0.0.1")
check("taking the name off ends the forever stay",
      server.one("SELECT id FROM sessions WHERE token_hash = ?",
                 (server.token_digest(resident),)) is None)
server.set_state("session_days", "")
clear_rules()

# ---------------------------------------------------------------------- 2. a copy of the book
print("\n— enforcement, against a copy of the shop's book —")
clone(REMOTE)
clone(COUNTER)
seed(REMOTE, {"shop_mac_login": ""})
seed(COUNTER, {"shop_mac_login": "1"})
PORT_REMOTE = free_port()
PORT_COUNTER = free_port()
check("the visitor's server came up on port %d" % PORT_REMOTE,
      start(PORT_REMOTE, REMOTE, ("--trust-proxy",)))
check("the counter's server came up on port %d" % PORT_COUNTER, start(PORT_COUNTER, COUNTER))
LIVE_NOW = "select count(*) from sessions where expires_at > %f" % time.time()
# The book already writes "turned away" for an unauthenticated poke at the ledger, so the count
# below is of *this* kind of turning-away: the one that only happens after the right word.
TURNED = ("select count(*) from security_log where kind='turned_away'"
          " and detail like 'The shop lets new devices sign in%'")
turned_before = scrape(REMOTE, TURNED)[0][0]

print("\n— before anybody signs in —")
code, body, _ = call(PORT_REMOTE, "/api/session")
expect("a stranger is told the timetable", code, 200)
check("and not who is inside the book", "devices" not in body.get("hours", {}),
      body.get("hours", {}).get("when_text", ""))
expect("a stranger's /api/bootstrap is still refused",
       call(PORT_REMOTE, "/api/bootstrap")[0], 401)

print("\n— outside the window —")
seed(REMOTE, shut_window())
code, body, _ = call(PORT_REMOTE, "/api/login", "POST", {"password": "not the shop's word"})
expect("a wrong word outside the window is still just wrong", code, 401)
check("and it says nothing about hours", "window" not in json.dumps(body).lower(),
      body.get("error", ""))
code, body, _ = call(PORT_REMOTE, "/api/login", "POST", {"password": WORD})
expect("the right word outside the window is turned away", code, 403)
check("and it is told when the shop lets devices in",
      server.SHOP_DAY_NAMES[int(shut_window()["open_days"])] in body.get("error", ""),
      body.get("error", "")[:70])
expect("a turning-away leaves no session behind", scrape(REMOTE, LIVE_NOW)[0][0], 0)
expect("and it is written in the book's own trail",
       scrape(REMOTE, TURNED)[0][0], turned_before + 1)

print("\n— inside the window —")
seed(REMOTE, dict(open_window(), device_limit="3"))
tokens = []
for _ in range(3):
    code, body, token = call(PORT_REMOTE, "/api/login", "POST", {"password": WORD})
    if code == 200 and token:
        tokens.append(token)
expect("three devices get in while the shop is open", len(tokens), 3)
expect("the book counts exactly three live sessions", scrape(REMOTE, LIVE_NOW)[0][0], 3)
code, body, _ = call(PORT_REMOTE, "/api/login", "POST", {"password": WORD})
expect("a fourth is turned away", code, 403)
check("and is told which three hold the places", body.get("error", "").count("·") >= 3,
      body.get("error", "")[:70])
code, body, _ = call(PORT_REMOTE, "/api/session", token=tokens[0])
expect("a signed-in device sees the occupancy", body.get("hours", {}).get("devices"), 3)
expect("and is handed no new sign-in to remember it by",
       cookie_max_age(LAST["cookies"], "crispprint_session"), -1)
code, body, _ = call(PORT_REMOTE, "/api/dashboard", token=tokens[0])
expect("so does the dashboard it is looking at", body.get("hours", {}).get("limit"), 3)

print("\n— closing time —")
seed(REMOTE, dict(shut_window(), device_limit="3"))
expect("a device already inside keeps working when the window shuts",
       call(PORT_REMOTE, "/api/bootstrap", token=tokens[0])[0], 200)
code, body, _ = call(PORT_REMOTE, "/api/dashboard", token=tokens[0])
expect("and its timer counts up to opening", body.get("hours", {}).get("changes"), "open")
expect("while a fresh sign-in is still refused",
       call(PORT_REMOTE, "/api/login", "POST", {"password": WORD})[0], 403)
seed(REMOTE, dict(open_window(), device_limit="3"))
con = sqlite3.connect(REMOTE)
con.execute("DELETE FROM sessions WHERE id = (SELECT min(id) FROM sessions)")
con.commit()
con.close()
code, body, fourth = call(PORT_REMOTE, "/api/login", "POST", {"password": WORD})
check("freeing a place lets the next device in", code == 200 and bool(fourth),
      body.get("error", "")[:60])
expect("a visiting device cannot rewrite the rules",
       call(PORT_REMOTE, "/api/security", "PUT", {"device_limit": "2"}, token=fourth)[0], 403)

print("\n— the shop's own machines, named at the counter —")
# The plate for the counter's iMac is planted the way the counter would have written it: the book
# only ever keeps a digest, so the check hashes a plate it chose itself. The visiting sessions left
# over from the section above are cleared first, so every count below means what it says.
PLATE = "KR7P-M2XQ"
con = sqlite3.connect(REMOTE)
con.execute("DELETE FROM sessions")
con.execute("INSERT INTO shop_computers (name, code_hash, expires_at) VALUES (?, ?, ?)",
            ("CHRISPRINT’s iMac", hashlib.sha256(PLATE.replace("-", "").encode("utf-8")).hexdigest(),
             time.time() + 86400))
con.commit()
con.close()
seed(REMOTE, dict(shut_window(), device_limit="1"))
expect("a stranger with no word at all cannot even reach the list of shop machines",
       call(PORT_REMOTE, "/api/shop-computers")[0], 401)
code, body, _ = call(PORT_REMOTE, "/api/login", "POST",
                    {"password": WORD, "shop_code": "QQQQ-QQQQ"})
check("a wrong plate outside the window is turned away, and told why", code == 403,
      body.get("error", "")[:60])
check("in words about the plate, not the hour", "name plate" in body.get("error", ""),
      body.get("error", "")[:60])
expect("and it leaves no session behind", scrape(REMOTE, LIVE_NOW)[0][0], 0)
code, body, iMac = call(PORT_REMOTE, "/api/login", "POST",
                        {"password": WORD, "shop_code": PLATE}, ua="Safari on Mac")
check("the right word and the counter's plate get the iMac in outside the window",
      code == 200 and bool(iMac), body.get("error", "")[:60])
expect("and it is named in the answer", body.get("shop_computer"), "CHRISPRINT’s iMac")
KEY = machine_of()
check("the machine is left a key of its own", len(KEY) > 20, "%d characters" % len(KEY))
expect("and its browser is handed a sign-in meant to outlast the shop",
       cookie_max_age(LAST["cookies"], "crispprint_session"), server.SHOP_COMPUTER_KEY_AGE)
expect("with a session in the book stamped the same way",
       scrape(REMOTE, "select count(*) from sessions where expires_at >= %f"
              % server.SESSION_NEVER)[0][0], 1)
seed(REMOTE, dict(open_window(), device_limit="1"))
code, body, guest = call(PORT_REMOTE, "/api/login", "POST", {"password": WORD})
check("with the shop open, one visitor can take the one place", code == 200 and bool(guest),
      body.get("error", "")[:60])
expect("while a visiting device is given the shop's countdown, not the long one",
       cookie_max_age(LAST["cookies"], "crispprint_session"), server.session_seconds())
expect("a visitor cannot name itself as one of the shop's own",
       call(PORT_REMOTE, "/api/shop-computers", "POST", {"name": "some laptop"}, token=guest)[0],
       403)
expect("nor even read the list of them",
       call(PORT_REMOTE, "/api/shop-computers", token=guest)[0], 403)
expect("the next one is turned away",
       call(PORT_REMOTE, "/api/login", "POST", {"password": WORD})[0], 403)
code, body, named_again = call(PORT_REMOTE, "/api/login", "POST", {"password": WORD}, machine=KEY)
check("while the named machine signs in anyway and uses none of the places", code == 200,
      body.get("error", "")[:60])
expect("so the book holds one visitor and one named machine",
       scrape(REMOTE, LIVE_NOW)[0][0], 2)
code, body, _ = call(PORT_REMOTE, "/api/session", token=named_again, machine=KEY)
expect("a named machine is told as much about itself", body.get("shop_computer"),
       "CHRISPRINT’s iMac")
check("and an ordinary visit hands the same sign-in back to it, so a browser that forgets long"
      " cookies never loses its place",
      token_of(LAST["cookies"]) == named_again and
      cookie_max_age(LAST["cookies"], "crispprint_session") == server.SHOP_COMPUTER_KEY_AGE,
      "Max-Age %d" % cookie_max_age(LAST["cookies"], "crispprint_session"))
check("and it is not counted among the visiting places",
      body.get("hours", {}).get("devices"), 1)
expect("while its own kind is counted apart", body.get("hours", {}).get("own_devices"), 1)
con = sqlite3.connect(REMOTE)
con.execute("DELETE FROM sessions WHERE token_hash = ?",
            (hashlib.sha256(named_again.encode("utf-8")).hexdigest(),))
con.commit()
con.close()
code, body, again = call(PORT_REMOTE, "/api/login", "POST", {"password": WORD}, machine=KEY)
check("the key alone signs it back in, hours later and with no plate", code == 200,
      body.get("error", "")[:60])
expect("as the same machine", body.get("shop_computer"), "CHRISPRINT’s iMac")
code, body, _ = call(PORT_REMOTE, "/api/login", "POST", {"password": WORD, "shop_code": PLATE})
check("the spent plate cannot be given to a second machine", code == 403,
      body.get("error", "")[:60])
code, body, _ = call(PORT_REMOTE, "/api/login", "POST", {"password": WORD, "shop_code": "KR7PM2XQ"},
                     machine=KEY)
check("while a machine already named does not need its plate again", code == 200,
      body.get("error", "")[:60])
NAMED = scrape(REMOTE, "select count(*) from security_log where kind='computer_admitted'")[0][0]
check("every admission of a named machine is in the book's own trail", NAMED >= 3, str(NAMED))
check("and no name plate is written there",
      PLATE not in str(scrape(REMOTE, "select detail from security_log")))

print("\n— the counter naming and un-naming a machine —")
seed(COUNTER, dict(shut_window(), device_limit="1"))
tokens_b = []
for _ in range(2):
    code, body, token = call(PORT_COUNTER, "/api/login", "POST", {"password": WORD},
                             as_visitor=False, ua="ChrisphicsHub/1.0 Mac")
    if code == 200 and token:
        tokens_b.append(token)
expect("the counter is inside its own shop first", len(tokens_b), 2)
code, body, _ = call(PORT_COUNTER, "/api/shop-computers", "POST", {"name": "  "},
                    token=tokens_b[-1], as_visitor=False)
expect("the counter has to give it a name", code, 400)
code, body, _ = call(PORT_COUNTER, "/api/shop-computers", "POST", {"name": "CHRISPRINT’s iMac (2)"},
                    token=tokens_b[-1], as_visitor=False)
expect("the shop's own Mac can name one", code, 201)
PLATE2 = body.get("plate", "")
check("the plate is handed to this screen alone", len(PLATE2) == 8, body.get("plate_words", ""))
check("in two halves, to be read across a counter", body.get("plate_words", "").count("-"), 1)
code, body, _ = call(PORT_COUNTER, "/api/shop-computers", token=tokens_b[-1], as_visitor=False)
rows = body.get("computers", [])
expect("the list now carries it", len(rows), 1)
expect("waiting for its machine", rows[0]["waiting"], True)
check("and the plate is not in the list the counter re-reads", PLATE2 not in json.dumps(body))
code, body, press = call(PORT_COUNTER, "/api/login", "POST",
                         {"password": WORD, "shop_code": PLATE2}, as_visitor=False,
                         ua="Safari on Mac")
check("the plate signs that machine in", code == 200 and bool(press), body.get("error", "")[:60])
KEY2 = machine_of()
code, body, _ = call(PORT_COUNTER, "/api/shop-computers", token=tokens_b[-1], as_visitor=False)
row = [r for r in body.get("computers", []) if r["name"] == "CHRISPRINT’s iMac (2)"][0]
expect("named now, and signed in", (row["named"], row["signed_in"]), (True, True))
expect("the counter can take the name back off",
       call(PORT_COUNTER, "/api/shop-computers/%d" % row["id"], "DELETE",
            token=tokens_b[-1], as_visitor=False)[0], 200)
code, body, _ = call(PORT_COUNTER, "/api/shop-computers", token=tokens_b[-1], as_visitor=False)
expect("and it is off the list", len(body.get("computers", [])), 0)
expect("an un-named machine's session goes with its name",
       call(PORT_COUNTER, "/api/bootstrap", token=press, as_visitor=False)[0], 401)
expect("and its key names nothing",
       call(PORT_COUNTER, "/api/login", "POST", {"password": WORD}, machine=KEY2,
            as_visitor=False)[1].get("shop_computer", ""), "")
expect("a machine that was never on the list cannot be taken off it",
       call(PORT_COUNTER, "/api/shop-computers/9999", "DELETE",
            token=tokens_b[-1], as_visitor=False)[0], 404)
check("the book remembers both halves of that",
      scrape(COUNTER, "select count(*) from security_log where kind='computer_named'")[0][0] >= 1
      and scrape(COUNTER, "select count(*) from security_log where kind='computer_revoked'")[0][0]
      >= 1)

print("\n— the counter's own Mac —")
seed(COUNTER, dict(shut_window(), device_limit="1"))
tokens_b = []
for _ in range(2):
    code, body, token = call(PORT_COUNTER, "/api/login", "POST", {"password": WORD},
                             as_visitor=False, ua="ChrisphicsHub/1.0 Mac")
    if code == 200 and token:
        tokens_b.append(token)
expect("the counter signs in outside the window and past the cap", len(tokens_b), 2)
code, body, _ = call(PORT_COUNTER, "/api/dashboard", token=tokens_b[-1], as_visitor=False)
expect("while its dashboard still calls the book shut",
       body.get("hours", {}).get("open"), False)
code, body, _ = call(PORT_COUNTER, "/api/security", "PUT", {"device_limit": "3"},
                    token=tokens_b[-1], as_visitor=False)
expect("the shop's Mac can save the cap", body.get("hours", {}).get("limit"), 3)
code, body, _ = call(PORT_COUNTER, "/api/security", "PUT",
                    {"open_from": "19:00", "open_to": "08:00"}, token=tokens_b[-1],
                    as_visitor=False)
expect("and is argued with for closing before opening", code, 400)

cleanup()
live_after = os.stat(LIVE)
check("the shop's own book was not written to",
      live_after.st_size == live_before.st_size
      and int(live_after.st_mtime) == int(live_before.st_mtime),
      "%d bytes, unchanged" % live_after.st_size)
print("\n%d failed" % len(fails))
shutil.rmtree(work, ignore_errors=True)
if fails:
    print("FAILED: " + "; ".join(fails))
    sys.exit(1)
print("every shop-hours and device-cap check passed")
