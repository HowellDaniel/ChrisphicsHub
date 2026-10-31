#!/usr/bin/env python3
"""Prove the counter's own menus and Settings screen — against a COPY of the shop's book.

The owner asked, on 2026-10-29, to "Enable the menu bar, and the tabs that the mac has". A Windows
counter has no app window: the launcher opens a browser at this page, and a browser window brings no
menu bar, no Settings tabs and no ⌘ keys. So the page now carries all three itself. On 2026-10-31 he
settled where the strip belongs: "These things was not for the mac users it was for windows users, so
remove them from the mac". So a Windows browser draws the strip, and every Mac — the shop's app, its
portable copy, or a Mac's browser — draws none, with the Mac's own menu bar above the window.

Later that day he settled Settings the same way: "Remove this and all it's property from the installed
app on the other mac machines including this machine too because it's already at the top menu bar, and
leave the windows own like that". A Mac app window's menu bar carries a native Settings panel with the
same four panes, so inside the app the page's rail row, its screen, its tabs and Alt+S are gone; an old
Settings bookmark lands on the Dashboard. A Mac's browser keeps the row, because Safari's menu bar has
no Settings of the shop's; a Windows or Linux browser keeps all of it, as before.

Two things had to stay true while it grew, and both are proved here rather than argued:

  - Nothing a counter chooses at their own screen reaches through into the engine. The engine answering
    a joined counter is the shop's own Mac, and a page that could set the shop's start screen or text
    size would be changing the shop's computer on somebody else's say-so. So every per-computer choice
    is kept in that browser's own memory, and GET /api/counter only ever tells the page what it is
    looking at. This check writes nothing through that route and weighs the book before and after.
  - A control the engine would refuse is not offered. The shop's details change on the shop's
    computer, so a counter who is not the shop sees the Profile fields shut — proved by running a
    second throwaway engine under --trust-proxy, the one flag that makes loopback stop proving it.

The live book is opened read-only, copied with Connection.backup into a temp file, and every request
below goes to a throwaway server on a spare port with its own backup folder. The pages are read in the
shipped rendering engine (tools/wk-probe.swift), because "the menu is there" is a thing a screen shows.

    python3 tools/check-counter-settings.py
"""
import ast
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

import shopcheck  # noqa: E402  (lives beside this check)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE = os.path.expanduser("~/Library/Application Support/Chrisphics Hub/chrisphics.db")
PORT = 8897
PROXY_PORT = 8898
BASE = "http://127.0.0.1:%d" % PORT

# What a Windows counter's browser says about itself, worn before the page's first frame so the
# screen decides the way it would on that machine. Injected with WKPROBE_SCRIPT by the probe.
WINDOWS_USER_AGENT = (
    "Object.defineProperty(navigator, 'platform', { get: function () { return 'Win32'; } });\n"
    "Object.defineProperty(navigator, 'userAgent', { get: function () {\n"
    "  return 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36';\n"
    "} });\n")
PROXY = "http://127.0.0.1:%d" % PROXY_PORT
shopcheck.require_free(PORT, PROXY_PORT)

APP_JS = os.path.join(REPO, "public", "app.js")
INDEX_HTML = os.path.join(REPO, "public", "index.html")
STYLES_CSS = os.path.join(REPO, "public", "styles.css")
SW_JS = os.path.join(REPO, "public", "sw.js")
BUILD_TOOL = os.path.join(REPO, "tools", "build-installer.py")

# Every field the Settings screen reads out of /api/counter. A name missing here is a name the page
# asks for and the engine never gave, which shows up as a blank cell rather than an error.
FACT_KEYS = ["version", "system", "python", "machine", "book", "backups", "address", "tls",
             "reachable_from_wifi", "answered_by", "keeps_own_book", "joined", "shop_computer",
             "sign_in", "awake", "hours"]

fails = []


def check(label, ok, detail=""):
    print("%-58s %s   %s" % (label, "ok" if ok else "FAIL", str(detail)[:64]))
    if not ok:
        fails.append(label)
    return ok


def expect(label, got, want):
    return check(label, got == want, "got %r, want %r" % (got, want))


def line_saying(text, needle):
    """The first line of a file holding `needle`, for a report that names what it actually found."""
    return next((ln.strip() for ln in text.splitlines() if needle in ln), "no line holds %r" % needle)


def call(method, path, payload=None, base=BASE):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


