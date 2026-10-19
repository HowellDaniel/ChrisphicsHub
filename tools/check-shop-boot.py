#!/usr/bin/env python3
"""Prove the shop comes online by itself when this Mac is switched on, and stays online when the
router hands out a different number while the counter is open.

tools/shop-boot.sh is the part that cannot be tested by reading it: its whole job is to survive a
Wi-Fi that arrives late, an address that moves under it, and a stop that must not leave an orphan
holding the port. Each scenario below runs the real keeper against a throwaway copy of the book, a
throwaway support folder and a throwaway certificate, and watches what it does.

The live book is opened read-only and copied with Connection.backup; its bytes are compared before
and after. The live LaunchAgent, the live certificate and the shop's own port are only ever read —
the keeper under test is pointed at a fake helper through CHRISPHICS_SHOP_HELPER, so no run of this
file can re-issue the shop's certificate. A sign-in word is generated here for the throwaway copy
and is never the shop's.
"""
import os
import plistlib
import shutil
import socket
import sqlite3
import ssl
import subprocess
import tempfile
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEEPER = os.path.join(REPO, "tools", "shop-boot.sh")
HELPER = os.path.join(REPO, "tools", "shop-server.sh")
PLIST = os.path.expanduser("~/Library/LaunchAgents/com.chrisphics.shop-server.plist")
GUARD_PLIST = os.path.expanduser("~/Library/LaunchAgents/com.chrisphics.shop-guard.plist")
LIVE = os.path.expanduser("~/Library/Application Support/Chrisphics Hub/chrisphics.db")
SERVED = os.path.expanduser("~/Applications/Chrisphics Hub")
SHOP_PORT = 8834

# Ports for this check alone. A port somebody else is holding turns every assertion below into a
# lie, so each is claimed here first and the run stops out loud if it is not free.
PORT_EARLY = 8911      # the Wi-Fi has not arrived yet
PORT_MOVE = 8912       # the address moves while the shop is serving
PORT_ALONE = 8913      # a second keeper must refuse to start an engine

work = tempfile.mkdtemp(prefix="chrisphics-shop-boot-")
CLONE = os.path.join(work, "clone.db")
src = sqlite3.connect("file:%s?mode=ro" % LIVE, uri=True)
dst = sqlite3.connect(CLONE)
src.backup(dst)
dst.close()
src.close()
before = open(LIVE, "rb").read()

subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                "-subj", "/CN=throwaway", "-addext", "subjectAltName=IP:192.168.100.11",
                "-keyout", os.path.join(work, "k.pem"), "-out", os.path.join(work, "c.pem")],
               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

CTX = ssl._create_unverified_context()
total = 0
fails = []


def check(label, ok, detail=""):
    global total
    total += 1
    print("%-58s %s %s" % (label, "ok" if ok else "FAIL", detail))
    if not ok:
        fails.append(label)


def claim(port):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
    except OSError as err:
        print("port %d is already held (%s) — this check cannot trust what it sees there." % (port, err))
        raise SystemExit(2)
    finally:
        s.close()


for _port in (PORT_EARLY, PORT_MOVE, PORT_ALONE):
    claim(_port)


def wifi_address():
    return subprocess.run(["bash", HELPER, "wifi-ip"], capture_output=True, text=True).stdout.strip()


def fake_helper(path, address_file, log):
    """The stand-in shop-server.sh. It answers the address question from the same file the keeper
    is made to watch, and writes down every re-certification it is asked for."""
    with open(path, "w") as handle:
        handle.write("""#!/bin/bash
set -uo pipefail
case "${1:-}" in
  wifi-ip) if [ -f "%(ipfile)s" ]; then head -n 1 "%(ipfile)s" | tr -d ' \\t\\n'; fi ;;
  certs)   printf 'certs\\n' >> "%(log)s" ;;
  *)       exit 2 ;;
esac
""" % {"ipfile": address_file, "log": log})
    os.chmod(path, 0o755)


