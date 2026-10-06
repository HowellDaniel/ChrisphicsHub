#!/bin/bash
# One night, one copy of the book. Checking the copy, keeping only recent ones and recording
# the run are all done by server.py; this only starts it at the right moment, stops two nights
# from colliding, and leaves one line in the log so the shop can see it happened.
# Usage: tools/nightly-backup.sh [--quiet]
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$SRC")"
BOOK_DIR="$HOME/Library/Application Support/Chrisphics Hub"
LOG="$BOOK_DIR/backup.log"
LOCK="/tmp/com.chrisphics.backup.lock"
PY=/usr/bin/python3

quiet=0
if [ $# -gt 0 ]; then
  if [ "$1" = "--quiet" ]; then
    quiet=1
  else
    echo "Usage: tools/nightly-backup.sh [--quiet]" >&2
    exit 2
  fi
fi

if [ ! -x "$PY" ]; then
  PY="$(command -v python3 || true)"
fi
if [ -z "$PY" ]; then
  echo "This Mac has no Python to run the backup with. Open Chrisphics Hub once, then try again." >&2
  exit 1
fi

if ! mkdir "$LOCK" 2>/dev/null; then
  # mkdir fails only when the folder is already there, which means another copy is running.
  line="$(date '+%Y-%m-%d %H:%M:%S')  skipped — another backup was still running"
  mkdir -p "$BOOK_DIR" 2>/dev/null || true
  (printf '%s\n' "$line" >> "$LOG") 2>/dev/null || true
  if [ "$quiet" -eq 0 ]; then
    echo "$line"
    echo "If this keeps happening and no backup is actually running, delete $LOCK and try again."
  fi
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT INT TERM

mkdir -p "$BOOK_DIR" 2>/dev/null || true

out=""
rc=0
out="$(cd "$REPO" && "$PY" server.py --backup 2>&1)" || rc=$?

stamp="$(date '+%Y-%m-%d %H:%M:%S')"
if [ "$rc" -eq 0 ]; then
  file="$(printf '%s\n' "$out" | sed -n 's/^Backup written to //p' | head -1)"
  if [ -z "$file" ]; then
    file="server.py did not name the file it wrote"
  fi
  line="$stamp  ok  $file"
else
  # Only the first line of the failure, kept short: nothing that was typed is ever written here.
  brief="$(printf '%s\n' "$out" | grep -v '^[[:space:]]*$' | tail -1 | cut -c1-140)"
  line="$stamp  failed (exit $rc)  $brief"
fi

if ! (printf '%s\n' "$line" >> "$LOG") 2>/dev/null; then
  # A backup that worked must not look like it failed just because the log could not be written.
  line="$line  (this line could not be written to the log)"
fi

if [ "$quiet" -eq 0 ]; then
  printf '%s\n' "$line"
  if [ "$rc" -ne 0 ]; then
    printf '%s\n' "$out" >&2
    echo "The book is still safe — this was only the nightly copy. Run: tools/nightly-backup.sh"
  fi
fi

exit "$rc"
