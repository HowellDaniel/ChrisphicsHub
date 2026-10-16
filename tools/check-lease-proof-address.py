#!/usr/bin/env python3
"""Prove the shop hands each device the shape of its address that will still open next month.

The counter's phone bookmarks what the install page tells it to type. Until now that was always the
Wi-Fi number this Mac holds, and a number is a loan from the router: the night it hands out a new
one, every device that wrote the old number down goes dark, and nothing on this Mac can reach them
to say so — the page that would tell them lives behind the number that just died. So Apple devices
and Windows PCs, which all look the shop's name up, are led to the name; Android has no way to ask,
so it is led to the number and the page says out loud why the name is not for it. Either way the
shape that is not the lead stays printed on the page, because a device that cannot open one still
has the other.

Two doors this check has to hold shut. A name the shop's certificate does not answer to is worse
than a number that moves — a padlock refuses it and the counter has no way to tell the two apart —
so a boot whose certificate names no host must never lead with the name. And the square a phone
scans has to be the same address the page tells it to type, per kind of device, or the QR quietly
sends Android back to the number the page just said to avoid.

The live book is opened read-only and copied with Connection.backup into a temp file. Each boot is a
throwaway server on a spare port with its own support folder, its own trust root (via
CHRISPHICS_TLS_DIR) and its own throwaway certificate. Nobody signs in, so the shop's password is
never touched and the shop's own private key is never loaded. `--allow-sleep` keeps the test from
asserting power management on this Mac.
"""
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
OPEN_PORT = 8901     # listens on every address the Mac holds
BOUND_PORT = 8902    # listens only on the Wi-Fi number, the way the shop service does
NAMELESS_PORT = 8903 # listens on everything, behind a certificate that names no host

work = tempfile.mkdtemp(prefix="chrisphics-lease-proof-")
TLS = os.path.join(work, "tls")
NAMELESS_TLS = os.path.join(work, "tls-no-name")
CLONE = os.path.join(work, "clone.db")
src = sqlite3.connect("file:%s?mode=ro" % LIVE, uri=True)
dst = sqlite3.connect(CLONE)
src.backup(dst)
dst.close()
src.close()
live_stat = os.stat(LIVE)

# A throwaway certificate: the banner and the page both claim `https`, so the test has to run real
# TLS rather than pretend. The shop's own key stays where it is.
subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                "-subj", "/CN=throwaway", "-keyout", os.path.join(work, "k.pem"),
                "-out", os.path.join(work, "c.pem")],
               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# The trust roots are written here and now: the record of what the certificate answers to is a plain
# list of names, and it is the only thing the shop's code reads to decide whether a name is safe to
# hand out. Nothing in the shop's own trust root is read or written.
def make_trust_root(folder, names):
    download = os.path.join(folder, "device-download")
    os.makedirs(download)
    for leaf in ("shop-root-ca.cer", "shop-root-ca.pem"):
        shutil.copyfile(os.path.join(work, "c.pem"), os.path.join(download, leaf))
    with open(os.path.join(download, "valid-for.txt"), "w", encoding="utf-8") as fh:
        fh.write("".join("%s\n" % n for n in names))
    return folder

sys.path.insert(0, REPO)
# Two helpers below resolve this Mac's own name and number, so work out both before deciding what
# either trust root is allowed to answer to.
wifi_probe = subprocess.run(["/usr/bin/python3", "-c",
                             "import socket,sys; sys.path.insert(0, %r); import server;"
                             "print(server.wifi_address() or '')" % REPO],
                            capture_output=True, text=True)
wifi = wifi_probe.stdout.strip()
mac_name = socket.gethostname()

make_trust_root(TLS, [mac_name, "localhost", "127.0.0.1", "::1", wifi] if wifi else
                [mac_name, "localhost", "127.0.0.1", "::1"])
make_trust_root(NAMELESS_TLS, ["localhost", "127.0.0.1", "::1"] + ([wifi] if wifi else []))

