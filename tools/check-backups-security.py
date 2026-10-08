#!/usr/bin/env python3
"""Prove the book's copies and its security against a COPY of the shop's records.

The live book is opened read-only, copied with Connection.backup into a temp file, and throwaway
servers take turns on that clone — each with its own backups folder, on a spare port:

  8899  loopback: this is the shop's own Mac — the copies, the mirror, the schedule, the rules
  8897  a second Mac with a copy overdue, to watch a running server copy the book by itself
  8898  --trust-proxy, so every visitor is a device out on the Wi-Fi — sign-in, lockout, throttling

The shop's own file is never written to; its size and its last write are compared at the end.
Nothing typed is printed here, and no password or session cookie goes anywhere but 127.0.0.1.

Usage: python3 tools/check-backups-security.py
"""
import json
import hashlib
import http.client
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
GOOD = "Tinted-Register-Plate-7"
BETTER = "Curing-Lamp-Blown-9182"

work = tempfile.mkdtemp(prefix="chrisphics-copies-security-")
CLONE = os.path.join(work, "clone.db")
try:
    source = sqlite3.connect("file:%s?mode=ro" % LIVE, uri=True)
except sqlite3.Error:
    # A WAL book with no -shm beside it cannot be opened read-only. Nothing here points a server
    # at the shop's own file, so the copy is still only ever a copy.
    source = sqlite3.connect(LIVE)
target = sqlite3.connect(CLONE)
source.backup(target)
target.close()
source.close()
live_stat = os.stat(LIVE)

book = sqlite3.connect(CLONE)
BEFORE = {table: book.execute("SELECT count(*) FROM %s" % table).fetchone()[0]
          for table in ("jobs", "clients", "payments", "expenses", "notifications")}
STATE = dict(book.execute("SELECT key, value FROM app_state").fetchall())
SHOP_NAME = STATE.get("shop_name", "CRISPprint Ghana")
# The copy starts from a known position, not from wherever the shop got to: its password hash,
# the devices holding cookies and every schedule the shop settled are all taken out. Nothing here
# ever needs the shop's real word, and a check must not pass or fail because of what it is.
for key in ("shop_password", "shop_mac_login", "session_days", "backup_auto",
            "backup_every_hours", "backup_keep_days", "backup_mirror", "last_backup_at",
            "last_backup_file", "last_backup_note", "last_backup_error",
            "last_backup_error_at", "last_backup_kind"):
    book.execute("DELETE FROM app_state WHERE key = ?", (key,))
if [r for r in book.execute("SELECT 1 FROM sqlite_master WHERE type='table'"
                            " AND name='security_log'").fetchall()]:
    book.execute("DELETE FROM security_log")
if [r for r in book.execute("SELECT 1 FROM sqlite_master WHERE type='table'"
                            " AND name='sessions'").fetchall()]:
    book.execute("DELETE FROM sessions")
# Yesterday evening, so the default one-a-day cadence already wants a fresh copy.
book.execute("INSERT INTO app_state(key, value) VALUES ('last_backup_at', ?)"
             " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
             (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 26 * 3600)),))
book.commit()
book.close()

WEAK = [("the everyguesser's first word", "password"),
        ("eight counting characters", "12345678"),
        ("the shop's own app name", "chrisphicshub"),
        ("only seven characters", "1111111"),
        ("digits alone", "0506399999"),
        ("a word anyone who watches the counter can type", "abcdefgh"),
        ("the trading name printed on the job sheet", SHOP_NAME + " 2026"),
        ("four letters twice", "aaaabbbb")]

MAC_DIR = os.path.join(work, "Backups")
MIRROR = os.path.join(work, "mirror")
os.makedirs(MIRROR, exist_ok=True)
fails = []
log = open(os.path.join(work, "server.log"), "wb")


def check(label, ok, detail=""):
    print("%-54s %s %s" % (label, "ok" if ok else "FAIL", detail))
    if not ok:
        fails.append(label)


