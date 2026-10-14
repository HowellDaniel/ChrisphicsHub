#!/usr/bin/env python3
"""Prove the Mac app's own day/night rule — on the real code, hour by hour, no live shop touched.

The counter's word on 2026-10-13 was "Remove Light, Dark or System because By clock is already
there", so the app no longer has a mode to pick: the shop's clock (light 08:00 to 17:00, dark
outside them) is the shell's only rule, and the same rule is what the page and its pre-paint script
work to. This runs the rule hour by hour out of the real code and then reads the shell and the page
back, refusing any leftover appearance choice — a menu item, a Settings pane, a stored mode, a
segmented button.

Nothing here waits for 5:00 pm. `desktop/src/Theme.swift` is compiled on its own into a throwaway
command line tool that answers for all twenty-four hours, so the rule is run rather than read.
The same tool is what stops the app's hours drifting away from the page's: the numbers live in
three files now, and this one fails if they ever disagree.

The shop's book is opened read-only and hashed before and after — this check should not touch it,
and says so.

    python3 tools/check-app-daylight.py
"""
import hashlib
import os
import re
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE = os.path.expanduser("~/Library/Application Support/Chrisphics Hub/chrisphics.db")
THEME_SWIFT = os.path.join(REPO, "desktop/src/Theme.swift")
MAIN_SWIFT = os.path.join(REPO, "desktop/src/main.swift")
BUILD_SH = os.path.join(REPO, "desktop/build-app.sh")

DAY_START = 8
DAY_END = 17

work = tempfile.mkdtemp(prefix="chrisphics-app-daylight-")
fails = []
live_hash = hashlib.sha256(open(LIVE, "rb").read()).hexdigest()


def check(label, ok, detail=""):
    print("%-58s %s   %s" % (label, "ok" if ok else "FAIL", str(detail)[:66]))
    if not ok:
        fails.append(label)
    return ok


def expect(label, got, want):
    return check(label, got == want, "got %r, want %r" % (got, want))


def text(path):
    full = path if os.path.isabs(path) else os.path.join(REPO, path)
    return open(full, encoding="utf-8").read()


# ---------------------------------------------------------------- the app's rule, compiled alone
open(os.path.join(work, "main.swift"), "w", encoding="utf-8").write(r"""
// Ask Theme.swift what it would do at every hour of the day, and say it back in one letter each:
// L for the light window chrome, D for the dark. Nothing here opens a window or touches a book.
import AppKit

func describe(_ a: NSAppearance) -> String {
    if a.name == NSAppearance.Name.darkAqua { return "D" }
    if a.name == NSAppearance.Name.aqua { return "L" }
    return "?" + a.name.rawValue
}

func at(_ hour: Int, _ minute: Int = 0) -> Date {
    var c = Calendar.current.dateComponents([.year, .month, .day], from: Date())
    c.hour = hour
    c.minute = minute
    c.second = 0
    return Calendar.current.date(from: c)!
}

let table = (0..<24).map { describe(Theme.appearance(at: at($0))) }.joined()
print("table=\(table)")
print("edge=\(describe(Theme.appearance(at: at(7, 59))))"
    + "/\(describe(Theme.appearance(at: at(8, 0))))"
    + "/\(describe(Theme.appearance(at: at(16, 59))))"
    + "/\(describe(Theme.appearance(at: at(17, 0))))")
print("band=\(Theme.dayStart)-\(Theme.dayEnd)")
print("eveningAt17=\(Theme.isEvening(at(17)))")
print("eveningAt8=\(Theme.isEvening(at(8)))")
print("midMorningEvening=\(Theme.isEvening(at(10)))")
print("afterEightPmEvening=\(Theme.isEvening(at(20)))")
""")

BIN = os.path.join(work, "daylight-harness")
built = subprocess.run(["swiftc", "-O", "-swift-version", "5", "-framework", "Cocoa",
                        "-o", BIN, THEME_SWIFT, os.path.join(work, "main.swift")],
                       capture_output=True, text=True, timeout=420)
if not check("the app's appearance rule compiles on its own", built.returncode == 0,
             (built.stderr or "")[-260:]):
    print("\ncould not build desktop/src/Theme.swift — no hour checks ran")
    sys.exit(1)

