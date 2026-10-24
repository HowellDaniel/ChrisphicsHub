#!/usr/bin/env python3
"""Prove the book's copy can be posted to a mailbox — without a real mailbox, and without this
shop's records being read or touched.

The letter is built by the shipped `send_over_email`, so what is examined is the real message: who
it goes to, the file riding inside it, and whether that file opens as a whole book once it has been
taken back out of the letter. Only the provider's end of the wire is stood in for, because a check
that emailed a live mailbox every time it ran would be the shop sending its ledger to somebody.

Everything happens against a throwaway book in a temp folder with a throwaway mailbox. The shop's
own book is opened read-only at the end to prove this check wrote nothing there.

    python3 tools/check-book-mail.py
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE_BOOK = os.path.expanduser("~/Library/Application Support/Chrisphics Hub/chrisphics.db")

failures = []
checks = 0


def check(label, ok, detail=""):
    global checks
    checks += 1
    if not ok:
        failures.append(label)
    print("%-62s %s %s" % (label[:62], "ok" if ok else "FAIL", str(detail)[:150]))


WORD = "canary-%s" % os.urandom(3).hex()
ADDRESS = "shop-backups@example.com"
CLIENT = "Mefaum Trading Ltd"
live_bytes_before = os.stat(LIVE_BOOK).st_size

temp = tempfile.mkdtemp(prefix="book-mail-")
book = os.path.join(temp, "chrisphics.db")
support = os.path.join(temp, "support")
backups = os.path.join(temp, "backups")
os.makedirs(support)
os.makedirs(backups)

# The environment is set before the import: this module reads its own folders once, at import.
os.environ["CHRISPHICS_DB"] = book
os.environ["CHRISPHICS_SUPPORT"] = support
os.environ["CHRISPHICS_BACKUP_DIR"] = backups
os.environ["CHRISPHICS_BACKUP_CHECK"] = "3600"
sys.path.insert(0, REPO)
import server  # noqa: E402

server.connect()


class FakeProvider:
    """The provider's side of the conversation, inside this process. It answers a well-formed
    client the way Gmail does, and can be told to refuse at the door instead."""

    class SMTPException(Exception):
        pass

    class SMTPAuthenticationError(SMTPException):
        pass

    class SMTPServerDisconnected(SMTPException):
        pass

    sessions = []
    refuse_at = None          # None, "login", "hangup-login", "hangup-send"

    def __init__(self, host, port, timeout=0, context=None):
        self.host, self.port = host, port
        self.sent = []
        self.secure = context is not None
        FakeProvider.sessions.append(self)

    def ehlo(self, *a):
        self.esmtp_features = {"size": "26214400"}

    def starttls(self, context=None, **kwargs):
        self.secure = True

    def login(self, user, password):
        self.user, self.password = user, password
        if FakeProvider.refuse_at == "login":
            raise FakeProvider.SMTPAuthenticationError(535, b"Username and Password not accepted")
        if FakeProvider.refuse_at == "hangup-login":
            raise FakeProvider.SMTPServerDisconnected("Connection unexpectedly closed")

    def send_message(self, message, **kwargs):
        if FakeProvider.refuse_at == "hangup-send":
            raise FakeProvider.SMTPServerDisconnected("Connection unexpectedly closed")
        self.sent.append(message)
        return {}

    def quit(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


FakeProvider.SMTP = FakeProvider            # the starttls path
FakeProvider.SMTP_SSL = FakeProvider        # and the ssl one
server.smtplib = FakeProvider               # send_over_email looks the module up by name

server._db.execute("INSERT INTO email_accounts (id, label, from_address, smtp_host, smtp_port,"
                   " smtp_security, is_default) VALUES (1, 'Shop Gmail', ?,"
                   " 'smtp.gmail.com', 587, 'starttls', 1)", ("shop.sender@example.com",))
server.create_client({"name": CLIENT, "phone": "0535583460"})
server._db.commit()
server.write_email_secrets({"1": WORD})

# ── nothing asked for, nothing posted ──────────────────────────────────────────────────────────
check("with no address set, no conversation with a provider is opened",
      server.backup_mail() == "" and not FakeProvider.sessions)
before = len(os.listdir(backups))
server.run_backup("backup")
check("the copy is still made when nobody asked for a letter",
      len(os.listdir(backups)) == before + 1)
report = server.backup_report()
check("and the card claims neither a posting nor a problem",
      report["mail_set"] is False and report["mail_ready"] is False
      and report["mail_problem"] == "" and report["mail_note"] == "")

# ── the address is settled the way the shop's own screen settles it ────────────────────────────
try:
    server.set_backup_settings({"mail": "not an address"})
    check("a nonsense address was accepted", False)
except ValueError as err:
    check("a nonsense address is refused before it is saved", True, str(err)[:60])
check("the refused address never reached the book", server.backup_mail() == "")

answer = server.set_backup_settings({"mail": "  Shop-Backups@Example.COM  "})
check("a real address is saved, lower-cased the way a mailbox spells it",
      server.backup_mail() == ADDRESS, server.backup_mail())
check("the save answers with that address and says the mailbox is ready",
      answer["mail"] == ADDRESS and answer["mail_ready"] is True)
check("and the mailbox's word is not in the answer the screen gets",
      WORD not in json.dumps(answer))

# ── one run, and the letter it produces ────────────────────────────────────────────────────────
FakeProvider.sessions = []
path, counts, _tidy = server.run_backup("backup")
sessions = FakeProvider.sessions
letters = [m for s in sessions for m in s.sent]
check("the copy run opened exactly one conversation with the provider",
      len(sessions) == 1, "%d" % len(sessions))
check("and left exactly one letter in it", len(letters) == 1, "%d" % len(letters))
session = sessions[0] if sessions else None
check("through the mailbox the shop named, over starttls",
      session is not None and session.host == "smtp.gmail.com" and session.port == 587
      and session.secure and session.user == "shop.sender@example.com",
      "%s:%s" % (getattr(session, "host", ""), getattr(session, "port", "")))
check("using the word saved for it, handed over whole",
      session is not None and session.password == WORD)

letter = letters[0] if letters else None
raw = letter.as_bytes() if letter else b""
text = raw.decode("utf-8", "replace")
check("posted to the address the shop set", letter is not None and letter["To"] == ADDRESS,
      letter["To"] if letter else "")
check("from the shop's own mailbox, not a stranger's",
      letter is not None and "shop.sender@example.com" in letter["From"],
      letter["From"] if letter else "")
check("with a subject naming this book and this copy",
      letter is not None and "book copy" in letter["Subject"]
      and os.path.basename(path) in letter["Subject"], letter["Subject"] if letter else "")

files = [p for p in (letter.walk() if letter else [])
         if p.get_content_disposition() == "attachment"]
check("the copy rides as one attached file", len(files) == 1, "%d" % len(files))
attached = files[0] if files else None
check("named exactly as the file on this Mac, so the nights can be told apart",
      attached is not None and attached.get_filename() == os.path.basename(path),
      attached.get_filename() if attached else "")
carried = attached.get_payload(decode=True) if attached else b""
with open(path, "rb") as fh:
    on_disk = fh.read()
check("byte for byte the copy that stayed on this Mac",
      carried == on_disk and len(carried) > 10000, "%d bytes" % len(carried))

out = os.path.join(temp, "from-the-letter.db")
with open(out, "wb") as fh:
    fh.write(carried)
opened = sqlite3.connect("file:%s?mode=ro" % out, uri=True)
verdict = opened.execute("PRAGMA integrity_check").fetchone()[0]
jobs = opened.execute("SELECT count(*) FROM jobs").fetchone()[0]
named = opened.execute("SELECT count(*) FROM clients WHERE name=?", (CLIENT,)).fetchone()[0]
opened.close()
check("and the file pulled out of the letter opens as a whole book",
      verdict == "ok" and jobs == counts["jobs"], "%s, %d jobs" % (verdict, jobs))
check("holding the records the book had when it was copied", named == 1, "%d" % named)
check("so the ledger travels inside the file, not written out in the letter",
      CLIENT not in text and WORD not in text,
      "name leaked" if CLIENT in text else ("word leaked" if WORD in text else ""))
check("with the way back spelled out for whoever finds it", "To bring the book back" in text)

report = server.backup_report()
check("the run is written down as mailed, not merely copied",
      report["mail_note"] == "sent to " + ADDRESS and "mailed to " + ADDRESS in report["note"],
      report["note"])

# ── a provider that says no must not read as a night the book was lost ─────────────────────────
FakeProvider.refuse_at = "login"
FakeProvider.sessions = []
before = len(os.listdir(backups))
path2, _c2, _t2 = server.run_backup("backup")
check("when the mailbox refuses the word, the copy is still written",
      len(os.listdir(backups)) == before + 1 and os.path.exists(path2))
report = server.backup_report()
check("the book still records the moment it was copied",
      report["at"] != "" and report["due"] is False, report["at"])
check("the card says the letter failed, in the provider's own words",
      report["mail_note"].startswith("failed:") and "refused the word" in report["mail_note"],
      report["mail_note"])
check("a failed letter is not filed as a failed copy", report["error"] == "", report["error"])
check("the refusal names no password", WORD not in report["mail_note"])

# Gmail sometimes ends the call rather than answering a word it dislikes. That is a door the owner can
# fix, so it must not be filed as a broken wire — and a wire that breaks later must still read as a wire.
FakeProvider.refuse_at = "hangup-login"
FakeProvider.sessions = []
path_h, _c, _t = server.run_backup("backup")
report = server.backup_report()
check("a mailbox that hangs up at the word still writes the copy",
      os.path.exists(path_h) and report["error"] == "", report["error"])
check("and it is the word it blames, not the connection",
      report["mail_note"].startswith("failed:") and "would not take the word" in report["mail_note"]
      and "App Password" in report["mail_note"], report["mail_note"])
check("the provider's own words ride along", "Connection unexpectedly closed" in report["mail_note"],
      report["mail_note"])
check("and the hang-up names no password", WORD not in report["mail_note"])

FakeProvider.refuse_at = "hangup-send"
FakeProvider.sessions = []
server.run_backup("backup")
report = server.backup_report()
check("a wire that breaks on the way out still reads as the wire",
      "could not be sent through smtp.gmail.com" in report["mail_note"], report["mail_note"])
FakeProvider.refuse_at = None

os.remove(os.path.join(support, "email-secrets.json"))
server.run_backup("backup")
report = server.backup_report()
check("with no word saved, the card says why nothing could go out",
      report["mail_ready"] is False and "no password saved" in report["mail_problem"],
      report["mail_problem"])
server.write_email_secrets({"1": WORD})

# ── too big for any mailbox ────────────────────────────────────────────────────────────────────
small = server.MAIL_ATTACHMENT_MAX
server.MAIL_ATTACHMENT_MAX = 1024
FakeProvider.sessions = []
server.run_backup("backup")
report = server.backup_report()
check("a book too big for a mailbox is refused before anything is sent",
      not FakeProvider.sessions and "too big for a mailbox" in report["mail_note"],
      report["mail_note"])
server.MAIL_ATTACHMENT_MAX = small

# ── what the nightly log will say ──────────────────────────────────────────────────────────────
# Point the throwaway mailbox at a door nobody is home behind, so this leg cannot reach the network.
server._db.execute("UPDATE email_accounts SET smtp_host='127.0.0.1', smtp_port=2")
server._db.commit()
env = dict(os.environ, CHRISPHICS_DB=book, CHRISPHICS_SUPPORT=support,
           CHRISPHICS_BACKUP_DIR=backups)
run = subprocess.run([sys.executable, os.path.join(REPO, "server.py"), "--backup"],
                     cwd=REPO, env=env, capture_output=True, text=True, timeout=180)
mailed = [l for l in run.stdout.splitlines() if l.startswith("Mailed copy:")]
check("the nightly run prints the letter's verdict into its own log",
      len(mailed) == 1 and "failed" in mailed[0], mailed)
check("and still calls the copy written", "Backup written to" in run.stdout, run.returncode)

# ── the shop's own book was never part of this ─────────────────────────────────────────────────
live = sqlite3.connect("file:%s?mode=ro" % LIVE_BOOK, uri=True)
# The shop may name a real address here — the owner's own Gmail, once typed, sits in these same
# keys. What must never appear is this check's throwaway one, so the test is for the test's words.
saved = [v for (v,) in live.execute("SELECT value FROM app_state WHERE key IN"
                                    " ('backup_mail','last_backup_mail')") if v]
leaked = [v for v in saved if "example.com" in v]
senders = live.execute("SELECT count(*) FROM email_accounts WHERE smtp_host='127.0.0.1'").fetchone()[0]
live.close()
check("the live book holds no mailing address written by this check", not leaked,
      "; ".join(leaked) or "%d real row(s) left alone" % len(saved))
check("and its mailbox was not repointed", senders == 0, "%d" % senders)
check("and is the size it was before this ran",
      os.stat(LIVE_BOOK).st_size == live_bytes_before, "%d bytes" % os.stat(LIVE_BOOK).st_size)

shutil.rmtree(temp, ignore_errors=True)
print("\n%d checks, %d failed" % (checks, len(failures)))
if failures:
    for name in failures:
        print("  " + name)
    raise SystemExit(1)
print("every letter this book can write was opened and found whole")
