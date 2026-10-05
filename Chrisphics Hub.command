#!/bin/zsh
# Double-click this in Finder to open Chrisphics Hub in your browser.
# Prefer the app? Just double-click "Chrisphics Hub.app" — same records either way.
cd "$(dirname "$0")" || exit 1

PORT=${CHRISPHICS_PORT:-8712}
URL="http://127.0.0.1:${PORT}/"
if curl -s -o /dev/null --max-time 1 "${URL}"; then
  echo "Chrisphics Hub is already running at ${URL}"
  open "${URL}"
  exit 0
fi
if pgrep -f "Chrisphics Hub.app/Contents/MacOS" > /dev/null; then
  echo "The Chrisphics Hub app is open and has your book. Quit it first, or just use the app." >&2
  exit 1
fi
echo "Starting Chrisphics Hub on ${URL}"
echo "Records: $HOME/Library/Application Support/Chrisphics Hub/chrisphics.db"
echo "Leave this Terminal window open while you work. Press Ctrl+C (or quit the window) to stop."
exec ./run.sh --port "${PORT}"