# ----------------------------------------------------------------- the throwaway book
work = tempfile.mkdtemp(prefix="chrisphics-counter-")
CLONE = os.path.join(work, "clone.db")
src = sqlite3.connect("file:%s?mode=ro" % LIVE, uri=True)
dst = sqlite3.connect(CLONE)
src.backup(dst)
dst.close()
src.close()
tidy = sqlite3.connect(CLONE)
tidy.execute("UPDATE app_state SET value='0' WHERE key='shop_mac_login'")
tidy.commit()
tidy.close()
live_stat = os.stat(LIVE)

log = open(os.path.join(work, "server.log"), "wb")
proxy_log = open(os.path.join(work, "proxy.log"), "wb")


def boot(port, extra=None, logfile=None, folder=None):
    env = dict(os.environ, CHRISPHICS_DB=folder or CLONE,
               CHRISPHICS_SUPPORT=os.path.join(work, "support-%d" % port),
               CHRISPHICS_BACKUP_DIR=os.path.join(work, "backups-%d" % port),
               PYTHONUNBUFFERED="1")
    args = [sys.executable, os.path.join(REPO, "server.py"), "--port", str(port), "--no-browser"]
    p = subprocess.Popen(args + (extra or []), cwd=REPO, env=env,
                         stdout=logfile or log, stderr=logfile or log)
    probe_base = "http://127.0.0.1:%d" % port
    for _ in range(80):
        try:
            if json.loads(call("GET", "/healthz", base=probe_base)[1].decode())["ok"]:
                return p
        except Exception:
            time.sleep(0.15)
    raise SystemExit("the test server on %d never answered; see %s" % (port, (logfile or log).name))


def stop(proc):
    if not proc:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


# ------------------------------------------------------- 1. what the engine will answer
print("\n== GET /api/counter, read-only, on a shop computer")
proc = boot(PORT)
try:
    code, raw = call("GET", "/api/counter")
    facts = json.loads(raw.decode())
    check("GET /api/counter answers", code == 200, "%d" % code)
    missing = [k for k in FACT_KEYS if k not in facts]
    check("and hands over every field the screen reads", not missing, "missing %s" % (missing or "none"))

    repo_version = open(os.path.join(REPO, "VERSION"), encoding="utf-8").read().strip()
    expect("the build number is the one this repository carries", facts.get("version"), repo_version)
    check("the system is named, not guessed", facts.get("system", "").startswith("macOS"),
          facts.get("system"))
    check("python is named", facts.get("python", "")[:1].isdigit(), facts.get("python"))
    check("the computer has a name", bool(facts.get("machine")), facts.get("machine"))
    check("the book it names is the throwaway copy", facts.get("book", "").endswith("clone.db"),
          facts.get("book"))
    check("and never the shop's own file", "Application Support/Chrisphics" not in facts.get("book", ""),
          facts.get("book"))
    expect("a counter on a spare port is answered for itself", facts.get("answered_by"),
           "this computer")
    expect("loopback is the shop's own computer", facts.get("shop_computer"), True)
    check("the sign-in answer is a shape the page can read",
          isinstance(facts.get("sign_in"), dict)
          and set(["required", "from_environment", "chosen", "this_mac_asked"])
              <= set(facts["sign_in"]),
          json.dumps(facts.get("sign_in")))
    check("the hours come along too", isinstance(facts.get("hours"), dict),
          json.dumps(facts.get("hours"))[:60])
    check("tls and reachability are said, not left for the page to infer",
          isinstance(facts.get("tls"), bool) and isinstance(facts.get("reachable_from_wifi"), bool)
          and isinstance(facts.get("awake"), bool),
          "tls=%s wifi=%s awake=%s" % (facts.get("tls"), facts.get("reachable_from_wifi"),
                                       facts.get("awake")))

    # Read-only is the whole rule, so it is the thing to press on. The copy is weighed before the
    # pressing and again after it: an engine writes to its own book at boot, and that is not this.
    weight = os.stat(CLONE)
    asked = call("GET", "/api/shop")[1]
    for verb in ("POST", "PUT", "DELETE", "PATCH"):
        code, raw = call(verb, "/api/counter", {"start": "#/expenses"})
        # 404: the engine knows the route and has no write in it. 405/501: it never even took the
        # verb. Either way nothing reached the book, which is the next line's job to prove.
        check("%s /api/counter is refused" % verb, code in (404, 405, 501), "%d %s"
              % (code, raw.decode()[:40].replace("\n", " ")))
    for _ in range(6):
        call("GET", "/api/counter")
    after = os.stat(CLONE)
    check("and asking it all changed nothing in the copy",
          (after.st_size, after.st_mtime) == (weight.st_size, weight.st_mtime),
          "%d bytes, last written %s" % (after.st_size, time.ctime(after.st_mtime)))
    check("the shop's own details read the same after all of it",
          call("GET", "/api/shop")[1] == asked,
          json.loads(asked.decode())["shop"]["name"])
