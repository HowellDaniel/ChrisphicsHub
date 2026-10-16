#!/usr/bin/env python3
"""Prove the hosted book is safe and sane before it ever faces the internet.

The dashboard has to answer when this Mac is switched off, which means it has to live on somebody
else's machine, in somebody else's timezone, reached by strangers instead of by the shop's own
Wi-Fi. Three things follow, and each one is a failure this file drives into the ground:

  1. a book with no sign-in must refuse to start rather than serve the accounts to the world;
  2. it must keep Accra's clock — every figure in the book is read off the server's own time, and
     Render's machines sit seven to eight hours behind Ghana, which would file Tuesday's takings
     under Monday and shut the shop at noon;
  3. loopback must stop proving anything, because behind a proxy every visitor arrives as loopback.

Everything here runs the start command parsed out of render.yaml itself, so the check goes stale
the moment the blueprint changes rather than a day after it. The live book is opened read-only and
never written; the hosted cases run against throwaway empty books in a temp folder with their own
support and backup directories, on a port claimed first. A sign-in word is generated here for the
throwaway books and is never the shop's.

    tools/check-hosted-book.py
"""
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(REPO, "server.py")
BLUEPRINT = os.path.join(REPO, "render.yaml")
LIVE = os.path.expanduser("~/Library/Application Support/Chrisphics Hub/chrisphics.db")

PORT = 8931                                   # this check alone
WORD = "throwaway-%s" % os.urandom(4).hex()   # for the temp books only, never the shop's

total = 0
fails = []


def check(label, ok, detail=""):
    global total
    total += 1
    print("%-62s %s %s" % (label, "ok" if ok else "FAIL", detail))
    if not ok:
        fails.append(label)


probe = socket.socket()
try:
    probe.bind(("127.0.0.1", PORT))
except OSError as err:
    print("port %d is already held (%s) — this check cannot trust what it sees there." % (PORT, err))
    raise SystemExit(2)
finally:
    probe.close()

before = open(LIVE, "rb").read()

# ---------------------------------------------------------------- the blueprint, as it is written
text = open(BLUEPRINT).read()
matched = re.search(r"startCommand:\s*\"?([^\"\n]+)\"?", text)
start = matched.group(1).strip() if matched else ""
tokens = shlex.split(start)
env_in_file = dict(re.findall(r"-\s*key:\s*(\S+)\s*\n\s*value:\s*\"?([^\"\n]*)\"?", text))
secret_line = re.search(r"-\s*key:\s*CHRISPHICS_AUTH_PASSWORD\s*\n\s*(\S+)", text)

check("the blueprint starts with this very file's engine",
      len(tokens) > 1 and tokens[1] == "server.py", start)
check("it starts behind the proxy, not with its own certificate",
      "--trust-proxy" in tokens and "--tls-cert" not in tokens, "")
check("it listens for the provider, on the port the provider hands it",
      "--host 0.0.0.0" in start and "$PORT" in start, "")
check("the open-door flag is nowhere in the blueprint",
      "allow-unauthenticated-lan" not in text and "allow-insecure-lan" not in text, "")
check("the book lives on the persistent disk, not the ephemeral one",
      env_in_file.get("CHRISPHICS_DB", "").startswith("/var/data/")
      and env_in_file.get("CHRISPHICS_BACKUP_DIR", "").startswith("/var/data/"),
      "%s" % env_in_file.get("CHRISPHICS_DB"))
check("the blueprint names the shop's own zone",
      env_in_file.get("CHRISPHICS_TZ") == "GMT", env_in_file.get("CHRISPHICS_TZ", "absent"))
check("the sign-in word is left for the shop to type, not written in the file",
      secret_line is not None and secret_line.group(1).startswith("sync:")
      and "value" not in secret_line.group(1),
      secret_line.group(1) if secret_line else "no CHRISPHICS_AUTH_PASSWORD entry")

# The start command, with the two things only this run knows.
tokens[0] = sys.executable
tokens[1] = SERVER
tokens[tokens.index("$PORT")] = str(PORT)

work = tempfile.mkdtemp(prefix="chrisphics-hosted-")
SUPPORT = os.path.join(work, "support")
BACKUPS = os.path.join(work, "Backups")
os.makedirs(SUPPORT, exist_ok=True)
os.makedirs(BACKUPS, exist_ok=True)


