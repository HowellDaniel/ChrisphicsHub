#!/bin/bash
# Make this Mac the shop's server: a certificate the shop's own devices already trust, a
# service that starts when the Mac starts and stays up on its own, and a fresh copy of the book
# every night. The book itself is never touched here — same file, same place as always.
# Usage: tools/shop-server.sh check | certs | ca | install | status | remove [--take-over]
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$SRC")"
PORT="${CHRISPHICS_SHOP_PORT:-8834}"   # 8834 is the shop's address; the variable is for dry runs
LABEL=com.chrisphics.shop-server
BACKUP_LABEL=com.chrisphics.nightly-backup
AGENTS="$HOME/Library/LaunchAgents"
PLIST="$AGENTS/$LABEL.plist"
BACKUP_PLIST="$AGENTS/$BACKUP_LABEL.plist"
BOOK_DIR="$HOME/Library/Application Support/Chrisphics Hub"
BOOK="${CHRISPHICS_DB:-$BOOK_DIR/chrisphics.db}"   # the same book the app opens, unless a dry run says otherwise
TLS_DIR="$HOME/Library/Application Support/CRISPprint TLS"
DL_DIR="$TLS_DIR/device-download"
CERT="$TLS_DIR/shop-cert.pem"
KEY="$TLS_DIR/shop-key.pem"
LOG="$BOOK_DIR/shop-server.log"
ERRLOG="$BOOK_DIR/shop-server.error.log"
DOMAIN="gui/$(id -u)"
take_over=0

usage() {
  cat <<HELP
tools/shop-server.sh — make this Mac the shop's always-on server

  check     Tell me how things stand right now. Reads only, changes nothing.
  certs     Get (or refresh) the certificate the shop's devices look for.
  ca        Put the public certificate-authority file where a phone can pick it up.
  install   Run the book as a service that comes back by itself, and copy it nightly.
  status    Is it running, what did it last say, when is the next copy.
  remove    Stop the services and take back the two files this tool created. Logs stay.

Add --take-over when a server is already running by hand and you want this tool to stop
it and take the address over. It always asks you to type yes before changing anything.

Start with:  tools/shop-server.sh check
HELP
}

note() { printf '%s\n' "$*"; }
warn() { printf '%s\n' "$*" >&2; }
fail_with() { warn "$*"; exit 1; }   # every bad road ends with one sentence about what to do next

wifi_ip() {
  local ip="" iface="" n=""
  iface="$(route -n get default 2>/dev/null | awk '/interface:/{print $2; exit}' || true)"
  if [ -n "$iface" ]; then
    ip="$(ipconfig getifaddr "$iface" 2>/dev/null || true)"
  fi
  for n in 0 1 2 3 4 5 6 7 8; do
    if [ -z "$ip" ]; then
      ip="$(ipconfig getifaddr "en$n" 2>/dev/null || true)"
    fi
  done
  printf '%s' "$ip"
}

mac_name() {
  local name=""
  name="$(scutil --get LocalHostName 2>/dev/null || true)"
  if [ -z "$name" ]; then
    name="$(hostname 2>/dev/null | cut -d. -f1 || true)"
  fi
  printf '%s' "$name"
}

every_ip() {   # every address this Mac answers on right now — Wi-Fi, hotspot, Thunderbolt bridge
  ifconfig -a 2>/dev/null | awk '/[[:space:]]inet /{print $2}' | sort -u || true
}

shop_names() {   # the one list every device and this Mac agree on
  local line=""
  printf '%s\n' "$1" "$2.local" localhost ::1
  while IFS= read -r line; do
    if [ -n "$line" ] && [ "$line" != "$1" ]; then
      printf '%s\n' "$line"
    fi
  done <<IPS
$(every_ip)
IPS
}

mkcert_bin() {
  local found="" c=""
  found="$(command -v mkcert || true)"
  for c in /opt/homebrew/bin/mkcert /usr/local/bin/mkcert "$HOME/bin/mkcert"; do
    if [ -z "$found" ] && [ -x "$c" ]; then
      found="$c"
    fi
  done
  printf '%s' "$found"
}