finally:
    stop(proc)

# ------------------------------------- 2. the same engine, no longer a shop computer
print("\n== the same screen on a computer the shop cannot vouch for")
other_book = os.path.join(work, "other-computer.db")
shutil.copyfile(CLONE, other_book)
proxy = boot(PROXY_PORT, extra=["--trust-proxy"], logfile=proxy_log, folder=other_book)
try:
    facts2 = json.loads(call("GET", "/api/counter", base=PROXY)[1].decode())
    expect("--trust-proxy: loopback stops proving the shop", facts2.get("shop_computer"), False)
    check("and the page is told which computer it is really talking to",
          facts2.get("answered_by") == "this computer", facts2.get("answered_by"))
finally:
    stop(proxy)

# ------------------------------------------------------- 3. the menu and the keys are one list
print("\n== the rows on the screen and the keys under the hand")
app = open(APP_JS, encoding="utf-8").read()

BARE_KEY = re.compile(r"(?<=[{,\s])([a-z][a-z0-9]*):")


def literal_after(marker, opener, closer, source=app):
    """Pull one JS literal out of the source and read it as Python data.

    The menu, its keys and the tab names are written as plain literals rather than built at runtime,
    so a check can compare them as sets instead of trusting a comment. Only the names go through: a
    bare `n:` becomes `"n":`, which is the one difference between what JavaScript allows and what
    Python's literal reader does. Nothing in these literals puts a colon after a bare word inside a
    string, so that single substitution is the whole translation."""
    start = source.index(marker)
    cut = source.index(opener, start)
    depth, at = 0, cut
    while at < len(source):
        if source[at] == opener:
            depth += 1
        elif source[at] == closer:
            depth -= 1
            if depth == 0:
                return ast.literal_eval(BARE_KEY.sub(r'"\1":', source[cut:at + 1]))
        at += 1
    raise SystemExit("cannot find %r in app.js — the menu moved and this check did not follow" % marker)


# Python's literal reader turns the digit keys into numbers; JavaScript hands the handler a string for
# every key, including '1' for Alt+1, so the table is read back the way the page sees it.
menu_keys = {str(k): v for k, v in literal_after("const MENU_KEYS = {", "{", "}").items()}
menu = literal_after("const MENU = [", "[", "]")
tabs_names = literal_after("const SETTINGS_TABS = [", "[", "]")
screens = literal_after("const SCREENS = [", "[", "]")
rows = [(m["label"], r) for m in menu for r in m["rows"] if r[0] != "-"]

check("the bar carries the Mac's five menus",
      [m["label"] for m in menu] == ["File", "Edit", "View", "Data", "Shop"],
      " / ".join(m["label"] for m in menu))
expect("the tabs are the Mac's four, in the Mac's order",
       [t[0] for t in tabs_names], ["general", "appearance", "profile", "updates"])


def chord_of(hint):
    """The key the page's own listener builds: Alt held, the rest read off the printed hint.

    'Alt+Shift+R' is the same press as 'shift+r' in the table, because a capital R under Shift is
    what the browser hands over — so the hint has to be folded the way the handler folds it."""
    if not hint.startswith("Alt+"):
        return None
    rest = hint[4:].split("+")
    return (("shift+" if "Shift" in rest else "") + rest[-1].lower())


# Names runMenuItem answers without going through ACTIONS, and the three prefixes it splits itself.
HANDLED = ["reload", "settings", "print-sheet", "settings-refresh", "settings-shop-save",
           "settings-check", "settings-repo", "focus-search"]
PREFIXES = ("view:", "edit:", "settings:")
action_region = app[app.index("const ACTIONS = {"):app.index("\n};", app.index("const ACTIONS = {"))]
bound = set(re.findall(r"^  (?:async )?'([a-z0-9-]+)'", action_region, re.M))
known = bound | set(HANDLED)

# Every key a hand can press is a row somebody can see, and every row that prints a key prints the
# one that actually works. This is the drift the owner would notice first: a menu that lies.
printed = {}
for _, row in rows:
    folded = chord_of(row[1])
    if folded:
        printed[folded] = (row[2], row[1])
