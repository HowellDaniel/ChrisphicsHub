#!/usr/bin/env python3
"""Give the shop a fresh sign-in word, written straight into the book.

The owner's instruction for this one time: the old word is lost, and both in-app routes demand
knowing it, so the hash is replaced in app_state directly. Nothing here prints the word — it goes
to one 0600 file on the Desktop that the owner moves into their vault and then deletes.
"""
import hashlib
import http.cookiejar
import json
import os
import secrets
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.request

HOME = os.path.expanduser("~")
SUPPORT = os.path.join(HOME, "Library/Application Support/Chrisphics Hub")
LIVE = os.path.join(SUPPORT, "chrisphics.db")
ROOT_CA = os.path.join(HOME, "Library/Application Support/mkcert/rootCA.pem")
DESK = os.path.join(HOME, "Desktop/CRISPprint shop sign-in.txt")

ROUNDS = 600_000
SHOP_NAME = "crispprint ghana"
SHOP_TAGLINE = "printing & design services"
WORN_OUT = set("""
password password1 password123 passw0rd p@ssword 123456 1234567 12345678 123456789 1234567890
qwerty qwerty123 asdfgh zxcvb letmein welcome welcome1 welcome123 admin administrator root
iloveyou abc123 abcd1234 a1b2c3d1 111111 000000 007007 123123 654321 sunshine princess warrior
trustno1 dragon monkey football baseball business company shop shop123 chrisphics chrisphicshub
crispprint crispprint1 accra ghana gabz0000 money money123 cash cash123 print printing
""".split())

# Everyday words only: short, spelled close to the way they sound, easy to type on a phone at a
# counter. Nothing trade-related, so a word built from them never reads like the shop's own name.
POOL = """
anchor autumn beaver bicycle blanket bridge bronze cabin cactus candle canyon canvas
cascade cherry clarify cobalt collect coral cotton coyote current daffodil delta divide
donkey double dune eagle ember empire falcon feather fennel figure flint
flurry forest fossil gadget galaxy garnet gather ginger glacier granite gravel grove
guitar hamper harbor hazel hollow ivory jacket jaguar jasmine jigsaw jungle
kettle kingdom kiwi lantern learn lemon limit marble match meadow melody
napkin nickel northern nutmeg oasis october onyx orange orbit otter oxygen paddle
pebble pepper pigeon pillar planet pocket pollen pond puma pyramid quill quartz
quiver rabbit rattle raven recipe reef remark river rocket saffron sail sable sample
scale science serif shadow signal silver slate snowflake solar spool spruce
stadium statue summit sunset swamp swan tangerine tapir temple thistle timber
topaz torch totem towel tulip tundra umber umbrella uniform valley
vase velvet vessel violet vulcan walnut willow winter wren yonder zebra zenith
""".split()

fails = []


def check(label, ok, detail=""):
    print("%-54s %s %s" % (label, "ok" if ok else "FAIL", detail))
    if not ok:
        fails.append(label)


def weak(word):
    """server.py:weak_password_reason, rule for rule."""
    if len(word) < 8:
        return "under 8 characters"
    low = word.lower()
    if low in WORN_OUT:
        return "on the worn-out list"
    for filler in (SHOP_NAME, SHOP_TAGLINE):
        if len(filler) > 4 and filler in low:
            return "holds the shop's own name"
    if len(set(word)) < 5:
        return "repeats too few characters"
    if word.isdigit():
        return "digits alone"
    classes = sum(1 for g in (str.islower, str.isupper, str.isdigit,
                              lambda c: not c.isalnum())
                  if any(g(c) for c in word))
    if len(word) < 12 and classes < 3:
        return "short and one-classed"
    return ""


def hash_password(word, salt=None, rounds=ROUNDS):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", word.encode("utf-8"), bytes.fromhex(salt), rounds)
    return "pbkdf2_sha256$%d$%s$%s" % (rounds, salt, digest.hex())


check("the word pool is clean", len(set(POOL)) == len(POOL) and len(POOL) > 120
      and not [w for w in POOL if w in SHOP_NAME or w in SHOP_TAGLINE or w in WORN_OUT],
      "%d distinct words" % len(POOL))

# -------------------------------------------------------------------- 1. snapshot
stat_before = os.stat(LIVE)
stamp = time.strftime("%Y%m%d-%H%M%S")
snapshot = os.path.join(SUPPORT, "Backups", "manual-%s-before-new-word.db" % stamp)
src = sqlite3.connect("file:%s?mode=ro" % LIVE, uri=True)
dst = sqlite3.connect(snapshot)
src.backup(dst)
dst.close()
src.close()
os.chmod(snapshot, 0o600)
check("book snapshotted before the change", os.path.getsize(snapshot) > 100_000,
      "%s (%d bytes)" % (os.path.basename(snapshot), os.path.getsize(snapshot)))

