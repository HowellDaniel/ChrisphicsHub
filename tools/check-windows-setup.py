#!/usr/bin/env python3
"""Prove the Windows copy joins the shop's one book, and that the setup .exe is a real installer.

The owner's complaint was that the portable Windows app "doesn't want to come online". Two things
have to be true before a PC on the shop's Wi-Fi works in the shop's book, and this check holds the
door on both: the engine must decide correctly what is the shop, and the thing a person double-clicks
must actually put that engine on their PC the way Windows expects.

Nothing here asks the real shop anything. The engine under test is the one that ships inside the
built bundle, run from a copy of its own app folder whose authority file and address list are
throwaway certificates made for this run. A stand-in server — plain `http.server` under real TLS, no
SQLite, no book — plays the shop on a spare port, and a second stand-in under a different CA plays a
stranger sitting on a shop-looking number. The stranger is the whole point: an address on the Wi-Fi
is a lease, and the only thing that makes it the shop is a certificate the shop's own authority
signed. Neither throwaway CA is installed anywhere, so this Mac's trust store is not touched either.

Every write goes into a temp support folder (`CHRISPHICS_SUPPORT`), never into the shop's records.
The shop's live book is opened read-only, before and after, only to prove this check left it alone.

Ports: three spare ones, found free when this runs. 89xx is where the shop's checks live, and a port
someone else squatted there answers /healthz as readily as a stand-in does — so the numbers are
looked up rather than assumed, and the guard in shopcheck still refuses to run on one that is taken.
"""
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shopcheck

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE = os.path.expanduser("~/Library/Application Support/Chrisphics Hub/chrisphics.db")
SHOP_CA = os.path.expanduser("~/Library/Application Support/mkcert/rootCA.pem")
DIST = os.path.join(REPO, "dist")
WIN_BUNDLE = os.path.join(DIST, "Chrisphics Hub for Windows")
WIN_PORTABLE = os.path.join(DIST, "Chrisphics Hub for Windows (portable)")
SETUP = os.path.join(DIST, "Chrisphics-Hub-Setup-x64.exe")
SETUP_NSI = os.path.join(DIST, "Chrisphics-Hub-Setup-x64.nsi")
free = [p for p in range(8911, 8991) if not shopcheck.in_use(p)][:3]
if len(free) < 3:
    raise SystemExit("no three spare ports between 8911 and 8990 — nothing to stand in as the shop")
SHOP_PORT, STRANGER_PORT, NOT_SHOP_PORT = free
shopcheck.require_free(SHOP_PORT, STRANGER_PORT, NOT_SHOP_PORT)
work = tempfile.mkdtemp(prefix="chrisphics-win-setup-")
live_before = os.stat(LIVE) if os.path.isfile(LIVE) else None
ran = fails = 0


def check(name, ok, detail=""):
    global fails, ran
    ran += 1
    fails += 0 if ok else 1
    print("%-54s %s%s" % (name, "ok" if ok else "FAIL", " %s" % detail if detail else ""))
    return ok


def run(*cmd, **kw):
    env = dict(os.environ)
    env.update(kw.pop("env", {}))
    return subprocess.run(list(cmd), capture_output=True, text=True, env=env, **kw)


# ----------------------------------------------------------------- throwaway certificates
# Two authorities that have never met: one is presented as the shop's, one signs a stranger who
# answers on a shop-looking address. mkcert issues the leaves so the host names on them are the real
# thing — the engine checks the name, and a leaf without it would be refused for the wrong reason.
def make_ca(folder, name):
    os.makedirs(folder, exist_ok=True)
    cnf = os.path.join(folder, "ca.cnf")
    open(cnf, "w", encoding="utf-8").write(
        "[req]\ndistinguished_name=dn\nx509_extensions=v3_ca\nprompt=no\n"
        "[dn]\nO=mkcert development CA\nOU=chrisphics-check\nCN=%s\n"
        "[v3_ca]\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\n" % name)
    said = run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
               "-config", cnf, "-keyout", os.path.join(folder, "rootCA-key.pem"),
               "-out", os.path.join(folder, "rootCA.pem"))
    if said.returncode:
        raise SystemExit("no throwaway CA: %s" % said.stderr.strip()[-200:])
    os.chmod(os.path.join(folder, "rootCA-key.pem"), 0o600)
    return folder