silent = [k for k in sorted(menu_keys) if k not in printed]
check("every key the page answers is printed on a row", not silent,
      "no row says Alt+%s" % (", ".join(silent) or "none"))
lying = ["row %s prints %s, the table says %s" % (action, hint, menu_keys[chord])
         for chord, (action, hint) in sorted(printed.items())
         if menu_keys.get(chord) != action]
expect("and every key a row prints does the thing the row does", lying, [])
unbound = [r[2] for _, r in rows
           if not (r[2] in known or r[2].startswith(PREFIXES))]
check("no row asks for something the page has not bound", not unbound, ", ".join(unbound) or "none")
nowhere = [r[2][5:] for _, r in rows if r[2].startswith("view:")
           and r[2][5:] not in [s[0] for s in screens]]
check("and every View row names a screen the rail has", not nowhere, ", ".join(nowhere) or "none")

# ---------------------------------------------------------------- 4. the pages, in the engine
print("\n== the counter's own window, in the shipped rendering engine")
built = subprocess.run(["swiftc", os.path.join(REPO, "tools/wk-probe.swift"),
                        "-o", os.path.join(work, "wk-probe")],
                       capture_output=True, text=True, timeout=300)
PROBE = os.path.join(work, "wk-probe")
if not check("the rendering engine probe compiles", built.returncode == 0,
             (built.stderr or "")[-200:]):
    print("\ncould not build tools/wk-probe.swift — no browser checks ran")
    sys.exit(1)


def say(tag, expr):
    """One `say` command that prints its own name.

    The probe answers with the evaluated value alone, so an untagged value is indistinguishable from
    any other line it printed — the tag has to be part of the string the page hands back."""
    return "say|'%s='+(%s)" % (tag, expr)


def probe(commands, size=None, fresh=False, handlers=None, route="/", base=BASE, script=None):
    args = [PROBE, base, work]
    if size:
        args.append("size|%dx%d" % size)
    args.append("open|%s" % route)
    args.append("poll|!document.querySelector('#view .loading')")
    args += commands
    env = dict(os.environ)
    if fresh:
        env["WKPROBE_FRESH_DATA"] = "1"
    if handlers:
        env["WKPROBE_HANDLERS"] = handlers
    if script:
        path = os.path.join(work, "probe-script.js")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(script)
        env["WKPROBE_SCRIPT"] = path
    out = subprocess.run(args, capture_output=True, text=True, timeout=240, env=env)
    said = out.stdout or ""
    for line in said.splitlines() + (out.stderr or "").splitlines():
        if line.strip().startswith(("EVAL ERROR", "TIMEOUT waiting", "NAV FAILED", "SNAPSHOT ERROR")):
            fails.append("probe: " + line.strip()[:70])
            print("   " + line.strip()[:90])
    return said


def read(prose, tag):
    """Pull the value the probe printed for one tagged `say` out of its whole answer."""
    for line in prose.splitlines():
        stripped = line.strip()
        if stripped.startswith(tag + "="):
            return stripped[len(tag) + 1:]
    return None


def ask(prose, tag):
    try:
        return json.loads(read(prose, tag))
    except (TypeError, ValueError):
        return {}


proc = boot(PORT)
ready = False
for _ in range(60):
    time.sleep(0.25)
    try:
        if "authenticated" in json.loads(call("GET", "/api/session")[1].decode()):
            ready = True
            break
    except Exception:
        continue
if not check("a throwaway shop server is answering on 127.0.0.1:%d" % PORT, ready):
    print("\nthe throwaway shop server did not start — no browser checks ran")
    sys.exit(1)