ca_root() {
  local mk="" root=""
  mk="$(mkcert_bin)"
  if [ -n "$mk" ]; then
    root="$("$mk" -CAROOT 2>/dev/null || true)"
  fi
  if [ -z "$root" ]; then
    root="$HOME/Library/Application Support/mkcert"
  fi
  printf '%s' "$root"
}

cert_san_lines() {   # one name per line, however this Mac's openssl chooses to spell them
  local raw=""
  raw="$(openssl x509 -noout -ext subjectAltName -in "$1" 2>/dev/null || true)"
  if ! printf '%s' "$raw" | grep -q 'DNS\|IP'; then
    raw="$(openssl x509 -noout -text -in "$1" 2>/dev/null | awk '/Subject Alternative Name/{getline; print}' || true)"
  fi
  printf '%s\n' "$raw" \
    | tr ',' '\n' \
    | sed -e 's/^[[:space:]]*//' -e 's/^DNS://' -e 's/^IP Address://' -e 's/^IP://' \
        -e 's/^0:0:0:0:0:0:0:1$/::1/' \
    | grep -v '^[[:space:]]*$' || true
}

missing_names() {   # $1 = certificate, $2 = Wi-Fi address, $3 = Mac's name — what it does not answer to
  local have="" line="" out=""
  have="$(cert_san_lines "$1")"
  while IFS= read -r line; do
    if [ -n "$line" ] && ! printf '%s\n' "$have" | grep -Fxq -- "$line"; then
      out="$out $line"
    fi
  done <<NAMES
$(shop_names "$2" "$3")
NAMES
  printf '%s' "$out"
}

# The service answers on every address this Mac has, not on the Wi-Fi number of the day: a router
# that hands out a fresh address at midnight must not take the shop offline until someone notices.
# The certificate is the part that names one address, and `certs` is what refreshes it.
SERVE_HOST="0.0.0.0"

book_has_password() {   # 0 yes, 1 no, 2 cannot tell
  local value=""
  if [ ! -f "$BOOK" ] || ! command -v sqlite3 >/dev/null 2>&1; then
    return 2
  fi
  value="$(sqlite3 -readonly "$BOOK" "SELECT length(value) FROM app_state WHERE key='shop_password';" 2>/dev/null || true)"
  [ -n "$value" ] && [ "$value" != "0" ]
}

cert_expiry() {
  local raw="" epoch="" days=""
  raw="$(openssl x509 -noout -enddate -in "$1" 2>/dev/null | cut -d= -f2 || true)"
  if [ -z "$raw" ]; then
    printf 'unknown'
    return
  fi
  epoch="$(date -j -f '%b %e %T %Y %Z' "$raw" +%s 2>/dev/null || true)"
  if [ -n "$epoch" ]; then
    days=$(( (epoch - $(date +%s)) / 86400 ))
    printf '%s, about %s days from now' "$raw" "$days"
  else
    printf '%s' "$raw"
  fi
}

listener_pid() {
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1 || true
}

agent_pid() {   # $1 = label
  local out=""
  out="$(launchctl print "$DOMAIN/$1" 2>/dev/null || true)"
  printf '%s\n' "$out" | awk '/^[[:space:]]*pid = /{print $3; exit}'
}

