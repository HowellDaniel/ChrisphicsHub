#!/usr/bin/env python3
"""Prove the shop's Email accounts against a mail server that is real but worth nothing.

The counter registers one or more sending mailboxes on Shop & devices, and the book then decides
which one a client's notice leaves from. Two things make that worth a verifier rather than a read of
the code:

  * A mail password must never live in the book. The book is copied every night, mirrored to another
    folder, and carried onto a hosted server — and the repository is public. A word kept in the
    ledger is a word already leaked. So the word goes in one file beside the book at mode 600, and
    every answer this screen gets back has to be free of it.
  * "Test this account" has to mean a letter really went to a real server. A check that fakes the
    provider proves the fake, not the shop's mail.

So the provider here is a genuine one: it listens on 127.0.0.1, holds a certificate this run issued
from a throwaway CA, answers EHLO, STARTTLS, AUTH PLAIN/LOGIN, MAIL FROM, RCPT TO and DATA, and
replies 250. The engine reaches it through its own smtplib, on a real wire. The only thing bent is
where trust comes from: in the last half of this run create_default_context() is asked to load this
run's own CA instead of the Mac's keychain — nothing is added to the keychain, and certificate
checking stays switched on. That contrast is itself one of the checks: the very same mailbox,
refused under the server's own default trust and accepted under this run's CA.

Nothing here touches the shop's live book. It is read only and copied with Connection.backup into a
temp file, which is emptied of sign-ins, copies and mailboxes before a throwaway server takes a turn
on it — each with its own SUPPORT and BACKUP_DIR folders and a spare port. Three surfaces are run:
loopback (the app's own route to the book), --trust-proxy with a phone's address in front of it (so
every write must be turned away), and this Mac's own Wi-Fi number, which is how the counter's screen
really reaches the shop's book and so has to count as the shop's own computer.

The password used below is one this run made, for a provider this run made; it is never printed, and
the shop's own credentials are never asked for or sent anywhere off this machine.

Usage: python3 tools/check-email-accounts.py
"""
import base64
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE = os.path.expanduser("~/Library/Application Support/Chrisphics Hub/chrisphics.db")
OPENSSL = "/usr/bin/openssl"
APPJS = os.path.join(REPO, "public", "app.js")
WORD = secrets.token_urlsafe(18)     # a throwaway provider's throwaway app password
WRONG = "not-" + secrets.token_urlsafe(12)
CLIENT_MAIL = "throwaway.client@example.org"
SHOP_MAIL = "shop.office@example.test"
SECOND_MAIL = "back.office@example.test"

work = tempfile.mkdtemp(prefix="chrisphics-mailboxes-")
CLONE = os.path.join(work, "clone.db")
BACKUPS = os.path.join(work, "Backups")
os.makedirs(BACKUPS, exist_ok=True)

# The engine reads its fallback mail settings — and where the secrets file sits — from the
# environment at import time, so the environment is settled before the import, and nobody's
# CHRISPHICS_EMAIL_* is allowed in.
for _name in [n for n in os.environ if n.startswith("CHRISPHICS_EMAIL")]:
    del os.environ[_name]
os.environ["CHRISPHICS_SUPPORT"] = work
os.environ["CHRISPHICS_DB"] = CLONE
os.environ["CHRISPHICS_BACKUP_DIR"] = BACKUPS
sys.path.insert(0, REPO)
import server  # noqa: E402

SECRETS_FILE = server.EMAIL_SECRETS_PATH
fails = []
live_before = os.stat(LIVE)


def clean(spoken):
    """The rule every printed line obeys: neither word this run invented reaches the transcript,
    however a provider chooses to repeat it back."""
    text_value = str(spoken or "")
    for secret, name in ((WORD, "the saved word"), (WRONG, "the wrong word")):
        text_value = text_value.replace(secret, "<%s>" % name)
    return " ".join(text_value.split())[:110]


def check(label, ok, detail=""):
    print("%-58s %s %s" % (label, "ok" if ok else "FAIL", clean(detail)))
    if not ok:
        fails.append(label)


def no_word(spoken):
    return WORD not in str(spoken) and WRONG not in str(spoken)


def holds_word(raw):
    """True when one of the two words this run invented is in a file's bytes — read as bytes,
    because a book and a copy of one are not text."""
    return WORD.encode() in raw or WRONG.encode() in raw


def free_port():
    """Ask for a port nobody holds. A fixed number hides the worst failure there is: a leftover
    server from a crashed run answers the health check and every request goes to its stale book."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def own_wifi_address():
    """The number this Mac is reachable at on its own network. The counter's screen opens the shop
    at that number rather than at 127.0.0.1, so the rule about which machine is the shop's own has
    to be proved against it — and nothing is sent anywhere by asking the kernel this."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 80))  # never routed, so this only chooses a source address
        return probe.getsockname()[0]
    except OSError:
        return ""
    finally:
        probe.close()