# --- a Windows computer opened in a browser: the bar is the page's job. The owner's rule of
# 2026-10-31 is that the strip belongs to a computer with no menu bar of its own, so this leg wears
# Windows before the page's first frame. Fresh, because WebKit keeps the service worker's shell copy
# after a process ends: without the wipe this leg would read the app.js some earlier run cached, not
# the one on disk, and a fix that had landed would look like it had not. The pair further down that
# proves a preference keeps is the only part of this run allowed to carry storage between its two
# windows.
browser = probe([
    "poll|!!document.documentElement.dataset.menubar",
    say("BAR", "JSON.stringify({hidden:document.getElementById('menubar').hidden,"
               "flag:document.documentElement.dataset.menubar,"
               "labels:Array.prototype.map.call(document.querySelectorAll('#menubar .menubtn'),"
               "function(b){return b.textContent;}).join(','),"
               "hint:(document.querySelector('#menubar .menuhint')||{}).textContent||'',"
               "at:location.hash,"
               "pageSettings:document.documentElement.dataset.pageSettings,"
               "rail:document.querySelectorAll('#nav a[data-view=settings]').length,"
               "real:SCREENS.some(function(s){return '#/'+s[0]===location.hash;})})"),
    "eval|document.querySelector('#menubar .menubtn').click()",
    "poll|!document.querySelector('#menubar .menudrop').hidden",
    say("FILE", "JSON.stringify({shown:!document.querySelector('#menubar .menudrop').hidden,"
                "rows:document.querySelectorAll('#menubar .menudrop .menuitem').length,"
                "keys:Array.prototype.map.call(document.querySelectorAll("
                "'#menubar .menudrop .menuitem i'),function(i){return i.textContent;}).join(',')})"),
    "eval|document.querySelector('#menubar .menuitem[data-do=print-sheet]').click()",
    "poll|/Open a job first/.test(document.getElementById('toasts').textContent)",
    say("PRESS", "JSON.stringify({said:(document.querySelector('#toasts .toast')||{}).textContent,"
                 "drop:document.querySelector('#menubar .menudrop').hidden})"),
], fresh=True, script=WINDOWS_USER_AGENT)
bar = ask(browser, "BAR")
expect("a Windows computer's page draws the menu strip its window does not have",
       (bar.get("hidden"), bar.get("flag")), (False, "on"))
expect("with the shop's five menus, in order", bar.get("labels"), "File,Edit,View,Data,Shop")
check("and tells the counter where the keys live", "Alt" in (bar.get("hint") or ""), bar.get("hint"))
check("the window opens on a screen the shop has, never on a hash of its own making",
      bar.get("real") is True, bar.get("at"))
expect("and a Windows counter keeps the rail's Settings, because its menu bar has none",
       (bar.get("pageSettings"), bar.get("rail")), ("page", 1))
filemenu = ask(browser, "FILE")
check("File opens, and its rows carry their keys",
      filemenu.get("shown") is True and filemenu.get("rows", 0) >= 8
      and "Alt+N" in (filemenu.get("keys") or ""),
      "%s rows: %s" % (filemenu.get("rows"), filemenu.get("keys")))
pressed = ask(browser, "PRESS")
check("a row pressed in the bar does that row's one thing and shuts the menu",
      "Open a job first" in (pressed.get("said") or "") and pressed.get("drop") is True,
      pressed.get("said"))

# --- a Mac's browser: the system's own menu bar is above the window, so the page draws none
macbrowser = probe([
    "poll|document.documentElement.dataset.menubar==='off'",
    say("MACBROWSER", "JSON.stringify({hidden:document.getElementById('menubar').hidden,"
                      "flag:document.documentElement.dataset.menubar,"
                      "buttons:document.querySelectorAll('#menubar .menubtn').length,"
                      "pageSettings:document.documentElement.dataset.pageSettings,"
                      "rail:document.querySelectorAll('#nav a[data-view=settings]').length,"
                      "top:getComputedStyle(document.querySelector('.topbar')).top})"),
], fresh=True)
macbar = ask(macbrowser, "MACBROWSER")
expect("a Mac's browser keeps the strip off the page altogether",
       (macbar.get("hidden"), macbar.get("flag")), (True, "off"))
expect("with no buttons painted behind it and no gap under the title bar",
       (macbar.get("buttons"), macbar.get("top")), (0, "0px"))
expect("and keeps the rail's Settings, because Safari's menu bar has none of the shop's",
       (macbar.get("pageSettings"), macbar.get("rail")), ("page", 1))