ran = subprocess.run([BIN], capture_output=True, text=True, timeout=120)
answers = {}
for line in (ran.stdout or "").splitlines():
    if "=" in line:
        k, v = line.split("=", 1)
        answers[k.strip()] = v.strip()
if not check("and answers when asked", ran.returncode == 0 and bool(answers.get("table")),
             (ran.stderr or "")[-200:] or ran.stdout[:200]):
    print("\nthe harness did not answer — no hour checks ran")
    sys.exit(1)

want = "D" * DAY_START + "L" * (DAY_END - DAY_START) + "D" * (24 - DAY_END)
expect("dark till 08:00, light 08:00 to 16:59, dark again from 17:00",
       answers.get("table"), want)
expect("the turn is exactly at the two hours, not before or after",
       answers.get("edge"), "D/L/L/D")
expect("the app's own numbers are 8 and 17", answers.get("band"), "%d-%d" % (DAY_START, DAY_END))

# ---------------------------------------------------------------- one rule, three files
swift = text(THEME_SWIFT)
app = text("public/app.js")
head = text("public/index.html")
sw = re.search(r"static let dayStart = (\d+)\n\s*static let dayEnd = (\d+)", swift)
js = re.search(r"const DAY_START = (\d+);\nconst DAY_END = (\d+);", app)
html = re.search(r"h < (\d+) \|\| h >= (\d+)", head)
check("the shell keeps its hours in one named pair", bool(sw), sw.groups() if sw else "not found")
if check("the page and the shell state the same band",
         bool(sw) and bool(js) and sw.groups() == js.groups(),
         "swift %s vs app.js %s" % (sw.groups() if sw else "-", js.groups() if js else "-")):
    expect("and the pre-paint script of the page agrees", html.groups() if html else None,
           sw.groups())

expect("half past eight in the evening is evening, eight in the morning is not",
       answers.get("eveningAt17") + "/" + answers.get("eveningAt8"), "true/false")
expect("ten in the morning is not evening", answers.get("midMorningEvening"), "false")
expect("and eight at night is", answers.get("afterEightPmEvening"), "true")

# ---------------------------------------------------------------- nothing left to choose
main = text(MAIN_SWIFT)
check("the shell has no mode to pick, stored or otherwise",
      "Theme.saved" not in swift and "Theme.saved" not in main and "allCases" not in swift
      and "commit()" not in main, "")
check("the View menu carries no Appearance submenu",
      "appearanceSubmenu" not in main and "pickTheme" not in main, "")
check("Settings has no Appearance pane, and the three that remain are still listed",
      "AppearancePane" not in main and "for pane in [GeneralPane(), ProfilePane(), UpdatesPane()] {" in main,
      "")
check("the shell watches the hour after the window is drawn",
      "Theme.startClockWatch()" in main and "func startClockWatch" in swift, "")
check("the page can no longer tell the shell a look, or be told one by it",
      'Bridge.bind("theme"' not in main and 'case "theme":' not in main, "")
build = text(BUILD_SH)
check("the build compiles the rule and the shell together",
      "src/main.swift" in build and "src/Theme.swift" in build, "")
check("the page ships no appearance buttons, in its markup or its stylesheet",
      "data-theme-set" not in head and ".themeseg" not in text("public/styles.css")
      and "crispprint-theme" not in head, "")
check("and the page's own entry point cannot be walked to a fixed look",
      "THEME_MODES" not in app and "setTheme" not in app and "prefersDark" not in app, "")

# ---------------------------------------------------------------- the bundle the shop is running
live = subprocess.run(["ps", "ax"], capture_output=True, text=True).stdout
running = [ln.split()[0] for ln in live.splitlines()
           if "Chrisphics Hub.app/Contents/MacOS" in ln]
check("the counter's app was not touched by this check", bool(running),
      "pid(s) " + " ".join(running) if running else "not running")

after = hashlib.sha256(open(LIVE, "rb").read()).hexdigest()
expect("the live book was not written to", after, live_hash)

print("\nharness: " + BIN)
print("%d checks failed" % len(fails))
sys.exit(1 if fails else 0)