def raw_call(method, path, payload=None, port=8899, headers=None, host=None):
    data = None if payload is None else json.dumps(payload).encode()
    heads = {"Content-Type": "application/json"}
    heads.update(headers or {})
    if host:
        heads["Host"] = host
    request = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=data,
                                     method=method, headers=heads)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as err:
        return err.code, err.read(), dict(err.headers)


def ask(method, path, payload=None, **kwargs):
    code, raw, _headers = raw_call(method, path, payload, **kwargs)
    try:
        return code, json.loads(raw.decode() or "{}")
    except ValueError:
        return code, {"error": (raw.decode("utf-8", "replace") or "")[:140]}


def start(port=8899, args=(), backup_dir=MAC_DIR, support=None, tag="main", calls="200",
          check_every="600"):
    env = dict(os.environ, CHRISPHICS_DB=CLONE, CHRISPHICS_SUPPORT=support or work,
               CHRISPHICS_BACKUP_DIR=backup_dir, PYTHONUNBUFFERED="1",
               CHRISPHICS_CALLS_PER_MINUTE=calls, CHRISPHICS_BACKUP_CHECK=check_every)
    handle = open(os.path.join(work, "server-%s.log" % tag), "wb")
    proc = subprocess.Popen(["/usr/bin/python3", os.path.join(REPO, "server.py"),
                             "--port", str(port), "--no-browser"] + list(args),
                            cwd=REPO, env=env, stdout=handle, stderr=handle)
    for _ in range(60):
        try:
            if ask("GET", "/healthz", port=port)[0] == 200:
                proc.chrisphics_log = handle
                return proc
        except Exception:
            pass
        time.sleep(0.15)
    raise SystemExit("the test server on port %d never answered; see server-%s.log" % (port, tag))


def stop(proc):
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    proc.chrisphics_log.close()


def folder_count(path):
    return len(os.listdir(path)) if os.path.isdir(path) else 0


def wait_for_copy(prefix, port=8899, within=25.0):
    """The watcher runs on its own clock, so a check that wants to prove its work has to wait for
    it rather than guess. Returns the report once a copy named prefix is written, checked, tidied
    and mirrored — a half-finished run would let the next count mean two copies."""
    deadline = time.time() + within
    while time.time() < deadline:
        report = ask("GET", "/api/backups", port=port)[1]
        if (report.get("file") or "").startswith(prefix) and "mirror done" in (report.get("note") or ""):
            return report
        time.sleep(0.2)
    return None


def trail(kind):
    rows = sqlite3.connect("file:%s?mode=ro" % CLONE, uri=True)
    rows.row_factory = sqlite3.Row
    found = [dict(r) for r in rows.execute(
        "SELECT kind, address, detail, at FROM security_log WHERE kind = ?"
        " ORDER BY id DESC LIMIT 5", (kind,)).fetchall()]
    rows.close()
    return found


def note_of(kind):
    rows = trail(kind)
    return rows[0]["detail"] if rows else ""


