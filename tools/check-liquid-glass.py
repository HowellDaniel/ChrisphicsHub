#!/usr/bin/env python3
"""Prove the dashboard's glass is a real material, that it can be SEEN, and that it stays on the
dashboard — all of it on a COPY of the shop's book.

The owner's asks (2026-10-13): "add the apple liquid glass effects on the dashboard", and then, on
the build that shipped: "am not seeing the apple liquid effects". The second one is the reason this
file measures colour rather than rules. A stylesheet can say `backdrop-filter` all day; what the
counter sees is whatever the wash under a 72%-white panel leaves behind once 28% of it comes through,
and the first wash was dark pigment at a whisper — chroma 6 of 255 through the glass, which is grey.
So the checks below ask, in this order: is there a material, does it have colour a person can see
(both in the open gap and through the panel), and can the shop's figures still be read on it.

Two things a glass change breaks, and this file is built around catching both:

  legibility  — translucent panels over a coloured wash move every contrast ratio. The ratios are
                computed twice: once from the tokens in styles.css, once from the colours a real
                browser reports for the page it actually painted, over EVERY stop of BOTH wash layers
                and with the panel's own saturate() applied before the compositing. Both must clear
                WCAG AA.
  blast radius — the class is only ever put on by viewDashboard() and taken off by render(), so
                every other screen is loaded here and shown to still be opaque.

The live book is opened read-only and copied into a temp folder; the shop's file is never written
to. Loopback is the shop, so none of this needs the shop's password.

    python3 tools/check-liquid-glass.py
"""
import hashlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE = os.path.expanduser("~/Library/Application Support/Chrisphics Hub/chrisphics.db")


def free_port(start):
    """A port of our own, so a check that crashed earlier and left its server behind does not
    wedge this one. The shop's 8834 is never asked for."""
    for port in range(start, start + 40):
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", port))
            probe.close()
            return port
        except OSError:
            probe.close()
    raise SystemExit("no free loopback port above %d" % start)


PORT = free_port(8917)
BASE = "http://127.0.0.1:%d" % PORT

work = tempfile.mkdtemp(prefix="chrisphics-glass-")
CLONE = os.path.join(work, "clone.db")
src = sqlite3.connect("file:%s?mode=ro" % LIVE, uri=True)
dst = sqlite3.connect(CLONE)
src.backup(dst)
dst.close()
src.close()

wipe = sqlite3.connect(CLONE)
# The counter's Mac would otherwise ask this loopback to sign in, and no check here holds the
# shop's word. Cleared on this throwaway copy only.
wipe.execute("UPDATE app_state SET value='0' WHERE key='shop_mac_login'")
wipe.commit()
wipe.close()

live_stat = os.stat(LIVE)
live_hash = hashlib.sha256(open(LIVE, "rb").read()).hexdigest()

env = dict(os.environ, CHRISPHICS_DB=CLONE, CHRISPHICS_SUPPORT=work,
           CHRISPHICS_BACKUP_DIR=os.path.join(work, "Backups"),
           CHRISPHICS_CALLS_PER_MINUTE="4000", PYTHONUNBUFFERED="1")
log = open(os.path.join(work, "server.log"), "wb")

fails = []


def check(label, ok, detail=""):
    print("%-58s %s   %s" % (label, "ok" if ok else "FAIL", str(detail)[:66]))
    if not ok:
        fails.append(label)
    return ok


def expect(label, got, want):
    return check(label, got == want, "got %r, want %r" % (got, want))


def text(path):
    return open(os.path.join(REPO, path), encoding="utf-8").read()


def get_json(path):
    with urllib.request.urlopen(BASE + path, timeout=8) as res:
        return json.loads(res.read().decode())


# ---------------------------------------------------------------- the material, as written
css = text("public/styles.css")
app = text("public/app.js")
light_css, dark_css = css.split(':root[data-theme="dark"]', 1)
GLASS_TOKENS = ("--glass", "--glass-hover", "--glass-line", "--glass-rim", "--glass-spec",
                "--glass-spec-2", "--glass-spec-3", "--glass-shadow", "--wash-brand",
                "--wash-cool", "--wash-warn")