src = sqlite3.connect("file:%s?mode=ro&immutable=1" % snapshot, uri=True)
old_hash = src.execute("SELECT value FROM app_state WHERE key='shop_password'").fetchone()
sessions_before = src.execute("SELECT count(*) FROM sessions").fetchone()[0]
src.close()
check("snapshot holds the book as it was", bool(old_hash),
      "old hash %d characters, %d live session(s)" % (len(old_hash[0]), sessions_before))

# ------------------------------------------------------------------ 2. fresh word
word = ""
for _ in range(500):
    digits = "".join(str(secrets.randbelow(10)) for _ in range(2))
    candidate = "-".join(secrets.choice(POOL) for _ in range(4)) + "-" + digits
    if not weak(candidate):
        word = candidate
        break
check("a fresh word was generated", bool(word), "%d characters, past every strength rule"
      % len(word))

hashed = hash_password(word)
check("hash is the shape the server reads", hashed.startswith("pbkdf2_sha256$600000$")
      and len(hashed) == 118, "%d characters" % len(hashed))

# ---------------------------------------------------------------------- 3. write
conn = sqlite3.connect(LIVE, timeout=10)
try:
    conn.execute("PRAGMA busy_timeout=8000")
    conn.execute("INSERT INTO app_state (key, value, updated_at)"
                 " VALUES ('shop_password', ?, datetime('now','localtime'))"
                 " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
                 " updated_at = excluded.updated_at", (hashed,))
    conn.execute("DELETE FROM sessions")
    conn.commit()
finally:
    conn.close()
check("new hash written to the live book", True, "any old sign-in cleared")

row = sqlite3.connect("file:%s?mode=ro" % LIVE, uri=True).execute(
    "SELECT value, updated_at FROM app_state WHERE key='shop_password'").fetchone()
check("the book now answers with the new hash", row[0] == hashed, "updated %s" % row[1])
_, rounds, salt, digest = row[0].split("$")
check("the word hashes back to what is stored",
      hashlib.pbkdf2_hmac("sha256", word.encode(), bytes.fromhex(salt), int(rounds)).hex() == digest,
      "%s rounds" % rounds)
stat_after = os.stat(LIVE)
check("the book is whole after the write", stat_after.st_size >= stat_before.st_size,
      "%d -> %d bytes" % (stat_before.st_size, stat_after.st_size))

# ------------------------------------------------------- 4. hand it to the owner
note = ("CRISPprint shop sign-in — set %s\n\n"
        "Type this on any device that asks for the shop password, including this Mac.\n"
        "Move it into your password vault, then delete this file (Finder: Cmd+Delete).\n"
        "It was never printed, logged, or sent anywhere else.\n\n"
        "        %s\n\n"
        "To change it yourself later: sign in, then Shop & devices.\n"
        % (time.strftime("%Y-%m-%d %H:%M"), word))
existed = os.path.exists(DESK)
with open(DESK, "w") as fh:
    fh.write(note)
os.chmod(DESK, 0o600)
check("the word is in one file on the Desktop", bool(not existed) and os.path.getsize(DESK) > 120,
      "%s, mode %s" % (os.path.basename(DESK), oct(os.stat(DESK).st_mode & 0o777)))

# ----------------------------------------------------------------- 5. live proof
jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(
    urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ROOT_CA)),
    urllib.request.HTTPCookieProcessor(jar))


def call(method, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request("https://127.0.0.1:8834" + path, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          "Origin": "https://127.0.0.1:8834"})
    try:
        with opener.open(req, timeout=25) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


code, _ = call("POST", "/api/login", {"password": word})
check("the live server accepts the fresh word", code == 200, "HTTP %d" % code)
code, _ = call("GET", "/api/shop")
check("the signed-in session reaches the ledger", code == 200, "HTTP %d" % code)
# No deliberate wrong guess is sent: this Mac's address already carries the owner's failed
# attempts, and five in a row locks the counter out — one correct sign-in clears it.
code, _ = call("POST", "/api/logout")
check("the test session closed itself", code == 200, "so Shop & devices stays honest")
left = sqlite3.connect("file:%s?mode=ro" % LIVE, uri=True).execute(
    "SELECT count(*) FROM sessions").fetchone()[0]
check("no session left in the book", left == 0, "%d present" % left)

print("\n%s" % ("ALL GOOD" if not fails else "PROBLEMS: " + "; ".join(fails)))
sys.exit(1 if fails else 0)