# Everything this process reads out of `server` — the helper paths, the address maths, the QR — has
# to be the same shape the child sees, so the seams are set before the import rather than patched
# after it.
os.environ.update({"CHRISPHICS_DB": CLONE, "CHRISPHICS_SUPPORT": work,
                   "CHRISPHICS_BACKUP_DIR": os.path.join(work, "Backups"),
                   "CHRISPHICS_TLS_DIR": TLS, "PYTHONUNBUFFERED": "1",
                   "CHRISPHICS_AUTH_PASSWORD": "throwaway-" + os.urandom(8).hex()})
import server  # noqa: E402
# The pure functions answer for a shop server on this port over TLS, which is what the child is told
# to be. The parent never listens, so this only says what the address maths should assume.
server.SERVE.update({"host": "0.0.0.0", "port": OPEN_PORT, "tls": True})

# One line per kind of device, taken from what those browsers actually send.
UA = {
    "android": "Mozilla/5.0 (Linux; Android 13; SM-A125F) AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/120.0.0.0 Mobile Safari/537.36",
    "iphone": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "ipad": "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like "
            "Gecko) Version/17.0 Safari/605.1.15",
    "mac": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like "
           "Gecko) Version/17.0 Safari/605.1.15",
    "windows": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/120.0.0.0 Safari/537.36",
    # The two shapes a Mac reaches the shop in that are not Safari. Neither says the word `Safari` in
    # a way a rule can lean on — and the shop's own app window is the second one.
    "chrome-mac": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like "
                  "Gecko) Chrome/120.0.0.0 Safari/537.36",
    "webview": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko)",
    "unknown": "curl/8.4.0",
}

fails = []


def check(label, ok, detail=""):
    print("%-58s %s %s" % (label, "ok" if ok else "FAIL", detail))
    if not ok:
        fails.append(label)


def free(port):
    out = subprocess.run(["lsof", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN"],
                         capture_output=True, text=True).stdout.splitlines()
    return [l.split()[1] for l in out[1:] if len(l.split()) > 2 and l.split()[1].isdigit()]


def boot(port, extra, tls_dir):
    """Start one throwaway server and keep its stdout, which is the thing under test."""
    handle = open(os.path.join(work, "port-%d.log" % port), "wb")
    env = dict(os.environ, CHRISPHICS_TLS_DIR=tls_dir)
    proc = subprocess.Popen(["/usr/bin/python3", os.path.join(REPO, "server.py"),
                             "--port", str(port), "--no-browser", "--allow-sleep"] + extra,
                            cwd=REPO, env=env, stdout=handle, stderr=handle)
    return proc, handle


def wait_ready(host, port, proc):
    ctx = __import__("ssl")._create_unverified_context()
    for _ in range(100):
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    "https://%s:%d/healthz" % (host, port)), timeout=3, context=ctx) as res:
                if json.loads(res.read().decode())["ok"]:
                    return True
        except Exception:
            time.sleep(0.15)
    return False


def fetch(url, ua="", host_header=None):
    ctx = __import__("ssl")._create_unverified_context() if url.startswith("https") else None
    headers = {"User-Agent": ua} if ua else {}
    if host_header:
        headers["Host"] = host_header
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=6, context=ctx) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def text(url, ua="", host_header=None):
    code, body = fetch(url, ua, host_header)
    return code, body.decode("utf-8", "replace")


def lead_of(page_html):
    """The one address the page tells this device to type, scan and bookmark."""
    box = re.search(r'<div class=url>.*?</div>', page_html, re.S)
    href = re.search(r'href="([^"]+)"', box.group(0)) if box else None
    return href.group(1) if href else ""


name_url = "https://%s:%d/" % (mac_name, OPEN_PORT)
number_url = "https://%s:%d/" % (wifi, OPEN_PORT)

# The square a phone scans is drawn by the small helper the shop's own server builds into its support
# folder. The child and the address maths in this process both read it out of the temp folder, so copy
# it across: without it the QR checks would compare nothing against nothing and call that agreement.
real_helper = os.path.expanduser("~/Library/Application Support/Chrisphics Hub/qr-encode")
if os.path.isfile(real_helper):
    shutil.copyfile(real_helper, os.path.join(work, "qr-encode"))
    os.chmod(os.path.join(work, "qr-encode"), 0o755)