# --- Settings: four tabs, and each one says something true about this computer
tabs = probe([
    "eval|location.hash='#/settings'",
    "poll|document.querySelectorAll('#view .tab').length===4",
    say("TABS", "JSON.stringify({h1:document.querySelector('#topbar h1').textContent,"
               "on:(document.querySelector('#view .tab.on')||{}).textContent,"
               "tabs:Array.prototype.map.call(document.querySelectorAll('#view .tab'),"
               "function(t){return t.textContent;}).join(','),"
               "rows:document.querySelectorAll('#view .srow').length,"
               "awakeState:!!document.getElementById('awakeState')})"),
    say("GENERAL", "JSON.stringify({start:(document.querySelector('[data-pref=start]')||{}).value,"
                   "text:(document.querySelector('[data-pref=text]')||{}).value,"
                   "book:/clone\\.db/.test(document.querySelector('#view').textContent),"
                   "size:getComputedStyle(document.body).fontSize})"),
    "eval|location.hash='#/settings?pane=appearance'",
    "poll|!!document.querySelector('[data-pref=bg]')",
    say("LOOK", "JSON.stringify({on:(document.querySelector('#view .tab.on')||{}).textContent,"
                "papers:document.querySelectorAll('[data-pref=bg] option').length,"
                "held:!!document.querySelector('[data-pref=bg]:disabled')})"),
    "eval|location.hash='#/settings?pane=profile'",
    "poll|!!document.getElementById('setName')",
    say("PRO", "JSON.stringify({on:(document.querySelector('#view .tab.on')||{}).textContent,"
               "name:document.getElementById('setName').value,"
               "save:document.querySelector('[data-do=settings-shop-save]').disabled})"),
    "eval|location.hash='#/settings?pane=updates'",
    "poll|!!document.getElementById('updateResult')",
    say("UPD", "JSON.stringify({on:(document.querySelector('#view .tab.on')||{}).textContent,"
               "fields:Array.prototype.map.call(document.querySelectorAll('#view .sfield code'),"
               "function(c){return c.textContent;}).join(' | ')})"),
    "eval|location.hash='#/settings?pane=nonsense'",
    "poll|document.querySelector('#view .tab.on').textContent==='General'",
    say("FELL", "JSON.stringify(document.querySelector('#view .tab.on').textContent)"),
])
got = ask(tabs, "TABS")
expect("Settings opens on the Mac's four tabs", got.get("tabs"), "General,Appearance,Profile,Updates")
expect("and the first one is General", got.get("on"), "General")
check("General lists this computer's choices and the engine's facts together",
      got.get("rows", 0) >= 9 and got.get("awakeState") is True, "%s rows" % got.get("rows"))
general = ask(tabs, "GENERAL")
check("it offers the start screen and the text size the Mac offers",
      general.get("start") is not None and general.get("text") is not None,
      "start=%s text=%s" % (general.get("start"), general.get("text")))
check("and names the book it is looking at, which is the copy and not the shop's",
      general.get("book") is True, general.get("book"))
look = ask(tabs, "LOOK")
expect("Appearance is the paper the Mac's own pane offers", look.get("papers"), 14)
expect("and in a browser window nothing there is held back", look.get("held"), False)
profile = ask(tabs, "PRO")
expect("Profile opens on this computer, because this is the shop's own Mac",
       (profile.get("on"), profile.get("save")), ("Profile", False))
check("and shows the shop's name out of the book, not out of nowhere",
      bool(profile.get("name")), profile.get("name"))
updates = ask(tabs, "UPD")
check("Updates names the build the repository carries, the engine and the road back",
      repo_version in (updates.get("fields") or "") and "github.com" in (updates.get("fields") or ""),
      updates.get("fields"))
expect("a tab nobody named falls back to General", ask(tabs, "FELL"), "General")

# --- the choices stay on this computer, and come back with it
kept = probe([
    "eval|var s=document.querySelector('[data-pref=text]');"
    "s.value='1.25';s.dispatchEvent(new Event('change',{bubbles:true}))",
    "poll|getComputedStyle(document.documentElement).getPropertyValue('--text-scale')"
          ".trim()==='1.25'",
    "eval|location.hash='#/settings?pane=appearance'",
    "poll|!!document.querySelector('[data-pref=bg]')",
    "eval|var b=document.querySelector('[data-pref=bg]');"
    "b.value='sky';b.dispatchEvent(new Event('change',{bubbles:true}))",
    "poll|document.documentElement.dataset.bg==='sky'",
    "eval|localStorage.setItem('crispprint-start-route','accounts')",
    say("WROTE", "JSON.stringify({scale:getComputedStyle(document.documentElement)"
                 ".getPropertyValue('--text-scale').trim(),bg:document.documentElement.dataset.bg,"
                 "body:getComputedStyle(document.body).fontSize,"
                 "stored:localStorage.getItem('crispprint-start-route')})"),
], route="/#/settings")
wrote = ask(kept, "WROTE")
expect("the text size reaches the words on the screen", wrote.get("scale"), "1.25")
expect("the paper changes", wrote.get("bg"), "sky")

