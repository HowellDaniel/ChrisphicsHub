#!/usr/bin/env python3
"""Prove Settings ▸ Profile against a COPY of the shop's book.

The live book is opened read-only, copied with Connection.backup into a temp file, and every
request below goes to a throwaway server on a spare port with its own backup folder. The shop's
own file is never written to, and nothing here needs the shop password: loopback is the shop.
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
PORT = 8899
BASE = "http://127.0.0.1:%d" % PORT

work = tempfile.mkdtemp(prefix="chrisphics-profile-")
CLONE = os.path.join(work, "clone.db")
src = sqlite3.connect("file:%s?mode=ro" % LIVE, uri=True)
dst = sqlite3.connect(CLONE)
src.backup(dst)
dst.close()
src.close()
live_stat = os.stat(LIVE)

env = dict(os.environ, CHRISPHICS_DB=CLONE, CHRISPHICS_SUPPORT=work,
           CHRISPHICS_BACKUP_DIR=os.path.join(work, "Backups"), PYTHONUNBUFFERED="1")
log = open(os.path.join(work, "server.log"), "wb")


def boot():
    p = subprocess.Popen(["/usr/bin/python3", os.path.join(REPO, "server.py"), "--port", str(PORT),
                          "--no-browser"], cwd=REPO, env=env, stdout=log, stderr=log)
    for _ in range(80):
        try:
            if json.loads(call("GET", "/healthz")[1].decode())["ok"]:
                return p
        except Exception:
            time.sleep(0.15)
    raise SystemExit("the test server never answered; see " + os.path.join(work, "server.log"))


def call(method, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


fails = []


def check(label, ok, detail=""):
    print("%-46s %s %s" % (label, "ok" if ok else "FAIL", detail))
    if not ok:
        fails.append(label)


proc = boot()
try:
    code, raw = call("GET", "/api/shop")
    shop = json.loads(raw.decode())
    check("GET /api/shop answers", code == 200, "%s" % shop["shop"]["name"])
    check("the money number is shown", shop.get("momo_number") == "0506399641",
          shop.get("momo_number", ""))
    check("nothing is kept in the book yet",
          not any(shop.get("kept_in_book", {}).values()), str(shop.get("kept_in_book")))

    code, raw = call("PUT", "/api/shop", {"name": "CRISPprint Ghana Ltd",
                                          "tagline": "Printing & Design Services",
                                          "phone": "+233 50 295 4541",
                                          "address": "28 Airport Road, Accra"})
    out = json.loads(raw.decode())
    check("PUT /api/shop saves all four", code == 200 and out["shop"]["name"] == "CRISPprint Ghana Ltd",
          "%s / %s" % (out["shop"]["phone"], out["shop"]["address"]))
    code, raw = call("GET", "/api/shop")
    again = json.loads(raw.decode())
    check("the live values changed at once",
          again["shop"]["tagline"] == "Printing & Design Services"
          and again["shop"]["phone"].startswith("+233 50"),
          again["shop"]["phone"])
    check("all four are now kept in the book",
          all(again["kept_in_book"].values()), str(again["kept_in_book"]))

    code, raw = call("GET", "/api/bootstrap")
    boot_shop = json.loads(raw.decode())["shop"]
    check("a screen reads the new name", boot_shop["name"] == "CRISPprint Ghana Ltd", boot_shop["name"])

    bad = [("an empty name", {"name": "   "}), ("a 61-character name", {"name": "x" * 61}),
           ("a phone with letters", {"phone": "call me"}), ("nothing to change", {}),
           ("a 25-character phone", {"phone": "1" * 25})]
    for label, payload in bad:
        code, raw = call("PUT", "/api/shop", payload)
        check("refuses %s with 400" % label, code == 400,
              json.loads(raw.decode()).get("error", "")[:52])

    code, raw = call("GET", "/api/shop")
    untouched = json.loads(raw.decode())["shop"]
    check("a refused save changed nothing", untouched["name"] == "CRISPprint Ghana Ltd",
          untouched["name"])

    proc.terminate()
    proc.wait(timeout=10)
    proc = boot()
    code, raw = call("GET", "/api/shop")
    after = json.loads(raw.decode())
    check("the book still says so after a restart",
          after["shop"]["name"] == "CRISPprint Ghana Ltd"
          and after["shop"]["phone"] == "+233 50 295 4541", after["shop"]["name"])

    code, raw = call("PUT", "/api/shop", {"phone": "", "address": ""})
    cleared = json.loads(raw.decode())["shop"]
    check("clearing the phone falls back, not to blank",
          code == 200 and cleared["phone"] == "" and cleared["address"] == "Accra, Ghana",
          "%r / %r" % (cleared["phone"], cleared["address"]))

    book = sqlite3.connect(CLONE)
    keys = sorted(r[0] for r in book.execute(
        "SELECT key FROM app_state WHERE key IN ('shop_name','shop_tagline','shop_phone',"
        " 'shop_address')").fetchall())
    book.close()
    check("only the two left behind are stored", keys == ["shop_name", "shop_tagline"], str(keys))
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()
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