check("the helper that draws the square is in the throwaway shop",
      os.path.isfile(server.QR_HELPER), server.QR_HELPER)

check("this Mac has a Wi-Fi number to fall back on", bool(wifi), wifi or "(none)")
check("and a name that ends in .local", mac_name.endswith(".local"), mac_name)
check("the name and the number are different shapes of one door", bool(wifi) and wifi != mac_name)

if free(OPEN_PORT) or free(BOUND_PORT) or free(NAMELESS_PORT):
    print("\nSomething else is already holding one of these ports: %s, %s, %s" % (
        free(OPEN_PORT), free(BOUND_PORT), free(NAMELESS_PORT)))
    print("A check that talks to a port it did not open proves nothing, so this one stops here.")
    sys.exit(1)

# ---------------------------------------------------------------- everything the router can move
open_proc, open_log = boot(OPEN_PORT, ["--host", "0.0.0.0", "--tls-cert", os.path.join(work, "c.pem"),
                                       "--tls-key", os.path.join(work, "k.pem")], TLS)
check("a shop server listening on every address came up", wait_ready(wifi, OPEN_PORT, open_proc))
check("and only this run's child answers on that port", free(OPEN_PORT) == [str(open_proc.pid)],
      ",".join(free(OPEN_PORT)) or "(nothing)")

pages = {}
for kind, ua in sorted(UA.items()):
    code, html = text("https://%s:%d/setup" % (wifi, OPEN_PORT), ua)
    pages[kind] = html
    check("%s opens the install page" % kind, code == 200, str(code))

leads = {kind: lead_of(html) for kind, html in sorted(pages.items())}
for kind in ("iphone", "ipad", "mac", "windows", "chrome-mac", "webview"):
    check("%s is led to the name, which outlives the number" % kind,
          leads[kind] == name_url, leads[kind])
for kind in ("android", "unknown"):
    check("%s is led to the number, which it can actually open" % kind,
          leads[kind] == number_url, leads[kind])

check("the page says the name is the one that does not move",
      "this shape does not move" in pages["iphone"])
check("the page never tells an iPhone to keep a number it does not need",
      "Android browsers cannot" not in pages["iphone"])
check("the Android page says the number is the one to keep",
      "keep the number on Android" in pages["android"], leads["android"])
check("and it names the other shape rather than hiding it",
      "This number opens on every device" in pages["android"] and name_url in pages["android"])
check("an iPhone is offered the number the same way", number_url in pages["iphone"])

# A device that already dialled the lead is shown one address, not the same address twice.
code, dialled_by_name = text("https://localhost:%d/setup" % OPEN_PORT, UA["iphone"],
                             host_header=mac_name)
check("an iPhone that dialled the name gets the name", code == 200 and lead_of(dialled_by_name) == name_url,
      lead_of(dialled_by_name))
check("and the page does not repeat it back as a second choice",
      "opens the same book" not in dialled_by_name)

# -------------------------------------------------------------------- the square really matches
first, second = fetch("https://%s:%d/setup-qr.png" % (wifi, OPEN_PORT), UA["iphone"])
third, android_qr = fetch("https://%s:%d/setup-qr.png" % (wifi, OPEN_PORT), UA["android"])
check("the QR an iPhone scans is drawn for the name it was led to",
      first == 200 and second == server.qr_png(leads["iphone"]),
      "%d, %d bytes" % (first, len(second)))
check("the QR an Android scans is drawn for its number",
      third == 200 and android_qr == server.qr_png(leads["android"]),
      "%d, %d bytes" % (third, len(android_qr)))
check("so the two devices are not handed the same square", second != android_qr)
check("and asking twice draws the same square", second == server.qr_png(leads["iphone"]))

# ------------------------------------------------------------------- what the shop screen is told
code, raw = text("https://%s:%d/api/bootstrap" % (wifi, OPEN_PORT), UA["iphone"])
check("the shop's own surfaces answer an iPhone", code == 200, str(code))
body = json.loads(raw)
check("the bootstrap hands the screen the name first",
      body.get("address") == name_url, str(body.get("address")))
