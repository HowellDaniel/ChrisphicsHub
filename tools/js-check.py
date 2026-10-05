#!/usr/bin/env python3
"""Syntax-check the no-build front end with the JS engine macOS already ships.

public/app.js is loaded straight into a <script> tag, so one stray parenthesis
leaves the whole counter blank with no build step to catch it. This parses the
file through JavaScriptCore (osascript -l JavaScript, no Node, no npm) and
reports the line the engine gave up on.

    python3 tools/js-check.py [public/app.js]

A clean parse only proves the file loads; behaviour still needs a real window.
"""
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TARGET = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "public" / "app.js"
if not TARGET.is_absolute():
    TARGET = ROOT / TARGET

PROBE = """ObjC.import('Foundation');
function run(argv) {
  const src = $.NSString.stringWithContentsOfFileEncodingError(argv[0], $.NSUTF8StringEncoding, null).js;
  if (src === null || src === undefined) return 'could not read ' + argv[0];
  try { new Function(src); return 'parsed clean'; }
  catch (e) { return e.name + ': ' + e.message + (e.line ? ' @line ' + e.line : ''); }
}
"""

probe_path = pathlib.Path("/tmp/chriphics_js_check.js")
probe_path.parent.mkdir(exist_ok=True)
probe_path.write_text(PROBE, encoding="utf-8")

out = subprocess.run(
    ["osascript", "-l", "JavaScript", str(probe_path), str(TARGET)],
    capture_output=True, text=True, timeout=60,
)
report = (out.stdout or out.stderr).strip()
print(report or "(no report)")
sys.exit(0 if report == "parsed clean" else 1)