def boot(book, flags=None, env_extra=None):
    """Start the engine the way the blueprint says to, with the environment the blueprint declares."""
    env = dict(os.environ, CHRISPHICS_DB=os.path.join(work, book), CHRISPHICS_SUPPORT=SUPPORT,
               CHRISPHICS_BACKUP_DIR=BACKUPS, PYTHONUNBUFFERED="1")
    if "CHRISPHICS_TZ" in env_in_file:
        env["CHRISPHICS_TZ"] = env_in_file["CHRISPHICS_TZ"]
    env.update(env_extra or {})
    log = os.path.join(work, "%s.log" % book)
    handle = open(log, "w")
    child = subprocess.Popen(flags if flags is not None else list(tokens), env=env, cwd=REPO,
                             stdout=handle, stderr=subprocess.STDOUT)
    return child, log


def get(path, timeout=6, cookie=None):
    headers = {"Cookie": cookie} if cookie else {}
    try:
        with urllib.request.urlopen(urllib.request.Request(
                "http://127.0.0.1:%d%s" % (PORT, path), headers=headers), timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8")), r.headers
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8")
        try:
            return err.code, json.loads(body), err.headers
        except ValueError:
            return err.code, {"raw": body[:120]}, err.headers
    except Exception as err:  # noqa: BLE001 - a box that will not answer is itself an answer
        return 0, {"error": "%s" % type(err).__name__}, {}


def health(timeout=25):
    """Poll until the box answers /healthz, and say what it says about its own clock."""
    cutoff = time.time() + timeout
    while time.time() < cutoff:
        code, body, _ = get("/healthz", timeout=3)
        if code == 200 and body.get("ok"):
            return body
        time.sleep(0.4)
    return None


def utc_clock():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def down(child):
    child.terminate()
    try:
        child.wait(timeout=20)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=20)


# ---------------------------------------------------------------- 1. no sign-in, no service
# A brand-new disk holds an empty book, which is what Render mounts on the first boot. With no
# CHRISPHICS_AUTH_PASSWORD the box must refuse to serve rather than open the accounts to the world.
child, log = boot("nosignin.db", env_extra={"CHRISPHICS_TZ": "GMT"})
child.wait(timeout=40)
out = open(log).read()
check("a hosted book with no sign-in refuses to start at all",
      child.returncode != 0 and health(2) is None, "exit=%s" % child.returncode)
check("it names the password as the reason rather than throwing a stack trace",
      "needs a password" in out and "Traceback" not in out,
      out.strip().splitlines()[-1][:110] if out.strip() else "no output")
check("nothing is left listening on the port",
      socket.socket().connect_ex(("127.0.0.1", PORT)) != 0, "")

# ---------------------------------------------------------------- 2. Accra's clock, not the host's
child, log = boot("accra.db", env_extra={"CHRISPHICS_TZ": "GMT", "CHRISPHICS_AUTH_PASSWORD": WORD})
body = health()
check("with a sign-in the same blueprint boots and passes its own health check",
      body is not None, "" if body else open(log).read().strip()[-140:])
if body:
    skew = abs((utc_clock() - datetime.fromisoformat(body["now"])).total_seconds())
    check("it reports the shop's zone, not the provider's",
          body.get("timezone") == "GMT", body.get("timezone", "not reported"))
    check("its own clock reads the same minute as Greenwich",
          skew < 120, "%s against %s (%.0fs apart)" % (body.get("now"),
                                                       utc_clock().isoformat(timespec="minutes"), skew))
else:
    check("it reports the shop's zone, not the provider's", False, "the box never answered")
    check("its own clock reads the same minute as Greenwich", False, "the box never answered")
down(child)

# The same command with the zone taken back to the provider's own, so the difference above is
# proven to be that one setting and not this Mac happening to sit on Greenwich already.
child, log = boot("la.db", env_extra={"CHRISPHICS_TZ": "America/Los_Angeles",
                                      "CHRISPHICS_AUTH_PASSWORD": WORD})
body = health()
if body:
    behind = (utc_clock() - datetime.fromisoformat(body["now"])).total_seconds() / 3600.0
    check("set the provider's own zone and the same box drifts 7-8 hours off Accra",
          6.5 <= behind <= 8.5, "%+.1f hours behind Greenwich" % behind)
    check("so the answer above came from CHRISPHICS_TZ and not from luck",
          body.get("timezone") == "America/Los_Angeles", body.get("timezone", ""))
else:
    check("set the provider's own zone and the same box drifts 7-8 hours off Accra", False,
          "no answer: " + open(log).read().strip()[-120:])
    check("so the answer above came from CHRISPHICS_TZ and not from luck", False, "no answer")
down(child)