def start_keeper(port, address_file, helper):
    """Run the keeper the way launchd does: /bin/bash, the shop flags, no --host of its own."""
    log = os.path.join(work, "keeper-%d.log" % port)
    handle = open(log, "wb")
    env = dict(os.environ,
               CHRISPHICS_DB=CLONE,
               CHRISPHICS_SUPPORT=os.path.join(work, "support-%d" % port),
               CHRISPHICS_BACKUP_DIR=os.path.join(work, "backups-%d" % port),
               CHRISPHICS_AUTH_PASSWORD="throwaway-" + os.urandom(8).hex(),
               CHRISPHICS_SHOP_HELPER=helper,
               CHRISPHICS_SHOP_BOOT_IP_FILE=address_file,
               CHRISPHICS_SHOP_BOOT_WATCH="1", CHRISPHICS_SHOP_BOOT_WAIT="1",
               PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(["/bin/bash", KEEPER, "--port", str(port),
                             "--tls-cert", os.path.join(work, "c.pem"),
                             "--tls-key", os.path.join(work, "k.pem"),
                             "--allow-unauthenticated-lan", "--no-browser", "--allow-sleep"],
                            cwd=REPO, env=env, stdout=handle, stderr=handle)
    return proc, log, handle


def read(log):
    try:
        with open(log, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def health(address, port):
    try:
        with urllib.request.urlopen("https://%s:%d/healthz" % (address, port),
                                    timeout=2, context=CTX) as res:
            return b'"ok"' in res.read()
    except Exception:  # noqa: BLE001 - anything that is not an answer means "not serving"
        return False


def serving(address, port, seconds=25):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if health(address, port):
            return True
        time.sleep(0.5)
    return False


def wait_for(log, needle, seconds=15):
    """The keeper writes its own line about an attempt a moment after deciding to make it, so an
    assertion read straight after a move races the log rather than the behaviour."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if needle in read(log):
            return True
        time.sleep(0.4)
    return False


def held(address, port):
    s = socket.socket()
    s.settimeout(1)
    try:
        return s.connect_ex((address, port)) == 0
    finally:
        s.close()


def stop(proc, handle):
    if proc.poll() is None:
        proc.terminate()
        for _ in range(40):
            if proc.poll() is not None:
                break
            time.sleep(0.25)
        if proc.poll() is None:
            proc.kill()
    handle.close()


ADDRESS = wifi_address()
IP_FILE = os.path.join(work, "address.txt")
CERT_CALLS = os.path.join(work, "helper-calls.log")
FAKE = os.path.join(work, "fake-shop-server.sh")
fake_helper(FAKE, IP_FILE, CERT_CALLS)

check("this Mac has a shop Wi-Fi address to bind", bool(ADDRESS), ADDRESS)
if not ADDRESS:
    print("nothing to bind — join this Mac to the shop network and run this again.")
    shutil.rmtree(work, ignore_errors=True)
    raise SystemExit(2)

# 1. Power on before the Wi-Fi has finished joining. The keeper waits, then serves on its own, and
#    re-certifies because the leaf names the number the devices type.

with open(IP_FILE, "w") as handle:            # the router has not answered yet
    handle.write("")
early, early_log, early_handle = start_keeper(PORT_EARLY, IP_FILE, FAKE)
time.sleep(3)
check("with no address to hold it does not pretend to be a server",
      not held(ADDRESS, PORT_EARLY), "nothing listening")
check("and it says what it is waiting for, in the log the shop reads",
      "no shop Wi-Fi address yet" in read(early_log), "")

with open(IP_FILE, "w") as handle:            # the lease arrives, the way it does at 06:55
    handle.write(ADDRESS + "\n")
check("the address appearing is enough — nobody restarts anything",
      serving(ADDRESS, PORT_EARLY), "https://%s:%d/" % (ADDRESS, PORT_EARLY))
check("the counter's own page answers over the Wi-Fi address",
      health(ADDRESS, PORT_EARLY), "/healthz")
stop(early, early_handle)
time.sleep(1)
check("stopping the keeper stops the engine too, leaving no orphan on the port",
      not held(ADDRESS, PORT_EARLY), "port free again")

# 2. The address moves while the shop is serving. A job bound to a number that no longer exists
#    keeps telling launchd it is healthy while every device in the shop finds nothing.

with open(IP_FILE, "w") as handle:
    handle.write(ADDRESS + "\n")
moved, moved_log, moved_handle = start_keeper(PORT_MOVE, IP_FILE, FAKE)
check("it serves first, so there is something to move away from",
      serving(ADDRESS, PORT_MOVE), "https://%s:%d/" % (ADDRESS, PORT_MOVE))
asked = read(CERT_CALLS).count("certs")

with open(IP_FILE, "w") as handle:            # the router hands out a different number
    handle.write("192.168.99.77\n")
for _ in range(40):
    if "moved" in read(moved_log):
        break
    time.sleep(0.5)
check("it notices the address it was told to hold is gone", "moved" in read(moved_log), "")
check("it let go of the stale bind instead of sitting on a dead one",
      not held(ADDRESS, PORT_MOVE), "port free")
check("and it tries the new number, the one the devices are dialling",
      wait_for(moved_log, "https://192.168.99.77:%d" % PORT_MOVE), "")

with open(IP_FILE, "w") as handle:            # and back to the real one, as a rejoin does
    handle.write(ADDRESS + "\n")
check("it is serving again by itself, with nobody told",
      serving(ADDRESS, PORT_MOVE), "https://%s:%d/" % (ADDRESS, PORT_MOVE))
check("a moved address is a re-issued certificate, not a padlock warning on a phone",
      read(CERT_CALLS).count("certs") > asked,
      "%d asks of the helper" % read(CERT_CALLS).count("certs"))
stop(moved, moved_handle)

# 3. Two keepers must never mean two engines over one book.

first, first_log, first_handle = start_keeper(PORT_ALONE, IP_FILE, FAKE)
ready = serving(ADDRESS, PORT_ALONE)
second, second_log, second_handle = start_keeper(PORT_ALONE, IP_FILE, FAKE)
code = None
for _ in range(60):
    if second.poll() is not None:
        code = second.returncode
        break
    time.sleep(0.25)
check("one keeper is serving the book", ready, "")
check("a second keeper refuses to start an engine over the same book",
      code == 0 and "already answering" in read(second_log), "exit %s" % code)
stop(first, first_handle)
second_handle.close()

# 4. What launchd is pointed at: the service file is the other half of "by itself".

try:
    with open(PLIST, "rb") as handle:
        agent = plistlib.load(handle)
    plist_ok = True
except OSError as err:
    agent, plist_ok = {}, False
    check("the shop's LaunchAgent is readable", False, str(err))
if plist_ok:
    args = agent.get("ProgramArguments", [])
    check("launchd runs the keeper, not a server with a number of the day baked in",
          any(a.endswith("shop-boot.sh") for a in args), " ".join(args[:2]))
    check("the book still gets every flag it needs",
          "--allow-unauthenticated-lan" in args and "--tls-cert" in args, "")
    check("no address is pinned in the service file, so a moved router cannot strand the shop",
          "--host" not in args, "")
    check("it starts at login", agent.get("RunAtLoad") is True, "")
    check("it comes back when it dies badly",
          isinstance(agent.get("KeepAlive"), dict)
          and agent["KeepAlive"].get("SuccessfulExit") is False, "")
    interval = agent.get("StartInterval")
    check("and it re-arms on a timer, for the login that missed it",
          isinstance(interval, int) and 0 < interval <= 600, "every %s s" % interval)
    check("with a short throttle, so a crash at boot is retried before the counter opens",
          isinstance(agent.get("ThrottleInterval"), int)
          and agent["ThrottleInterval"] <= 30, "%s s" % agent.get("ThrottleInterval"))

# The service file existing is not the service running: on 2026-10-14, 10-16 and again 10-19 macOS
# started this Mac with the shop's agent in ~/Library/LaunchAgents and never loaded it, and the
# counter stayed dark for hours because nothing was left awake to notice. tools/shop-guard.sh is
# that notice, so it has to be installed and registered — a watchman that launchd also skipped is
# no watchman at all.

try:
    with open(GUARD_PLIST, "rb") as handle:
        guard = plistlib.load(handle)
    gargs = guard.get("ProgramArguments", [])
    check("a watchman is written to catch the login that never loaded the shop",
          any(a.endswith("shop-guard.sh") for a in gargs), " ".join(gargs[:2]))
    check("it starts at login and every few minutes after that",
          guard.get("RunAtLoad") is True and isinstance(guard.get("StartInterval"), int)
          and 0 < guard["StartInterval"] <= 600, "every %s s" % guard.get("StartInterval"))
    # It runs from the served copy: a launchd-spawned process cannot read anything under Documents,
    # so a watchman pointed into the repository would die exactly like the shop it watches.
    check("and it reads from a place launchd is allowed to reach",
          any(a.startswith(SERVED) for a in gargs[1:2]), gargs[1] if len(gargs) > 1 else "nothing")
    live = subprocess.run(["/bin/launchctl", "print", "gui/%d/com.chrisphics.shop-guard" % os.getuid()],
                          capture_output=True, text=True)
    check("launchd is running that watchman right now", live.returncode == 0,
          live.stdout.splitlines()[0] if live.stdout.strip() else live.stderr.strip()[:60])
except OSError as err:
    check("a watchman is written to catch the login that never loaded the shop", False, str(err))

# A job launchd registered is not a counter that answers. The keeper is a bash script, so launchd
# reads a dead engine behind it as a running service — and the devices cannot report that, because
# the page that would tell them lives behind the door that stopped answering. So the watchman also
# asks the kernel who holds the shop's port, and it signs a timesheet every pass: an empty log cannot
# tell "the shop was fine all day" from "the watchman never ran".
BEAT_FILE = os.path.expanduser("~/Library/Application Support/Chrisphics Hub/shop-guard.heartbeat")
try:
    beat_age = time.time() - os.path.getmtime(BEAT_FILE)
    beat_line = open(BEAT_FILE).read().strip()
except OSError as err:
    beat_age, beat_line = 1e9, "no timesheet (%s)" % err
check("the watchman signs a timesheet, so a quiet log means a healthy shop",
      beat_age < 1200, beat_line or "empty")
check("and the last thing it read was the shop's own door answering",
      str(SHOP_PORT) in beat_line and "no job" not in beat_line and "dark" not in beat_line, beat_line)

# The rest runs the served bytes against a throwaway world. It aims at the watchman's own job — the
# one launchd entry this check may restart without touching the book — and at a port claimed free at
# the start, so a dark read here is a real dark read and not somebody else's squatted door.
WATCH = os.path.join(work, "watch")
os.makedirs(WATCH, exist_ok=True)
WATCH_PORT = 8914
claim(WATCH_PORT)
blind_helper = os.path.join(WATCH, "blind.sh")
talk_helper = os.path.join(WATCH, "talk.sh")
with open(blind_helper, "w") as fh:
    fh.write('#!/bin/bash\n[ "$1" = wifi-ip ] && exit 0\n')
with open(talk_helper, "w") as fh:
    fh.write('#!/bin/bash\n[ "$1" = wifi-ip ] && echo %s\n' % (ADDRESS or "192.0.2.7"))
os.chmod(blind_helper, 0o755)
os.chmod(talk_helper, 0o755)
GUARD_SH = os.path.join(SERVED, "tools", "shop-guard.sh")
watch_env = dict(os.environ, GUARD_LABEL="com.chrisphics.shop-guard", GUARD_SUPPORT=WATCH,
                 GUARD_PORT=str(WATCH_PORT), GUARD_WAIT_N="2", GUARD_COOLDOWN_S="1800")


def watch(run_env):
    env = dict(watch_env, **run_env)
    subprocess.run(["/bin/bash", GUARD_SH], capture_output=True, text=True, env=env, timeout=60)
    path = os.path.join(WATCH, "shop-guard.heartbeat")
    try:
        return open(path).read().strip()
    except OSError:
        return "no timesheet written"


shop_engine = subprocess.run(["/usr/sbin/lsof", "-nP", "-iTCP:%d" % SHOP_PORT, "-sTCP:LISTEN"],
                             capture_output=True, text=True).stdout
first_dark = watch({"GUARD_HELPER": talk_helper})
check("a counter nobody answers is counted before it is touched",
      "dark turn 1 of" in first_dark, first_dark)
second_dark = watch({"GUARD_HELPER": talk_helper})
check("two dark turns with a real address to serve is the restart the devices cannot ask for",
      "started the shop service again" in second_dark, second_dark)
# A rescue zeroes the count, so the wait in front of the next one only reads on a shop that went dark
# again on its own. Seed that: five turns already counted, one restart just taken.
with open(os.path.join(WATCH, "shop-guard.state"), "w") as fh:
    fh.write("5 %d\n" % int(time.time()))
held_back = watch({"GUARD_HELPER": talk_helper})
check("and it then waits before restarting the same shop twice",
      "held back" in held_back, held_back)
watched_shop = subprocess.run(["/usr/sbin/lsof", "-nP", "-iTCP:%d" % SHOP_PORT, "-sTCP:LISTEN"],
                              capture_output=True, text=True).stdout
check("none of that disturbed the book the shop is trading on",
      watched_shop == shop_engine and watched_shop.strip() != "",
      "the same engine holds the door")
silent = watch({"GUARD_HELPER": blind_helper, "GUARD_WAIT_N": "1"})
check("a Mac with no Wi-Fi number is left to the keeper, not rebooted every five minutes",
      "no Wi-Fi number" in silent, silent)
open(os.path.join(WATCH, "shop-guard.state"), "w").write("0 0\n")
answered = watch({"GUARD_HELPER": talk_helper, "GUARD_PORT": str(SHOP_PORT)})
check("a door that is held ends the counting", "holds" in answered and "dark" not in answered,
      answered)

# 5. The copy launchd runs is the copy this repository says it runs.

for name in ("tools/shop-boot.sh", "tools/shop-server.sh", "tools/shop-guard.sh"):
    repo_bytes = open(os.path.join(REPO, name), "rb").read()
    live_path = os.path.join(SERVED, name)
    live_bytes = open(live_path, "rb").read() if os.path.exists(live_path) else b""
    check("the served copy of %s matches this repository" % name, repo_bytes == live_bytes,
          "%d bytes" % len(live_bytes))

# 6. One source for the address rule, and the shop as it runs right now is on that address.

out = subprocess.run(["/bin/bash", HELPER, "wifi-ip"], capture_output=True, text=True)
check("the helper answers the address question in one line",
      out.returncode == 0 and out.stdout.strip() == ADDRESS, out.stdout.strip())
check("the keeper does not keep a second copy of that rule",
      "getifaddr" not in open(KEEPER).read(), "")
keeper_text = open(KEEPER).read()
check("it asks the kernel, not the network, who holds the port",
      "nc -z" not in keeper_text and "-sTCP:LISTEN" in keeper_text,
      "a dial into a number nothing answers on held the keeper for half a minute, and bash "
      "will not honour a stop while a command is in front of it")
check("the shop as it runs answers on its Wi-Fi address, no restart needed to see it",
      health(ADDRESS, SHOP_PORT), "https://%s:%d/" % (ADDRESS, SHOP_PORT))

after = open(LIVE, "rb").read()
check("the live book is exactly the bytes this check started with", after == before,
      "%d bytes" % len(after))

shutil.rmtree(work, ignore_errors=True)
print()
print("%d checks, %d failed" % (total, len(fails)))
for name in fails:
    print("  FAILED: %s" % name)
raise SystemExit(1 if fails else 0)