# ------------------------------------------------------------------ the shop's own Mac
print("\n--- how the book gets copied (loopback = the shop's own Mac)")
proc = start()
try:
    code, copy = ask("GET", "/api/backups")
    check("GET /api/backups answers with the copies", code == 200 and "folder" in copy,
          "%s · %s copies" % (copy.get("folder"), copy.get("copies")))
    check("automatic copying starts switched on", copy.get("auto") is True,
          "every %s h · keep %s nights" % (copy.get("every_hours"), copy.get("keep_days")))
    check("a book last copied yesterday is asking for a fresh one", copy.get("due") is True,
          "was %s h ago, next in %s h" % (copy.get("age_hours"), copy.get("next_in_hours")))
    check("a folder that is not even made yet is no copies, not an error",
          copy.get("copies") == 0 and copy.get("error") == "" and folder_count(MAC_DIR) == 0,
          "%r" % copy.get("error"))
    check("free space where the copies live is reported",
          isinstance(copy.get("free_gb"), (int, float)), "%s GB" % copy.get("free_gb"))
    check("the log table arrived with the schema", bool(sqlite3.connect(
        "file:%s?mode=ro" % CLONE, uri=True).execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='security_log'").fetchall()))

    for label, payload, phrase in [("a cadence of nought hours", {"every_hours": 0}, "1 to 168"),
                                   ("a cadence of 4000 hours", {"every_hours": 4000}, "1 to 168"),
                                   ("one night kept only", {"keep_days": 1}, "3 to 120"),
                                   ("121 nights kept", {"keep_days": 121}, "3 to 120"),
                                   ("a second folder that is not there",
                                    {"mirror": os.path.join(work, "nowhere")}, "not there yet"),
                                   ("the folder the copies already go to",
                                    {"mirror": MAC_DIR}, "already go to")]:
        code, answer = ask("PUT", "/api/backups", payload)
        check("refuses " + label, code == 400 and phrase in answer.get("error", ""),
              answer.get("error", "")[:58])

    code, saved = ask("PUT", "/api/backups", {"every_hours": 1, "keep_days": 9, "mirror": MIRROR})
    check("saves a new cadence, a new retention and a second folder",
          code == 200 and saved["every_hours"] == 1 and saved["keep_days"] == 9
          and saved["mirror"].endswith("mirror"), "mirror %s" % saved["mirror"])
    check("says which of the four it took", saved.get("saved") == ["every_hours", "keep_days", "mirror"],
          str(saved.get("saved")))
    check("the schedule is now overdue, one hour being long past", saved["due"] is True)

    # Nothing else asked for a copy, so the one that lands here was made by the server's own clock.
    woken = wait_for_copy("backup-")
    check("a cadence settled on the shop's Mac is taken up at once, not when the old gap ends",
          woken is not None, (woken or {}).get("file") or "no copy of its own within 25 s")
    check("the copy it made itself is named as an automatic one",
          woken is not None and woken["file"].startswith("backup-")
          and woken["recent"][0]["kind"] == "backup",
          "%s · %s" % (woken.get("file"), (woken.get("recent") or [{}])[0].get("kind")))
    check("and it went to the second folder as well",
          woken is not None and woken["file"] in os.listdir(MIRROR), str(os.listdir(MIRROR)))
    check("after that copy nothing is due again", woken is not None and woken["due"] is False,
          "next in %s h" % (woken or {}).get("next_in_hours"))

    count_before = folder_count(MAC_DIR)
    code, after = ask("POST", "/api/backups")
    check("POST /api/backups writes a copy on the spot",
          code == 200 and folder_count(MAC_DIR) == count_before + 1,
          "was %d, now %d" % (count_before, folder_count(MAC_DIR)))
    check("that copy was opened and counted against the book", "checked" in (after["note"] or ""),
          after["note"][:64])
    check("the second folder took the same copy", after["file"] in os.listdir(MIRROR),
          str(os.listdir(MIRROR)))
    check("a fresh copy is not due again", after["due"] is False, "next in %s h" % after["next_in_hours"])
    check("the copy is named in the book as an asked-for one",
          after["file"] == after["recent"][0]["name"] and after["recent"][0]["kind"] == "manual",
          "%s · %s" % (after["file"], after["recent"][0]["kind"]))
    check("the newest copies are listed with their size",
          bool(after["recent"]) and after["recent"][0]["bytes"] > 0,
          "%d listed, newest %d bytes" % (len(after["recent"]), after["recent"][0]["bytes"]))

    code, answer = ask("PUT", "/api/backups", {"auto": False})
    check("the shop can switch the automatic copying off",
          code == 200 and answer["auto"] is False and answer["due"] is False)
    ask("PUT", "/api/backups", {"auto": True})
    check("and switch it back on", ask("GET", "/api/backups")[1]["auto"] is True)
    check("the shop's own Mac reads the copies through /api/shop too",
          "every_hours" in ask("GET", "/api/shop")[1]["backup"])
finally:
    stop(proc)

# ------------------------------------------------- a running server makes its own copies
print("\n--- a server that is simply running makes the copies")
alone = os.path.join(work, "alone")
os.makedirs(alone, exist_ok=True)
alone_book = sqlite3.connect(CLONE)
alone_book.execute("UPDATE app_state SET value = ? WHERE key='last_backup_at'",
                   (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 3 * 3600)),))
