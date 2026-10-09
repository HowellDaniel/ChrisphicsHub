#!/usr/bin/env python3
"""Prove the shop server hands other systems an address they can actually open.

The startup banner is the only place the counter is told what to say to a customer whose phone
wants the book. When the server listens on every address the Mac holds, the banner used to print
`127.0.0.1` — which no other machine can reach — and the log the shop reads was full of it.

The live book is opened read-only, copied with Connection.backup into a temp file, and each boot
below is a throwaway server on a spare port with its own support folder and its own throwaway
certificate. Nothing is signed in, so the shop password is never touched; the private key of the
shop is never loaded. `--allow-sleep` keeps the test from asserting power management on this Mac.
"""
import json
import os
import re
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
PORT = 8897
NAME_PORT = 8898
LOCAL_PORT = 8899

work = tempfile.mkdtemp(prefix="chrisphics-device-address-")
CLONE = os.path.join(work, "clone.db")
src = sqlite3.connect("file:%s?mode=ro" % LIVE, uri=True)
dst = sqlite3.connect(CLONE)
src.backup(dst)
dst.close()
src.close()
live_stat = os.stat(LIVE)

# A throwaway certificate: the banner claims `https`, so the test has to run real TLS rather than
# pretend. The shop's own key stays where it is.
subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                "-subj", "/CN=throwaway", "-keyout", os.path.join(work, "k.pem"),
                "-out", os.path.join(work, "c.pem")],
               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

env = dict(os.environ, CHRISPHICS_DB=CLONE, CHRISPHICS_SUPPORT=work,
           CHRISPHICS_BACKUP_DIR=os.path.join(work, "Backups"), PYTHONUNBUFFERED="1")

fails = []


def check(label, ok, detail=""):
    print("%-52s %s %s" % (label, "ok" if ok else "FAIL", detail))
    if not ok:
        fails.append(label)


def boot(port, extra, log_path):
    """Start one throwaway server and keep its stdout, which is the thing under test."""
    handle = open(log_path, "wb")
    proc = subprocess.Popen(["/usr/bin/python3", os.path.join(REPO, "server.py"),
                             "--port", str(port), "--no-browser", "--allow-sleep"] + extra,
                            cwd=REPO, env=env, stdout=handle, stderr=handle)
    return proc, handle


def wait_ready(scheme, host, port, proc):
    url = "%s://%s:%d/healthz" % (scheme, host, port)
    ctx = None
    if scheme == "https":
        ctx = __import__("ssl")._create_unverified_context()
    for _ in range(100):
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(urllib.request.Request(url), timeout=3, context=ctx) as res:
                if json.loads(res.read().decode())["ok"]:
                    return True
        except Exception:
            time.sleep(0.15)
    return False


def page(url, host_header=None):
    ctx = __import__("ssl")._create_unverified_context() if url.startswith("https") else None
    req = urllib.request.Request(url, headers={"Host": host_header} if host_header else {})
    try:
        with urllib.request.urlopen(req, timeout=6, context=ctx) as res:
            return res.status, res.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode("utf-8", "replace")


def read_log(path):
    try:
        with open(path, "r", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


sys.path.insert(0, REPO)
import server  # noqa: E402  (only for wifi_address(), the same helper the banner calls)

wifi = server.wifi_address()
mac_name = server.socket.gethostname()

check("this Mac has a Wi-Fi address to hand out", bool(wifi), wifi or "(none)")
check("and a name that ends in .local", mac_name.endswith(".local"), mac_name)
check("the address is not the loopback one", wifi != "127.0.0.1", wifi)

# ------------------------------------------------------------------- listening on everything
every_log = os.path.join(work, "every.log")
one, handle = boot(PORT, ["--host", "0.0.0.0", "--tls-cert", os.path.join(work, "c.pem"),
                          "--tls-key", os.path.join(work, "k.pem")], every_log)
ready = wait_ready("https", wifi, PORT, one) if wifi else False
check("a server listening on every address came up", ready)
time.sleep(0.4)
handle.flush()
os.fsync(handle.fileno())
banner = read_log(every_log)
device_line = [l for l in banner.splitlines() if "Devices on this Wi-Fi" in l]
print("\n--- what it printed\n" + banner.rstrip())
check("it prints exactly one device line", len(device_line) == 1, str(len(device_line)))
printed = device_line[0] if device_line else ""
check("the device line is not 127.0.0.1", "127.0.0.1" not in printed, printed.strip())
check("the device line is not 0.0.0.0", "0.0.0.0" not in printed, printed.strip())
check("it names the address another machine holds",
      ("://%s:%d/setup" % (wifi, PORT)) in printed, wifi)
check("the address is the one the helper resolves", wifi in printed)
name_line = [l for l in banner.splitlines() if mac_name in l]
check("it also offers this Mac's name", bool(name_line), (name_line or [""])[0].strip())
check("the name line carries the same port",
      bool(name_line) and (":%d/" % PORT) in name_line[0], (name_line or [""])[0].strip())

# The install page a device actually opens must agree with the banner.
code, setup = page("https://%s:%d/setup" % (wifi, PORT))
check("/setup over the Wi-Fi address answers", code == 200, str(code))
check("/setup shows that same address back", ("://%s:%d/" % (wifi, PORT)) in setup)
check("/setup offers the certificate to trust", "/shop-root-ca.cer" in setup)
if mac_name.endswith(".local"):
    code, by_name = page("https://localhost:%d/setup" % PORT, host_header=mac_name)
    check("/setup opened by name shows the name", code == 200 and ("://%s:%d/" % (mac_name, PORT)) in by_name,
          str(code))
code, _ = page("https://%s:%d/api/bootstrap" % (wifi, PORT))
check("a stranger on the Wi-Fi is still asked to sign in", code == 401, str(code))

one.terminate()
one.wait(timeout=10)

# --------------------------------------------------------------------- one address only
by_host_log = os.path.join(work, "by-host.log")
two, handle2 = boot(NAME_PORT, ["--host", wifi, "--tls-cert", os.path.join(work, "c.pem"),
                                "--tls-key", os.path.join(work, "k.pem")], by_host_log)
check("a server bound to the Wi-Fi address came up", wait_ready("https", wifi, NAME_PORT, two))
time.sleep(0.4)
handle2.flush()
os.fsync(handle2.fileno())
bound = read_log(by_host_log)
bound_line = [l for l in bound.splitlines() if "Devices on this Wi-Fi" in l]
check("it prints the address it was told to hold",
      bool(bound_line) and ("://%s:%d/setup" % (wifi, NAME_PORT)) in bound_line[0],
      (bound_line or [""])[0].strip())
check("it does not invent a name it cannot serve",
      not [l for l in bound.splitlines() if mac_name in l])
two.terminate()
two.wait(timeout=10)

# ------------------------------------------------------------------------ this Mac only
quiet_log = os.path.join(work, "local.log")
three, handle3 = boot(LOCAL_PORT, ["--host", "127.0.0.1"], quiet_log)
check("a loopback-only server came up", wait_ready("http", "127.0.0.1", LOCAL_PORT, three))
time.sleep(0.4)
handle3.flush()
os.fsync(handle3.fileno())
quiet = read_log(quiet_log)
check("a book only on this Mac promises nothing to other devices",
      "Devices on this Wi-Fi" not in quiet,
      [l for l in quiet.splitlines() if "install" in l][:1] and "saw a device line" or "no device line")
check("and still says 127.0.0.1 for itself", "127.0.0.1:%d" % LOCAL_PORT in quiet)
three.terminate()
three.wait(timeout=10)

# --------------------------------------------------------------------------- what it cost
for proc in (one, two, three):
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
