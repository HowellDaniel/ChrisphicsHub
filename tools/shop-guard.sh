#!/bin/bash
# Two jobs: make sure the shop's server is registered with launchd, and make sure something is
# actually standing on its door.
#
# Twice now this Mac has come back from a reboot with the shop's agent file sitting in
# ~/Library/LaunchAgents, launchd having never loaded it, and the counter dark for hours with
# nobody to say so (2026-10-14, four hours; 2026-10-19, from the night before). A file in that
# folder is not a promise — only a job in the domain is. That was the first hole.
#
# The second is quieter: a job launchd has *registered* is not a counter that answers. The keeper is
# a bash script, so its PID proves only that the supervisor woke up; the engine behind it can exit on
# a certificate the shop no longer trusts, or hold a bind the router took back, and launchd reads all
# of that as a running service. A registered-but-dark shop is exactly the failure the devices cannot
# report, because the page that would tell them lives behind the door that stopped answering. So the
# watchman asks the kernel who holds the port, and if nothing does for two turns in a row while this
# Mac genuinely has a Wi-Fi number to serve, it starts the service again.
#
# Every pass writes one line to shop-guard.heartbeat. Without it an empty log means either "the shop
# was fine all day" or "the watchman never ran", and those two want different answers.
#
# Nothing here opens a connection. A connect to a number the router just stopped handing out does not
# time out when told (see the keeper's note on `nc -z`), and a watchman stuck mid-probe is a watchman
# that cannot be stopped.
#
# Usage: tools/shop-guard.sh          (silent unless it has to act)
#   GUARD_LABEL / GUARD_PLIST aim it at another job, GUARD_PORT at another door, GUARD_SUPPORT puts
#   its notes somewhere else, GUARD_HELPER answers `wifi-ip`, and GUARD_WAIT_N / GUARD_COOLDOWN_S
#   shorten the patience. That is how a check proves it for real against a throwaway job.
set -uo pipefail

DOMAIN="gui/$(id -u)"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="${GUARD_LABEL:-com.chrisphics.shop-server}"
PLIST="${GUARD_PLIST:-$HOME/Library/LaunchAgents/$LABEL.plist}"
SUPPORT="${GUARD_SUPPORT:-$HOME/Library/Application Support/Chrisphics Hub}"
HELPER="${GUARD_HELPER:-$SRC/shop-server.sh}"
LOG="$SUPPORT/shop-guard.log"
BEAT="$SUPPORT/shop-guard.heartbeat"
STATE="$SUPPORT/shop-guard.state"
LSOF="$(command -v lsof || echo /usr/sbin/lsof)"

# How long a counter has to be dark before the watchman touches it, and how long it then leaves it
# alone. Two turns at the launchd interval (300 s) is ten minutes: longer than any rebind the keeper
# does by itself, shorter than anyone waits before phoning.
NEED_TURNS="${GUARD_WAIT_N:-2}"
COOLDOWN="${GUARD_COOLDOWN_S:-1800}"

say() {
  printf '%s [shop-guard] %s\n' "$(date +%H:%M:%S)" "$*"
  printf '%s %s [shop-guard] %s\n' "$(date +%Y-%m-%d)" "$(date +%H:%M:%S)" "$*" >>"$LOG" 2>/dev/null || true
}

beat() {   # the watchman signing its own timesheet: one line, overwritten every pass
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >"$BEAT" 2>/dev/null || true
}

# One line: "<turns read dark> <epoch of the last restart it forced>".
read_streak() { local s; s="$(head -n1 "$STATE" 2>/dev/null)"; printf '%s\n' "${s:-0 0}"; }
write_streak() { printf '%s %s\n' "$1" "$2" >"$STATE" 2>/dev/null || true; }

# The door this keeper was told to stand on. Read from the job's own arguments, so a shop moved to
# another port is followed by the watchman rather than watched against a door nobody uses. Flattened
# first: a plist is free to put its whole arguments array on one line or on twenty.
shop_port() {
  local v=""
  [ -f "$PLIST" ] && v="$(tr '\n' ' ' <"$PLIST" | sed -n \
    's|.*<string>--port</string>[[:space:]]*<string>\([0-9]\{1,\}\)</string>.*|\1|p')"
  case "$v" in (*[!0-9]*|'') printf '8834\n';; (*) printf '%s\n' "$v";; esac
}

holds_door() {   # any listener at all is a counter that answers; the keeper's collision rules are its own
  "$LSOF" -nP -iTCP:"$1" -sTCP:LISTEN 2>/dev/null | grep -q .
}

PORT="${GUARD_PORT:-}"
[ -n "$PORT" ] || PORT="$(shop_port)"
set -- $(read_streak)
streak="${1:-0}"
last_rescue="${2:-0}"

# ------------------------------------------------------------------ hole one: no job in the domain
if ! launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  if [ ! -f "$PLIST" ]; then
    beat "no job, and no file that would give one"
    say "launchd has no $LABEL and the file that would give it is gone ($PLIST) — run tools/shop-server.sh install"
    exit 1
  fi
  if launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null; then
    beat "registered the job again"
    say "launchd had no $LABEL this session, so the shop server is registered again"
    exit 0
  fi
  beat "could not register the job"
  say "the shop server was missing and could not be registered — run tools/shop-server.sh status"
  exit 1
fi

# ------------------------------------------------------------------ hole two: a job that answers to nobody
if holds_door "$PORT"; then
  [ "$streak" != "0" ] && write_streak 0 "$last_rescue"
  beat "job registered, something holds $PORT"
  exit 0
fi

ip="$(bash "$HELPER" wifi-ip 2>/dev/null || true)"
if [ -z "$ip" ]; then
  # The keeper is right to sit this one out: it has no private number to bind, and restarting it would
  # not invent one. Counting this as dark would have the watchman reboot the shop for the whole time
  # this Mac is off the Wi-Fi.
  beat "job registered, nothing on $PORT, no Wi-Fi number to serve"
  exit 0
fi

streak=$((streak + 1))
write_streak "$streak" "$last_rescue"
now="$(date +%s)"

if [ "$streak" -lt "$NEED_TURNS" ]; then
  beat "nothing on $PORT, dark turn $streak of $NEED_TURNS"
  exit 0
fi

if [ "$last_rescue" != "0" ] && [ $((now - last_rescue)) -lt "$COOLDOWN" ]; then
  beat "counter dark, restart held back ($(( (now - last_rescue) / 60 )) min of the $((COOLDOWN / 60)) min wait)"
  exit 0
fi

if launchctl kickstart -k "$DOMAIN/$LABEL" 2>/dev/null; then
  write_streak 0 "$now"
  beat "started the shop service again, $streak dark turns on $ip:$PORT"
  say "the counter answered nobody for $streak turns on $ip:$PORT, so $LABEL was started again"
  exit 0
fi

beat "counter dark and the restart failed"
say "nothing holds $PORT and $LABEL could not be restarted — run tools/shop-server.sh status"
exit 1