WASH_TOKENS = ("--wash-brand", "--wash-cool", "--wash-warn")
INK_TOKENS = ("--text", "--muted", "--ok", "--bad")
# --r-glass is a shape, not a colour: the panels are just as round at 5:00pm as at noon, so it is
# written once in :root and both palettes inherit it. Requiring it twice would only punish whoever
# retunes a colour later.
SHAPE_TOKEN = "--r-glass"


def token(block, name):
    """A colour token as the browser would resolve it, following one var() hop."""
    found = re.search(r"\n\s*%s:\s*([^;]+);" % re.escape(name), block)
    if not found:
        return None
    value = found.group(1).strip()
    hop = re.fullmatch(r"var\((--[\w-]+)\)", value)
    if hop:
        return token(block, hop.group(1))
    return value


def split_colour(value):
    """#rrggbb or rgba(r,g,b,a) as ((r, g, b), alpha)."""
    if value.startswith("#"):
        digits = value.lstrip("#")
        return (int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16)), 1.0
    numbers = [float(x) for x in re.findall(r"[\d.]+", value)]
    rgb = (int(numbers[0]), int(numbers[1]), int(numbers[2]))
    return rgb, (numbers[3] if len(numbers) > 3 else 1.0)


def over(fg, alpha, bg):
    return tuple(fg[i] * alpha + bg[i] * (1 - alpha) for i in range(3))


def saturate(rgb, k):
    """CSS saturate(), the feColorMatrix it is defined by, applied in sRGB as the engines do.
    This matters here and not in most reviews: the panel sits over the wash, the filter sits over
    the panel, so the colour a figure is measured against is the saturated one, not the token."""
    m = ((0.213 + 0.787 * k, 0.715 - 0.715 * k, 0.072 - 0.072 * k),
         (0.213 - 0.213 * k, 0.715 + 0.285 * k, 0.072 - 0.072 * k),
         (0.213 - 0.213 * k, 0.715 - 0.715 * k, 0.072 + 0.928 * k))
    return tuple(max(0.0, min(255.0, sum(m[i][j] * rgb[j] for j in range(3)))) for i in range(3))


def chroma(rgb):
    """How far a colour is from grey — the only number that says whether the wash can be SEEN.
    The complaint this file now answers was not that the glass was wrong but that it was invisible,
    and a panel can be perfectly translucent over a wash that is already grey."""
    return max(rgb) - min(rgb)