alone_book.commit()
alone_book.close()
quiet_dir = os.path.join(alone, "Backups")
proc = start(port=8897, backup_dir=quiet_dir, support=alone, tag="alone", check_every="2")
made = False
for _ in range(40):
    time.sleep(0.5)
    made = os.path.isdir(quiet_dir) and len(os.listdir(quiet_dir)) > 0
    if made:
        break
answer = ask("GET", "/api/backups", port=8897)[1]
stop(proc)
check("a running server copies an overdue book without being told", made,
      made and answer.get("note", "")[:52] or "nothing appeared within 20 s")
check("the copy it made on its own is named as an automatic one",
      made and answer["file"].startswith("backup-"), str(answer.get("file")))
check("and the book was not left looking overdue", answer.get("due") is False,
      "next in %s h" % answer.get("next_in_hours"))

# ------------------------------------------------------- the rules of signing in, on this Mac
print("\n--- the rules of signing in (settled on the shop's own Mac)")
proc = start()
try:
    code, guard = ask("GET", "/api/security")
    check("GET /api/security answers", code == 200 and guard.get("rounds", 0) >= 200000,
          "%s rounds · %s days signed in" % (guard.get("rounds"), guard.get("session_days")))
    check("this Mac is let through on its own, until told otherwise",
          guard["loopback_open"] is True and guard["shop_mac_signin"] is False)
    check("the hash is deliberately slow", guard["rounds"] >= 600000, "%s" % guard["rounds"])

    for label, payload, phrase in [("a sign-in of nought days", {"session_days": 0}, "1 to 90"),
                                   ("a sign-in of 400 days", {"session_days": 400}, "1 to 90"),
                                   ("a sign-in asked in words", {"session_days": "soon"},
                                    "whole number")]:
        code, answer = ask("PUT", "/api/security", payload)
        check("refuses " + label, code == 400 and phrase in answer.get("error", ""),
              answer.get("error", "")[:56])
    code, answer = ask("PUT", "/api/security", {"shop_mac_signin": True})
    check("refuses to ask this Mac for a password there is none of yet",
          code == 400 and not trail("shop_mac_signin"), answer.get("error", "")[:56])

    code, saved = ask("PUT", "/api/security", {"session_days": 2})
    check("a device can be told to ask again after two days",
          code == 200 and saved["session_days"] == 2, "days %s" % saved.get("session_days"))

    for label, word in WEAK:
        code, answer = ask("POST", "/api/shop-password", {"password": word})
        check("refuses " + label, code == 400, answer.get("error", "")[:66])
    check("and none of those left a password in the book",
          ask("GET", "/api/security")[1]["chosen"] is False)

    code, answer = ask("POST", "/api/shop-password", {"password": GOOD})
    check("accepts a strong first password and signs every device out",
          code == 200 and answer.get("password_set") and answer.get("devices") == 0)
    code, answer = ask("POST", "/api/shop-password", {"password": BETTER, "current": "not it"})
    check("refuses to replace a password without the one before it", code == 400,
          answer.get("error", "")[:52])
    check("and the refusal is written down", bool(trail("wrong_current")),
          note_of("wrong_current")[:52])
    record = [r[0] for r in sqlite3.connect("file:%s?mode=ro" % CLONE, uri=True).execute(
        "SELECT value FROM app_state WHERE key = 'shop_password'").fetchall()]
    head, rounds, salt, digest = record[0].split("$") if record else ("", "", "", "")
    check("the book keeps a salted hash, never the word",
          record and head == "pbkdf2_sha256" and rounds == "600000" and GOOD not in record[0],
          "%s · %s rounds · %s" % (head, rounds, salt[:8] + "…"))
    check("that hash is the one this password turns into",
          record and hashlib.pbkdf2_hmac("sha256", GOOD.encode(), bytes.fromhex(salt),
                                         int(rounds)).hex() == digest)

    code, answer = ask("PUT", "/api/security", {"shop_mac_signin": True})
    check("this Mac can be told to sign in like every other device",
          code == 200 and answer["shop_mac_signin"] is True)
    check("and is turned away until it does", ask("GET", "/api/bootstrap")[0] == 401)
    code, _body, headers = raw_call("POST", "/api/login", {"password": GOOD})
    signed_here = [v for k, v in headers.items() if k.lower() == "set-cookie"]
    check("the shop's Mac can sign in the same way a phone does",
          code == 200 and bool(signed_here))
    check("the password screen hands a cookie that lasts two days",
          bool(signed_here) and "Max-Age=%d" % (2 * 86400) in signed_here[0],
          "Max-Age %s" % (signed_here[0].split("Max-Age=")[1].split(";")[0] if signed_here else "-"))
    here = {"Cookie": signed_here[0].split(";")[0]} if signed_here else {}
    code, answer = ask("PUT", "/api/security", {"shop_mac_signin": False}, headers=here)
    check("and the signed-in Mac can put itself back the way it was", code == 200,
          answer.get("error", "")[:52])
    check("switching that back off lets this Mac through again", ask("GET", "/api/bootstrap")[0] == 200)

    code, answer = ask("POST", "/api/backups", {}, headers={"Origin": "https://somewhere-else.test"})
    check("a page on another website cannot make the book do things",
          code == 403 and "another page" in answer.get("error", "").lower()
          or code == 403, answer.get("error", "")[:58])
    check("and the book wrote down that it was tried", bool(trail("cross_origin")),
          note_of("cross_origin")[:60])
    code, answer = ask("POST", "/api/backups", {},
                       headers={"Origin": "http://127.0.0.1:8899"})
    check("its own page may still ask for a copy", code == 200, str(answer.get("file")))

    # A browser does not open a new connection for the next call, so whatever a refusal leaves
    # unread on the wire would be taken for that call's request line.
    keep = http.client.HTTPConnection("127.0.0.1", 8899, timeout=10)
    keep.request("POST", "/api/backups", body=b'{"every_hours": 3}',
                 headers={"Content-Type": "application/json", "Origin": "https://somewhere-else.test"})
    refused = keep.getresponse()
    refused_code, refused_text = refused.status, refused.read().decode()
    keep.request("GET", "/api/shop", headers={"Content-Type": "application/json"})
    follow = keep.getresponse()
    follow_code, follow_text = follow.status, follow.read().decode()
    keep.close()
    check("a refused call leaves the shop's next call on that connection clean",
          refused_code == 403 and follow_code == 200 and '"shop"' in follow_text,
          "%s then %s%s" % (refused_code, follow_code, "" if follow_code == 200 else
                            " — " + (follow_text[:40] or "")))
