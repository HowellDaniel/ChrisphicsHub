#!/usr/bin/env python3
"""Prove the dashboard keeps the shop's day, and that the look follows the clock — on a COPY.

The owner's ask (2026-10-13): put a calendar and a time on the dashboard, and let the screen turn
dark by itself when the evening comes and light again in the morning, between 8:00am and 5:00pm.
He asked four times. The first three answers were a line of small text, a tile in the corner of a
card, and a single big day number — the iPhone's *small* calendar widget — and the last of them drew
"let the calendar widget be the medium type with all the days on it, not small". So this checks the
medium form as the browser paints it: the whole month on one face, six rows of seven, every day of
it present and in order, today set in a roundel, beside a clock drawn at lock-screen size.

The clock is now the only rule there is. He said on 2026-10-13: "Remove Light, Dark or System
because By clock is already there", so the page's buttons, the stored mode and the Mac shell's
Appearance pane are gone — and these checks fail if any of them comes back, or if a value an older
build left in someone's storage is ever read again.

Then he asked for the weather in the same tile size, after the calendar ("Add the weather after the
calendar widget in the same size like the calendar"). That is checked the same way, and against the
calendar: the two tiles' painted boxes, faces, corners and shadows are compared with each other, and
the figures come from a fixture rather than from the sky, because a check that depends on the weather
outside the counter is not a check. What the page does when the service answers, when it does not, and
when the machine only has an old reading in its pocket are all three gated.

Nothing here is a screenshot someone has to squint at. Every colour is read out of the palette block
the page is actually wearing and re-measured against AA; every size is measured off the layout, so a
widget that drifts back towards body text, a month that loses a day, a roundel that ends up on two
days at once, a weather tile that quietly becomes a different size, or a grid that hangs off a phone
fails here instead of quietly becoming invisible — which is how this card got redone three times.

Waiting for 5:00 pm is not a check. Every page here is run through the shipped rendering engine
(the same WebKit the Mac app uses) with its clock frozen by an injected script, so the hour under
test is the hour the page believes — from the pre-paint head script onwards, not just afterwards.
One run hands the clock over the boundary mid-page and then waits for the screen to change with
nobody touching it. Another walks the clock across midnight and across a month-end, which is when a
calendar widget is worth anything.

The live book is opened read-only and copied into a temp folder; the shop's file is never written
to. Loopback is the shop, so none of this needs the shop's password.

    python3 tools/check-daylight-theme.py
"""
import calendar
import datetime as dt
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


PORT = free_port(8903)
BASE = "http://127.0.0.1:%d" % PORT

DAY_START = 8      # what the counter asked for: light from 8:00am
DAY_END = 17       # ... dark from 5:00pm

work = tempfile.mkdtemp(prefix="chrisphics-daylight-")
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

# The probe reloads the whole page a dozen times inside a minute. That is this check's own noise,
# not a shop under load, so the throwaway server is told not to throttle it.
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


# ---------------------------------------------------------------- static: one clock, two files
app = text("public/app.js")
head = text("public/index.html")
app_band = re.search(r"const DAY_START = (\d+);\nconst DAY_END = (\d+);", app)
head_band = re.search(r"h < (\d+) \|\| h >= (\d+)", head)

expect("app.js names the band 8 and 17",
       app_band.groups() if app_band else None, ("8", "17"))
expect("index.html's pre-paint script agrees on 8 and 17",
       head_band.groups() if head_band else None, ("8", "17"))
check("neither file names a mode to pick any more",
      '"light", "dark", "system"' not in head and "THEME_MODES" not in app
      and "setTheme" not in app and "themeMode" not in app,
      "head list %s / app.js modes %s" % ('"light", "dark", "system"' in head,
                                           "THEME_MODES" in app))
# The head script does read from localStorage now — the paper and the size of the words, so a window
# does not flash plain white before the shop's wallpaper lands. Neither of those is a look the clock
# decides, so the rule still holds in the form that matters: nothing is ever written, and the two
# names read back are exactly these. An "anyThemeStored" style test would have failed for a reason
# that has nothing to do with the day/night band.
check("the page's own head script stores nothing the clock could be contradicted by",
      "setItem" not in head and "crispprint-theme" not in head and "matchMedia" not in head,
      "setItem in head: %s / theme key in head: %s" % ("setItem" in head, "crispprint-theme" in head))
expect("and the only looks it reads back are the paper and the words, never a theme",
       sorted(set(re.findall(r"localStorage\.getItem\('([^']+)'\)", head))),
       ["crispprint-background", "crispprint-text-size"])
check("and the stylesheet has no appearance control left in it",
      "themeseg" not in text("public/styles.css"), "")

