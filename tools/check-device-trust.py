#!/usr/bin/env python3
"""Prove a phone or PC on the shop Wi-Fi can be walked through trusting the shop certificate.

An iPhone got in first, so everything since has been written for the way iOS behaves. Android and
Windows are stricter in two specific ways, and both look like the same thing to whoever is holding
the device: a certificate warning that survives being told to trust the shop.

  * The server offered its own certificate and nothing else. iOS completes the path from a trust
    anchor already in its store; Chrome on Android and Edge on Windows will not, so the authority
    has to travel with the leaf on the wire.
  * Android's certificate installer reads the file name and opens only .0, .pem, .crt or
    .cacert.cer. The shop's file was CRISPprint-Shop-Root-CA.cer, which it refuses outright.

The live book is opened read-only and copied with Connection.backup into a temp file; the server
below is a throwaway on a spare port with its own support folder, its own throwaway authority and
its own throwaway leaf. Nothing is signed in, so the shop password is never touched and the shop's
private key is never loaded. The authority the shop actually hands to devices is read from disk
unchanged, because that is the file a device ends up trusting.
"""
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
TLS = os.path.expanduser("~/Library/Application Support/CRISPprint TLS/device-download")
PORT = 8896
ANDROID_NAME = "CRISPprint-Shop-Root-CA.cacert.cer"
import shopcheck  # noqa: E402  (lives beside this check)
shopcheck.require_free(PORT)

work = tempfile.mkdtemp(prefix="chrisphics-device-trust-")
CLONE = os.path.join(work, "clone.db")
src = sqlite3.connect("file:%s?mode=ro" % LIVE, uri=True)
dst = sqlite3.connect(CLONE)
src.backup(dst)
dst.close()
src.close()
live_stat = os.stat(LIVE)
# The book the shop is writing in already has the devices signed into it, so a bare count would
# blame this server for the phone that signed in last week. Compare against the clone as made.
copied = sqlite3.connect(CLONE)
sessions_as_copied = copied.execute("select count(*), coalesce(max(id), 0) from sessions").fetchone()
copied.close()


def openssl(*args):
    subprocess.run(["openssl"] + list(args), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# A throwaway authority and a leaf signed by it, chained the way the shop's now has to be.
openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
        "-subj", "/CN=Throwaway Shop CA", "-keyout", os.path.join(work, "ca-key.pem"),
        "-out", os.path.join(work, "ca.pem"))
openssl("req", "-new", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=throwaway-shop",
        "-keyout", os.path.join(work, "leaf-key.pem"), "-out", os.path.join(work, "leaf.csr"))
openssl("x509", "-req", "-in", os.path.join(work, "leaf.csr"),
        "-CA", os.path.join(work, "ca.pem"), "-CAkey", os.path.join(work, "ca-key.pem"),
        "-CAcreateserial", "-days", "1", "-out", os.path.join(work, "leaf.pem"))
CHAIN = os.path.join(work, "chain.pem")
with open(CHAIN, "w") as fh:
    for part in ("leaf.pem", "ca.pem"):
        with open(os.path.join(work, part)) as piece:
            fh.write(piece.read())

env = dict(os.environ, CHRISPHICS_DB=CLONE, CHRISPHICS_SUPPORT=work,
           CHRISPHICS_BACKUP_DIR=os.path.join(work, "Backups"), PYTHONUNBUFFERED="1")

fails = []


def check(label, ok, detail=""):
    print("%-56s %s %s" % (label, "ok" if ok else "FAIL", detail))
    if not ok:
        fails.append(label)


log = os.path.join(work, "server.log")
handle = open(log, "wb")
proc = subprocess.Popen(["/usr/bin/python3", os.path.join(REPO, "server.py"),
                         "--host", "127.0.0.1", "--port", str(PORT), "--no-browser",
                         "--allow-sleep", "--tls-cert", CHAIN,
                         "--tls-key", os.path.join(work, "leaf-key.pem")],
                        cwd=REPO, env=env, stdout=handle, stderr=handle)


def wait_ready():
    ctx = __import__("ssl")._create_unverified_context()
    for _ in range(100):
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen("https://127.0.0.1:%d/healthz" % PORT,
                                        timeout=3, context=ctx) as res:
                if json.loads(res.read().decode())["ok"]:
                    return True
        except Exception:
            time.sleep(0.15)
    return False


def fetch(path, user_agent="", host_header=None):
    """One request as a particular kind of device, with the headers that device would send."""
    ctx = __import__("ssl")._create_unverified_context()
    headers = {}
    if user_agent:
        headers["User-Agent"] = user_agent
    if host_header:
        headers["Host"] = host_header
    req = urllib.request.Request("https://127.0.0.1:%d%s" % (PORT, path), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=6, context=ctx) as res:
            return res.status, res.read(), dict(res.headers)
    except urllib.error.HTTPError as err:
        return err.code, err.read(), dict(err.headers)


UAS = {
    "android": "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko)"
               " Chrome/129.0.0.0 Mobile Safari/537.36",
    "ios": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15"
           " (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
    "windows": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"
               " Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
    "mac": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like"
           " Gecko) Version/18.0 Safari/605.1.15",
    "unknown": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko)"
               " Chrome/129.0.0.0 Safari/537.36",
}

check("the throwaway server came up on a spare port", wait_ready())

# ---------------------------------------------------------------- 1. the chain on the wire
shown = subprocess.run(["openssl", "s_client", "-connect", "127.0.0.1:%d" % PORT, "-showcerts"],
                       cwd=work, input="", capture_output=True, text=True, timeout=30).stdout
