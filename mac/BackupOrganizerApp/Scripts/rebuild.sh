#!/bin/zsh
# One command for the whole edit/rebuild/relaunch loop: quits the running
# app (if any), rebuilds and reinstalls it (via build_app.sh), and reopens
# it. This is what you want while iterating — build_app.sh alone doesn't
# touch a running instance or relaunch anything.
#
# Usage: Scripts/rebuild.sh [--applications]
#   --applications   forwarded to build_app.sh: install to /Applications
#                     instead of ~/Applications.

set -euo pipefail

SCRIPT_DIR="${0:A:h}"
APP_NAME="BackupOrganizer"

RUNNING_PID="$(pgrep -f "$APP_NAME.app/Contents/MacOS/$APP_NAME" || true)"
if [[ -n "$RUNNING_PID" ]]; then
    echo "==> Quitting running $APP_NAME (pid $RUNNING_PID)..."
    kill "$RUNNING_PID"
    for _ in $(seq 1 25); do
        kill -0 "$RUNNING_PID" 2>/dev/null || break
        sleep 0.2
    done
    kill -0 "$RUNNING_PID" 2>/dev/null && kill -9 "$RUNNING_PID" 2>/dev/null || true
fi

"$SCRIPT_DIR/build_app.sh" "$@"

INSTALL_DIR="$HOME/Applications"
if [[ "${1:-}" == "--applications" ]]; then
    INSTALL_DIR="/Applications"
fi

echo "==> Relaunching..."
open "$INSTALL_DIR/$APP_NAME.app"