# ---------------------------------------------------------------- the page, with a frozen clock
PROBE_JS = os.path.join(work, "clock-probe.js")
open(PROBE_JS, "w", encoding="utf-8").write(r"""
/* Run the page on a clock of the check's own choosing, before any of its scripts exist:
   ?probe=HH:MM:SS freezes this window's Date, ?mode= writes a value into localStorage the way an
   older build did while the page still had Light / Dark / System buttons ("clear" removes it), so a
   check can prove the page now ignores whatever one left behind. The frozen clock still runs
   forward, so a screen that is meant to tick can be seen ticking. */
(function () {
  var Real = Date;
  var fixed = null, origin = 0;
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
  window.__freeze = function (h, m, s) {
    var d = new Real();
    d.setHours(h, m || 0, s || 0, 0);
    fixed = d.getTime();
    origin = Real.now();
  };
  /* Move the believed clock by a span of time rather than to a time of day, so a page can be
     walked across midnight — setHours would pin it to today. */
  window.__shift = function (ms) {
    if (fixed === null) { fixed = Real.now(); origin = Real.now(); }
    fixed += ms;
  };
  var want = "", mode = "", wx = "";
  (location.search || "").replace(/^\?/, "").split("&").forEach(function (kv) {
    var p = kv.split("=");
    if (p[0] === "probe") want = decodeURIComponent(p[1] || "");
    if (p[0] === "mode") mode = decodeURIComponent(p[1] || "");
    if (p[0] === "wx") wx = decodeURIComponent(p[1] || "");
  });
  if (want) {
    var b = want.split(":");
    window.__freeze(+b[0], +(b[1] || 0), +(b[2] || 0));
  }
  try {
    if (mode === "clear") localStorage.removeItem("crispprint-theme");
    else if (mode) localStorage.setItem("crispprint-theme", mode);
  } catch (e) {}
  /* The weather tile asks a real forecast service over the real network, and a check that passes or
     fails depending on what the sky is doing outside the counter is not a check. So this window's
     fetch() answers for api.open-meteo.com itself: ?wx= names one of the fixtures below (whose
     numbers are the ones Open-Meteo really returned for Accra at 19:30 on 2026-10-13, so the shape is
     the shape the page will meet), ?wx=fail rejects the way an offline machine does, ?wx=old seeds a
     three-day-old reading into storage and then goes offline — which is the case that tempts a page
     to invent a figure — and ?wx=live leaves the page's own fetch alone, so the tile can be seen
     reaching the actual service. Every other request, the shop's own /api/ calls included, goes to the
     real fetch untouched. Whatever ?wx= is, the reading stored by the last run is thrown away first:
     the page paints whatever it finds in storage before it asks anything, and a figure left there by
     one run's fixture would otherwise be reported as the next run's answer — which is exactly the
     mistake that made a "live" run here once print a made-up forecast and look like it had earned it. */
  var WX_FIX = {
    night: {
      current: { time: "2026-10-13T19:00", temperature_2m: 26.6, apparent_temperature: 30.3,
                 relative_humidity_2m: 80, is_day: 0, weather_code: 3 },
      hourly: { time: ["2026-10-13T19:00", "2026-10-13T20:00", "2026-10-13T21:00",
                       "2026-10-13T22:00", "2026-10-13T23:00", "2026-10-14T00:00",
                       "2026-10-14T01:00"],
                temperature_2m: [26.6, 26.2, 25.8, 25.5, 25.2, 25.0, 24.9],
                weather_code: [3, 3, 2, 2, 1, 1, 0] },
      daily: { temperature_2m_max: [31.5], temperature_2m_min: [24.6] }
    },
    day: {
      current: { time: "2026-10-13T13:00", temperature_2m: 30.9, apparent_temperature: 33.4,
                 relative_humidity_2m: 66, is_day: 1, weather_code: 0 },
      hourly: { time: ["2026-10-13T13:00", "2026-10-13T14:00", "2026-10-13T15:00",
                       "2026-10-13T16:00", "2026-10-13T17:00", "2026-10-13T18:00",
                       "2026-10-13T19:00"],
                temperature_2m: [30.9, 31.2, 31.5, 31.0, 30.2, 29.1, 28.0],
                weather_code: [0, 0, 1, 1, 2, 2, 3] },
      daily: { temperature_2m_max: [31.5], temperature_2m_min: [24.6] }
    },
    rain: {
      current: { time: "2026-10-13T13:00", temperature_2m: 27.4, apparent_temperature: 29.8,
                 relative_humidity_2m: 88, is_day: 1, weather_code: 63 },
      hourly: { time: ["2026-10-13T13:00", "2026-10-13T14:00", "2026-10-13T15:00",
                       "2026-10-13T16:00", "2026-10-13T17:00", "2026-10-13T18:00",
                       "2026-10-13T19:00"],
                temperature_2m: [27.4, 27.1, 26.9, 26.6, 26.4, 26.1, 25.9],
                weather_code: [63, 61, 61, 55, 53, 51, 3] },
      daily: { temperature_2m_max: [29.2], temperature_2m_min: [24.1] }
    }
  };
  function wxAnswer(body, wait) {
    return new Promise(function (res) {
      setTimeout(function () {
        res({ ok: true, status: 200, statusText: "OK",
              json: function () { return Promise.resolve(body); } });
      }, wait || 30);
    });
  }
  try { localStorage.removeItem("crispprint-weather"); } catch (e) {}
  if (wx === "old") {
    try {
      var n = WX_FIX.night.current, stale = {
        place: "Accra", temperature: n.temperature_2m, feels: n.apparent_temperature,
        humidity: n.relative_humidity_2m, code: n.weather_code, isDay: n.is_day,
        hi: WX_FIX.night.daily.temperature_2m_max[0], lo: WX_FIX.night.daily.temperature_2m_min[0],
        hours: WX_FIX.night.hourly.time.slice(0, 6).map(function (t, i) {
          return { t: t, temp: WX_FIX.night.hourly.temperature_2m[i] };
        }),
        at: Real.now() - 3 * 86400000
      };
      localStorage.setItem("crispprint-weather", JSON.stringify(stale));
    } catch (e) {}
  }
  if (wx && wx !== "live") {
    var realFetch = window.fetch.bind(window);
    var openMeteoTries = 0;
    window.__liveTries = 0;
    window.__shopTries = 0;
    window.fetch = function (input, init) {
      var u = typeof input === "string" ? input : (input && input.url) || "";
      var byShop = u.indexOf("/api/weather") !== -1;
      if (u.indexOf("api.open-meteo.com") === -1 && !byShop) return realFetch(input, init);
      /* Only the page's own ask is counted, because the retry rule is about that ask. The shop's
         relay is the second half of the same road: ?wx=fail and ?wx=old refuse both, or the check
         would be watching a tile quietly filled by the road it meant to close. ?wx=shoponly refuses
         only the machine's own ask — the counter whose internet is down while the shop's is up. */
      if (byShop) window.__shopTries += 1;
      else { openMeteoTries += 1; window.__liveTries = openMeteoTries; }
      /* ?wx=offthenon is the machine that was briefly out of range: the first ask is refused, every
         ask after it answers. It exists to catch a page that gives up on one try. */
      if (wx === "fail" || wx === "old") return Promise.reject(new TypeError("offline"));
      if (wx === "shoponly" && !byShop) return Promise.reject(new TypeError("offline"));
      if (wx === "offthenon" && openMeteoTries === 1) return Promise.reject(new TypeError("offline"));
      /* ?wx=slow keeps the answer in the air for three seconds, which is the only way to see what the
         tile says while a request it never made is still on its way. */
      return wxAnswer(WX_FIX[wx === "shoponly" ? "rain" : wx] || WX_FIX.night,
                      wx === "slow" ? 3000 : 30);
    };
    window.__liveLog = "fixture";
  }
  if (wx === "live") {
    /* The real-service run is the one check here that cannot promise an answer, so when it settles on
       "No weather" the report has to say why rather than shrug. This wrapper only watches. */
    var watched = window.fetch.bind(window);
    window.__liveLog = "not asked yet";
    window.__liveTries = 0;
    window.fetch = function (input, init) {
      var u = typeof input === "string" ? input : (input && input.url) || "";
      if (u.indexOf("api.open-meteo.com") === -1) return watched(input, init);
      window.__liveTries += 1;
      window.__liveLog = "asked";
      return watched(input, init).then(function (r) {
        window.__liveLog = "answered http " + r.status;
        return r;
      }, function (e) {
        window.__liveLog = "refused: " + (e && e.name) + " " + (e && e.message);
        throw e;
      });
    };
  }
  /* Being "inside the Mac app" is not something this script can fake: WebKit owns window.webkit
     and installs its own bridge after the page's scripts run. The probe can still ask for the
     app's handler — see WKPROBE_HANDLERS in run_probe — which is how a check proves the page
     posts nothing there now, the shell having no appearance choice left to keep in step. */
  window.Date = Patched;
})();
""")

REPORT = ("JSON.stringify({"
          "theme:document.documentElement.dataset.theme,"
          "modeAttr:(document.documentElement.dataset.themeMode===undefined"
          " ? 'none' : document.documentElement.dataset.themeMode),"
          "look:(document.getElementById('todayLook')||{}).textContent,"
          "clock:(document.getElementById('todayClock')||{}).textContent,"
          "date:(document.getElementById('todayDate')||{}).textContent,"
          "card:(document.getElementById('todayCard')||{}).className,"
          "buttons:document.querySelectorAll('[data-theme-set]').length,"
          "hint:((document.querySelector('#view .today-face .hint')||{}).textContent||'').trim(),"
          "stored:(function(){try{return localStorage.getItem('crispprint-theme')||'nothing';}"
          "catch(e){return 'nothing';}})()"
          "})")

LOOKS = ("Array.apply(null,{length:24}).map(function(_n,h){"
         "return window.chrisphics.look(h)?'D':'L';}).join('')")