check("and keeps the number beside it",
      body.get("address_other") == number_url, str(body.get("address_other")))
check("saying which shape it led with", body.get("address_kind") == "name",
      str(body.get("address_kind")))
code, raw = text("https://%s:%d/api/bootstrap" % (wifi, OPEN_PORT), UA["android"])
android_boot = json.loads(raw)
check("and the same screen leads an Android with the number",
      android_boot.get("address") == number_url and android_boot.get("address_kind") == "number",
      "%s / %s" % (android_boot.get("address"), android_boot.get("address_kind")))

open_proc.terminate()
open_proc.wait(timeout=10)

# --------------------------------------------------- a number that moves, and what a device sees
# The router handing this Mac a new number is the whole reason for the name. The shop service binds
# to one address, so a device dialling the name reaches it the same way — and the page must still
# lead with the name and offer the number beneath it.
bound_proc, bound_log = boot(BOUND_PORT, ["--host", wifi, "--tls-cert", os.path.join(work, "c.pem"),
                                          "--tls-key", os.path.join(work, "k.pem")], TLS)
check("a server bound to one Wi-Fi number came up", wait_ready(wifi, BOUND_PORT, bound_proc))
bound_url = "https://%s:%d/" % (wifi, BOUND_PORT)
code, by_number = text("https://%s:%d/setup" % (wifi, BOUND_PORT), UA["iphone"])
check("an iPhone reaching it by number is led to the name",
      code == 200 and lead_of(by_number) == "https://%s:%d/" % (mac_name, BOUND_PORT),
      lead_of(by_number))
check("and the number it dialled is still printed under it", bound_url in by_number)
# The night the router moves, the number in that bookmark answers nothing — but the name on the page
# the device already holds is the address the shop service is reachable at all.
check("the name answers the health of the shop on the same port",
      text("https://%s:%d/healthz" % (mac_name, BOUND_PORT), UA["iphone"])[0] == 200)
bound_proc.terminate()
bound_proc.wait(timeout=10)

# ------------------------------------------------- a name the padlock refuses is never handed out
nameless_proc, nameless_log = boot(NAMELESS_PORT, ["--host", "0.0.0.0",
                                                   "--tls-cert", os.path.join(work, "c.pem"),
                                                   "--tls-key", os.path.join(work, "k.pem")],
                                   NAMELESS_TLS)
check("a server whose certificate names no host came up",
      wait_ready(wifi, NAMELESS_PORT, nameless_proc))
silent_name = "https://%s:%d/" % (mac_name, NAMELESS_PORT)
for kind in ("iphone", "windows"):
    code, html = text("https://%s:%d/setup" % (wifi, NAMELESS_PORT), UA[kind])
    check("%s is not handed a name the certificate refuses" % kind,
          code == 200 and lead_of(html) == "https://%s:%d/" % (wifi, NAMELESS_PORT), lead_of(html))
    check("%s never sees that name offered as an address" % kind, silent_name not in html)
code, html = text("https://%s:%d/api/bootstrap" % (wifi, NAMELESS_PORT), UA["iphone"])
check("and the shop screen gets the number too",
      code == 200 and json.loads(html).get("address") == "https://%s:%d/" % (wifi, NAMELESS_PORT)
      and not json.loads(html).get("address_other"), str(json.loads(html).get("address")))
nameless_proc.terminate()
nameless_proc.wait(timeout=10)

# --------------------------------------------------------------------------- what it cost
for proc in (open_proc, bound_proc, nameless_proc):
    if proc.poll() is None:
        proc.terminate()
after_stat = os.stat(LIVE)
same = (after_stat.st_size == live_stat.st_size and after_stat.st_mtime == live_stat.st_mtime)
print("\nLive book: %d bytes, last written %s -- %s" % (
    after_stat.st_size, time.ctime(after_stat.st_mtime),
    "untouched" if same else "CHANGED, WHICH MUST NOT HAPPEN"))
if not same:
    fails.append("the shop's own book was written to")
shutil.rmtree(work, ignore_errors=True)

print("\n%s" % ("all checks passed" if not fails else "FAILED: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
