#!/bin/zsh
# Builds BackupOrganizer.app (release) and installs it to ~/Applications.
#
# No Xcode project is involved: `swift build` produces a bare executable,
# and this script does the small amount of manual bundling (Info.plist,
# icon, ad-hoc codesign) a real .app needs. Safe to re-run — it only ever
# touches its own dist/ output and ~/Applications/BackupOrganizer.app.
#
# Usage: Scripts/build_app.sh [--applications]
#   --applications   install to /Applications instead of ~/Applications
#                     (will prompt for sudo the first time; not the default
#                     because a dev rebuild shouldn't need elevation).

set -euo pipefail

SCRIPT_DIR="${0:A:h}"
PACKAGE_DIR="${SCRIPT_DIR:h}"
cd "$PACKAGE_DIR"

APP_NAME="BackupOrganizer"
BUNDLE_ID="com.alyz.BackupOrganizerApp"
DIST_DIR="$PACKAGE_DIR/dist"
APP_BUNDLE="$DIST_DIR/$APP_NAME.app"

INSTALL_DIR="$HOME/Applications"
if [[ "${1:-}" == "--applications" ]]; then
    INSTALL_DIR="/Applications"
fi

echo "==> Building release binary..."
swift build -c release

BINARY_PATH="$(swift build -c release --show-bin-path)/BackupOrganizerApp"
if [[ ! -x "$BINARY_PATH" ]]; then
    echo "error: expected binary not found at $BINARY_PATH" >&2
    exit 1
fi

echo "==> Generating app icon..."
ICONSET_DIR="$DIST_DIR/AppIcon.iconset"
rm -rf "$ICONSET_DIR"
swift "$SCRIPT_DIR/generate_icon.swift" "$ICONSET_DIR" >/dev/null
mkdir -p "$DIST_DIR"
iconutil -c icns "$ICONSET_DIR" -o "$DIST_DIR/AppIcon.icns"
rm -rf "$ICONSET_DIR"

echo "==> Assembling $APP_NAME.app..."
rm -rf "$APP_BUNDLE"
mkdir -p "$APP_BUNDLE/Contents/MacOS" "$APP_BUNDLE/Contents/Resources"
cp "$BINARY_PATH" "$APP_BUNDLE/Contents/MacOS/BackupOrganizerApp"
cp "$PACKAGE_DIR/Info.plist" "$APP_BUNDLE/Contents/Info.plist"
cp "$DIST_DIR/AppIcon.icns" "$APP_BUNDLE/Contents/Resources/AppIcon.icns"

echo "==> Ad-hoc code signing..."
codesign --force --deep --sign - --identifier "$BUNDLE_ID" "$APP_BUNDLE"

echo "==> Installing to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
rm -rf "$INSTALL_DIR/$APP_NAME.app"
cp -R "$APP_BUNDLE" "$INSTALL_DIR/$APP_NAME.app"

echo
echo "Done: $INSTALL_DIR/$APP_NAME.app"
echo
echo "First launch: this build is ad-hoc signed (no paid Developer ID), so"
echo "Gatekeeper will refuse a plain double-click the first time. Right-click"
echo "the app in Finder -> Open -> Open, once. After that it launches normally."