CLOCKED = "(document.getElementById('todayClock')||{}).textContent"
DATED = "(document.getElementById('todayDate')||{}).textContent"
# What the two widgets actually are, as the browser paints them: every day the month has, in order,
# the one cell wearing today, the ink each part came out in, the size it was drawn at, whether the
# grid really holds seven columns, and whether the big clock sits beside the face or under it.
WIDGET = ("(function(){function g(sel,prop){var e=document.querySelector(sel);"
          "return e?getComputedStyle(e)[prop]:'none';}"
          "function t(id){var e=document.getElementById(id);return e?e.textContent:'';}"
          "function box(sel){var e=document.querySelector(sel);if(!e)return 'none';"
          "var r=e.getBoundingClientRect();return Math.round(r.width)+'x'+Math.round(r.height);}"
          "var cal=document.getElementById('todayCal'),clock=document.getElementById('todayClock');"
          "var cells=[].slice.call(document.querySelectorAll('.cal-cell'));"
          "var a=cal&&cal.getBoundingClientRect(),c=clock&&clock.getBoundingClientRect();"
          "return JSON.stringify({"
          "mon:t('todayMonth'),year:t('todayYear'),num:t('todayNum'),date:t('todayDate'),"
          "label:cal?cal.getAttribute('aria-label'):'',"
          "cells:cells.length,marked:document.querySelectorAll('.cal-cell.is-today').length,"
          "dows:document.querySelectorAll('.cal-dows span').length,"
          "days:cells.map(function(x){return x.textContent;}).join(','),"
          "inside:!!(document.querySelector('.cal-cell.is-today')&&cal"
          "&&cal.contains(document.querySelector('.cal-cell.is-today'))),"
          "beside:(a&&c&&c.left>=a.right)?'yes':'no',"
          "stacked:(a&&c&&c.top>=a.bottom)?'yes':'no',"
          "faceBg:g('.today-cal','backgroundColor'),"
          "headFg:g('.cal-mon','color'),numFg:g('.cal-cell:not(.edge):not(.is-today)','color'),"
          "edgeFg:g('.cal-cell.edge','color'),dowFg:g('.cal-dows span','color'),"
          "markBg:g('.cal-cell.is-today','backgroundColor'),"
          "markFg:g('.cal-cell.is-today','color'),"
          "radius:g('.today-cal','borderRadius'),box:box('.today-cal'),"
          "round:box('.cal-cell.is-today'),cols:g('.cal-grid','gridTemplateColumns'),"
          "markClass:((document.querySelector('.cal-cell.is-today')||{}).className||''),"
          "markSize:parseFloat(g('.cal-cell.is-today','fontSize')),"
          "headSize:parseFloat(g('.cal-mon','fontSize')),"
          "cellSize:parseFloat(g('.cal-cell','fontSize')),"
          "dowSize:parseFloat(g('.cal-dows span','fontSize')),"
          "rowH:parseFloat(g('.cal-cell','height')),"
          "clockSize:parseFloat(g('.today-clock','fontSize')),"
          "secSize:parseFloat(g('.today-clock s','fontSize')),"
          "mdmSize:parseFloat(g('.today-clock u','fontSize')),"
          "clock:t('todayClock'),"
          "spill:(document.documentElement.scrollWidth>document.documentElement.clientWidth)"
          "?'yes':'no'});})()")
# The weather tile, measured the same way — and measured against the calendar it was asked to match,
# because the word was "the same size like the calendar". Width, height, face ink, corner and shadow
# all come back for both tiles from one painted page, so the two cannot drift apart without this
# saying so. The figures are the fixture's, so what is checked is the page's reading of them, not the
# sky's mood outside the counter.
WEATHER = ("(function(){function g(sel,prop){var e=document.querySelector(sel);"
          "return e?getComputedStyle(e)[prop]:'none';}"
          "function t(sel){var e=document.querySelector(sel);return e?e.textContent.trim():'';}"
          "var wx=document.getElementById('todayWx'),cal=document.getElementById('todayCal');"
          "var wr=wx&&wx.getBoundingClientRect(),cr=cal&&cal.getBoundingClientRect();"
          "var sp=[].slice.call(document.querySelectorAll('.wx-hours span'));"
          "var gl=document.querySelector('.wx-glyph');"
          "return JSON.stringify({"
          "place:t('.wx-place'),temp:t('.wx-temp'),cond:t('.wx-cond'),hilo:t('.wx-hilo'),"
          "feels:t('.wx-feels'),age:t('.wx-age'),none:t('.wx-none'),"
          "cells:sp.length,"
          "labels:sp.map(function(x){return (x.querySelector('b')||{}).textContent||'';}).join(','),"
          "temps:sp.map(function(x){return (x.querySelector('i')||{}).textContent||'';}).join(','),"
          "glyph:gl?gl.className.replace('wx-glyph','').trim():'',"
          "svg:gl&&gl.querySelector('svg')?'yes':'no',glyphColor:g('.wx-glyph','color'),"
          "after:(wr&&cr&&wr.left>=cr.right)?'yes':'no',"
          "below:(wr&&cr&&wr.top>=cr.bottom)?'yes':'no',"
          "wxBox:Math.round(wr&&wr.width)+'x'+Math.round(wr&&wr.height),"
          "calBox:Math.round(cr&&cr.width)+'x'+Math.round(cr&&cr.height),"
          "faceBg:g('.today-wx','backgroundColor'),ink:g('.today-wx','color'),"
          "radius:g('.today-wx','borderRadius'),shadow:g('.today-wx','boxShadow'),"
          "calBg:g('.today-cal','backgroundColor'),calRadius:g('.today-cal','borderRadius'),"
          "calShadow:g('.today-cal','boxShadow'),"
          "tempSize:parseFloat(g('.wx-temp','fontSize')),"
          "tempFg:g('.wx-temp','color'),placeFg:g('.wx-place','color'),condFg:g('.wx-cond','color'),"
          "hiloFg:g('.wx-hilo','color'),quietFg:g('.wx-feels','color'),"
          "hourFg:g('.wx-hours b','color'),hourTempFg:g('.wx-hours i','color'),"
          "hourCols:sp.length?g('.wx-hours','gridTemplateColumns'):'none',"
          "live:(function(){try{return window.__liveLog||'';}catch(e){return '';}})(),"
          "tries:(function(){try{return String(window.__liveTries||0);}catch(e){return '';}})(),"
          "shopTries:(function(){try{return String(window.__shopTries||0);}catch(e){return '';}})(),"
          "label:wx?wx.getAttribute('aria-label'):'',"
          "spill:(document.documentElement.scrollWidth>document.documentElement.clientWidth)"
          "?'yes':'no'});})()")
WAIT1 = "poll|(window.__n=(window.__n||0)+1)>6"          # about 1.4 seconds, on the probe's own clock
HAVE_CLOCK = "poll|!!document.getElementById('todayClock')"
# The tile answers with a figure once the forecast is in, and with the shop's own plain words when it
# is not. Both are waited for by what is actually on the screen, so no check here sleeps on a hope.
HAVE_WX = "poll|!!document.querySelector('.wx-temp')"
HAVE_OFFLINE = "poll|/No weather/.test((document.querySelector('.wx-none')||{}).textContent||'')"

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


def run_probe(query, commands, label="", size=None, native=False):
    """Load the dashboard once, believing the hour the query names, and hand back what it said.

    native=True registers a handler under the name the shipped Mac shell registers, so anything the
    page still posted to the shell comes back as a "posted theme: …" line — which is how the check
    proves the page no longer posts one."""
    args = [PROBE_BIN, BASE, work]
    if size:
        args.append("size|%dx%d" % size)
    args.append("open|/?%s#/dashboard" % query)
    args.append(HAVE_CLOCK)
    args += commands
    args.append("state|")
    probe_env = dict(os.environ, WKPROBE_SCRIPT=PROBE_JS)
    if native:
        probe_env["WKPROBE_HANDLERS"] = "theme"
    out = subprocess.run(args, capture_output=True, text=True, timeout=180, env=probe_env)
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


DAY_WORDS = "Daytime · dark from 5:00 pm"
NIGHT_WORDS = "Evening · light from 8:00 am"

# 1. An evening: the clock decides, and it decides dark. Nothing else is consulted.
said, reports = run_probe("probe=20:05:00&mode=clear",
                          ["say|" + REPORT, "say|" + LOOKS, "shot|evening-desktop"], "evening")
r = reports[0] if reports else {}
expect("evening, nothing stored: the screen is dark", r.get("theme"), "dark")
expect("and the page keeps no mode of its own to say so", r.get("modeAttr"), "none")
expect("evening: the card says when the light returns", r.get("look"), NIGHT_WORDS)
check("evening: the card is dressed for the evening", "night" in (r.get("card") or ""), r.get("card"))
expect("evening: the clock reads 12-hour am/pm", r.get("clock"), "8:05:00 pm")
expect("and the page carries no appearance buttons at all", r.get("buttons"), 0)
hint = r.get("hint") or ""
check("the card's own words offer no look to pick",
      not re.search(r"choos|overrul|by clock|system|follow the mac", hint, re.I), hint[:78])
check("and say the one rule instead, in the shop's own hours",
      "8:00 am" in hint and "5:00 pm" in hint and "by itself" in hint, hint[:78])
table = "D" * DAY_START + "L" * (DAY_END - DAY_START) + "D" * (24 - DAY_END)
expect("all twenty-four hours, answered by the page's own rule",
       said[1] if len(said) > 1 else "", table)

