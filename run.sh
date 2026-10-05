#!/bin/zsh
# Start Chriphics Hub. Double-click "Chriphics Hub.command" in Finder, or run ./run.sh
cd "$(dirname "$0")" || exit 1
PY=/usr/bin/python3
[ -x "$PY" ] || PY=$(command -v python3)
if [ -z "$PY" ]; then
  echo "Python 3 was not found on this Mac." >&2
  exit 1
fi
exec "$PY" server.py "$@"