handed = shown.count("BEGIN CERTIFICATE")
check("the server hands the leaf and the authority together", shown.count("BEGIN CERTIFICATE") >= 2,
      "%d certificates on the wire" % handed)
check("and the leaf comes first", shown.find("throwaway-shop") < shown.find("Throwaway Shop CA")
      if handed >= 2 else False)

# -------------------------------------------------------------- 2. each device gets its own page
bodies = {}
for kind, agent in UAS.items():
    status, raw, _ = fetch("/setup", agent, "127.0.0.1:%d" % PORT)
    bodies[kind] = raw.decode("utf-8", "replace")
    check("%-8s gets the install page" % kind, status == 200 and bodies[kind].startswith("<!doctype html"),
          bodies[kind][:60] if not bodies[kind].startswith("<!doctype html") else "")

check("Android is pointed at a file its installer opens",
      ANDROID_NAME in bodies["android"] and ".cacert.cer" in bodies["android"]
      and "CA certificate" in bodies["android"])
check("Android is not told to trust it the iOS way",
      "Certificate Trust Settings" not in bodies["android"].split("On another kind")[0])
check("iPhone gets its own two-step trust",
      "Certificate Trust Settings" in bodies["ios"] and "VPN &amp; Device Management" in bodies["ios"])
check("Windows gets Current User, not an administrator prompt",
      "Current User" in bodies["windows"] and "Trusted Root Certification Authorities" in bodies["windows"])
check("Windows is told to quit the browser before retrying",
      "quit the browser fully" in bodies["windows"].lower())
check("a Mac gets Keychain Access", "Keychain Access" in bodies["mac"])
check("an unknown browser is shown every kind of device",
      all(word in bodies["unknown"] for word in
          ("Android phone or tablet", "iPhone or iPad", "Windows PC", "Another Mac")))
check("the fingerprint is printed for every device",
      all("It must read" in bodies[k] for k in UAS))

# ------------------------------------------------------------------- 3. the files themselves
cer_status, cer_bytes, _ = fetch("/shop-root-ca.cer", UAS["windows"])
and_status, and_bytes, and_headers = fetch("/" + ANDROID_NAME, UAS["android"])
check("the Android door answers", and_status == 200, "%d" % and_status)
check("and saves under the name Android accepts",
      ANDROID_NAME in and_headers.get("Content-Disposition", ""),
      and_headers.get("Content-Disposition", "(none)"))
check("with the same authority the other doors give",
      and_bytes == cer_bytes and len(and_bytes) > 500, "%d bytes" % len(and_bytes))
pem_status, pem_bytes, _ = fetch("/shop-root-ca.pem", UAS["mac"])
check("the plain-text copy opens for a Mac",
      pem_status == 200 and pem_bytes.startswith(b"-----BEGIN CERTIFICATE"), "%d bytes" % len(pem_bytes))
if os.path.isfile(os.path.join(TLS, "shop-root-ca.cer")):
    with open(os.path.join(TLS, "shop-root-ca.cer"), "rb") as fh:
        check("and it is the shop's real authority, byte for byte",
              fh.read() == cer_bytes)
else:
    check("the shop has made an authority to hand out", False, os.path.join(TLS, "shop-root-ca.cer"))

# ------------------------------------------------- 4. a moved Wi-Fi address has to say so plainly
sys.path.insert(0, REPO)
import server  # noqa: E402  (setup_html is rendered here as a plain function)

server.SERVE["tls"] = True
# setup_html builds the working address from the port the server is actually on. Render as the shop
# runs it, not as this module defaults.
server.SERVE["port"] = 8834
sidecar = os.path.join(work, "valid-for.txt")


def rendered(host, names, agent="android"):
    with open(sidecar, "w") as fh:
        fh.write("".join(name + "\n" for name in names))
    server.CA_VALID_FOR = sidecar
    return server.setup_html(host, UAS[agent])


with open(sidecar, "w") as fh:
    fh.write("198.51.100.43\nshop-counter.local\n")
server.CA_VALID_FOR = sidecar
page = rendered("198.51.100.43:8834", ["198.51.100.43", "shop-counter.local"])
check("an address the certificate answers to raises no alarm",
      "only answers to" not in page and "198.51.100.43" in page)
ipv6 = rendered("[::1]:8834", ["::1", "198.51.100.43"])
check("an IPv6 address is read as one address, not split on its own colons",
      "only answers to" not in ipv6)
moved = rendered("198.51.100.77:8834", ["198.51.100.43", "shop-counter.local"])
check("a moved address names the mismatch instead of a mystery warning",
      "only answers to" in moved and "198.51.100.43" in moved)
check("and offers the address that does work",
      'href="https://198.51.100.43:8834/setup"' in moved)
server.CA_VALID_FOR = os.path.join(work, "not-written-yet.txt")
unknown = rendered("198.51.100.77:8834", [])
check("a shop that has not recorded its names says so rather than guessing",
      "not recorded on this Mac" in unknown and "only answers to" not in unknown)

# ------------------------------------------------------------------------- 5. the live book is whole
proc.terminate()
time.sleep(0.6)
handle.close()
stat_after = os.stat(LIVE)
check("the shop's own book was not written to",
      live_stat.st_size == stat_after.st_size and int(live_stat.st_mtime) == int(stat_after.st_mtime),
      "%d bytes both times" % stat_after.st_size)
check("the throwaway server left no session behind",
      sqlite3.connect(CLONE).execute("select count(*), coalesce(max(id), 0) from sessions").fetchone()
      == sessions_as_copied, "was %s" % (sessions_as_copied,))
shutil.rmtree(work, ignore_errors=True)

print()
if fails:
    print("%d check(s) failed: %s" % (len(fails), ", ".join(fails)))
    sys.exit(1)
print("every check passed.")