# 2. The two turns of the band, to the minute.
for hour, theme, words in (("07:59:30", "dark", NIGHT_WORDS),
                           ("08:00:00", "light", DAY_WORDS),
                           ("16:59:30", "light", DAY_WORDS),
                           ("17:00:00", "dark", NIGHT_WORDS)):
    said, reports = run_probe("probe=%s&mode=clear" % hour, ["say|" + REPORT], hour[:5])
    r = reports[0] if reports else {}
    expect("%s: the screen is %s" % (hour[:5], theme), r.get("theme"), theme)
    expect("%s: and the card says so" % hour[:5], r.get("look"), words)

# 3. The turn itself: one page held open, nobody presses anything, the evening arrives.
said, reports = run_probe("probe=16:00:00&mode=clear", [
    "say|" + REPORT,
    "eval|window.__freeze(17,0,0)",
    "poll|document.documentElement.dataset.theme==='dark'",
    "say|" + REPORT,
    "shot|evening-after-the-turn",
    "eval|window.__freeze(8,0,0)",
    "poll|document.documentElement.dataset.theme==='light'",
    "say|" + REPORT,
], "the turn")
afternoon = reports[0].get("theme") if reports else None
if check("the page opened in the afternoon wearing the light look", afternoon == "light",
         str(reports[:1])):
    turned = reports[1].get("theme") if len(reports) > 1 else None
    woke = reports[2].get("theme") if len(reports) > 2 else None
    check("5:00 pm came and it went dark with nobody touching it", turned == "dark", str(reports[1:2]))
    check("8:00 am came and it went light the same way", woke == "light", str(reports[2:3]))
    check("and the card's words turned with the screen",
          len(reports) > 2 and reports[1].get("look") == NIGHT_WORDS
          and reports[2].get("look") == DAY_WORDS,
          " | ".join(x.get("look", "") for x in reports[1:3]))

# 4. Whatever an older build left in storage changes nothing, in either direction: the clock
#    outranks a stored Light, a stored Dark and a stored System alike.
for left, hour, paints in (("dark", "09:00:00", "light"), ("light", "21:00:00", "dark"),
                           ("system", "21:00:00", "dark")):
    said, reports = run_probe("probe=%s&mode=%s" % (hour, left), ["say|" + REPORT],
                              "leftover-" + left)
    r = reports[0] if reports else {}
    check("%s stored by an old build, at %s: the clock paints %s" % (left, hour[:5], paints),
          r.get("theme") == paints, "got %s, want %s" % (r.get("theme"), paints))
    check("...after really handing the page that value to ignore", r.get("stored") == left,
          r.get("stored"))

# 5. Inside the Mac app. The shell used to seed the page with the look its own Settings had stored
#    and hide the page's buttons; it has no such choice any more, so the page reads the clock exactly
#    as a phone does and posts nothing back. The probe registers the handler the shipped app still
#    binds, so anything the page posted would come back as a "posted theme:" line.
said, reports = run_probe("probe=21:00:00&mode=clear", [
    "say|" + REPORT,
    "eval|window.chrisphics.theme('dark')",
    "say|" + REPORT,
    "shot|in-the-app-at-nine",
], "in the app", native=True)
r = reports[0] if reports else {}
expect("the app at nine in the evening is dark", r.get("theme"), "dark")
expect("and it shows no appearance buttons, inside the app or out", r.get("buttons"), 0)
after = reports[1] if len(reports) > 1 else {}
expect("a menu call from an older shell leaves it on the clock", after.get("theme"), "dark")
check("and nothing at all is posted to the shell to be remembered",
      not any("posted theme:" in line for line in said), " | ".join(said[:3]))
expect("the card in the app says the same", after.get("look"), NIGHT_WORDS)
said, reports = run_probe("probe=10:00:00&mode=dark", ["say|" + REPORT],
                          "app at ten", native=True)
r = reports[0] if reports else {}
expect("the same app in the morning is light, whatever its storage holds", r.get("theme"), "light")

# 6. The clock runs, and the date it carries is the shop's real day.
said, reports = run_probe("probe=11:23:00&mode=clear", [
    "say|" + CLOCKED, WAIT1, "say|" + CLOCKED, "say|" + DATED,
    "size|390x844", "shot|morning-on-a-phone",
], "ticking")
now = time.localtime()
today = "%s, %d %s %d" % (time.strftime("%A", now), now.tm_mday, time.strftime("%B", now), now.tm_year)
first = said[0] if said else ""
second = said[1] if len(said) > 1 else ""
check("the clock shows the hour it was handed", first.startswith("11:23:") and first.endswith(" am"),
      first)
check("and it moves on by itself a second later", first != second and second.endswith(" am"),
      "%s -> %s" % (first, second))
expect("the date on the dashboard is today's", (said[2] if len(said) > 2 else ""), today)

# 7. The two widgets the counter named, as the browser paints them — the month on one face with
#    today in a roundel, and a clock at lock-screen size. Nothing below is hardcoded: each colour is
#    read out of the palette block the page is actually wearing, each size out of the stylesheet's own
#    rule, and every ink is re-measured against AA here. So a retune that makes the widget prettier
#    than it is readable, a month that loses a day, or a grid that goes back to body text fails here
#    instead of quietly becoming invisible again — which is how this card got redone three times.
css = text("public/styles.css")
light_css, dark_css = css.split(':root[data-theme="dark"]', 1)


def token(block, name):
    """A colour token as the browser would resolve it, following one var() hop."""
    found = re.search(r"\n\s*%s:\s*([^;]+);" % re.escape(name), block)
    if not found:
        return None
    value = found.group(1).strip()
    hop = re.fullmatch(r"var\((--[\w-]+)\)", value)
    if hop:
        return token(block, hop.group(1))
    digits = value.lstrip("#")
    return "rgb(%d, %d, %d)" % (int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16))


def palette(theme):
    """The block of tokens the page is wearing at the hour under test."""
    return dark_css if theme == "dark" else light_css


def rgb_of(value):
    """Either spelling of a colour — a token's #rrggbb or the page's rgb() — as three numbers."""
    if not value:
        return None
    if value.startswith("#"):
        digits = value.lstrip("#")
        return tuple(int(digits[i:i + 2], 16) for i in (0, 2, 4))
    found = re.match(r"rgba?\(([^)]+)\)", value)
    if not found:
        return None
    return tuple(int(float(x.strip())) for x in found.group(1).split(",")[:3])


def aa(fg, bg):
    """WCAG contrast ratio, from the colours the stylesheet and the page each named."""
    f, b = rgb_of(fg), rgb_of(bg)
    if not f or not b:
        return 0.0

    def luminance(c):
        def part(v):
            v = v / 255.0
            return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
        return 0.2126 * part(c[0]) + 0.7152 * part(c[1]) + 0.0722 * part(c[2])

    x, y = luminance(f), luminance(b)
    return (max(x, y) + 0.05) / (min(x, y) + 0.05)


def decl(selector, prop):
    """One declaration out of one rule, so an expectation can be the stylesheet's own number. A
    declaration may share its line with its neighbours, so the boundary here is the brace or a
    semicolon rather than the start of a line."""
    at = css.index(selector + " {")
    block = css[at:css.index("}", at)]
    found = re.search(r"(?:\{|;)\s*%s:\s*([^;]+);" % re.escape(prop), block)
    if not found:
        raise AssertionError("%s has no %s" % (selector, prop))
    return found.group(1).strip()


def widget_colours(where, w):
    """Every ink on the month face against the surface it is painted on, AA or not, printed with its
    provenance so a stale palette or a wrong surface self-diagnoses instead of costing a cycle. The
    roundel, the month name, the day numerals, the neighbouring months' days and the weekday initials
    are five different pairs, and a medium widget shows all five at once."""
    for pair, fg, bg in (("today's roundel", w.get("markFg"), w.get("markBg")),
                         ("month name", w.get("headFg"), w.get("faceBg")),
                         ("day numeral", w.get("numFg"), w.get("faceBg")),
                         ("neighbouring month", w.get("edgeFg"), w.get("faceBg")),
                         ("weekday initial", w.get("dowFg"), w.get("faceBg"))):
        ratio = aa(fg, bg)
        check("%s: the %s clears AA on the widget" % (where, pair), ratio >= 4.5,
              "%.2f:1  fg=%s bg=%s" % (ratio, fg, bg))