finally:
    stop(proc)

# -------------------------------------------------------------------- a device on the Wi-Fi
print("\n--- a device out on the shop Wi-Fi")
wifi_book = sqlite3.connect(CLONE)
wifi_book.execute("DELETE FROM sessions")
# The shop's own Mac has since been told to sign in like every other device, and no device is
# left inside the book from the phase before this one.
wifi_book.execute("INSERT INTO app_state(key, value) VALUES ('shop_mac_login', '1')"
                  " ON CONFLICT(key) DO UPDATE SET value='1'")
wifi_book.commit()
wifi_book.close()
device = start(port=8898, args=("--trust-proxy",), tag="wifi", calls="20")
PHONE = {"X-Forwarded-For": "192.168.100.44"}
OTHER = {"X-Forwarded-For": "192.168.100.77"}
SECOND = {"X-Forwarded-For": "192.168.100.88"}
try:
    code, answer = ask("GET", "/api/bootstrap", port=8898, headers=PHONE)
    check("a stranger's first look at the accounts is refused",
          code == 401 and "sign in" in answer.get("error", "").lower(), answer.get("error", "")[:56])
    check("the refusal is remembered", bool(trail("turned_away")), note_of("turned_away")[:56])

    for n in range(1, 5):
        code, answer = ask("POST", "/api/login", {"password": "guessed-%d" % n},
                           port=8898, headers=PHONE)
        check("wrong password %d is answered as wrong" % n, code == 401, answer.get("error", "")[:40])
    code, answer, headers = raw_call("POST", "/api/login", {"password": "guessed-5"},
                                     port=8898, headers=PHONE)
    check("the fifth wrong password makes that device wait",
          code == 429 and int(headers.get("Retry-After", "0")) >= 30,
          "Retry-After %s" % headers.get("Retry-After", "-"))
    check("the wait is written down", bool(trail("locked_out")), note_of("locked_out")[:44])
    code, answer = ask("POST", "/api/login", {"password": GOOD}, port=8898, headers=PHONE)
    check("even the right password is not looked at while it waits", code == 429,
          answer.get("error", "")[:52])

    code, _body, headers = raw_call("POST", "/api/login", {"password": GOOD},
                                    port=8898, headers=OTHER)
    cookies = [v for k, v in headers.items() if k.lower() == "set-cookie"]
    flags = cookies[0] if cookies else ""
    cookie = flags.split(";")[0] if flags else ""
    check("the right password opens the book for that device", code == 200 and bool(cookie))
    check("the cookie is HttpOnly and SameSite=Strict",
          "HttpOnly" in flags and "SameSite=Strict" in flags,
          " ".join(p for p in flags.split(";")[1:3]))
    check("and Secure, because a proxy stands in front of it over HTTPS", "Secure" in flags)
    check("the sign-in is written down with the address it came from",
          bool(trail("signed_in")), note_of("signed_in")[:44])
    in_book = {"Cookie": cookie}

    code, answer = ask("PUT", "/api/security", {"session_days": 9}, port=8898, headers=in_book)
    check("a phone cannot change how long a sign-in lasts",
          code == 403 and "shop's own computer" in answer.get("error", ""), answer.get("error", "")[:56])
    code, answer = ask("PUT", "/api/backups", {"every_hours": 2}, port=8898, headers=in_book)
    check("a phone cannot change how often the book is copied", code == 403,
          answer.get("error", "")[:56])
    code, answer = ask("GET", "/api/security", port=8898, headers=in_book)
    check("a phone can read the rules it is signing in under",
          code == 200 and answer["session_days"] == 2 and answer["loopback_open"] is False)

    code, _body, headers = raw_call("POST", "/api/login", {"password": GOOD},
                                    port=8898, headers=SECOND)
    second_cookie = [v for k, v in headers.items() if k.lower() == "set-cookie"][0].split(";")[0]
    code, answer = ask("GET", "/api/devices", port=8898, headers=in_book)
    check("two phones can be signed in at once", code == 200 and len(answer["devices"]) == 2,
          " · ".join(d["device"] for d in answer["devices"]))
    check("each one is named by its kind, not its person",
          all(d["device"].count("·") == 1 for d in answer["devices"]))
    code, answer = ask("DELETE", "/api/devices/0", port=8898, headers=in_book)
    check("a device number that is not there is answered honestly", code == 404,
          answer.get("error", "")[:44])
    code, answer = ask("DELETE", "/api/devices", port=8898, headers=in_book)
    check("one call throws out every other device",
          code == 200 and answer["devices"] == 1 and answer["signed_out"],
          "%s left" % answer.get("devices"))
    check("and writes down who did it", bool(trail("signed_out_all")), note_of("signed_out_all")[:44])
    check("the thrown-out phone is turned away again",
          ask("GET", "/api/bootstrap", port=8898, headers={"Cookie": second_cookie})[0] == 401)

    # One machine leaning on the book hard — its own address, so the shop's phones are unaffected.
    HAMMER = dict(in_book)
    HAMMER["X-Forwarded-For"] = "192.168.100.99"
    codes, waited = [], {}
    for _ in range(34):
        code, _body, headers = raw_call("GET", "/api/bootstrap", port=8898, headers=HAMMER)
        codes.append(code)
        if code == 429 and not waited:
            waited = headers
    first_429 = codes.index(429) + 1 if 429 in codes else None
    check("a burst past the ceiling is answered with a wait, not a freeze",
          429 in codes and first_429 == 21,
          "%d calls, first wait at %d of %d" % (len(codes), first_429 or -1, len(codes)))
    check("the ceiling it crossed is the one the book says it uses",
          ask("GET", "/api/security", port=8898, headers=in_book)[1]["calls_per_minute"] == 20)
    check("and it is told how long to wait", int(waited.get("Retry-After", "0")) > 0,
          "Retry-After %s" % waited.get("Retry-After", "-"))
    check("the throttling is written down once, not per call", len(trail("throttled")) == 1,
          note_of("throttled")[:44])

    code, _body, headers = raw_call("GET", "/healthz", port=8898, host="shop.example.com")
    hsts = [v for k, v in headers.items() if k.lower() == "strict-transport-security"]
    check("a named address is told to stay on HTTPS", bool(hsts), hsts[0] if hsts else "no header")
    code, _body, headers = raw_call("GET", "/healthz", port=8898, host="192.168.100.29:8898")
    check("a bare IP address is not, because a browser ignores it there",
          not [v for k, v in headers.items() if k.lower() == "strict-transport-security"])
    server = [v for k, v in headers.items() if k.lower() == "server"]
    check("the Server header says nothing about this Mac's Python",
          bool(server) and "Python" not in server[0], server[0] if server else "missing")

    code, answer = ask("POST", "/api/shop-password", {"password": BETTER, "current": GOOD},
                       port=8898, headers=in_book)
    check("a signed-in device may change the shop password",
          code == 200 and answer["devices"] == 0, "%s left" % answer.get("devices"))
    check("that change signs every device out, this one included",
          ask("GET", "/api/bootstrap", port=8898, headers=in_book)[0] == 401)
    check("and the book says which device did it", bool(trail("password_set")),
          note_of("password_set")[:44])
    check("the old password no longer opens anything",
          ask("POST", "/api/login", {"password": GOOD}, port=8898, headers=OTHER)[0] == 401)
    code, _body, headers = raw_call("POST", "/api/login", {"password": BETTER},
                                    port=8898, headers=OTHER)
    fresh = [v for k, v in headers.items() if k.lower() == "set-cookie"]
    check("the new one does", code == 200 and bool(fresh))
    code, answer = ask("DELETE", "/api/shop-password", {}, port=8898,
                       headers=dict(OTHER, **{"Cookie": fresh[0].split(";")[0]} if fresh else {}))
    check("a phone cannot switch the whole book open", code == 403, answer.get("error", "")[:56])