again = probe([
    "poll|location.hash==='#/accounts'",
    "poll|!document.querySelector('#view .loading')",
    say("BACK", "JSON.stringify({hash:location.hash,"
                "scale:getComputedStyle(document.documentElement)"
                ".getPropertyValue('--text-scale').trim(),bg:document.documentElement.dataset.bg,"
                "stored:localStorage.getItem('crispprint-start-route'),"
                "h1:document.querySelector('#topbar h1').textContent})"),
])
back = ask(again, "BACK")
expect("a fresh load opens on the screen this computer asked for", back.get("hash"), "#/accounts")
check("the rail agrees it is standing there", back.get("h1"), "Accounts & payments")
expect("with the same words and the same paper as before",
       (back.get("scale"), back.get("bg")), ("1.25", "sky"))
expect("and every one of them came out of this browser's own memory", back.get("stored"), "accounts")

# --- the keys, pressed
chords = probe([
    "eval|location.hash='#/expenses'",
    "poll|location.hash==='#/expenses'",
    "eval|document.dispatchEvent(new KeyboardEvent('keydown',{key:'1',altKey:true,bubbles:true}))",
    "poll|location.hash==='#/dashboard'",
    say("ONE", "JSON.stringify(location.hash)"),
    "eval|document.dispatchEvent(new KeyboardEvent('keydown',{key:'s',altKey:true,bubbles:true}))",
    "poll|location.hash==='#/settings'",
    say("TWO", "JSON.stringify(location.hash)"),
    "eval|location.hash='#/expenses'",
    "poll|location.hash==='#/expenses'",
    "eval|document.dispatchEvent(new KeyboardEvent('keydown',{key:'4',altKey:true,bubbles:true}))",
    "poll|location.hash==='#/leads'",
    say("THREE", "JSON.stringify(location.hash)"),
])
expect("Alt+1 walks to the Dashboard, wherever the counter was standing", ask(chords, "ONE"),
       "#/dashboard")
expect("Alt+S opens Settings, the way the Mac's ⌘, does", ask(chords, "TWO"), "#/settings")
expect("and Alt+4 leaves the screen the counter was on for the one the row named",
       ask(chords, "THREE"), "#/leads")

# --- a phone: the tab bar is the menu, and the two cannot both be away
small = probe([
    "poll|document.getElementById('menubar').hidden===true",
    say("PHONE", "JSON.stringify({hidden:document.getElementById('menubar').hidden,"
                 "flag:document.documentElement.dataset.menubar,"
                 "tabbar:!!document.getElementById('tabbar')})"),
], size=(390, 844))
phone = ask(small, "PHONE")
expect("a phone keeps the tab bar", phone.get("tabbar"), True)
expect("and gives up the menu bar it has no room for", (phone.get("hidden"), phone.get("flag")),
       (True, "off"))

# --- the Mac's own window: the menu bar's native Settings panel is the only Settings there
inmac = probe([
    "poll|document.documentElement.dataset.pageSettings==='shell'",
    say("MAC", "JSON.stringify({hidden:document.getElementById('menubar').hidden,"
               "flag:document.documentElement.dataset.menubar,"
               "rail:document.querySelectorAll('#nav a[data-view=settings]').length,"
               "said:document.getElementById('nav').textContent.indexOf('Settings')>=0,"
               "screens:document.querySelectorAll('#nav a[data-view]').length})"),
    "eval|location.hash='#/settings?pane=appearance'",
    "poll|location.hash==='#/dashboard'",
    say("GONE", "JSON.stringify({hash:location.hash,"
                "tabs:document.querySelectorAll('#view .tab').length,"
                "srows:document.querySelectorAll('#view .srow').length,"
                "h1:document.querySelector('#topbar h1').textContent})"),
    "eval|document.dispatchEvent(new KeyboardEvent('keydown',{key:'s',altKey:true,bubbles:true}))",
    say("KEY", "JSON.stringify({hash:location.hash,"
               "tabs:document.querySelectorAll('#view .tab').length})"),
    "eval|window.chrisphics.act('settings')",
    say("ACT", "JSON.stringify(location.hash)"),
    "eval|location.hash='#/jobs'",
    "poll|location.hash==='#/jobs'",
    say("PAPER", "JSON.stringify(document.documentElement.dataset.bg||'')"),
], handlers="sheetPrint", fresh=True)
shell = ask(inmac, "MAC")
expect("inside the Mac's window the page stands back from the menu bar",
       (shell.get("hidden"), shell.get("flag")), (True, "off"))
expect("and its rail offers no Settings row, because the menu bar owns it",
       (shell.get("rail"), shell.get("said")), (0, False))
