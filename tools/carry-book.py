#!/usr/bin/env python3
"""Carry the shop's book into an empty hosted one — the step that turns a fresh deployment into
the shop's own records instead of a blank ledger.

    tools/carry-book.py https://chrisphicshub.onrender.com              # the newest copy
    tools/carry-book.py https://chrisphicshub.onrender.com my-book.db   # a copy you name

The password is asked for at the terminal, so it never sits in the command line or the shell's
history. The far side refuses a carry-over unless its own book holds no records, so this can
never overwrite work that has already started there.
"""
import getpass
import http.cookiejar
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.request

BACKUP_NAME = re.compile(r"^(backup|manual)-\d{8}-\d{6}\.db$")
RECORD_TABLES = ("clients", "jobs", "job_items", "expenses", "spoiled_work", "leads",
                 "payments", "job_events", "notifications", "money_signals")


def backup_folder():
    support = os.environ.get("CHRISPHICS_SUPPORT") or os.path.expanduser(
        "~/Library/Application Support/Chrisphics Hub")
    return os.environ.get("CHRISPHICS_BACKUP_DIR") or os.path.join(support, "Backups")


def newest_copy(folder):
    try:
        names = [n for n in os.listdir(folder) if BACKUP_NAME.match(n)]
    except OSError:
        return None
    if not names:
        return None
    return os.path.join(folder, max(names))


def describe(path):
    """Read the copy the shop is about to hand over, so the figures can be said out loud first."""
    check = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            sys.exit("That copy is damaged — it will not be sent. Make a fresh one with"
                     " tools/nightly-backup.sh and try again.")
        tables = {r[0] for r in check.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        return {t: check.execute("SELECT count(*) FROM %s" % t).fetchone()[0]
                for t in RECORD_TABLES if t in tables}
    finally:
        check.close()


def opener():
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def ask(web, url, payload, blob=None):
    body = blob if blob is not None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body)
    request.add_header("Content-Type",
                       "application/octet-stream" if blob is not None else "application/json")
    try:
        with web.open(request, timeout=180) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as err:
        try:
            return err.code, json.loads(err.read().decode("utf-8") or "{}")
        except ValueError:
            return err.code, {}


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__.strip())
        return 2
    target = args[0].rstrip("/")
    source = args[1] if len(args) > 1 else newest_copy(backup_folder())
    if not source or not os.path.isfile(source):
        sys.exit("There is no copy of the book to carry over yet. Press 'Copy it now' on Shop &"
                 " devices, or run tools/nightly-backup.sh, then run this again.")

    counts = describe(source)
    print("The copy:      %s" % source)
    print("It holds:      " + ", ".join("%s %d" % (t, n) for t, n in counts.items() if n))
    print("Into:          %s" % target)
    if input("Carry it over? Type yes: ").strip().lower() != "yes":
        print("Nothing was sent.")
        return 1

    web = opener()
    code, result = ask(web, target + "/api/login", {"password": getpass.getpass("Shop password: ")})
    if code != 200:
        sys.exit("The shop at %s would not open its door: %s"
                 % (target, result.get("error") or "HTTP %d" % code))

    with open(source, "rb") as fh:
        blob = fh.read()
    code, result = ask(web, target + "/api/import-book", {}, blob=blob)
    if code != 200:
        sys.exit("The carry-over was refused: %s" % (result.get("error") or "HTTP %d" % code))
    print("Carried over. The hosted book now holds "
          + ", ".join("%s %d" % (t, n) for t, n in result["records"].items() if n) + ".")
    print("Signed-in devices did not travel: every device has to enter the shop password again.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