# SQLite's 'localtime' is a second clock, and it is the one most of the book's figures actually
# ask. One line has to move both, or the ledger and the dashboard disagree about what day it is.
probe = subprocess.run(
    [sys.executable, "-c",
     "import os,sys,sqlite3,datetime as d;"
     "sys.path.insert(0,%r);import server;"
     "print(d.datetime.now().isoformat(timespec='minutes'));"
     "print(sqlite3.connect(':memory:').execute(\"select datetime('now','localtime')\").fetchone()[0])"
     % REPO],
    env=dict(os.environ, CHRISPHICS_TZ="America/Los_Angeles",
             CHRISPHICS_DB=os.path.join(work, "imported.db")),
    capture_output=True, text=True, timeout=90)
lines = [value.strip() for value in probe.stdout.splitlines() if value.strip()]
if len(lines) == 2:
    # Python answers `2026-10-16T15:43` and SQLite `2026-10-16 15:43:51`; both have to be read as the
    # same instant before the two clocks can be compared at all.
    py = datetime.fromisoformat(lines[0])
    sq = datetime.fromisoformat(lines[1].replace(" ", "T"))
    check("Python's clock and SQLite's move on the one setting",
          abs((py - sq).total_seconds()) < 90, "%s against %s" % (lines[0], lines[1]))
    check("and datetime('now','localtime') follows it, so the ledger days move with it",
          6.5 <= (utc_clock() - sq).total_seconds() / 3600.0 <= 8.5, lines[1])
else:
    check("Python's clock and SQLite's move on the one setting", False,
          (probe.stdout + probe.stderr)[-140:])
    check("and datetime('now','localtime') follows it, so the ledger days move with it", False,
          "no output")

# ---------------------------------------------------------------- 3. the door, with strangers outside
child, log = boot("served.db", env_extra={"CHRISPHICS_TZ": "GMT", "CHRISPHICS_AUTH_PASSWORD": WORD})
health()
code, body, _ = get("/api/session")
check("a hosted box says a sign-in is required",
      code == 200 and body.get("required") is True, str(body)[:90])
check("behind the proxy, arriving from loopback is not the shop's own Mac",
      code == 200 and body.get("authenticated") is False
      and body.get("this_is_the_shop_computer") is False, str(body)[:120])
code, body, _ = get("/api/shop")
check("and a call for the book with no sign-in is turned away",
      code == 401, "%d %s" % (code, str(body)[:70]))
turned = subprocess.run(["sqlite3", os.path.join(work, "served.db"),
                         "select kind, detail from security_log order by id desc limit 1;"],
                        capture_output=True, text=True, timeout=30).stdout.strip()
check("the turning-away is written down in the book's own security log",
      "turned_away" in turned and "/api/shop" in turned, turned[:90])

cookie, signed = "", False
try:
    with urllib.request.urlopen(urllib.request.Request(
            "http://127.0.0.1:%d/api/login" % PORT, data=json.dumps({"password": WORD}).encode(),
            headers={"Content-Type": "application/json"}), timeout=15) as r:
        signed = r.status == 200
        cookie = r.headers.get("Set-Cookie") or ""
except urllib.error.HTTPError as err:
    cookie, detail = "", "HTTP %d" % err.code
else:
    detail = ""
check("the shop's own word opens the hosted book", signed, detail)
check("the sign-in cookie is HttpOnly and marked Secure across the proxy",
      "HttpOnly" in cookie and "Secure" in cookie, cookie[:90])
if signed and cookie:
    code, body, _ = get("/api/shop", cookie=cookie.split(";")[0])
    check("and once signed in the book answers over the proxy", code == 200,
          "%d %s" % (code, str(body)[:70]))
else:
    check("and once signed in the book answers over the proxy", False, "never signed in")
down(child)

# ---------------------------------------------------------------- 4. the flag that opens everything
# `0.0.0.0` counts as private to Python because it means "this network", so the flag written for a
# shop's Wi-Fi would have bound every interface and handed the accounts to the internet unsigned.
flags = list(tokens)
flags.insert(flags.index("--trust-proxy"), "--allow-unauthenticated-lan")
flags.pop(flags.index("--trust-proxy"))
child, log = boot("flagged.db", flags=flags, env_extra={"CHRISPHICS_TZ": "GMT"})
child.wait(timeout=40)
out = open(log).read()
check("--allow-unauthenticated-lan refuses to bind every interface",
      child.returncode != 0 and "private Wi-Fi IPv4" in out,
      out.strip().splitlines()[-1][:100] if out.strip() else "no output")

after = open(LIVE, "rb").read()
check("the live book is exactly the bytes this check started with", after == before,
      "%d bytes" % len(after))

shutil.rmtree(work, ignore_errors=True)
print()
print("%d checks, %d failed" % (total, len(fails)))
for name in fails:
    print("  FAILED: %s" % name)
raise SystemExit(1 if fails else 0)