def make_leaf(ca_folder, out_folder):
    os.makedirs(out_folder, exist_ok=True)
    said = run("mkcert", "-cert-file", os.path.join(out_folder, "cert.pem"),
               "-key-file", os.path.join(out_folder, "key.pem"), "localhost", "127.0.0.1",
               env={"CAROOT": ca_folder})
    if said.returncode:
        raise SystemExit("no throwaway leaf: %s" % said.stderr.strip()[-200:])
    return out_folder


SHOP_AUTH = make_ca(os.path.join(work, "ca-shop"), "chrisphics check shop CA")
STR_AUTH = make_ca(os.path.join(work, "ca-stranger"), "chrisphics check stranger CA")
SHOP_TLS = make_leaf(SHOP_AUTH, os.path.join(work, "tls-shop"))
STRANGER_TLS = make_leaf(STR_AUTH, os.path.join(work, "tls-stranger"))
NOT_SHOP_TLS = make_leaf(SHOP_AUTH, os.path.join(work, "tls-not-shop"))
check("two separate authorities and three leaves made",
      all(os.path.isfile(os.path.join(p, f)) for p, f in [
          (SHOP_AUTH, "rootCA.pem"), (STR_AUTH, "rootCA.pem"), (SHOP_TLS, "cert.pem"),
          (STRANGER_TLS, "cert.pem"), (NOT_SHOP_TLS, "cert.pem")]) and
      SHOP_AUTH != STR_AUTH,
      "in %s — neither CA installed anywhere on this Mac" % work)

# ----------------------------------------------------------------- stand-in servers
STAND_IN = r'''
import http.server, ssl, sys
body, cert, key, port = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain(cert, key)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        answer = body.encode() if self.path == "/healthz" else b"not the door"
        self.send_response(200 if self.path == "/healthz" else 404)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    def log_message(self, *args):
        pass


server = http.server.HTTPServer(("127.0.0.1", port), Handler)
server.socket = context.wrap_socket(server.socket, server_side=True)
server.serve_forever()
'''
stand_script = os.path.join(work, "stand-in.py")
open(stand_script, "w", encoding="utf-8").write(STAND_IN)
servers = []