def luminance(rgb):
    def channel(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    hi, lo = max(luminance(a), luminance(b)), min(luminance(a), luminance(b))
    return (hi + 0.05) / (lo + 0.05)


def panel_rule():
    """The declaration block the glass panels get, found by content rather than by line number: the
    same selector is written twice in this file, once for the material and once for the fallback, and
    a check that read the wrong one would silently measure nothing."""
    starts = [i for i in range(len(css)) if css.startswith(PANEL_SELECTOR, i)]
    for start in starts:
        block = css[start:css.index("}", start)]
        if "backdrop-filter" in block:
            return block
    raise SystemExit("no glass panel rule with a backdrop-filter was found")


PANEL_SELECTOR = ".view.glass .card:not(.warn-card):not(.shut), .view.glass .stat {"
GLASS_RULE = panel_rule()
BLUR_PX = float(re.search(r"blur\((\d+(?:\.\d+)?)px\)", GLASS_RULE).group(1))
SAT_K = float(re.search(r"saturate\((\d+(?:\.\d+)?)%\)", GLASS_RULE).group(1)) / 100.0


def layers(block):
    """The three wash stops of one palette, and what each looks like both in the open gap between
    panels and under a panel: bare is what the eye sees on the page, panel is what the ink sits on."""
    paper = split_colour(token(block, "--paper"))
    glass = split_colour(token(block, "--glass"))
    out = []
    for wash in (split_colour(token(block, n)) for n in WASH_TOKENS):
        bare = saturate(over(wash[0], wash[1], paper[0]), SAT_K)
        out.append((bare, over(glass[0], glass[1], bare)))
    return out


def ratio_on_glass(block, ink_name):
    """The worst case the counter's eye can meet: the strongest wash stop, saturated by the panel's
    own filter, under the glass, under the ink. Each layer goes over the one it has already
    accumulated, in that order."""
    ink = split_colour(token(block, ink_name))
    return min(contrast(ink[0], panel) for _, panel in layers(block))


check("both palettes carry the whole set of glass colours",
      all(token(b, n) for b in (light_css, dark_css) for n in GLASS_TOKENS),
      " ".join(n for n in GLASS_TOKENS if not token(light_css, n) or not token(dark_css, n)))
check("the glass radius is written once, so both palettes share a shape",
      css.count("%s:" % SHAPE_TOKEN) == 1, token(light_css, SHAPE_TOKEN))
check("the glass is see-through in both palettes, and never below half",
      all(0.5 < split_colour(token(b, "--glass"))[1] < 1 for b in (light_css, dark_css)),
      "%s / %s" % (token(light_css, "--glass"), token(dark_css, "--glass")))
for palette, block in (("light", light_css), ("dark", dark_css)):
    figures = min(ratio_on_glass(block, n) for n in INK_TOKENS)
    check("%s: the worst ink on the glass clears WCAG AA" % palette, figures >= 4.5,
          "%.2f:1" % figures)
    shown = layers(block)
    gap = min(chroma(bare) for bare, _ in shown)
    on = min(chroma(panel) for _, panel in shown)
    check("%s: the wash carries colour a person can see, in the gap and through the glass" % palette,
          gap >= 45 and on >= 18, "gap chroma %d, panel chroma %d" % (gap, on))
check("the alarm cards are the one thing that stays opaque",
      ":not(.warn-card)" in css and ":not(.shut)" in css, "")
check("an engine with no backdrop-filter gets a solid panel, not a washed-out one",
      "@supports not ((backdrop-filter:" in css, "")
check("both spellings of the material are there, for WebKit and for Blink, and agree",
      css.count("blur(%dpx) saturate(%d%%)" % (int(BLUR_PX), int(SAT_K * 100))) == 2,
      "blur(%dpx) saturate(%d%%)" % (int(BLUR_PX), int(SAT_K * 100)))
check("the blur is wide enough to smear a blob edge, not just soften one",
      BLUR_PX >= 16, "%gpx" % BLUR_PX)
check("and both spellings of the rim's mask, which the two engines name differently",
      "-webkit-mask-composite: xor" in css and "mask-composite: exclude" in css, "")
def media_block(name):
    """The inside of one @media rule, found by counting braces rather than a regex — a regex that
    stops at the first `}` cannot cross the declaration blocks before the one being looked for, and
    a check that silently reads half a rule is worse than no check."""
    start = css.index("@media %s {" % name)
    depth = 0
    for i in range(start + len("@media %s {" % name) - 1, len(css)):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                return css[start:i + 1]
    raise SystemExit("@media %s is not balanced" % name)


check("the press gives, and stops giving when the counter asks for no motion",
      ".view.glass .stat.go:active { background: var(--glass); transform: scale(.985); }" in css
      and ".view.glass .card, .view.glass .stat { transition: none; }"
          in media_block("(prefers-reduced-motion: reduce)"),
      "")
check("and the wash stops drifting for that same counter, at rest rather than gone",
      ".view.glass::after { animation: none; opacity: .5; }"
      in media_block("(prefers-reduced-motion: reduce)"), "")
check("only the dashboard ever puts the glass on, and every route takes it off first",
      "$('#view').classList.add('glass')" in app and "v.classList.remove('glass')" in app, "")

# ---------------------------------------------------------------- the page, as the app draws it
THEME_JS = os.path.join(work, "theme-seed.js")
open(THEME_JS, "w", encoding="utf-8").write(r"""
/* The page has no look to pick any more — the shop's clock is the only rule (2026-10-13: "Remove
   Light, Dark or System because By clock is already there"). So this asks for a palette by handing
   the page an hour that wears one: mode=light freezes noon, mode=dark freezes nine at night. It also
   leaves the *opposite* word in localStorage, the way a build from before the removal would have, so
   every glass measurement here is additionally proof that a stored value is now ignored. */
(function () {
  var m = (location.search.match(/[?&]mode=([a-z]+)/) || [])[1];
  var Real = Date, fixed = null, origin = 0;
  function stamp() { return fixed === null ? Real.now() : fixed + (Real.now() - origin); }
  function Patched() {
    var args = Array.prototype.slice.call(arguments);
    if (!args.length) return fixed === null ? new Real() : new Real(stamp());
    return new (Function.prototype.bind.apply(Real, [null].concat(args)))();
  }
  Patched.prototype = Real.prototype;
  Patched.now = stamp;
  Patched.UTC = Real.UTC;
  Patched.parse = Real.parse;
  function freeze(hour) {
    var d = new Real();
    d.setHours(hour, 0, 0, 0);
    fixed = d.getTime();
    origin = Real.now();
  }
  if (m === "light") freeze(12);
  else if (m === "dark") freeze(21);
  try {
    if (m === "light" || m === "dark") localStorage.setItem("crispprint-theme", m === "light" ? "dark" : "light");
    else localStorage.removeItem("crispprint-theme");
  } catch (e) {}
  window.Date = Patched;
})();
""")

MEASURE = r"""
JSON.stringify((function () {
  var view = document.getElementById('view');
  if (!view) return { missing: 'view' };
  var card = document.querySelector('#view .card:not(.warn-card):not(.shut)');
  var stat = document.querySelector('#view .stat');
  var figure = document.querySelector('#view .stat .v');
  var gc = card ? getComputedStyle(card) : null;
  var gs = stat ? getComputedStyle(stat) : null;
  var rim = card ? getComputedStyle(card, '::after') : null;
  var wash = getComputedStyle(view, '::before');
  var wash2 = getComputedStyle(view, '::after');
  var numbers = function (value) { return (String(value).match(/[\d.]+/g) || []).map(Number); };
  var alpha = function (value) { var n = numbers(value); return n.length > 3 ? n[3] : 1; };
  return {
    theme: document.documentElement.dataset.theme,
    buttons: document.querySelectorAll('[data-theme-set]').length,
    stored: (function () { try { return localStorage.getItem('crispprint-theme'); }
                           catch (e) { return null; } })(),
    viewGlass: view.classList.contains('glass'),
    body: getComputedStyle(document.body).backgroundColor,
    cardAlpha: gc ? alpha(gc.backgroundColor) : null,
    cardRGB: gc ? numbers(gc.backgroundColor).slice(0, 3) : null,
    radius: gc ? gc.borderTopLeftRadius : null,
    filter: gc ? (gc.backdropFilter || gc.webkitBackdropFilter) : null,
    inset: gc ? /inset/.test(gc.boxShadow) : false,
    rim: rim ? rim.backgroundImage : null,
    rimMask: rim ? (rim.webkitMaskComposite || rim.maskComposite) : null,
    rimRadius: rim ? rim.borderTopLeftRadius : null,
    wash: wash.backgroundImage,
    wash2: wash2.backgroundImage,
    washAnim: wash2.animationName,
    washAnimTime: wash2.animationDuration,
    washSeen: wash2.opacity,
    statAlpha: gs ? alpha(gs.backgroundColor) : null,
    ink: figure ? getComputedStyle(figure).color : null,
    quiet: (function () { var h = document.querySelector('#view .hint');
      return h ? getComputedStyle(h).color : null; })()
  };
})())
"""

PROBE_BIN = os.path.join(work, "wk-probe")
built = subprocess.run(["swiftc", os.path.join(REPO, "tools/wk-probe.swift"), "-o", PROBE_BIN],
                       capture_output=True, text=True, timeout=300)
if not check("the rendering engine probe compiles", built.returncode == 0,
             (built.stderr or "")[-200:]):
    print("\ncould not build tools/wk-probe.swift — no browser checks ran")
    sys.exit(1)

server = subprocess.Popen([sys.executable, os.path.join(REPO, "server.py"),
                           "--port", str(PORT), "--no-browser"],
                          cwd=REPO, env=env, stdout=log, stderr=log)
ready = False
for _ in range(60):
    time.sleep(0.25)
    try:
        if server.poll() is None and "authenticated" in get_json("/api/session"):
            ready = True
            break
    except Exception:
        continue
if not check("a throwaway shop server is answering on 127.0.0.1:%d" % PORT, ready):
    print("\nthe throwaway shop server did not start — no browser checks ran")
    sys.exit(1)


def run_probe(route, commands, label="", size=None):
    """Load one screen of the throwaway shop in the shipped engine and hand back what it said."""
    args = [PROBE_BIN, BASE, work]
    if size:
        args.append("size|%dx%d" % size)
    args.append("open|/?mode=%s#/%s" % (MODE, route))
    args.append("poll|!document.querySelector('#view .loading')")
    args += commands
    args.append("state|")
    out = subprocess.run(args, capture_output=True, text=True, timeout=180,
                         env=dict(os.environ, WKPROBE_SCRIPT=THEME_JS))
    raw = out.stdout or ""
    said = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith(("OPEN", "SUMMARY", "view:", "shot ->", "viewport now",
                                        "TIMEOUT waiting", "console>", "ALERT:", "CONFIRM:",
                                        "NAV FAILED", "EVAL ERROR", "SNAPSHOT ERROR",
                                        "unknown command", "bad size")):
            continue
        said.append(line)
    for line in raw.splitlines() + (out.stderr or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(("EVAL ERROR", "SNAPSHOT ERROR", "TIMEOUT waiting", "NAV FAILED")):
            fails.append("%s: %s" % (label, stripped[:70]))
    reports = []
    for line in said:
        if line.startswith("{"):
            try:
                reports.append(json.loads(line))
            except ValueError:
                reports.append({})
    return said, reports


def measured_ratio(m, ink):
    """The same computation the tokens got, but from the colours this browser reported for the page
    it actually painted — and over EVERY stop of BOTH wash layers, not the first one it finds. The
    first ship of this check read only the first stop, which is exactly how a glass can be green and
    the page still report itself pink."""
    body = split_colour(m["body"])
    glass = (tuple(m["cardRGB"]), m["cardAlpha"])
    ink_rgb, _ = split_colour(ink)
    stops = re.findall(r"rgba?\([^)]*\)", (m.get("wash") or "") + (m.get("wash2") or ""))
    worst = 99.0
    for raw in stops:
        wash = split_colour(raw)
        bare = saturate(over(wash[0], wash[1], body[0]), SAT_K)
        worst = min(worst, contrast(ink_rgb, over(glass[0], glass[1], bare)))
    return worst if stops else 0.0


def measured_chroma(m):
    """How far from grey the panel actually is, on the page the engine painted. This is the number
    the owner's complaint is really about, so it is measured rather than argued."""
    body = split_colour(m["body"])
    glass = (tuple(m["cardRGB"]), m["cardAlpha"])
    out = 0
    for raw in re.findall(r"rgba?\([^)]*\)", (m.get("wash") or "") + (m.get("wash2") or "")):
        wash = split_colour(raw)
        bare = saturate(over(wash[0], wash[1], body[0]), SAT_K)
        out = max(out, chroma(over(glass[0], glass[1], bare)))
    return out


def want(block, name):
    """A value the CSS itself states, so the browser check can never drift from the stylesheet."""
    return split_colour(token(block, name))[1]


GLASS_A = {palette: want(block, "--glass") for palette, block in
           (("light", light_css), ("dark", dark_css))}
RADIUS = token(light_css, SHAPE_TOKEN)

MODE = "light"
said, reports = run_probe("dashboard", ["say|" + MEASURE, "shot|glass-light"], "light dashboard")
m = reports[0] if reports else {}
print("\n--- the dashboard, in the engine the Mac app ships")
expect("noon on the clock wears the light palette", m.get("theme"), "light")
expect("...with the opposite word left in storage by an older build, ignored",
       m.get("stored"), "dark")
expect("and the page carries no appearance buttons to override either", m.get("buttons"), 0)
expect("the dashboard puts the class on itself", m.get("viewGlass"), True)
expect("and the panel is the glass, not the surface", m.get("cardAlpha"), GLASS_A["light"])
check("its corner lifts to the glass radius", (m.get("radius") or "") == RADIUS, m.get("radius"))
check("it really does sample what is behind it",
      "blur(%dpx)" % int(BLUR_PX) in (m.get("filter") or "")
      and ("saturate(%d%%)" % int(SAT_K * 100) in (m.get("filter") or "")
           or "saturate(%g)" % SAT_K in (m.get("filter") or "")),
      m.get("filter"))
check("the light catches the top edge", m.get("inset") is True, "")
check("and runs along the whole rim as a masked gradient",
      "linear-gradient" in (m.get("rim") or "") and (m.get("rimRadius") or "").endswith(RADIUS),
      "%s / %s" % ((m.get("rim") or "")[:34], m.get("rimRadius")))
check("the rim is a ring, not a filled plate",
      str(m.get("rimMask") or "").strip() in ("xor", "exclude"), m.get("rimMask"))
check("there are two arrangements of the shop's three colours behind the glass to let through",
      (m.get("wash") or "").count("radial-gradient") == 3
      and (m.get("wash2") or "").count("radial-gradient") == 3,
      "%s + %s blobs" % ((m.get("wash") or "").count("radial-gradient"),
                         (m.get("wash2") or "").count("radial-gradient")))
check("the second one drifts over the first, so the tiles change hue under the figures",
      m.get("washAnim") not in (None, "none") and m.get("washAnimTime") not in (None, "0s"),
      "%s / %s" % (m.get("washAnim"), m.get("washAnimTime")))
expect("the money tiles are glass too", m.get("statAlpha"), GLASS_A["light"])
check("the figures stay readable on it, as drawn",
      measured_ratio(m, m.get("ink") or "#000") >= 4.5, "%.2f:1" % measured_ratio(m, m.get("ink") or "#000"))
check("and so does the quiet text", measured_ratio(m, m.get("quiet") or "#000") >= 4.5,
      "%.2f:1" % measured_ratio(m, m.get("quiet") or "#000"))
check("and as drawn, the glass is a coloured material and not a grey one",
      measured_chroma(m) >= 18, "panel chroma %d" % measured_chroma(m))

MODE = "dark"
said, reports = run_probe("dashboard", ["say|" + MEASURE, "shot|glass-dark"], "dark dashboard")
d = reports[0] if reports else {}
expect("nine at night wears the dark palette", d.get("theme"), "dark")
expect("and the dark palette gets its own glass", d.get("cardAlpha"), GLASS_A["dark"])
check("on a dark counter the rim keeps its shine", d.get("inset") is True, "")
check("and the figures still clear AA on it", measured_ratio(d, d.get("ink") or "#fff") >= 4.5,
      "%.2f:1" % measured_ratio(d, d.get("ink") or "#fff"))
check("with the evening wash showing through it", measured_chroma(d) >= 18,
      "panel chroma %d" % measured_chroma(d))

MODE = "light"
said, reports = run_probe("dashboard", ["say|" + MEASURE, "shot|glass-on-a-phone"], "phone",
                          (390, 844))
p = reports[0] if reports else {}
check("on a phone the panels are still glass",
      p.get("cardAlpha") == GLASS_A["light"] and (p.get("radius") or "") == RADIUS,
      "alpha=%s radius=%s" % (p.get("cardAlpha"), p.get("radius")))

print("\n--- every other screen, which must not have changed")
for route in ("jobs", "collect", "spoiled", "sync", "clients", "accounts", "leads", "expenses",
              "reports", "shop"):
    said, reports = run_probe(route, ["say|" + MEASURE], route)
    r = reports[0] if reports else {}
    check("%s: no glass on this screen" % route, r.get("viewGlass") is False, str(r.get("viewGlass")))
    if r.get("cardAlpha") is not None:
        expect("%s: and its panels are still solid" % route, r.get("cardAlpha"), 1)

# ---------------------------------------------------------------- the book is still the shop's
after = os.stat(LIVE)
expect("the live book was not written to",
       hashlib.sha256(open(LIVE, "rb").read()).hexdigest(), live_hash)
check("the live book is the size it was", after.st_size == live_stat.st_size,
      "%d vs %d" % (after.st_size, live_stat.st_size))

server.terminate()
for _ in range(40):
    if server.poll() is not None:
        break
    time.sleep(0.25)
log.close()

shots = sorted(f for f in os.listdir(work) if f.endswith(".png"))
print("\nshots: " + ", ".join(os.path.join(work, f) for f in shots))
print("live book: %d bytes, sha256 %s (untouched)" % (after.st_size, live_hash[:16]))
print("\n%d checks failed" % len(fails))
sys.exit(1 if fails else 0)
