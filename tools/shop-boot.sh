#!/bin/bash
# The shop's boot keeper: one small supervisor that makes this Mac answer on the shop's Wi-Fi
# address as soon as the machine is on, and stays answering when the router hands out a new
# number at 3am. launchd starts it at login and again every five minutes if it is not running,
# so a Mac that woke late, joined the Wi-Fi late, or lost its address still serves the counter.
#
# Why this exists next to server.py rather than inside the LaunchAgent: a service started with
# --allow-unauthenticated-lan must bind one private IPv4, and the router decides that number.
# A plain launchd job handed a fixed number either dies when the Wi-Fi is not up yet at login,
# or — worse — keeps running bound to an address this Mac no longer holds, so it looks perfectly
# healthy to launchd while every device in the shop finds nothing.
#
# Usage (by the LaunchAgent, not by hand): tools/shop-boot.sh <server flags, without --host>
set -uo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$SRC")"
SERVER="$REPO/server.py"
# tools/shop-server.sh owns the answers to "which address is the shop's" and "which certificate do
# the devices trust". A check that wants to watch this keeper react to a moved router points
# CHRISPHICS_SHOP_HELPER at a script of its own, so the shop's real certificate is never the one
# that gets re-issued mid-test.
HELPER="${CHRISPHICS_SHOP_HELPER:-$SRC/shop-server.sh}"
PORT="${CHRISPHICS_SHOP_PORT:-8834}"
CURL="$(command -v curl || echo /usr/bin/curl)"
WATCH_SECS="${CHRISPHICS_SHOP_BOOT_WATCH:-15}"    # how often the address is re-read
WAIT_SECS="${CHRISPHICS_SHOP_BOOT_WAIT:-5}"       # how long to wait when there is nothing to bind
LSOF="$(command -v lsof || echo /usr/sbin/lsof)"
note() { printf '%s [%s] %s\n' "$(date '+%H:%M:%S')" "shop-boot" "$*"; }
warn() { printf '%s [%s] %s\n' "$(date '+%H:%M:%S')" "shop-boot" "$*" >&2; }

shop_ip() {   # the address this Mac holds on the shop network right now
  # The seam the checks use: they rewrite a file to play the router handing out a fresh number,
  # which no fixed environment value can do to a process that is already running.
  if [ -n "${CHRISPHICS_SHOP_BOOT_IP_FILE:-}" ]; then
    if [ -f "$CHRISPHICS_SHOP_BOOT_IP_FILE" ]; then
      head -n 1 "$CHRISPHICS_SHOP_BOOT_IP_FILE" | tr -d ' \t\n'
    fi
    return
  fi
  if [ -n "${CHRISPHICS_SHOP_BOOT_IP+set}" ]; then
    printf '%s' "$CHRISPHICS_SHOP_BOOT_IP"        # the checks ask for an address they choose
    return
  fi
  bash "$HELPER" wifi-ip 2>/dev/null || true
}

bindable() {   # a number that is both private and legal for --allow-unauthenticated-lan
  case "$1" in
    10.*|192.168.*) return 0 ;;
    172.1[6-9].*|172.2[0-9].*|172.3[01].*) return 0 ;;
    *) return 1 ;;
  esac
}

LSOF="$(command -v lsof || echo /usr/sbin/lsof)"

already_online() {   # something is serving the shop on this address — never start a second engine
  [ -n "$1" ] || return 1
  if "$CURL" -sk --max-time 2 "https://$1:$PORT/healthz" 2>/dev/null | grep -q '"ok"'; then
    return 0
  fi
  # A book with a sign-in word answers 401 to everything except /healthz, so a page that will not
  # open proves nothing. Ask the kernel who holds the port instead of dialling it: a number the
  # router has just handed out is usually one nothing answers on, and a connection to it only lets
  # go when macOS decides, tens of seconds later. bash holds a TERM trap until the command in front
  # of it finishes, so a keeper stuck mid-probe is a counter that stays dark and cannot be stopped.
  local held; held="$("$LSOF" -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null)"
  printf '%s\n' "$held" | grep -q "$1:$PORT" && return 0
  # Someone listening on every address would take the bind as a collision, not a second shop.
  printf '%s\n' "$held" | grep -Eq "[[:space:]](\*|\[::\]|0\.0\.0\.0):$PORT\b"
}

make_certs() {   # the leaf names the address devices type, so a moved address needs a fresh leaf
  local before="" after=""
  before="$(stat -f %m "$CERT_FILE" 2>/dev/null || echo none)"
  if ! bash "$HELPER" certs >/dev/null 2>&1; then
    warn "could not re-issue the shop certificate; starting with the one on disk."
    return
  fi
  after="$(stat -f %m "$CERT_FILE" 2>/dev/null || echo none)"
  if [ "$after" != "$before" ]; then
    # Worth one line in the log: this is the moment the shop survived an address the router changed
    # by itself, and a padlock complaint on a phone starts here when it does not work.
    note "a fresh certificate was issued, because the old one did not name $1."
  fi
}

child=""
forward() {   # launchd stopping the keeper must stop the server too, or it orphans on the port
  [ -n "$child" ] && kill -TERM "$child" 2>/dev/null
  exit 0
}
trap forward TERM INT

args=("$@")
open_lan=0
CERT_FILE=""
prev=""
for a in "${args[@]}"; do
  [ "$a" = "--allow-unauthenticated-lan" ] && open_lan=1
  [ "$prev" = "--tls-cert" ] && CERT_FILE="$a"
  [ "$prev" = "--port" ] && PORT="$a"       # the probe has to aim at the port this keeper will bind
  prev="$a"
done

if [ ! -f "$SERVER" ]; then
  warn "there is no engine at $SERVER — the shop cannot serve its book from this copy."
  exit 1
fi

while : ; do
  host="0.0.0.0"
  if [ "$open_lan" -eq 1 ]; then
    ip="$(shop_ip)"
    if ! bindable "$ip"; then
      note "no shop Wi-Fi address yet (got '${ip:-nothing}') — the counter stays offline until this Mac joins the network."
      sleep "$WAIT_SECS"
      continue
    fi
    if already_online "$ip"; then
      note "the shop is already answering on $ip:$PORT — the keeper has nothing to do."
      exit 0
    fi
    make_certs "$ip"
    host="$ip"
  fi

  note "serving the book on https://$host:$PORT/"
  /usr/bin/python3 "$SERVER" --host "$host" "${args[@]}" &
  child=$!

  while kill -0 "$child" 2>/dev/null; do
    sleep "$WATCH_SECS"
    if [ "$open_lan" -eq 1 ]; then
      now="$(shop_ip)"
      if [ "$now" != "$host" ]; then
        warn "the Wi-Fi address moved: $host is gone, this Mac is now $now. Letting go of the old one."
        kill -TERM "$child" 2>/dev/null
        break
      fi
    fi
  done
  wait "$child"
  code=$?
  child=""
  if [ "$code" -eq 0 ]; then
    note "the engine stopped by itself."
    exit 0
  fi
  note "the engine exited ($code); starting another in ${WAIT_SECS}s."
  sleep "$WAIT_SECS"
done