finally:
    stop(device)

# ------------------------------------------------------------------------- what it cost
print("\n--- what the checks cost the records")
after = sqlite3.connect("file:%s?mode=ro" % CLONE, uri=True)
moved = ["%s %d->%d" % (table, count, after.execute("SELECT count(*) FROM %s" % table).fetchone()[0])
         for table, count in BEFORE.items()
         if after.execute("SELECT count(*) FROM %s" % table).fetchone()[0] != count]
after.close()
check("no job, client, payment, expense or message moved", not moved, ", ".join(moved))
check("nothing in the copies folder was left uncounted",
      all(n.endswith(".db") for n in os.listdir(MAC_DIR)), str(sorted(os.listdir(MAC_DIR))[:2]))

after_stat = os.stat(LIVE)
same = (after_stat.st_size == live_stat.st_size and after_stat.st_mtime == live_stat.st_mtime)
print("\nLive book: %d bytes, last written %s -- %s" % (
    after_stat.st_size, time.ctime(after_stat.st_mtime),
    "untouched" if same else "CHANGED, WHICH MUST NOT HAPPEN"))
if not same:
    fails.append("the shop's own book was written to")
log.close()
shutil.rmtree(work, ignore_errors=True)

print("\n%s" % ("all checks passed" if not fails else "FAILED: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