def month_days(when):
    """The forty-two numbers a Monday-first month face has to carry, in order — this month's days and
    the weeks of the months either side, which is how the widget is asked to draw them."""
    first = dt.date(when.year, when.month, 1)
    lead = first.weekday()                                    # Monday == 0, so October opens on Thu
    count = calendar.monthrange(when.year, when.month)[1]
    prev = first - dt.timedelta(days=1)
    out = []
    for i in range(42):
        n = i + 1 - lead
        out.append(str(prev.day + n if n < 1 else (n - count if n > count else n)))
    return out, lead


def side(which, box):
    """The width or the height out of the probe's 'WxH', so a shape can be gated on its own number."""
    try:
        return float(str(box).split("x")[which])
    except (TypeError, ValueError, IndexError):
        return 0.0


def written(d):
    """A date the way the card writes it: en-GB, the weekday spelled out, no leading zero."""
    return "%s, %d %s %d" % (d.strftime("%A"), d.day, d.strftime("%B"), d.year)


now = time.localtime()
this_day = dt.date(now.tm_year, now.tm_mon, now.tm_mday)
expected, lead = month_days(this_day)
marks = tuple("--cal-mark-%d" % i for i in range(7))
mark_of_day = marks[(now.tm_mday - 1) % 7]
check("the seven rotating roundel inks are all tokens the page carries, in both palettes",
      all(token(light_css, n) and token(dark_css, n) for n in
          marks + ("--cal-mark-ink", "--cal-head", "--cal-face", "--cal-ink", "--cal-soft")),
      " / ".join("%s=%s" % (n, token(light_css, n)) for n in marks))
# The owner's rule for the rotating roundel: the number has to read on whichever circle the date
# puts it in, so the one white ink is measured against all seven, not just today's.
for name in marks:
    check("the roundel ink clears AA on %s" % name, aa("#ffffff", token(light_css, name)) >= 4.5,
          "%.2f:1 on %s" % (aa("#ffffff", token(light_css, name)), token(light_css, name)))

said, reports = run_probe("probe=13:00:00&mode=clear",
                          ["say|" + REPORT, "say|" + WIDGET, "shot|daytime-widget"], "day widget")
r = reports[0] if reports else {}
w = reports[1] if len(reports) > 1 else {}
p = palette(r.get("theme"))
expect("midday: the page is wearing the light palette", r.get("theme"), "light")
expect("midday: the face carries this month", w.get("mon"), time.strftime("%B"))
expect("midday: and its year", w.get("year"), str(now.tm_year))
check("midday: the face is the medium type — six rows of seven, all the days on it",
      w.get("cells") == 42 and len(str(w.get("cols", "")).split()) == 7,
      "%s cells in %s columns" % (w.get("cells"), len(str(w.get("cols", "")).split())))
expect("midday: every day of the month, in order, with the weeks either side",
       w.get("days"), ",".join(expected))