# -------------------------------------------------- the throwaway provider: CA, leaf, and a wire
def issue_certificates():
    """A CA and a leaf for 127.0.0.1, both thrown away with this run's folder. The leaf carries a
    subjectAltName, because a client that has one ignores the common name."""
    ca_crt = os.path.join(work, "ca.crt")
    leaf_crt = os.path.join(work, "provider.crt")
    leaf_key = os.path.join(work, "provider.key")
    ext = os.path.join(work, "leaf.ext")
    with open(ext, "w", encoding="utf-8") as fh:
        fh.write("subjectAltName=IP:127.0.0.1,DNS:localhost\n"
                 "extendedKeyUsage=serverAuth\nbasicConstraints=CA:FALSE\n")
    steps = [
        [OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-keyout", os.path.join(work, "ca.key"), "-out", ca_crt,
         "-subj", "/CN=Chrisphics throwaway mail CA",
         "-addext", "basicConstraints=critical,CA:TRUE"],
        [OPENSSL, "req", "-newkey", "rsa:2048", "-nodes", "-keyout", leaf_key,
         "-out", os.path.join(work, "provider.csr"), "-subj", "/CN=127.0.0.1"],
        [OPENSSL, "x509", "-req", "-in", os.path.join(work, "provider.csr"),
         "-CA", ca_crt, "-CAkey", os.path.join(work, "ca.key"), "-CAcreateserial",
         "-out", leaf_crt, "-days", "1", "-extfile", ext],
    ]
    for step in steps:
        done = subprocess.run(step, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if done.returncode != 0:
            raise SystemExit("the throwaway certificate could not be made: "
                             + done.stderr.decode("utf-8", "replace")[:200])
    return ca_crt, leaf_crt, leaf_key


CA_CRT, LEAF_CRT, LEAF_KEY = issue_certificates()


class Wire:
    """One SMTP conversation, line-buffered, so a STARTTLS upgrade can wrap the same socket."""

    def __init__(self, sock):
        self.sock = sock
        self.buffer = b""

    def line(self):
        while b"\n" not in self.buffer:
            try:
                chunk = self.sock.recv(4096)
            except (OSError, ssl.SSLError):
                return None
            if not chunk:
                return None
            self.buffer += chunk
        cut = self.buffer.index(b"\n")
        spoken, self.buffer = self.buffer[:cut], self.buffer[cut + 1:]
        return spoken.decode("utf-8", "replace").rstrip("\r\n")

    def say(self, text_value):
        self.sock.sendall((text_value + "\r\n").encode())

    def upgrade(self, context):
        self.sock = context.wrap_socket(self.sock, server_side=True)
        self.buffer = b""


class ThrowawayProvider:
    """A stand-in mail server answering enough SMTP, over a real TLS channel, for the shop's own
    smtplib to believe it. It keeps what crossed the wire, including the one thing that must never
    reach the book."""

    def __init__(self):
        self.letters = []
        self.logins = []
        self.word = WORD
        self.reject_data = ""
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(LEAF_CRT, LEAF_KEY)
        self.socket = socket.socket()
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(8)
        self.socket.settimeout(0.2)
        self.port = self.socket.getsockname()[1]
        self.stopped = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self.stopped.set()
        try:
            self.socket.close()
        except OSError:
            pass

    def _accept(self):
        while not self.stopped.is_set():
            try:
                conn, _peer = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.settimeout(25)
            threading.Thread(target=self._session, args=(conn,), daemon=True).start()

    def _session(self, conn):
        try:
            self._talk(Wire(conn))
        except (OSError, ssl.SSLError, ValueError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _talk(self, wire):
        wire.say("220 smtp.throwaway.test ESMTP the shop's own check")
        letter = {"from": "", "rcpt": [], "data": ""}
        while True:
            spoken = wire.line()
            if spoken is None:
                return
            verb = spoken.strip().split(" ", 1)[0].upper()
            rest = spoken.strip()[len(verb):].strip()
            if verb in ("EHLO", "HELO"):
                if verb == "EHLO":
                    wire.say("250-smtp.throwaway.test")
                    wire.say("250-SIZE 5242880")
                    wire.say("250-STARTTLS")
                    wire.say("250-AUTH PLAIN LOGIN")
                    wire.say("250 Ok")
                else:
                    wire.say("250 Ok")
            elif verb == "STARTTLS":
                wire.say("220 Ready to start TLS")
                wire.upgrade(self.context)
            elif verb == "AUTH":
                # The answer goes out and the conversation stays open until the other side ends it.
                # Hanging up in the same breath as a 535 cuts the reply off before the shop's own
                # smtplib has read it, and the check would then be proving a dropped connection
                # instead of the refused word it set out to prove.
                self._auth(wire, rest)
            elif verb == "MAIL":
                letter["from"] = rest
                wire.say("250 2.1.0 Ok")
            elif verb == "RCPT":
                letter["rcpt"].append(rest)
                wire.say("250 2.1.5 Ok")
            elif verb == "DATA":
                wire.say("354 End data with <CR><LF>.<CR><LF>")
                body = []
                while True:
                    row = wire.line()
                    if row is None or row == ".":
                        break
                    body.append(row)
                letter["data"] = "\n".join(body)
                self.letters.append(dict(letter))
                if self.reject_data:
                    wire.say("554 5.0.0 " + self.reject_data)
                    letter = {"from": "", "rcpt": [], "data": ""}
                    continue
                wire.say("250 2.0.0 Ok: queued as THROWAWAY1")
                letter = {"from": "", "rcpt": [], "data": ""}
            elif verb == "QUIT":
                wire.say("221 Bye")
                return
            else:
                wire.say("250 Ok")

    def _auth(self, wire, rest):
        user = password = ""
        scheme = rest.split(" ", 1)[0].upper()
        if scheme == "PLAIN" and " " in rest:
            parts = base64.b64decode(rest.split(" ", 1)[1]).split(b"\x00")
            if len(parts) >= 3:
                user = parts[1].decode("utf-8", "replace")
                password = parts[2].decode("utf-8", "replace")
        elif scheme == "LOGIN":
            wire.say("334 " + base64.b64encode(b"Username:").decode())
            user = self._b64(wire.line())
            wire.say("334 " + base64.b64encode(b"Password:").decode())
            password = self._b64(wire.line())
        else:
            wire.say("504 Unrecognized authentication type")
            return False
        self.logins.append((user, password))
        if password == self.word:
            wire.say("235 2.7.0 Authentication successful")
            return True
        wire.say("535 5.7.8 Authentication credentials invalid")
        return False

    @staticmethod
    def _b64(spoken):
        try:
            return base64.b64decode(spoken or "").decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - a half-sent challenge is not worth a stack trace
            return ""


PROVIDER = ThrowawayProvider()
HOST = "127.0.0.1"


# ---------------------------------------------------------------------- the copy of the book
def book(sql_text, args=()):
    handle = sqlite3.connect(CLONE)
    handle.execute(sql_text, args)
    handle.commit()
    handle.close()


def rows(sql_text, args=()):
    """Read the copy of the book. A plain connection rather than mode=ro: the copy comes over in
    WAL mode because the shop's own book is one, and a read-only handle on a WAL database wants a
    -shm file beside it that this run has none of. Only SELECTs go through here."""
    handle = sqlite3.connect(CLONE)
    handle.row_factory = sqlite3.Row
    found = [dict(r) for r in handle.execute(sql_text, args).fetchall()]
    handle.close()
    return found


try:
    source = sqlite3.connect("file:%s?mode=ro" % LIVE, uri=True)
except sqlite3.Error:
    source = sqlite3.connect(LIVE)
target = sqlite3.connect(CLONE)
source.backup(target)
target.close()
source.close()

BEFORE = {table: rows("SELECT count(*) AS n FROM %s" % table)[0]["n"]
          for table in ("jobs", "clients", "payments", "expenses")}
# Where the shop got to is none of this run's business: its sign-in word, the devices holding
# cookies, the security trail and any mailbox already registered are taken off the copy, and the
# email_accounts table is dropped so its arrival is something this run proves rather than inherits.
# Nothing below ever needs the shop's real word.
for key in ("shop_password", "shop_mac_login", "last_backup_at", "last_backup_file",
            "last_backup_note", "last_backup_error", "last_backup_error_at"):
    book("DELETE FROM app_state WHERE key = ?", (key,))
book("DROP TABLE IF EXISTS email_accounts")
book("DELETE FROM notifications")
book("DELETE FROM money_signals")
for table in ("sessions", "security_log"):
    if rows("SELECT 1 AS n FROM sqlite_master WHERE type='table' AND name=?", (table,)):
        book("DELETE FROM %s" % table)

# --------------------------------------------------------------------------- the HTTP surface
def ask(method, path, payload=None, port=None, headers=None, host="127.0.0.1"):
    data = None if payload is None else json.dumps(payload).encode()
    heads = {"Content-Type": "application/json"}
    heads.update(headers or {})
    request = urllib.request.Request("http://%s:%d%s" % (host, port, path), data=data,
                                     method=method, headers=heads)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body, code = response.read(), response.status
    except urllib.error.HTTPError as err:
        body, code = err.read(), err.code
    except Exception as error:  # noqa: BLE001
        return 0, {"error": "%s: %s" % (type(error).__name__, error)}, ""
    text_value = body.decode("utf-8", "replace")
    try:
        return code, json.loads(text_value or "{}"), text_value
    except ValueError:
        return code, {"error": text_value[:140]}, text_value


def start(port, args=(), tag="main", host="127.0.0.1"):
    env = dict(os.environ, CHRISPHICS_DB=CLONE, CHRISPHICS_SUPPORT=work,
               CHRISPHICS_BACKUP_DIR=BACKUPS, PYTHONUNBUFFERED="1",
               CHRISPHICS_BACKUP_CHECK="3600")
    handle = open(os.path.join(work, "server-%s.log" % tag), "wb")
    proc = subprocess.Popen(["/usr/bin/python3", os.path.join(REPO, "server.py"),
                             "--host", host, "--port", str(port), "--no-browser"] + list(args),
                            cwd=REPO, env=env, stdout=handle, stderr=handle)
    proc.chrisphics_log = handle
    for _ in range(80):
        if ask("GET", "/healthz", port=port, host=host)[0] == 200:
            return proc
        time.sleep(0.15)
    raise SystemExit("the test server on port %d never answered; see server-%s.log" % (port, tag))


def stop(proc):
    proc.terminate()
    try:
        proc.wait(timeout=12)
    except subprocess.TimeoutExpired:
        proc.kill()
    proc.chrisphics_log.close()


def mailbox_body(label, address, word=WORD, host=HOST, port=None, name="CRISPprint Ghana"):
    return {"label": label, "from_address": address, "from_name": name, "smtp_host": host,
            "smtp_port": port or PROVIDER.port, "smtp_security": "starttls",
            "username": "", "password": word}


def by_label(body):
    return {a["label"]: a for a in body.get("accounts", [])}


print("\n--- registering a mailbox on the shop's own Mac")
PORT_A = free_port()
counter = start(PORT_A, tag="counter")
try:
    code, first, raw_first = ask("GET", "/api/email-accounts", port=PORT_A)
    check("GET /api/email-accounts answers with the shop's mailboxes",
          code == 200 and "accounts" in first and "sending" in first, str(first)[:70])
    check("the table arrives in a copy of the shop's own book",
          bool(rows("SELECT 1 AS n FROM sqlite_master WHERE type='table'"
                    " AND name='email_accounts'")), "created from schema.sql on the first run")
    check("with nothing registered, email says what it is waiting for",
          first.get("sending", "").startswith("Configure CHRISPHICS_EMAIL_"), first.get("sending"))

    for label, payload, phrase in [
            ("a mailbox with no name", {**mailbox_body("Desk", SHOP_MAIL), "label": ""},
             "A name for the mailbox"),
            ("a reply address that is not one", mailbox_body("Desk", "shop-at-mail"),
             "real email address"),
            ("a mail server with a space in it", mailbox_body("Desk", SHOP_MAIL, host="smtp x"),
             "no spaces or address"),
            ("a port of ninety thousand", {**mailbox_body("Desk", SHOP_MAIL), "smtp_port": 90000},
             "between 1 and 65535"),
            ("a port written as a word", {**mailbox_body("Desk", SHOP_MAIL), "smtp_port": "soon"},
             "a number, like 587"),
            ("a connection called 'maybe'", {**mailbox_body("Desk", SHOP_MAIL),
                                             "smtp_security": "maybe"}, "starttls or ssl"),
            ("a new mailbox with no password", {**mailbox_body("Desk", SHOP_MAIL), "password": ""},
             "needs its mail password")]:
        code, answer, _raw = ask("POST", "/api/email-accounts", payload, port=PORT_A)
        check("refuses " + label, code == 400 and phrase in answer.get("error", ""),
              answer.get("error"))

    code, added, raw_added = ask("POST", "/api/email-accounts",
                                 mailbox_body("Shop office", SHOP_MAIL), port=PORT_A)
    office = by_label(added).get("Shop office") or {}
    check("adds a mailbox and marks the first one as the sender",
          code == 201 and office.get("is_default") and office.get("has_password"), str(office))
    check("the new mailbox lists the server and port it was given",
          office.get("smtp_host") == HOST and int(office.get("smtp_port", 0)) == PROVIDER.port,
          "%s:%s" % (office.get("smtp_host"), office.get("smtp_port")))

    code, second_body, _raw = ask("POST", "/api/email-accounts",
                                  mailbox_body("Back office", SECOND_MAIL), port=PORT_A)
    back = by_label(second_body).get("Back office") or {}
    check("a second mailbox waits its turn rather than taking the sender's place",
          code == 201 and back and not back["is_default"], str(back))
    check("and the book keeps exactly one sender",
          len(rows("SELECT id FROM email_accounts WHERE is_default=1")) == 1)

    code, moved, _raw = ask("POST", "/api/email-accounts/%d/default" % back["id"], {}, port=PORT_A)
    chosen = by_label(moved)
    check("the counter can hand the sender's place to another mailbox",
          code == 200 and chosen["Back office"]["is_default"]
          and not chosen["Shop office"]["is_default"],
          "%s · %s" % (code, moved.get("error") or
                       " · ".join("%s=%s" % (k, v["is_default"]) for k, v in chosen.items())))
    check("with a sender registered, email says nothing is blocking it",
          ask("GET", "/api/email-accounts", port=PORT_A)[1].get("sending") == "")

    code, changed, _raw = ask("PUT", "/api/email-accounts/%d" % chosen["Shop office"]["id"],
                              {**mailbox_body("Shop office", SHOP_MAIL,
                                              name="CRISPprint Ghana, Accra"), "password": ""},
                              port=PORT_A)
    kept = by_label(changed).get("Shop office") or {}
    check("an edit that leaves the password box empty keeps the saved word",
          code == 200 and kept.get("has_password"), changed.get("error"))
    check("and it takes the display name the counter typed",
          kept.get("from_name", "").endswith("Accra"), kept.get("from_name"))
    code, answer, _raw = ask("PUT", "/api/email-accounts/424242", mailbox_body("Nobody", SHOP_MAIL),
                             port=PORT_A)
    check("changing a mailbox that is not in the book is answered honestly",
          code == 404 and "no longer" in answer.get("error", ""), answer.get("error"))

    # The provider is real, but the shop's server has no reason to trust a certificate this run made
    # up — so this is also the check that certificate verification is genuinely switched on.
    sender = by_label(ask("GET", "/api/email-accounts", port=PORT_A)[1])["Back office"]
    code, answer, raw_answer = ask("POST", "/api/email-accounts/%d/test" % sender["id"],
                                   {"to": CLIENT_MAIL}, port=PORT_A)
    check("'Test this account' against a certificate the Mac does not trust is refused",
          code == 400 and "could not be sent through" in answer.get("error", ""), answer.get("error"))
    check("a mail server's refusal is a 400 about the mail, not a 500 about the shop's Mac",
          "certificate" in answer.get("error", "").lower(), answer.get("error"))
    shown = by_label(ask("GET", "/api/email-accounts", port=PORT_A)[1])["Back office"]
    check("the provider's answer is written on the mailbox for the counter to read",
          bool(shown["last_error"]) and shown["last_sent_at"] is None, shown["last_error"])
    check("and no answer this screen got ever carried a word",
          no_word(raw_first + raw_added + raw_answer))

    code, copy, _raw = ask("POST", "/api/backups", {}, port=PORT_A)
    copy_file = os.path.join(BACKUPS, copy.get("file", ""))
    copied = open(copy_file, "rb").read() if os.path.isfile(copy_file) else b""
    check("a copy of the book carries no mail password", bool(copied) and not holds_word(copied),
          "%d bytes copied" % len(copied))
    check("the book itself holds no mail password", not holds_word(open(CLONE, "rb").read()))
    check("the secret file sits beside the book, not among the copies",
          os.path.isfile(SECRETS_FILE) and "email-secrets.json" not in os.listdir(BACKUPS),
          os.path.basename(SECRETS_FILE))
    mode = oct(os.stat(SECRETS_FILE).st_mode & 0o777) if os.path.isfile(SECRETS_FILE) else "missing"
    check("and that file is readable only by this Mac's own user", mode == "0o600", mode)

    trail = {r["kind"]: r["detail"] for r in rows("SELECT kind, detail FROM security_log")}
    check("the book remembers which mailboxes were added and chosen",
          "mailbox_added" in trail and "mailbox_chosen" in trail,
          " ".join(sorted(k for k in trail if k.startswith("mailbox"))))
    check("and its notes name a mailbox by number, never by word",
          all(no_word(detail) for detail in trail.values()), str(sorted(trail)))

    code, answer, _raw = ask("POST", "/api/email-accounts", mailbox_body("Elsewhere", SHOP_MAIL),
                             port=PORT_A, headers={"Origin": "https://somewhere-else.test"})
    check("a page on another website cannot register a mailbox",
          code == 403 and no_word(str(answer)), str(answer)[:70])
    ids = {label: row["id"] for label, row in by_label(
        ask("GET", "/api/email-accounts", port=PORT_A)[1]).items()}
    notes_after_counter = len(rows("SELECT id FROM security_log WHERE kind LIKE 'mailbox%'"))
finally:
    stop(counter)

print("\n--- a phone out on the shop Wi-Fi")
PORT_B = free_port()
wifi = start(PORT_B, ("--trust-proxy",), tag="wifi")
PHONE = {"X-Forwarded-For": "192.168.100.66"}
try:
    code, list_body, raw_list = ask("GET", "/api/email-accounts", port=PORT_B, headers=PHONE)
    check("a phone can read which mailbox letters go out through",
          code == 200 and len(list_body.get("accounts", [])) == 2,
          " · ".join(a["label"] for a in list_body.get("accounts", [])))
    check("and what it reads carries no password", no_word(raw_list))
    for method, path, payload, label in [
            ("POST", "/api/email-accounts", mailbox_body("From a phone", SHOP_MAIL),
             "register a mailbox"),
            ("PUT", "/api/email-accounts/%d" % ids["Shop office"],
             mailbox_body("Rewritten", SHOP_MAIL), "change one"),
            ("DELETE", "/api/email-accounts/%d" % ids["Shop office"], {}, "forget one"),
            ("POST", "/api/email-accounts/%d/default" % ids["Shop office"], {},
             "choose which one sends"),
            ("POST", "/api/email-accounts/%d/test" % ids["Shop office"], {},
             "send a letter through one")]:
        code, answer, _raw = ask(method, path, payload, port=PORT_B, headers=PHONE)
        check("a phone cannot %s" % label,
              code == 403 and "shop's own" in answer.get("error", ""),
              "%s · %s" % (code, answer.get("error")))
    check("and none of those attempts left a mailbox or a note in the book",
          len(by_label(ask("GET", "/api/email-accounts", port=PORT_B, headers=PHONE)[1])) == 2
          and len(rows("SELECT id FROM security_log WHERE kind LIKE 'mailbox%'"))
          == notes_after_counter,
          "%d notes" % len(rows("SELECT id FROM security_log WHERE kind LIKE 'mailbox%'")))
finally:
    stop(wifi)

# ------------------- the shop's own Mac, which reaches its own book over its Wi-Fi number
print("\n--- the shop's own Mac, arriving at its own Wi-Fi number")


class LocalSocket:
    def __init__(self, local):
        self.local = local

    def getsockname(self):
        if self.local is None:
            raise OSError("the socket has gone away")
        return (self.local, 8834)


def arrived(peer, local):
    """Just enough of a request for the shop's-own-Mac rule to answer for: the address the visitor
    came from and the address the shop's server was listening on. Handler.__init__ wants a real
    socket, a real book and a real browser, so the object is made without it and only these two
    facts are put in."""
    visitor = object.__new__(server.Handler)
    visitor.client_address = (peer, 51000)
    visitor.connection = LocalSocket(local)
    return visitor


LAN = own_wifi_address()
if not LAN:
    check("this Mac has a Wi-Fi number the counter arrives at", False, "none found")
else:
    PORT_OWN = free_port()
    # The book refuses to open to a network without a sign-in word, and this Mac has no word set —
    # so this throwaway server carries the same flag the shop's own always-on server runs under.
    # It is not softening anything being tested here: the mailbox gate is a separate rule about
    # which machine a request arrives from, and the phase below still proves a foreign address is
    # turned away by it.
    here = start(PORT_OWN, ("--allow-unauthenticated-lan",), tag="own-number", host=LAN)
    try:
        code, answer, _raw = ask("POST", "/api/email-accounts",
                                 {"label": "Probe", "from_address": "not-an-address"},
                                 port=PORT_OWN, host=LAN)
        check("the counter reaching its own Mac over that number is not turned away for its address",
              code == 400 and "shop's own" not in answer.get("error", ""),
              "%s · %s" % (code, answer.get("error")))
        code, added, _raw = ask("POST", "/api/email-accounts",
                                mailbox_body("Counter at the till", SHOP_MAIL),
                                port=PORT_OWN, host=LAN)
        wifi_id = (added or {}).get("id")
        check("and a mailbox really does register from that screen",
              code == 201 and "Counter at the till" in by_label(added)
              and by_label(added)["Counter at the till"]["is_default"] is False,
              "%s · %s" % (code, " ".join(by_label(added))))
        code, after, _raw = (ask("DELETE", "/api/email-accounts/%d" % wifi_id, {},
                                 port=PORT_OWN, host=LAN) if wifi_id else (0, {"accounts": []}, ""))
        check("and forgetting it leaves the two mailboxes the rest of this run reads",
              code == 200 and sorted(by_label(after)) == ["Back office", "Shop office"],
              " · ".join(sorted(by_label(after))))
        check("so the only machine that can settle a mailbox is this Mac, on either of its numbers",
              all(row["id"] != wifi_id for row in rows("SELECT id FROM email_accounts")))
    finally:
        stop(here)
    check("a phone out on the Wi-Fi arriving at the shop's number is not this Mac",
          not arrived("192.168.100.66", LAN).is_shop_computer())
    check("this Mac arriving at its own Wi-Fi number is this Mac",
          arrived(LAN, LAN).is_shop_computer())
    check("and the loopback route still counts, the way the app has always used it",
          arrived("127.0.0.1", "127.0.0.1").is_shop_computer())
    check("a socket that cannot say where it was listening is turned away, not let in",
          not arrived(LAN, None).is_shop_computer())

# --------------------------------------------- the same mailbox, with this run's CA trusted
print("\n--- a real letter through the default mailbox")


class TrustsThisRunOnly:
    """Everything the stdlib ssl module offers, with one change: the anchor is this run's throwaway
    CA. Certificate checking stays switched on — a test provider cannot be put into the shop's
    keychain, and this run will not put it there."""

    def __init__(self, ca):
        self.ca = ca

    def create_default_context(self, *args, **kwargs):
        context = ssl.create_default_context(*args, **kwargs)
        context.load_verify_locations(cafile=self.ca)
        return context

    def __getattr__(self, name):
        return getattr(ssl, name)


server.ssl = TrustsThisRunOnly(CA_CRT)
server.connect(CLONE)
listed = {a["label"]: a for a in server.email_account_view()}
sender_now = [a for a in listed.values() if a["is_default"]][0]
office_id = listed["Shop office"]["id"]
before = len(PROVIDER.letters)
try:
    answer = server.test_email_account(sender_now["id"], CLIENT_MAIL)
except Exception as error:  # noqa: BLE001
    answer = {"error": "%s: %s" % (type(error).__name__, error)}
letter = PROVIDER.letters[-1] if len(PROVIDER.letters) > before else {}
check("the same mailbox the shop's own trust refused now sends",
      answer.get("sent_to") == CLIENT_MAIL, answer.get("error") or "accepted")
check("the letter went over a real TLS channel to a real server",
      bool(PROVIDER.logins) and PROVIDER.logins[-1][1] == WORD,
      "%d sign-ins seen" % len(PROVIDER.logins))
check("with no sign-in name typed it logged in as the mailbox's own address",
      PROVIDER.logins and PROVIDER.logins[-1][0] == sender_now["from_address"],
      str([user for user, _w in PROVIDER.logins][-1:]))
check("the envelope went to the address the counter typed",
      CLIENT_MAIL in " ".join(letter.get("rcpt", [])), str(letter.get("rcpt")))
check("and came from the mailbox's own address",
      sender_now["from_address"] in letter.get("from", ""), letter.get("from"))
check("it carried the test subject the screen promises",
      "test from Chrisphics Hub" in letter.get("data", ""),
      (re.search(r"^Subject: (.*)$", letter.get("data", ""), re.M) or ["", ""])[1]
      if re.search(r"^Subject: (.*)$", letter.get("data", ""), re.M) else "")
sent_row = rows("SELECT last_sent_at, last_error FROM email_accounts WHERE id=?",
                (sender_now["id"],))[0]
check("the mailbox is marked as having sent, its old refusal cleared",
      sent_row["last_sent_at"] and not sent_row["last_error"], str(sent_row))

print("\n--- a client's job notice goes out by itself")
book("INSERT INTO clients (name, email, email_updates) VALUES (?, ?, 1)",
     ("Throwaway Print Client", CLIENT_MAIL))
client_id = rows("SELECT id FROM clients WHERE email=?", (CLIENT_MAIL,))[0]["id"]
book("INSERT INTO jobs (id, ref, client_id, kind, title, category, quantity, unit, unit_price,"
     " status) VALUES (931374, 'CH-CHECK-931374', ?, 'Job', 'Throwaway check job', 'Flyer',"
     " 100, 'pcs', 1.25, 'Ready')", (client_id,))
server.queue_message(931374, "Ready")
queued = rows("SELECT id, channel, state, delivery_state, to_address, auto_send"
              " FROM notifications WHERE job_id=931374")
check("the Ready notice is queued once, for email, waiting on the worker",
      [r["channel"] for r in queued] == ["Email"] and queued[0]["auto_send"] == 1
      and queued[0]["delivery_state"] == "Pending", " · ".join(r["channel"] for r in queued))
time.sleep(3.5)          # the worker is given a short breathing space, as in the shop
server.deliver_pending_notifications()
delivered = rows("SELECT state, delivery_state, provider_id, delivery_error FROM notifications"
                 " WHERE job_id=931374")[0]
notice = PROVIDER.letters[-1]
check("the delivery worker sends it through the mailbox the shop chose",
      delivered["state"] == "Sent" and delivered["delivery_state"] == "Sent",
      delivered["delivery_error"] or str(delivered))
check("the provider's own receipt is kept against the message",
      bool(delivered["provider_id"]), delivered["provider_id"])
check("it went to the client's address, not the shop's",
      CLIENT_MAIL in " ".join(notice["rcpt"]), str(notice["rcpt"]))
check("and the letter is the shop's own words about that job",
      "CH-CHECK-931374" in notice["data"] and "ready for collection" in notice["data"],
      (re.search(r"^Subject: (.*)$", notice["data"], re.M).group(1))
      if re.search(r"^Subject: (.*)$", notice["data"], re.M) else "")
check("the job's history says the update went out",
      bool(rows("SELECT id FROM job_events WHERE job_id=931374 AND detail LIKE '%sent to%Email%'")),
      str(rows("SELECT detail FROM job_events WHERE job_id=931374")[-1:]))

print("\n--- what a mail server can say back")
PROVIDER.word = WRONG
try:
    server.test_email_account(office_id, CLIENT_MAIL)
    refused = {"error": "it sent, which it should not have"}
except RuntimeError as error:
    refused = {"error": str(error)}
PROVIDER.word = WORD
check("a wrong word is answered in the shop's words, naming the mailbox",
      "refused the word saved for" in refused["error"]
      and SHOP_MAIL in refused["error"], refused["error"])
marked = rows("SELECT last_error FROM email_accounts WHERE id=?", (office_id,))[0]["last_error"]
check("the mailbox is left showing why it did not send", bool(marked), marked)
check("and neither the answer nor the note the book keeps holds any word",
      no_word(refused["error"]) and no_word(marked))

PROVIDER.reject_data = "Message rejected for %s (user %s)" % (WORD, SHOP_MAIL)
try:
    server.test_email_account(sender_now["id"], CLIENT_MAIL)
    echoed = {"error": "it sent, which it should not have"}
except RuntimeError as error:
    echoed = {"error": str(error)}
PROVIDER.reject_data = ""
check("a provider that repeats the word back is answered with it hidden",
      "[hidden]" in echoed["error"] and WORD not in echoed["error"], echoed["error"])
echoed_row = rows("SELECT last_error FROM email_accounts WHERE id=?", (sender_now["id"],))[0]
check("and the hidden form is what the book keeps",
      "[hidden]" in echoed_row["last_error"] and no_word(echoed_row["last_error"]),
      echoed_row["last_error"])
child_log = open(os.path.join(work, "server-counter.log"), encoding="utf-8", errors="replace").read()
check("nothing this run wrote to a server's log carried a word", no_word(child_log))
check("and the list the counter's screen is handed never carries one",
      no_word(json.dumps(server.email_account_view())))

print("\n--- the book's mailbox beats the machine's environment")
server.EMAIL_SMTP_HOST = "smtp.decoy.example"
server.EMAIL_SMTP_PORT = "2525"
server.EMAIL_SMTP_USERNAME = "decoy@decoy.example"
server.EMAIL_SMTP_PASSWORD = WRONG
server.EMAIL_FROM = "decoy@decoy.example"
server.EMAIL_SMTP_SECURITY = "starttls"
chosen, why = server.email_settings()
check("a mailbox written down in the app sends, whatever the environment says",
      chosen and chosen["host"] == HOST and chosen["account_id"],
      "%s · %s" % ((chosen or {}).get("host"), why))
check("and the environment's word is never the one taken", chosen and chosen["password"] != WRONG)
server.set_email_secret(office_id, "")
forgotten = json.loads(open(SECRETS_FILE, encoding="utf-8").read())
check("a mailbox's word can be taken out of the file while the mailbox itself stays",
      str(office_id) not in forgotten, "%d words left beside the book" % len(forgotten))
silent = server.email_settings(rows("SELECT * FROM email_accounts WHERE id=?", (office_id,))[0])
check("a mailbox with no saved word says so instead of failing quietly",
      silent[0] is None and "has no password saved" in silent[1], silent[1])
server.save_email_account({"label": "Shop office", "from_address": SHOP_MAIL,
                           "from_name": "CRISPprint Ghana", "smtp_host": HOST,
                           "smtp_port": PROVIDER.port, "smtp_security": "starttls",
                           "username": "", "password": WORD}, office_id)
check("typing it once puts it back and the mailbox can send again",
      str(office_id) in json.loads(open(SECRETS_FILE, encoding="utf-8").read())
      and server.email_settings(rows("SELECT * FROM email_accounts WHERE id=?", (office_id,))[0])[0],
      SHOP_MAIL)

sent_once = [a for a in server.email_account_view() if a["is_default"]][0]
server.delete_email_account(sent_once["id"])
rest = server.email_account_view()
check("a mailbox removed as the sender leaves another one sending",
      len(rest) == 1 and rest[0]["is_default"],
      " · ".join("%s=%s" % (a["label"], a["is_default"]) for a in rest))
check("and forgetting a mailbox takes its word out of the file with it",
      str(sent_once["id"]) not in json.loads(open(SECRETS_FILE, encoding="utf-8").read()),
      "%d words left" % len(json.loads(open(SECRETS_FILE, encoding="utf-8").read())))
for account in rest:
    server.delete_email_account(account["id"])
back_to_env = server.email_settings()
check("with no mailbox left, email falls back to the environment",
      back_to_env[0] and back_to_env[0]["host"] == "smtp.decoy.example", str(back_to_env[0])[:60])
for name in ("EMAIL_SMTP_HOST", "EMAIL_SMTP_USERNAME", "EMAIL_SMTP_PASSWORD", "EMAIL_FROM"):
    setattr(server, name, "")
check("with neither a mailbox nor an environment, the screen is told what is missing",
      server.notification_config_error("Email").startswith("Configure CHRISPHICS_EMAIL_"),
      server.notification_config_error("Email"))
check("and the file beside the book is left with nothing in it",
      not json.loads(open(SECRETS_FILE, encoding="utf-8").read())
      and not rows("SELECT id FROM email_accounts"),
      "%d mailboxes" % len(rows("SELECT id FROM email_accounts")))

print("\n--- the counter's own screen")
appjs = open(APPJS, encoding="utf-8").read()
check("Shop & devices shows the Email accounts card",
      "function emailAccountsCard" in appjs and "emailAccountsCard(mail, shopMac)" in appjs)
check("it asks the book at /api/email-accounts", "'/api/email-accounts'" in appjs)
check("it offers Test this account and Make this the sender",
      'data-action="email-test"' in appjs and 'data-action="email-default"' in appjs)
check("its password box is a password box, and is never filled back in",
      'name="password" type="password"' in appjs
      and not re.search(r'name="password"[^>]*value=', appjs))
check("and it tells the counter where the word really goes", "beside the book" in appjs)

print("\n--- what the checks cost the records")
after = {table: rows("SELECT count(*) AS n FROM %s" % table)[0]["n"] for table in BEFORE}
moved = sorted("%s %d->%d" % (table, BEFORE[table], after[table]) for table in BEFORE
               if after[table] != BEFORE[table])
check("the only records written are the throwaway job and client, in the copy",
      [m.split()[0] for m in moved] == ["clients", "jobs"], ", ".join(moved))
check("and no payment or expense moved",
      all(m.split()[0] not in ("payments", "expenses") for m in moved), ", ".join(moved))

live_after = os.stat(LIVE)
untouched = (live_after.st_size == live_before.st_size
             and live_after.st_mtime == live_before.st_mtime)
print("\nLive book: %d bytes, last written %s -- %s" % (
    live_after.st_size, time.ctime(live_after.st_mtime),
    "untouched" if untouched else "CHANGED, WHICH MUST NOT HAPPEN"))
if not untouched:
    fails.append("the shop's own book was written to")

PROVIDER.close()
server._db.close()
shutil.rmtree(work, ignore_errors=True)

print("\n%s" % ("all checks passed" if not fails else "FAILED: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
