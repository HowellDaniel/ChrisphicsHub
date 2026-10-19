#!/bin/bash
# One job: make sure the shop's own server is registered with launchd.
#
# Twice now this Mac has come back from a reboot with the shop's agent file sitting in
# ~/Library/LaunchAgents, launchd having never loaded it, and the counter dark for hours with
# nobody to say so (2026-10-14, four hours; 2026-10-19, from the night before). A file in that
# folder is not a promise — only a job in the domain is. This is the thing that notices.
#
# It changes nothing while the shop is registered, which is the usual case.
#
# Usage: tools/shop-guard.sh          (silent unless it has to act)
#   GUARD_LABEL / GUARD_PLIST aim it at another job — that is how a check tests it for real.
set -uo pipefail

DOMAIN="gui/$(id -u)"
LABEL="${GUARD_LABEL:-com.chrisphics.shop-server}"
PLIST="${GUARD_PLIST:-$HOME/Library/LaunchAgents/$LABEL.plist}"
LOG="$HOME/Library/Application Support/Chrisphics Hub/shop-guard.log"

say() {
  printf '%s [shop-guard] %s\n' "$(date +%H:%M:%S)" "$*"
  printf '%s [shop-guard] %s\n' "$(date +%H:%M:%S)" "$*" >>"$LOG" 2>/dev/null || true
}

if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  exit 0
fi

if [ ! -f "$PLIST" ]; then
  say "launchd has no $LABEL and the file that would give it is gone ($PLIST) — run tools/shop-server.sh install"
  exit 1
fi

if launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null; then
  say "launchd had no $LABEL this session, so the shop server is registered again"
  exit 0
fi

say "the shop server was missing and could not be registered — run tools/shop-server.sh status"
exit 1