def stand_in(port, body, tls):
    """A shop-shaped server that holds no book: it only ever answers /healthz."""
    servers.append(subprocess.Popen(
        [sys.executable, stand_script, body, os.path.join(tls, "cert.pem"),
         os.path.join(tls, "key.pem"), str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    return "https://127.0.0.1:%d/" % port


shop_url = stand_in(SHOP_PORT, "ok\n", SHOP_TLS)
stranger_url = stand_in(STRANGER_PORT, "ok\n", STRANGER_TLS)
not_shop_url = stand_in(NOT_SHOP_PORT, "printer", NOT_SHOP_TLS)

# ----------------------------------------------------------------- the engine as it ships
# The copy the build produced, not the repository's: this is the file a PC's launcher runs. Its
# authority is swapped for the throwaway shop CA, and its built-in address list is left out until the
# last leg, so nothing on this Mac's Wi-Fi can be mistaken for the shop from here.
APP_SRC = os.path.join(WIN_BUNDLE, "app") if os.path.isdir(os.path.join(WIN_BUNDLE, "app")) else REPO
bundle = os.path.join(work, "bundle")
app = os.path.join(bundle, "app")
support = os.path.join(bundle, "support")
os.makedirs(support, exist_ok=True)
shutil.copytree(APP_SRC, app, ignore=shutil.ignore_patterns(
    "__pycache__", "*.pyc", "shop-ca.pem", "shop-urls.txt"))
shutil.copyfile(os.path.join(SHOP_AUTH, "rootCA.pem"), os.path.join(app, "shop-ca.pem"))
ENGINE = os.path.join(app, "server.py")
from_shell = WIN_BUNDLE if APP_SRC.endswith("app") else "the repository"


def engine_env(**extra):
    env = {"CHRISPHICS_SUPPORT": support, "CHRISPHICS_SHOP_PORT": str(SHOP_PORT),
           "CHRISPHICS_SHOP_URL": "", "CHRISPHICS_DB": os.path.join(support, "shop.db")}
    env.update(extra)
    return env


def ask(*args, **env):
    return run(sys.executable, ENGINE, *args, env=engine_env(**env))


def joined():
    path = os.path.join(support, "shop-online.txt")
    return open(path, encoding="utf-8").read().strip() if os.path.isfile(path) else ""


def forget():
    for name in ("shop-online.txt", "own-book.flag"):
        path = os.path.join(support, name)
        if os.path.exists(path):
            os.remove(path)


print("\n== the join door, on a machine that is not the shop")
check("the engine being asked is the one that ships", os.path.isfile(ENGINE),
      "%d bytes from %s" % (os.path.getsize(ENGINE), from_shell))

said = ask("--find-shop", CHRISPHICS_SHOP_URL=stranger_url)
check("a stranger on a shop address is refused", said.returncode == 1 and not said.stdout.strip(),
      "rc %d — its number answered, its certificate did not" % said.returncode)
check("  and nothing is remembered from a refusal", joined() == "",
      joined() or "no shop-online.txt written")

said = ask("--find-shop", CHRISPHICS_SHOP_URL=not_shop_url)
check("a server under the shop's certificate that is not the shop is refused",
      said.returncode == 1 and not said.stdout.strip(),
      "rc %d — it said \"printer\", not the shop's ok" % said.returncode)

said = ask("--find-shop", CHRISPHICS_SHOP_URL=shop_url)
check("the shop's own certificate is taken", said.returncode == 0 and said.stdout.strip() == shop_url,
      said.stdout.strip() or said.stderr.strip())
check("  and the address is remembered for next time", joined() == shop_url, joined())

# A remembered address is the leg a bundle carries when the shop's number has moved since it was
# built: the list in the app folder is a hint, the join is a fact the PC learned on its own.
said = ask("--find-shop")
check("a remembered address needs no built-in list", said.returncode == 0 and
      said.stdout.strip() == shop_url and joined() == shop_url,
      "only shop-online.txt named it")

said = ask("--own-book")
flag = os.path.join(support, "own-book.flag")
check("a PC told to keep its own book says so", said.returncode == 0 and os.path.isfile(flag),
      said.stdout.strip())
said = ask("--find-shop")
check("  and the refusal wins even while the shop is answering",
      said.returncode == 2 and not said.stdout.strip(),
      "rc %d, %r" % (said.returncode, said.stderr.strip()))
said = ask("--join-shop")
check("asking for the shop again clears the choice", said.returncode == 0 and
      said.stdout.strip().endswith(shop_url) and not os.path.isfile(flag),
      " | ".join(said.stdout.split()))
check("  and the choice never wrote into a book", not os.path.isfile(
    os.path.join(support, "shop.db")), "the support folder holds %s" % (
    ", ".join(sorted(os.listdir(support))) or "nothing"))

# Nothing left on the wire: the engine's own bounded sweep of this Mac's /24 runs, trusts no
# certificate it finds, and the copy is told to fall back to its own book.
for proc in servers:
    proc.terminate()
for proc in servers:
    proc.wait()
forget()
open(os.path.join(app, "shop-urls.txt"), "w", encoding="utf-8").write(
    "%s\nhttps://192.0.2.7:%d/\n" % (shop_url, SHOP_PORT))
started = time.time()
said = ask("--find-shop")
check("when no shop answers, the launcher is told in words", said.returncode == 1 and
      not said.stdout.strip(), "rc %d, %r — the sweep found nothing trusted, in %.1fs" % (
          said.returncode, said.stderr.strip(), time.time() - started))

# ----------------------------------------------------------------- what the bundle carries
print("\n== what the Windows bundle carries")
check("the shop's authority travels inside the app folder",
      os.path.isfile(os.path.join(WIN_BUNDLE, "app", "shop-ca.pem")),
      WIN_BUNDLE if os.path.isdir(WIN_BUNDLE) else "not built — run tools/build-installer.py win-x64")
bundled_ca = os.path.join(WIN_BUNDLE, "app", "shop-ca.pem")
if os.path.isfile(SHOP_CA) and os.path.isfile(bundled_ca):
    check("  and it is the same authority the shop's own Mac holds",
          open(bundled_ca, "rb").read() == open(SHOP_CA, "rb").read(),
          "byte for byte as %s" % os.path.basename(SHOP_CA))
urls_path = os.path.join(WIN_BUNDLE, "app", "shop-urls.txt")
urls = [ln.strip() for ln in open(urls_path, encoding="utf-8").read().splitlines()
        if ln.strip() and not ln.startswith("#")] if os.path.isfile(urls_path) else []
check("the shop's number and its name are both pre-named",
      len(urls) >= 2 and any(".24:8834/" in u for u in urls) and any(".local" in u for u in urls),
      "; ".join(urls) or "no shop-urls.txt in the bundle")
check("  and every address is https on the shop's own door",
      all(u.startswith("https://") and u.endswith("/") and ":8834/" in u for u in urls))

# What each launcher has to say. The two shapes open the shop the same way but keep their records in
# different folders, so the installed copy gets its own launcher and the stick gets the portable one —
# and both of them must ask the engine before they start a server of their own.
needs = {"Start Chrisphics Hub.bat": ["--find-shop", "shop-found.txt", r"runtime\python.exe",
                                      ":open", "--app=", r"%SUPPORT%\shop.db", "--shop-copy"],
         "Start Chrisphics Hub (portable).bat": ["--find-shop", "shop-found.txt",
                                                 r"runtime\python.exe", ":open", "--app=",
                                                 r"%HERE%data\shop.db"],
         "Join the shop's book.bat": ["--join-shop"],
         "Keep this PC's own book.bat": ["--own-book"]}
for folder, label in ((WIN_BUNDLE, "installed"), (WIN_PORTABLE, "portable")):
    if not os.path.isdir(folder):
        check("%s launchers are in the bundle" % label, False, "%s is not built" % label)
        continue
    for name, wanted in needs.items():
        path = os.path.join(folder, name)
        if label == "portable" and name == "Start Chrisphics Hub.bat":
            continue  # the stick has one launcher, and it is the portable one
        raw = open(path, "rb").read() if os.path.isfile(path) else b""
        text = raw.decode("utf-8-sig", errors="replace")
        missing = [m for m in wanted if m not in text]
        crlf = raw.count(b"\r\n") >= 3 and raw.count(b"\n") == raw.count(b"\r\n")
        check("%s: %s" % (label, name), bool(raw) and not missing and crlf and "%(" not in text,
              "%d lines, %s%s" % (len(raw.splitlines()),
                                  "CRLF" if crlf else "LF — cmd.exe reads these badly",
                                  "" if not missing else ", no %s" % ", ".join(missing)))

for path in [os.path.join(DIST, "Chrisphics-Hub-Windows.zip"),
             os.path.join(DIST, "Chrisphics-Hub-Windows-portable.zip")]:
    if not os.path.isfile(path):
        check("the zip a person downloads is here", False, "%s missing" % os.path.basename(path))
        continue
    names = [n.replace("\\", "/").lower() for n in zipfile.ZipFile(path).namelist()]

    def holds(w):
        return any(n == w or n.endswith("/" + w) for n in names)

    want = ["app/shop-ca.pem", "app/shop-urls.txt", "app/server.py", "runtime/python.exe",
            "data/shop.db", "join the shop's book.bat", "keep this pc's own book.bat"]
    short = [w for w in want if not holds(w)]
    if not any(os.path.basename(n).startswith("start chrisphics hub") and n.endswith(".bat")
               for n in names):
        short.append("a launcher to double-click")
    check("  %s" % os.path.basename(path), not short,
          "%.1f MB, %d entries, %d of the shop's files inside%s" % (
              os.path.getsize(path) / 1048576.0, len(names), len(want) + 1,
              "" if not short else " — short of %s" % ", ".join(short)))

# ----------------------------------------------------------------- the setup .exe itself
print("\n== the setup .exe a Windows PC double-clicks")
if not os.path.isfile(SETUP):
    check("there is a setup .exe to hand over", False, "%s is not built" % SETUP)
else:
    head = open(SETUP, "rb").read(4096)
    dos = head[:2] == b"MZ"
    pe = int.from_bytes(head[0x3C:0x40], "little") if dos else 0
    machine = int.from_bytes(head[pe + 4:pe + 6], "little") if pe else 0
    size = os.path.getsize(SETUP)
    body = open(SETUP, "rb").read()
    check("it opens as a Windows program", dos and head[pe:pe + 4] == b"PE\0\0",
          "PE at 0x%x, machine 0x%04x%s" % (pe, machine, " — NSIS builds a 32-bit wizard, and it "
                                            "runs on every x64 Windows" if machine == 0x014C else ""))
    check("  and it is big enough to hold the whole shop", size > 5_000_000,
          "%.1f MB" % (size / 1048576.0))
    check("  and it is an NSIS wizard, not a renamed zip", b"NullsoftInst" in body,
          "signature at 0x%x" % body.find(b"NullsoftInst") if b"NullsoftInst" in body else "none")

if os.path.isfile(SETUP_NSI):
    script = open(SETUP_NSI, encoding="utf-8", errors="replace").read()
    marks = {"a wizard with pages": "MUI_PAGE_WELCOME", "a folder to choose": "MUI_PAGE_DIRECTORY",
             "progress while it copies": "MUI_PAGE_INSTFILES",
             "a finish page that opens the shop": "MUI_FINISHPAGE_RUN",
             "a line in Settings > Apps": r"Uninstall\ChrisphicsHub",
             "an uninstaller to go with it": "WriteUninstaller", "a Start Menu icon": "$SMPROGRAMS",
             "a Desktop icon": "$DESKTOP", "no administrator rights asked":
                 "RequestExecutionLevel user", "the 64-bit view of the registry": "SetRegView 64",
             "a word to a PC that is not 64-bit": "${RunningX64}",
             "the shop's authority trusted": "certutil -addstore -user Root",
             "and that trust taken back out": "certutil -delstore -user Root"}
    gaps = [n for n, m in marks.items() if m not in script]
    check("the wizard's own script is left beside it to read", not gaps,
          "%d of %d marks%s" % (len(marks) - len(gaps), len(marks),
                                "" if not gaps else " — no %s" % ", ".join(gaps)))
    # Every file the script says it packs has to be here, or the wizard opens, installs and leaves a
    # PC with nothing to run. The script was written on this Mac, so its paths read as this Mac's.
    paths = re.findall(r'^\s*File(?:\s+/r)?\s+"([^"]+)"', script, re.M)
    loose = [p for p in paths if "*" not in p]
    missing = sorted(os.path.basename(p) for p in loose if not os.path.isfile(p))
    empty = sorted(p for p in paths if "*" in p and not any(
        f for _, _, fs in os.walk(p.rsplit("/", 1)[0]) for f in fs))
    check("  and everything it packs is really here", len(paths) >= 8 and not missing and not empty,
          "%d File lines, %d loose files and %d folder trees%s%s" % (
              len(paths), len(loose), len(paths) - len(loose),
              "" if not missing else " — short of %s" % ", ".join(missing),
              "" if not empty else " — holds nothing: %s" % ", ".join(empty)))
    serial = re.search(r"certutil -delstore -user Root (\w+)", script)
    want_serial = ""
    if os.path.isfile(SHOP_CA):
        said = run("openssl", "x509", "-in", SHOP_CA, "-noout", "-serial")
        found = re.search(r"=([0-9A-Fa-f]+)", said.stdout or "")
        want_serial = found.group(1).upper() if found else ""
    check("  and it takes back only the certificate it put in",
          bool(serial) and (not want_serial or serial.group(1).upper() == want_serial),
          "uninstalls by serial %s%s" % (serial.group(1) if serial else "missing",
                                          "" if serial and want_serial and
                                          serial.group(1).upper() == want_serial
                                          else " — the shop's is %s" % (want_serial or "unknown")))
    out = re.search(r'OutFile "([^"]+)"', script)
    same = bool(out) and os.path.isfile(out.group(1)) and os.path.samefile(out.group(1), SETUP)
    check("  and it was compiled into the .exe that is on the shelf", same,
          out.group(1) if out else "no OutFile line")
else:
    check("the wizard's own script is left beside it to read", False, "%s missing" % SETUP_NSI)

# ----------------------------------------------------------------- the shop's book, untouched
print("\n== the shop's own records")
if live_before is None:
    check("the shop's live book is where it always was", False, "%s not found" % LIVE)
else:
    after = os.stat(LIVE)
    src = sqlite3.connect("file:%s?mode=ro" % LIVE, uri=True)
    jobs, clients = src.execute("select (select count(*) from jobs), (select count(*) from clients)"
                               ).fetchone()
    src.close()
    check("this check left the shop's live book alone",
          (after.st_size, after.st_mtime) == (live_before.st_size, live_before.st_mtime),
          "%d jobs, %d clients, %d bytes, mtime unchanged since %s" % (
              jobs, clients, after.st_size, time.strftime("%H:%M:%S",
                                                           time.localtime(live_before.st_mtime))))
    check("  and every address it wrote went into a temp folder",
          support.startswith(work) and os.path.isdir(support),
          "support: %s" % (", ".join(sorted(os.listdir(support))) or "nothing"))

shutil.rmtree(work, ignore_errors=True)
print("\n%d checks, %d failed" % (ran, fails) if fails else
      "\n%d checks, none failed — the Windows copy joins the shop's book on its certificate alone, "
      "and the setup .exe installs like one" % ran)
raise SystemExit(1 if fails else 0)