is_loaded() {   # $1 = label
  if launchctl print "$DOMAIN/$1" >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

app_holds_book() {
  pgrep -f "Chrisphics Hub.app/Contents/MacOS" 2>/dev/null | head -1 || true
}

PROBE_CODE=""
PROBE_INSECURE=0
probe_shop_url() {   # $1 = address; asks this Mac to reach its own shop address
  local url="https://$1:$PORT/healthz"
  PROBE_INSECURE=0
  PROBE_CODE="$(curl -s --max-time 6 -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || true)"
  if [ "$PROBE_CODE" = "000" ] || [ -z "$PROBE_CODE" ]; then
    PROBE_INSECURE=1
    PROBE_CODE="$(curl -sk --max-time 6 -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || true)"
  fi
}

typed_yes() {
  local answer=""
  printf 'Type yes and press Return to carry on, or anything else to stop: '
  read -r answer || answer=""
  if [ "$answer" != "yes" ]; then
    note "Stopped. Nothing was changed and the shop carries on exactly as it was."
    exit 1
  fi
}

# ------------------------------------------------------------------ check
cmd_check() {
  local ip="" name="" pid="" app="" root="" mk="" have="" missing=""
  note "How this Mac looks to the shop right now. Nothing here is changed."
  echo

  ip="$(wifi_ip)"
  if [ -n "$ip" ]; then
    note "Shop address:  https://$ip:$PORT/"
  else
    note "Shop address:  this Mac has no Wi-Fi address — join it to the shop network first."
  fi

  name="$(mac_name)"
  if [ -n "$name" ]; then
    note "This Mac's own name:  $name.local"
  else
    note "This Mac's own name:  not set"
  fi

  echo
  if [ -f "$CERT" ]; then
    note "The certificate is here:  $CERT"
    have="$(cert_san_lines "$CERT")"
    note "  it answers to:  $(printf '%s' "$have" | tr '\n' ' ')"
    note "  it is good until:  $(cert_expiry "$CERT")"
    if [ -f "$KEY" ]; then
      note "  its private key is here too and never leaves this Mac."
    else
      note "  its private key is missing — run: tools/shop-server.sh certs"
    fi
    if [ -n "$ip" ]; then
      missing="$(missing_names "$CERT" "$ip" "$name")"
      if [ -n "$missing" ]; then
        note "  it does not cover yet:$missing — run: tools/shop-server.sh certs"
      else
        note "  it covers this Mac's current address and name."
      fi
    fi
  else
    note "There is no certificate yet at $CERT — run: tools/shop-server.sh certs"
  fi

  echo
  root="$(ca_root)"
  mk="$(mkcert_bin)"
  if [ -f "$root/rootCA.pem" ]; then
    note "The authority your devices are asked to trust:  $root/rootCA.pem"
    if [ -f "$DL_DIR/shop-root-ca.cer" ]; then
      note "  a copy is already waiting for phones:  $DL_DIR/shop-root-ca.cer"
    else
      note "  it is not yet somewhere a phone can pick it up — run: tools/shop-server.sh ca"
    fi
  elif [ -n "$mk" ]; then
    note "This Mac has no certificate authority yet — run \"$mk -install\", then tools/shop-server.sh certs."
  else
    note "This Mac has no certificate authority and no mkcert — install mkcert, then run tools/shop-server.sh certs."
  fi

  echo
  pid="$(listener_pid)"
  if [ -n "$pid" ]; then
    local who="a server started by hand in a Terminal window" ap=""
    ap="$(agent_pid "$LABEL")"
    if [ -n "$ap" ] && [ "$ap" = "$pid" ]; then
      who="the always-on service this tool installs"
    fi
    note "Something is answering on port $PORT: process $pid, which is $who."
    note "  it was started as:  $(ps -o command= -p "$pid" 2>/dev/null | cut -c1-130 || true)"
  else
    note "Nothing is answering on port $PORT, so no device can reach the book yet."
  fi

  if is_loaded "$LABEL"; then
    note "The always-on service is installed and loaded."
  else
    note "The always-on service is not installed. When the shop is ready: tools/shop-server.sh install"
  fi

  app="$(app_holds_book)"
  if [ -n "$app" ]; then
    note "Heads up: the Chrisphics Hub app is open (process $app) and holds the book. Only one thing"
    note "  should have the book at a time — quit the app before running a second server on it."
  fi

  if [ -n "$ip" ] && [ -n "$pid" ]; then
    probe_shop_url "$ip"
    if [ "$PROBE_CODE" = "200" ]; then
      if [ "$PROBE_INSECURE" -eq 1 ]; then
        note "This Mac reaches its own shop address (answer 200). curl had to be told to look past the"
        note "  certificate, because this Mac's system trust list does not carry the shop authority — the"
        note "  devices need that file from tools/shop-server.sh ca."
      else
        note "This Mac reaches its own shop address and vouches for the certificate (answer 200)."
      fi
    else
      note "This Mac could not get an answer from https://$ip:$PORT/healthz — open the shop address in a"
      note "  browser on this Mac first, then look at tools/shop-server.sh status."
    fi
  fi
}

# ------------------------------------------------------------------ certs
cmd_certs() {
  local ip="" name="" mk="" root="" line=""
  declare -a wanted=()
  ip="$(wifi_ip)"
  if [ -z "$ip" ]; then
    fail_with "This Mac has no Wi-Fi address, so there is nothing honest to put on a certificate — join it to the shop network and run this again."
  fi
  name="$(mac_name)"
  while IFS= read -r line; do
    if [ -n "$line" ]; then
      wanted+=("$line")
    fi
  done <<NAMES
$(shop_names "$ip" "$name")
NAMES

  note "The certificate is what turns a phone's padlock honest, for these names:"
  printf '  %s\n' "${wanted[@]}"

  if [ -f "$CERT" ]; then
    if [ -z "$(missing_names "$CERT" "$ip" "$name")" ]; then
      note "The certificate already covers every one of those and is good until $(cert_expiry "$CERT")."
      note "Nothing to do, so nothing was changed."
      return
    fi
    note "It no longer covers what this Mac needs — the Wi-Fi address or the name moved — so I am issuing"
    note "  a fresh one in its place at $CERT. Your records are not involved at this step."
  else
    note "There is no certificate yet, so I am making one at $CERT."
  fi

  mk="$(mkcert_bin)"
  if [ -z "$mk" ]; then
    warn "mkcert was not found on this Mac, so nothing here can be signed."
    warn "Install it with \"brew install mkcert\", or put the mkcert program in $HOME/bin, then run this again."
    exit 1
  fi
  root="$(ca_root)"
  if [ ! -f "$root/rootCA.pem" ]; then
    fail_with "There is no certificate authority in $root yet — run \"$mk -install\" once, then run this again."
  fi

  mkdir -p "$TLS_DIR"
  if ! "$mk" -cert-file "$CERT" -key-file "$KEY" "${wanted[@]}"; then
    fail_with "mkcert could not issue the certificate — run tools/shop-server.sh check to see what is missing, then try again."
  fi
  chmod 600 "$KEY"
  chmod 644 "$CERT"

  note "Certificate ready: it answers to $(cert_san_lines "$CERT" | tr '\n' ' ') and is good until $(cert_expiry "$CERT")."
  note "A server that is already running keeps the certificate it started with, so run tools/shop-server.sh install"
  note "  (or press Ctrl+C in its Terminal window and start it again) before the devices see the new one."
}

# ------------------------------------------------------------------ ca
cmd_ca() {
  local root="" pem="" cer="" new="" print=""
  root="$(ca_root)"
  pem="$root/rootCA.pem"
  if [ ! -f "$pem" ]; then
    fail_with "There is no certificate authority at $pem yet — run \"mkcert -install\", then tools/shop-server.sh certs, then this."
  fi
  mkdir -p "$DL_DIR"
  cer="$DL_DIR/shop-root-ca.cer"
  new="$(mktemp)"
  if ! openssl x509 -inform PEM -in "$pem" -outform DER -out "$new"; then
    rm -f "$new"
    fail_with "This Mac could not turn the authority into the file phones want — run tools/shop-server.sh check, then try again."
  fi
  if [ -f "$cer" ] && cmp -s "$new" "$cer"; then
    rm -f "$new"
    note "The authority file phones need is already in place and unchanged: $cer"
  else
    mv "$new" "$cer"
    chmod 644 "$cer"   # it is the public half, and a phone has to be able to read it
    note "Wrote the public authority file for the devices: $cer"
  fi
  if cmp -s "$pem" "$DL_DIR/shop-root-ca.pem" 2>/dev/null; then
    note "The plain-text copy beside it is already up to date."
  else
    cp "$pem" "$DL_DIR/shop-root-ca.pem"
    note "A plain-text copy sits beside it as shop-root-ca.pem, for devices that want that shape."
  fi

  print="$(openssl x509 -inform PEM -in "$pem" -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2 || true)"
  echo
  note "Read this fingerprint out loud before any device trusts it:"
  note "  SHA-256 ${print:-unknown}"
  note "That is the public half only. The private half stays on this Mac and is never copied or shown here."
  note "Give shop-root-ca.cer to each device, trust it, and then https://$(wifi_ip):$PORT/ opens with a real padlock."
}

# ------------------------------------------------------------------ install
server_plist() {   # $1 = file to write
  cat > "$1" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>$REPO/server.py</string>
    <string>--host</string><string>$SERVE_HOST</string>
    <string>--port</string><string>$PORT</string>
    <string>--tls-cert</string><string>$CERT</string>
    <string>--tls-key</string><string>$KEY</string>
    <string>--no-browser</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>20</integer>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$ERRLOG</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
</dict>
</plist>
PLIST
}

backup_plist() {   # $1 = file to write
  cat > "$1" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$BACKUP_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$SRC/nightly-backup.sh</string>
    <string>--quiet</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>22</integer><key>Minute</key><integer>30</integer></dict>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>StandardOutPath</key><string>$BOOK_DIR/backup.log</string>
  <key>StandardErrorPath</key><string>$BOOK_DIR/backup.error.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
</dict>
</plist>
PLIST
}

load_agent() {   # $1 = plist, $2 = label
  if launchctl bootout "$DOMAIN/$2" 2>/dev/null; then
    note "  (the old one was still loaded, so it was set aside first)"
  fi
  if launchctl bootstrap "$DOMAIN" "$1" 2>/dev/null; then
    return 0
  fi
  if launchctl load -w "$1" 2>/dev/null; then
    note "  loaded the older way, which does the same job."
    return 0
  fi
  return 1
}

write_plist_quietly() {   # $1 = builder, $2 = destination, then whatever that builder needs
  local builder="$1" dest="$2" tmp=""
  shift 2
  tmp="$(mktemp)"
  "$builder" "$tmp" "$@"
  if [ -f "$dest" ] && cmp -s "$tmp" "$dest"; then
    rm -f "$tmp"
    return 1
  fi
  mv "$tmp" "$dest"
  return 0
}

cmd_install() {
  local ip="" name="" pid="" app="" ap="" left="" missing=""
  if [ ! -f "$REPO/server.py" ]; then
    fail_with "I cannot find the book's engine at $REPO/server.py — run this script from inside the Chrisphics Hub folder."
  fi
  if [ ! -f "$CERT" ] || [ ! -f "$KEY" ]; then
    fail_with "The devices have nothing to trust yet — run tools/shop-server.sh certs first, then come back to install."
  fi
  ip="$(wifi_ip)"
  if [ -z "$ip" ]; then
    fail_with "This Mac has no Wi-Fi address, so it cannot serve the shop — join it to the shop network and run this again."
  fi
  name="$(mac_name)"

  # A server the whole Wi-Fi can reach refuses to start over a book with no password, so saying so
  # here saves the shop from a service that restarts itself every twenty seconds and never serves.
  have_pw=0
  book_has_password || have_pw=$?
  if [ "$have_pw" -eq 1 ]; then
    fail_with "The book has no shop password yet. Open Chrisphics Hub on this Mac, go to Shop & devices, choose the password, then run install again."
  elif [ "$have_pw" -eq 2 ]; then
    warn "I could not read the book to check its shop password. If the service starts and falls over again,"
    warn "the password is the first thing to look at: Shop & devices on this Mac."
  fi

  app="$(app_holds_book)"
  pid="$(listener_pid)"
  ap="$(agent_pid "$LABEL")"
  if [ -z "$app" ] && [ -n "$pid" ] && [ "$pid" = "$ap" ] && is_loaded "$LABEL"; then
    note "The always-on server is already running on https://$ip:$PORT/ as process $pid, and nothing changed."
    missing="$(missing_names "$CERT" "$ip" "$name")"
    if [ -n "$missing" ]; then
      note "It answers, but the certificate no longer covers this Mac: $missing"
      note "The Wi-Fi address moved. Run tools/shop-server.sh certs, then install again to hand the"
      note "  service the fresh certificate; devices will see a padlock warning until then."
    fi
    note "To start it fresh: tools/shop-server.sh remove, then install again."
    return
  fi

  if [ -n "$app" ] || [ -n "$pid" ]; then
    note "Something already has the book or the shop address:"
    if [ -n "$app" ]; then
      note "  the Chrisphics Hub app, process $app, has the book open on this Mac."
    fi
    if [ -n "$pid" ] && [ "$pid" != "$ap" ]; then
      note "  process $pid is answering on port $PORT: $(ps -o command= -p "$pid" 2>/dev/null | cut -c1-130 || true)"
    fi
    note "Two servers over one book can spoil the records, so only one may run."
    if [ "$take_over" -ne 1 ]; then
      warn "I will not replace it unless you say so. Press Ctrl+C in the Terminal window running the shop"
      warn "server, quit Chrisphics Hub if it is open, then run this again — or run it with --take-over and"
      warn "I will stop that server for you after asking once."
      exit 1
    fi
    echo
    note "With --take-over I am about to:"
    if [ -n "$pid" ] && [ "$pid" != "$ap" ]; then
      note "  ask process $pid, and only that process, to let go of port $PORT"
    fi
    if [ -n "$app" ]; then
      note "  leave the app alone — please quit Chrisphics Hub yourself, since it still holds the book"
    fi
    note "  write $PLIST and start the book as a service on https://$ip:$PORT/ that restarts itself"
    note "  write $BACKUP_PLIST and copy the book every night at 22:30"
    typed_yes
    if [ -n "$pid" ] && [ "$pid" != "$ap" ]; then
      kill -TERM "$pid"
      left=20
      while [ "$left" -gt 0 ] && [ -n "$(listener_pid)" ]; do
        sleep 1
        left=$((left - 1))
      done
      if [ -n "$(listener_pid)" ]; then
        fail_with "Process $pid would not let go of port $PORT — press Ctrl+C in its Terminal window, then run this again."
      fi
      note "  done: port $PORT is free."
    fi
  fi

  mkdir -p "$AGENTS" "$BOOK_DIR"
  if write_plist_quietly server_plist "$PLIST"; then
    note "Wrote the service file: $PLIST"
  else
    note "The service file is already exactly right, so I left it alone."
  fi
  if write_plist_quietly backup_plist "$BACKUP_PLIST"; then
    note "Scheduled the nightly copy of the book at 22:30."
  else
    note "The nightly copy was already scheduled."
  fi

  if ! load_agent "$PLIST" "$LABEL"; then
    fail_with "macOS would not take on the service — restart this Mac, then run tools/shop-server.sh install again."
  fi
  if ! load_agent "$BACKUP_PLIST" "$BACKUP_LABEL"; then
    warn "The nightly copy did not load this time; run install again once the server is answering."
  fi

  left=45
  while [ "$left" -gt 0 ] && [ -z "$(listener_pid)" ]; do
    sleep 1
    left=$((left - 1))
  done
  if [ -z "$(listener_pid)" ]; then
    warn "The service is installed, but the shop address did not answer within 45 seconds."
    warn "Read what it managed to say: tools/shop-server.sh status"
    exit 1
  fi

  probe_shop_url "$ip"
  if [ "$PROBE_CODE" = "200" ]; then
    if [ "$PROBE_INSECURE" -eq 1 ]; then
      note "The address answered. curl was asked to look past the certificate (-k) only because this Mac's"
      note "  own trust list does not carry the shop authority — the devices need that file from ca."
    fi
  else
    warn "Something answers on $PORT but did not serve the book's health check (answer: ${PROBE_CODE:-nothing})."
    warn "Run tools/shop-server.sh status and read the last lines of the log."
  fi

  echo
  note "The shop is live:  https://$ip:$PORT/"
  if [ -n "$name" ]; then
    note "Also, from this Mac:  https://$name.local:$PORT/"
  fi
  note "Open this address on a phone or laptop on the shop Wi-Fi."
  note "One password opens the book for the whole shop, and that password lives in the book itself, not in"
  note "  any file here. To put the address where a phone can scan it:"
  note "  swift tools/make-qr.swift \"https://$ip:$PORT/\" /tmp/shop-qr.png"
}

# ------------------------------------------------------------------ remove
cmd_remove() {
  local label="" file=""
  for label in "$LABEL" "$BACKUP_LABEL"; do
    if is_loaded "$label"; then
      if ! launchctl bootout "$DOMAIN/$label" 2>/dev/null; then
        launchctl unload "$AGENTS/$label.plist" 2>/dev/null || true
      fi
      note "Stopped $label."
    else
      note "$label was not loaded, which is fine."
    fi
  done
  for file in "$PLIST" "$BACKUP_PLIST"; do
    if [ -f "$file" ]; then
      rm -f "$file"
      note "Took back the file this tool made: $file"
    fi
  done
  note "Your logs and your book stay exactly where they are."
  if [ -n "$(listener_pid)" ]; then
    note "Something still answers on port $PORT — that is the server you started by hand, so press Ctrl+C"
    note "  in its Terminal window if you want the shop address to go quiet."
  else
    note "Nothing answers on port $PORT now, so the shop address is closed."
  fi
}

# ------------------------------------------------------------------ status
cmd_status() {
  local pid="" ip="" when="" missing=""
  ip="$(wifi_ip)"
  if is_loaded "$LABEL"; then
    pid="$(agent_pid "$LABEL")"
    if [ -n "$pid" ]; then
      note "The always-on server: running, as process $pid."
    else
      note "The always-on server: installed, and waiting for its turn to start."
    fi
  else
    note "The always-on server: not installed. When the shop is ready: tools/shop-server.sh install"
  fi

  if [ -n "$(listener_pid)" ] && [ -n "$ip" ]; then
    probe_shop_url "$ip"
    if [ "$PROBE_CODE" = "200" ]; then
      note "Shop address https://$ip:$PORT/ answered, and the book is behind it."
    else
      note "Shop address https://$ip:$PORT/ did not answer — if the Wi-Fi address moved, run"
      note "  tools/shop-server.sh certs and then tools/shop-server.sh install again."
    fi
  elif [ -n "$ip" ]; then
    note "Shop address https://$ip:$PORT/: nothing is answering on port $PORT."
  fi

  echo
  if [ -f "$LOG" ]; then
    note "The last few words the server wrote:"
    tail -5 "$LOG" | sed 's/^/  /'
  else
    note "The server has not written anything yet at $LOG — normal before its first run."
  fi
  if [ -s "$ERRLOG" ]; then
    note "It also left notes about problems in $ERRLOG — the last of them:"
    tail -3 "$ERRLOG" | sed 's/^/  /'
  fi

  echo
  if [ -f "$CERT" ]; then
    note "The certificate is good until $(cert_expiry "$CERT")."
    if [ -n "$ip" ]; then
      missing="$(missing_names "$CERT" "$ip" "$(mac_name)")"
      if [ -n "$missing" ]; then
        note "  but it does not cover this Mac today: $missing — the Wi-Fi address moved."
        note "  Run tools/shop-server.sh certs, then install again to hand the service the fresh one."
      fi
    fi
  else
    note "There is no certificate yet — run tools/shop-server.sh certs."
  fi

  echo
  if is_loaded "$BACKUP_LABEL"; then
    when="tonight"
    if [ "$(date +%H%M)" -gt 2230 ]; then
      when="tomorrow"
    fi
    note "The next copy of the book is $when at 22:30."
    if [ -f "$BOOK_DIR/backup.log" ]; then
      note "The last copy said: $(tail -1 "$BOOK_DIR/backup.log")"
    fi
  else
    note "No nightly copy is scheduled yet — tools/shop-server.sh install sets that up as well."
  fi
}

args=()
for a in "$@"; do
  if [ "$a" = "--take-over" ]; then
    take_over=1
  else
    args+=("$a")
  fi
done
cmd="${args[0]:-}"

case "$cmd" in
  check) cmd_check ;;
  certs) cmd_certs ;;
  ca) cmd_ca ;;
  install) cmd_install ;;
  remove) cmd_remove ;;
  status) cmd_status ;;
  "") usage ;;
  *) warn "I do not know \"$cmd\"."; usage; exit 2 ;;
esac
