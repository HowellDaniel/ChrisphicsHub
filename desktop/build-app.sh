#!/bin/bash
# Build the native macOS wrapper around the local records engine.
# The bundle keeps its original file name; only what the shop sees changed, and the
# data folder is untouched, so the live book is still found where it always was.
# Re-run this after changing server.py, schema.sql or anything in public/.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(dirname "$SRC")"
APP="$PROJECT/Chrisphics Hub.app"
BIN="$APP/Contents/MacOS/ChrisphicsHub"

PY="$(command -v python3 || echo /usr/bin/python3)"

echo "1/5 checking the records engine compiles"
"$PY" -m py_compile "$PROJECT/server.py"

echo "2/5 assembling $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/app"
cp "$PROJECT/server.py" "$PROJECT/schema.sql" "$APP/Contents/Resources/app/"
cp -R "$PROJECT/public" "$APP/Contents/Resources/app/public"

cat > "$APP/Contents/PkgInfo" <<<'APPL????'

echo "3/5 building the icon"
ICONSET="$(mktemp -d)/AppIcon.iconset"
swift "$SRC/make-icon.swift" "$ICONSET" >/dev/null
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Chrisphics Hub</string>
  <key>CFBundleDisplayName</key><string>Chrisphics Hub</string>
  <key>CFBundleIdentifier</key><string>com.chrisphics.hub</string>
  <key>CFBundleExecutable</key><string>ChrisphicsHub</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSHumanReadableCopyright</key><string>CRISPprint Ghana — printing records and account book</string>
  <key>NSAppTransportSecurity</key>
  <dict>
    <key>NSAllowsLocalNetworking</key><true/>
    <key>NSExceptionDomains</key>
    <dict>
      <key>127.0.0.1</key>
      <dict>
        <key>NSExceptionAllowsInsecureHTTPLoads</key><true/>
        <key>NSIncludesSubdomains</key><false/>
      </dict>
      <key>localhost</key>
      <dict>
        <key>NSExceptionAllowsInsecureHTTPLoads</key><true/>
        <key>NSIncludesSubdomains</key><false/>
      </dict>
    </dict>
  </dict>
  <key>CFBundleDocumentTypes</key>
  <array>
    <dict>
      <key>CFBundleTypeName</key><string>CRISPprint Ghana data</string>
      <key>CFBundleTypeRole</key><string>Viewer</string>
      <key>LSItemContentTypes</key><array><string>com.sqlite-database</string></array>
    </dict>
  </array>
</dict>
</plist>
PLIST

# Settings ▸ Updates compares this build against the repository, so the bundle has to carry the
# commit and the moment it was built. Both come from the tree being built, never from a hand edit.
COMMIT="$(git -C "$PROJECT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
BUILT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
PLIST="$APP/Contents/Info.plist"
plutil -replace CFBundleShortVersionString -string 1.1 "$PLIST"
plutil -replace CFBundleVersion -string 1.1 "$PLIST"
plutil -replace ChrisphicsBuildCommit -string "$COMMIT" "$PLIST"
plutil -replace ChrisphicsBuildDate -string "$BUILT" "$PLIST"
echo "   stamped: version 1.1, commit $COMMIT, built $BUILT"

echo "4/5 compiling the Swift shell"
swiftc -O -swift-version 5 -framework Cocoa -framework WebKit -framework PDFKit \
  -framework ServiceManagement -framework IOKit \
  -o "$BIN" "$SRC/src/main.swift"
chmod +x "$BIN"

echo "5/5 signing so macOS will run it"
codesign --force --sign - --timestamp=none "$APP" >/dev/null 2>&1 || \
  echo "   (ad-hoc signing skipped — the app still runs on this Mac)"

touch "$APP"
echo
echo "Done: $APP"
echo "Double-click it, or:  open \"$APP\""