expect("midday: today's number is the day wearing the roundel", w.get("num"), str(now.tm_mday))
check("midday: and today is marked exactly once", w.get("marked") == 1, w.get("marked"))
check("midday: the roundel sits in the right column of the right row",
      (w.get("days") or "").split(",")[lead + now.tm_mday - 1] == str(now.tm_mday),
      "row %d, column %d of a Monday-first month" % (
          (lead + now.tm_mday - 1) // 7 + 1, (lead + now.tm_mday - 1) % 7 + 1))
check("midday: the initials run the seven days", w.get("dows") == 7, w.get("dows"))
check("midday: the month face is inside the day card", w.get("inside") is True, str(w.get("inside")))
expect("midday: the clock sits beside the month, not under it", w.get("beside"), "yes")
expect("midday: the roundel wears the ink its own date is tokened for", w.get("markBg"),
       token(p, mark_of_day))
expect("midday: with the white number that ink is tokened for", w.get("markFg"),
       token(p, "--cal-mark-ink"))
expect("midday: and the cell is stamped with that day's mark",
       "mark-%d" % ((now.tm_mday - 1) % 7) in str(w.get("markClass", "")).split(), True)
expect("midday: the month name takes the ink its palette tokens for it", w.get("headFg"),
       token(p, "--cal-head"))
expect("midday: the page is the shop's own surface", w.get("faceBg"), token(p, "--cal-face"))
expect("midday: and the numerals their own ink", w.get("numFg"), token(p, "--cal-ink"))
expect("midday: the days of the months either side are the quiet token, not a faded numeral",
       w.get("edgeFg"), token(p, "--cal-soft"))
widget_colours("midday", w)
expect("midday: rounded to the corner the stylesheet names", w.get("radius"),
       decl(".today-cal", "border-radius"))
check("midday: the face is the width of a medium widget", side(0, w.get("box")) >= 240, w.get("box"))
check("midday: the day numerals are not small", w.get("cellSize", 0) >= 15,
      "%spx, stylesheet says %s" % (w.get("cellSize"), decl(".cal-cell", "font-size")))
check("midday: the month name is the loudest word on the face",
      w.get("headSize", 0) >= 18 and w.get("headSize", 0) > w.get("cellSize", 99),
      "%spx over %spx" % (w.get("headSize"), w.get("cellSize")))
check("midday: today's mark is a roundel, not a stripe",
      side(0, w.get("round")) == side(1, w.get("round")) and side(0, w.get("round")) >= 19,
      w.get("round"))
# "let the day number be small in the red circle" (2026-10-24): the figure in the roundel is drawn
# smaller than the day numerals around it, and small enough that the whole number — two digits at
# most — sits inside the circle with room to spare.
check("midday: the number in the roundel is smaller than the day numerals",
      0 < w.get("markSize", 0) < w.get("cellSize", 99),
      "%spx inside, %spx around it" % (w.get("markSize"), w.get("cellSize")))
check("midday: and small enough to sit whole inside the circle",
      w.get("markSize", 0) * 1.6 <= side(0, w.get("round")),
      "%spx in a %spx circle" % (w.get("markSize"), side(0, w.get("round"))))
check("midday: and every row is still taller than the number in it", w.get("rowH", 0) >= 19,
      "%spx rows" % w.get("rowH"))
# "reduce the two widget and make them landscape" (2026-10-13, 21:00). The word is about the shape and
# about the size, so both are numbers here: the month lies down — at least a half again wider than it
# is tall — and the whole face fits inside 200px of the card, which is a third less than it took before
# he asked. Reducing it further than that would start cutting days off the face, and the face is the
# one thing he named: "all the days on it, not small".
check("midday: the month lies down — a landscape face, not a standing one",
      side(0, w.get("box")) >= side(1, w.get("box")) * 1.5, w.get("box"))
check("midday: reduced — the whole month in under 200px of height",
      side(1, w.get("box")) <= 200, w.get("box"))
check("midday: the clock is drawn at lock-screen size", w.get("clockSize", 0) >= 30,
      "%spx" % w.get("clockSize"))
check("midday: the seconds and am/pm stay small beside it",
      0 < w.get("secSize", 99) < w.get("clockSize", 0) and 0 < w.get("mdmSize", 99) <= w.get("secSize", 0),
      "%s / %s within %s" % (w.get("secSize"), w.get("mdmSize"), w.get("clockSize")))
check("midday: the split spans still read as one plain time",
      re.match(r"^\d{1,2}:\d{2}:\d{2} [ap]m$", w.get("clock") or ""), w.get("clock"))
check("midday: the month is announced to anyone reading the screen aloud",
      str(w.get("label", "")).startswith(time.strftime("%B"))
      and str(now.tm_mday) in str(w.get("label")), w.get("label"))
check("the widget is the shop's crimson, not Apple's red",
      "rgb(255, 59, 48)" not in (w.get("markBg"), w.get("markFg"), w.get("headFg")),
      w.get("markBg"))

said, reports = run_probe("probe=20:05:00&mode=clear",
                          ["say|" + REPORT, "say|" + WIDGET, "shot|evening-widget"], "evening widget")
r = reports[0] if reports else {}
w = reports[1] if len(reports) > 1 else {}
p = palette(r.get("theme"))
expect("eight in the evening: the page is wearing the dark palette", r.get("theme"), "dark")
expect("eight in the evening: the roundel keeps the ink its own date picked",
       w.get("markBg"), token(p, mark_of_day))
expect("eight in the evening: and the same white number on it", w.get("markFg"),
       token(p, "--cal-mark-ink"))
expect("eight in the evening: the month name turns with the palette", w.get("headFg"),
       token(p, "--cal-head"))
expect("eight in the evening: the page face turns with the palette", w.get("faceBg"),
       token(p, "--cal-face"))
widget_colours("evening", w)
expect("eight in the evening: still beside the month", w.get("beside"), "yes")
expect("eight in the evening: still the same month it was at noon", w.get("mon"),
       time.strftime("%B"))
expect("eight in the evening: the same days on it as at noon", w.get("days"), ",".join(expected))
check("eight in the evening: and the same one day marked", w.get("marked") == 1, w.get("marked"))
check("eight in the evening: and the clock is no smaller", w.get("clockSize", 0) >= 30,
      "%spx" % w.get("clockSize"))

# 8. The widget over a midnight the counter is asleep through, and over the last minute of a month: a
#    page left open overnight moves its roundel by itself, and at the month-end it redraws all
#    forty-two cells and rolls its header. That is where a calendar widget earns its place, so both
#    run on one page held open rather than by reloading it.
tomorrow = this_day + dt.timedelta(days=1)
said, reports = run_probe("probe=23:59:20&mode=clear", [
    "say|" + WIDGET,
    "eval|window.__before=(document.getElementById('todayNum')||{}).textContent",
    "eval|window.__shift(60000)",
    "poll|(document.getElementById('todayNum')||{}).textContent!==window.__before",
    "say|" + WIDGET, "shot|widget-after-midnight"], "midnight")
first = reports[0] if reports else {}
rolled = reports[1] if len(reports) > 1 else {}
expect("eleven minutes to midnight: the roundel is on today", first.get("num"), str(now.tm_mday))
expect("the night turned over and the roundel went with it", rolled.get("num"), str(tomorrow.day))
expect("and the written date rolled to tomorrow's weekday", rolled.get("date"), written(tomorrow))
check("and the month still holds all of its days after the roll", rolled.get("cells") == 42,
      rolled.get("cells"))
check("and still marks one day, not two", rolled.get("marked") == 1, rolled.get("marked"))

next_month = (this_day.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
said, reports = run_probe("probe=23:59:00&mode=clear", [
    "say|" + WIDGET,
    "eval|window.__mon=(document.getElementById('todayMonth')||{}).textContent",
    "eval|var n=new Date();window.__shift(new Date(n.getFullYear(),n.getMonth()+1,1,0,0,30)"
    ".getTime()-Date.now())",
    "poll|document.getElementById('todayMonth').textContent!==window.__mon",
    "say|" + WIDGET], "month-end")
rolled = reports[1] if len(reports) > 1 else {}
n_days, n_lead = month_days(next_month)
expect("the last minute of the month, and the header rolls", rolled.get("mon"),
       next_month.strftime("%B"))
expect("and the roundel lands on the first", rolled.get("num"), "1")
expect("the grid redraws for the new month, with all of its days", rolled.get("days"),
       ",".join(n_days))
check("and it opens on the new month's first weekday",
      (rolled.get("days") or "").split(",")[n_lead] == "1",
      "%s has %d days, %s opens the week" % (next_month.strftime("%B"),
                                             calendar.monthrange(next_month.year, next_month.month)[1],
                                             next_month.strftime("%A")))
check("and it still marks one day", rolled.get("marked") == 1, rolled.get("marked"))

# 9. A phone-sized dashboard keeps the whole month, in the dark, inside the screen.
said, reports = run_probe("probe=20:05:00&mode=clear", ["say|" + REPORT, "say|" + WIDGET,
                                                       "shot|widget-on-a-phone"],
                          "phone", (390, 844))
r = reports[0] if reports else {}
expect("on a phone at eight in the evening it is dark too", r.get("theme"), "dark")
check("on a phone the date is still there", bool(r.get("date")), r.get("date"))
w = reports[1] if len(reports) > 1 else {}
check("on a phone the month keeps every day", w.get("days") == ",".join(expected),
      "%s cells" % w.get("cells"))
expect("on a phone today is still the marked day", w.get("num"), str(now.tm_mday))
check("on a phone the month takes the row and the clock drops under it", w.get("stacked"), "yes")
check("on a phone the numerals are still not small", w.get("cellSize", 0) >= 14,
      "%spx" % w.get("cellSize"))
check("on a phone the clock is still the loudest thing on the card",
      w.get("clockSize", 0) >= 28, "%spx" % w.get("clockSize"))
check("on a phone the widget keeps its own face", w.get("radius") ==
      decl(".today-cal", "border-radius"), "%s" % w.get("radius"))
widget_colours("on a phone", w)
expect("and nothing hangs off the edge of the screen", w.get("spill"), "no")

# 10. The weather tile: "Add the weather after the calendar widget in the same size like the
#     calendar" (2026-10-13). Same size is a claim about two painted boxes, so both are measured from
#     the one page and compared — width, height, face ink, corner and shadow — and the tile's own
#     figures come from a fixture the page cannot argue with. What is checked here is the page's
#     reading of a forecast, the hour the forecast service says it read, the ink every line is drawn
#     in, and the two ways nothing arrives: a machine that cannot reach the service, and one that has
#     a three-day-old reading in its pocket. Neither is allowed to print an invented number.
csp = re.search(r"<meta[^>]+Content-Security-Policy[^>]*>", head, re.I)
check("the page's own content policy still lets the forecast through",
      csp is None or "api.open-meteo.com" in csp.group(0),
      csp.group(0)[:60] if csp else "no content policy on the page")
check("the offline shell does not try to answer for the forecast",
      "url.origin !== self.location.origin" in text("public/sw.js"), "sw.js lets cross-origin GETs go")
check("the tile asks for the shop's own town by name and number",
      "WX_PLACE = 'Accra'" in app and "latitude=' + WX_LAT" in app and "WX_LAT = 5.5560" in app,
      re.search(r"const WX_PLACE[^\n]*", app).group(0)[:60] if re.search(r"const WX_PLACE[^\n]*", app) else "")
check("and a figure that is not a number is never rounded into one",
      "typeof v === 'number' ? Math.round(v)" in app, "wxDeg() answers with an em dash")


def weather_colours(where, x):
    """Every ink on the weather face against the surface it is painted on. The tile borrows the
    calendar's tokens rather than carrying colours of its own, which is what lets this say the same
    thing about both faces at once: a token that moves for the month moves for the weather too, and a
    pair that fails for one fails here as well."""
    for pair, fg in (("big temperature", x.get("tempFg")), ("place name", x.get("placeFg")),
                     ("condition", x.get("condFg")), ("high and low", x.get("hiloFg")),
                     ("feels and humidity", x.get("quietFg")), ("hour label", x.get("hourFg")),
                     ("hour figure", x.get("hourTempFg"))):
        ratio = aa(fg, x.get("faceBg"))
        check("%s: the %s clears AA on the weather tile" % (where, pair), ratio >= 4.5,
              "%.2f:1  fg=%s bg=%s" % (ratio, fg, x.get("faceBg")))


said, reports = run_probe("probe=20:05:00&mode=clear&wx=night",
                          [HAVE_WX, "say|" + WEATHER, "shot|evening-weather"], "evening weather")
x = reports[0] if reports else {}
expect("eight in the evening: the place on the tile is the town the shop works in",
       x.get("place"), "Accra")
expect("evening: the fixture's 26.6 is drawn as the shop's own 27", x.get("temp"), "27°")
expect("evening: the WMO code 3 is a word, not a number", x.get("cond"), "Overcast")
expect("evening: with the day's high and low beside it", x.get("hilo"), "H 32° · L 25°")
expect("evening: and what it feels like, and the damp", x.get("feels"),
       "Feels 30° · Humidity 80%")
expect("evening: it is after the calendar, the way he asked", x.get("after"), "yes")
check("evening: the tile is exactly the calendar's width",
      side(0, x.get("wxBox")) == side(0, x.get("calBox")) and side(0, x.get("wxBox")) >= 240,
      "weather %s against the month's %s" % (x.get("wxBox"), x.get("calBox")))
check("evening: and level with it — the same height, face to face",
      abs(side(1, x.get("wxBox")) - side(1, x.get("calBox"))) <= 2,
      "weather %s against the month's %s" % (x.get("wxBox"), x.get("calBox")))
expect("evening: on the very same face the month is printed on", x.get("faceBg"), x.get("calBg"))
expect("evening: with the month's own corner", x.get("radius"), x.get("calRadius"))
expect("evening: with the counter's own shadow", x.get("shadow"), x.get("calShadow"))
expect("evening: and the corner the stylesheet names for both",
       decl(".today-wx", "border-radius"), decl(".today-cal", "border-radius"))
expect("evening: the face is the calendar's token, not a colour of its own",
       x.get("faceBg"), token(palette("dark"), "--cal-face"))
expect("evening: the big figure is the calendar's own ink",
       x.get("tempFg"), token(palette("dark"), "--cal-ink"))
check("evening: the temperature is drawn big, the way the widget is",
      x.get("tempSize", 0) >= 34, "%spx" % x.get("tempSize"))
check("evening: and the tile lies down with the month rather than standing beside it",
      side(0, x.get("wxBox")) >= side(1, x.get("wxBox")) * 1.5
      and side(1, x.get("wxBox")) <= 200, x.get("wxBox"))
expect("evening: overcast is a cloud, drawn in the shop's slate", x.get("glyph"),
       "wx-cloud is-night")
expect("evening: and the ink that slate is tokened for", x.get("glyphColor"),
       token(palette("dark"), "--cool"))
check("evening: the glyph is real drawing, not an emoji the counter's machine may not have",
      x.get("svg") == "yes" and "<svg" not in (x.get("cond") or ""), x.get("svg"))
expect("evening: six hours along the bottom", x.get("cells"), 6)
# The machine believes it is 20:05; the fixture says the service read at 19:00. The strip starts where
# the SERVICE says it is, so a wrong clock on the counter's laptop cannot move the forecast.
expect("evening: the strip runs on the service's own hour, not this machine's",
       x.get("labels"), "now,8pm,9pm,10pm,11pm,12am")
expect("evening: and six figures under six hours", x.get("temps"), "27°,26°,26°,26°,25°,25°")
expect("evening: a reading that just came in does not apologise for itself", x.get("age"), "")
expect("evening: read out loud, the tile says the weather in words", x.get("label"),
       "Accra: Overcast, 27 degrees, feels like 30, high 32, low 25.")
expect("evening: three tiles and a clock still do not hang off the screen", x.get("spill"), "no")
weather_colours("evening", x)

# 10a. The width the counter's own window opens at. This is the half of "make them landscape" that a
#      desktop run cannot see: at 340px apiece the two tiles could not share a row inside a 630px
#      content column, so they stacked one over the other and the clock fell off the screen. Reduced,
#      they fit side by side in that same window — which is what his screenshot was actually about.
said, reports = run_probe("probe=13:00:00&mode=clear&wx=day",
                          [HAVE_WX, "say|" + WEATHER, "shot|counter-window"], "counter window",
                          (860, 800))
z = reports[0] if reports else {}
expect("in the counter's window the weather is still after the month, not under it",
       z.get("after"), "yes")
expect("and the two tiles are still one box, reduced together", z.get("wxBox"), z.get("calBox"))
check("neither of them is pushed off the edge of that window", z.get("spill"), "no")

# 10b. The same tile in the morning, and in the rain: a different palette, a different sky, the same
#      face. The sun is the one glyph that changes ink with the weather it draws, and it changes to
#      the shop's own amber rather than to a colour invented for the occasion.
said, reports = run_probe("probe=13:00:00&mode=clear&wx=day",
                          [HAVE_WX, "say|" + WEATHER, "shot|daytime-weather"], "daytime weather")
y = reports[0] if reports else {}
expect("midday: the page is wearing the light palette", y.get("faceBg"),
       token(palette("light"), "--cal-face"))
expect("midday: 30.9 drawn as 31", y.get("temp"), "31°")
expect("midday: code 0 is clear, and the glyph says so", y.get("glyph"), "wx-sun")
expect("midday: in the shop's own amber", y.get("glyphColor"), token(palette("light"), "--warn"))
expect("midday: with the damp the service reported", y.get("feels"), "Feels 33° · Humidity 66%")
expect("midday: the strip follows the service's 1 pm", y.get("labels"), "now,2pm,3pm,4pm,5pm,6pm")
expect("midday: still after the month", y.get("after"), "yes")
check("midday: still the month's own width and height",
      side(0, y.get("wxBox")) == side(0, y.get("calBox"))
      and abs(side(1, y.get("wxBox")) - side(1, y.get("calBox"))) <= 2,
      "weather %s against the month's %s" % (y.get("wxBox"), y.get("calBox")))
expect("midday: the same corner as the month", y.get("radius"), y.get("calRadius"))
expect("midday: and the clock is not squeezed off the screen", y.get("spill"), "no")
weather_colours("midday", y)

said, reports = run_probe("probe=13:00:00&mode=clear&wx=rain",
                          [HAVE_WX, "say|" + WEATHER, "shot|rainy-weather"], "rain")
z = reports[0] if reports else {}
expect("in the rain: code 63 is rain, and it rains in strokes", z.get("glyph"), "wx-rain")
expect("in the rain: the word is the word", z.get("cond"), "Rain")
expect("in the rain: 27°", z.get("temp"), "27°")
expect("in the rain: the cloud keeps the shop's slate", z.get("glyphColor"),
       token(palette("light"), "--cool"))
check("the weather never borrows somebody else's blue or Apple's own system colours",
      all(v not in ("rgb(0, 122, 255)", "rgb(10, 132, 255)", "rgb(255, 59, 48)")
          for v in (x.get("glyphColor"), y.get("glyphColor"), z.get("glyphColor"))),
      " / ".join(str(v.get("glyphColor")) for v in (x, y, z)))

# 10c. The two ways nothing arrives. A machine with no way out to the forecast says so in plain words
#      and prints no figure; a machine that has an old reading in its pocket keeps it and says when it
#      was read. Neither invents a number, and neither leaves the counter staring at a spinner.
said, reports = run_probe("probe=13:00:00&mode=clear&wx=fail",
                          [HAVE_OFFLINE, "say|" + WEATHER], "no forecast")
off = reports[0] if reports else {}
expect("with nothing answering: no figure is drawn at all", off.get("temp"), "")
expect("with nothing answering: the month next door is untouched", off.get("place"), "Accra")
check("with nothing answering: the tile says which half failed",
      off.get("none") == "No weather — this machine cannot reach the forecast.", off.get("none"))
expect("with nothing answering: and it is announced as empty, not as a guess",
       off.get("label"), "Accra, no reading yet.")
check("with nothing answering: no strip of made-up hours", off.get("cells") == 0, off.get("cells"))

# 10c-i. The other road. A counter whose own machine cannot reach the service is not cut off from the
#        forecast while the shop's Mac can reach it: the page asks the shop, and the shop answers with
#        the service's own reply untouched. This is the case that used to print "No weather" on a
#        phone that was perfectly able to print a job.
said, reports = run_probe("probe=13:00:00&mode=clear&wx=shoponly",
                          [HAVE_WX, "say|" + WEATHER], "only the shop can reach the sky")
shop = reports[0] if reports else {}
expect("a counter that cannot reach the service gets its figure from the shop", shop.get("temp"), "27°")
expect("and the tile does not also admit there was no weather", shop.get("none"), "")
check("and it asked its own service first, then the shop when that was refused",
      shop.get("tries") == "1" and int(shop.get("shopTries") or 0) >= 1,
      "service asked %s times, shop asked %s" % (shop.get("tries"), shop.get("shopTries")))

said, reports = run_probe("probe=13:00:00&mode=clear&wx=old",
                          [HAVE_WX, "say|" + WEATHER], "three days old")
stale = reports[0] if reports else {}
expect("a stale reading and no network: the last real figure is kept", stale.get("temp"), "27°")
check("and the tile says when it was read rather than pretending it is now",
      str(stale.get("age", "")).startswith("read at "), stale.get("age"))
expect("and it does not also claim there was no weather", stale.get("none"), "")

# 10c-ii. A machine that was briefly out of range is the case that used to strand the tile: the page
#         stamps every attempt, so if one ask is refused and the page then waits its full twenty
#         minutes, a counter left open all day says "No weather" long after the network came back. The
#         clock here is moved a minute and six seconds forward with nothing else touched, and the tile
#         has to come back on its own.
said, reports = run_probe("probe=13:00:00&mode=clear&wx=offthenon",
                          [HAVE_OFFLINE, "say|" + WEATHER, "eval|window.__shift(61000)",
                           HAVE_WX, "say|" + WEATHER], "refused once, asked again")
gone = reports[0] if reports else {}
back = reports[1] if len(reports) > 1 else {}
expect("a refused ask is admitted straight away, not left spinning", gone.get("none"),
       "No weather — this machine cannot reach the forecast.")
expect("while it has nothing, no figure is drawn", gone.get("temp"), "")
expect("and it did ask, once", gone.get("tries"), "1")
expect("a minute later, by itself, the figure is there", back.get("temp"), "27°")
expect("and it came back because the page really asked a second time", back.get("tries"), "2")
check("and it came back whole: the six hours too", back.get("cells") == 6, back.get("cells"))
expect("and the tile no longer claims there is no weather", back.get("none"), "")
check("a live reading is still refreshed on twenty minutes, not every minute",
      "held ? WX_OLD : WX_RETRY" in app and "WX_RETRY = 60 * 1000" in app,
      re.search(r"const WX_HOURS[^;]+;", app).group(0)[:78]
      if re.search(r"const WX_HOURS[^;]+;", app) else "")

# 10c-iii. The other half of the same honesty: while an ask is still in the air, the tile is not
#          allowed to claim the machine cannot reach the service — at that second it is reaching it.
#          ?wx=slow holds the answer for three seconds, so the page is caught mid-request.
said, reports = run_probe("probe=13:00:00&mode=clear&wx=slow",
                          ["poll|window.__liveTries>0", "say|" + WEATHER,
                           HAVE_WX, "say|" + WEATHER], "an answer still on its way")
air = reports[0] if reports else {}
landed = reports[1] if len(reports) > 1 else {}
expect("the ask goes out on its own", air.get("tries"), "1")
expect("with the answer not back yet, the tile says what it is doing", air.get("none"),
       "Getting the weather…")
expect("and it draws no figure while it is in the air", air.get("temp"), "")
check("and it never cries offline over a request still in flight",
      "No weather" not in (air.get("none") or ""), air.get("none"))
expect("and when the answer lands the figure is drawn", landed.get("temp"), "27°")

SETTLED = ("poll|(function(){var e=document.querySelector('.wx-temp')"
           "||document.querySelector('.wx-none');"
           "return !!e&&e.textContent.indexOf('Getting')===-1;})()")
said, reports = run_probe("probe=13:00:00&mode=clear&wx=live",
                          [SETTLED, "say|" + WEATHER,
                           "say|localStorage.getItem('crispprint-weather')?'stored':'nothing stored'",
                           "shot|live-weather"], "the real service")
live = reports[0] if reports else {}
realline = (live.get("temp") or "") + " " + (live.get("cond") or "") + (live.get("none") or "")
got_a_figure = bool(re.match(r"^\d+°$", live.get("temp") or ""))
check("left to reach the real service it settles on an answer instead of a spinner",
      got_a_figure or "No weather" in (live.get("none") or ""),
      "%s · %s" % (realline[:44], live.get("live")))
check("and whatever it settled on, the hour strip and the figure agree with each other",
      not got_a_figure or (live.get("cells") == 6 and len(str(live.get("temps")).split(",")) == 6),
      "%s cells with %s" % (live.get("cells"), live.get("temps")))
# The one gate here that asks the real sky: a reading that genuinely arrived is kept on the machine,
# so the shop's screen next morning — before the network is up — still shows the last true figure
# rather than an empty tile. Skipped when there was no reading to keep, which is the honest offline
# case the run above already accepted.
if got_a_figure:
    check("and a reading that really arrived is kept for the next time this machine cannot reach it",
          (said[1] if len(said) > 1 else "") == "stored", said[1:2])
else:
    # Nothing came of the first ask, so this is the case the whole retry exists for — and it is checked
    # against the actual service, not the fixture: the page must send the ask a second time a minute
    # later instead of sitting on "No weather" for twenty. The poll has an 8s deadline; a page that
    # never asks again fails here rather than passing quietly.
    said, reports = run_probe("probe=13:00:00&mode=clear&wx=live",
                              [SETTLED, "eval|window.__shift(61000)",
                               "poll|window.__liveTries>1||window.__liveLog.indexOf('answered')===0",
                               "poll|window.__liveLog!=='asked'", "say|" + WEATHER],
                               "the real service, asked twice")
    twice = reports[0] if reports else {}
    check("a service that did not answer the first time is asked again a minute later, on its own",
          twice.get("tries") == "2" or bool(re.match(r"^\d+°$", twice.get("temp") or "")),
          "%s asks · %s" % (twice.get("tries"), twice.get("live")))

# 10d. On a phone the month takes the whole row, so the weather takes the next one — the same two
#      faces stacked, never one of them shrunk, and six hours still in view.
said, reports = run_probe("probe=20:05:00&mode=clear&wx=night",
                          [HAVE_WX, "say|" + WEATHER, "shot|weather-on-a-phone"],
                          "phone weather", (390, 844))
ph = reports[0] if reports else {}
expect("on a phone the weather drops under the month, not beside it", ph.get("below"), "yes")
expect("on a phone it is still exactly the month's width", ph.get("wxBox").split("x")[0],
       (ph.get("calBox") or "x").split("x")[0])
check("on a phone the six hours stay six",
      ph.get("cells") == 6 and len(str(ph.get("hourCols")).split()) == 6,
      "%s cells in %s" % (ph.get("cells"), ph.get("hourCols")))
expect("on a phone the figures are the same figures", ph.get("temp"), "27°")
weather_colours("on a phone", ph)
expect("on a phone nothing hangs off the edge of the screen", ph.get("spill"), "no")

# 11. Nothing complained, and the shop's own figures still draw under the new card.
midday = subprocess.run([PROBE_BIN, BASE, work, "open|/?probe=13:00&mode=clear#/dashboard",
                         HAVE_CLOCK, "poll|!!document.querySelector('#view .kpis .stat')", "state|"],
                        capture_output=True, text=True, timeout=120,
                        env=dict(os.environ, WKPROBE_SCRIPT=PROBE_JS))
state = midday.stdout or ""
tail = [ln for ln in state.splitlines() if ln.strip().startswith("view:")]
check("the shop's screens still draw over the new card",
      tail and "blocks" in tail[0] and "loading" not in tail[0].lower(), tail[0][:70] if tail else "")
check("no errors on the page", "errors:none" in state,
      state[state.find("errors:"):state.find("errors:") + 90] if "errors:" in state else tail)

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