expect("leaving the shop's own eleven screens on the rail", shell.get("screens"), 11)
gone = ask(inmac, "GONE")
expect("a Settings hash — an old bookmark — opens the Dashboard instead",
       (gone.get("hash"), gone.get("tabs")), ("#/dashboard", 0))
check("with no Settings tabs or rows painted anywhere",
      gone.get("srows") == 0 and gone.get("h1") == "Dashboard", gone.get("h1"))
expect("Alt+S does nothing there",
       (ask(inmac, "KEY").get("hash"), ask(inmac, "KEY").get("tabs")), ("#/dashboard", 0))
expect("and neither does a launch action that names Settings", ask(inmac, "ACT"), "#/dashboard")
expect("while the Mac's Appearance pane alone owns the paper", ask(inmac, "PAPER"), "")

# --- a computer the shop cannot vouch for is shown a shut Profile, not a refused save
proxy = boot(PROXY_PORT, extra=["--trust-proxy"], logfile=proxy_log, folder=other_book)
try:
    shut = probe([
        "poll|!!document.getElementById('setName')",
        say("SHUT", "JSON.stringify({name:document.getElementById('setName').disabled,"
                    "phone:document.getElementById('setPhone').disabled,"
                    "save:document.querySelector('[data-do=settings-shop-save]').disabled,"
                    "said:document.querySelector('#view .card-b').textContent})"),
    ], route="/#/settings?pane=profile", base=PROXY)
finally:
    stop(proxy)
refused = ask(shut, "SHUT")
expect("a counter who is not the shop sees the fields shut, not a save that will be refused",
       (refused.get("name"), refused.get("phone"), refused.get("save")), (True, True, True))
check("and is told why, in the engine's own words",
      "shop's computer" in (refused.get("said") or "").replace("\u2019", "'"),
      (refused.get("said") or "")[:70])
stop(proc)

# ------------------------------------------------------------------- 5. shipped alongside
print("\n== what a bundle and a browser cache are told to carry")
build = open(BUILD_TOOL, encoding="utf-8").read()
check("a built bundle carries the VERSION the counter's Updates tab reads",
      'APP_FILES = ("server.py", "schema.sql", "VERSION")' in build,
      line_saying(build, "APP_FILES"))
styles = open(STYLES_CSS, encoding="utf-8").read()
check("the strip has a style, and the sticky title bar knows to sit under it",
      ".menubar" in styles and 'data-menubar="on"' in styles,
      line_saying(styles, 'data-menubar="on"'))
sw = open(SW_JS, encoding="utf-8").read()
# The number is not the point; being ahead of the last cache that had no bar on it is. A check that
# named one exact version would fail the next time anybody shipped anything, which is a trap for the
# next deploy rather than a rule about this one.
shell_cache_version = int(re.search(r"crispprint-shell-v(\d+)", sw).group(1))
check("the shell cache is bumped past the copy that had no menu bar, so a counter who saw no menu "
      "yesterday sees one today", shell_cache_version >= 19, line_saying(sw, "CACHE_NAME ="))
check("and the new facts route is never served out of that cache",
      "url.pathname.startsWith('/api/')" in sw, "the worker lets /api/ past to the shop")
index = open(INDEX_HTML, encoding="utf-8").read()
check("the hours in the pre-paint script are still the shop's",
      "(h < 8 || h >= 17)" in index, "left byte-for-byte alone for check-daylight-theme.py")
check("the rail carries the Settings row for a computer whose menu bar has none",
      'data-view="settings"' in index, line_saying(index, 'data-view="settings"'))
check("and a Mac app window is marked before its first paint, with a style that hides the row",
      'dataset.pageSettings' in index and 'data-page-settings="shell"' in styles,
      line_saying(styles, 'data-page-settings="shell"'))
check("and the bar is in the page before the script runs", 'id="menubar"' in index)

# ------------------------------------------------------------------------- the book, whole
print("\n== the shop's own book")
after = os.stat(LIVE)
same = (after.st_size == live_stat.st_size and after.st_mtime == live_stat.st_mtime)
print("Live book: %d bytes, last written %s -- %s" % (
    after.st_size, time.ctime(after.st_mtime),
    "untouched" if same else "CHANGED, WHICH MUST NOT HAPPEN"))
if not same:
    fails.append("the shop's own book was written to")
shutil.rmtree(work, ignore_errors=True)

print("\n%s" % ("all checks passed" if not fails else "FAILED: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
