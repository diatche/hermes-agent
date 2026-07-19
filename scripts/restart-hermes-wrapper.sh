#!/usr/bin/env bash
# Restart Pavel's custom HermesGateway.app launchd wrapper.
#
# Usage:
#   scripts/restart-hermes-wrapper.sh            # foreground restart + verification
#   scripts/restart-hermes-wrapper.sh --detach   # return immediately, restart in background
#   scripts/restart-hermes-wrapper.sh --status   # read-only status check
#
# The --detach mode is intended for Hermes quick commands from chat: it prints a
# short message, then performs the restart after this process exits so the reply
# has a chance to reach the user before the wrapper kills/restarts the backend.

set -euo pipefail

LABEL="${HERMES_WRAPPER_LABEL:-nz.diatche.hermes-gateway}"
PLIST="${HERMES_WRAPPER_PLIST:-$HOME/Library/LaunchAgents/${LABEL}.plist}"
DOMAIN="${HERMES_WRAPPER_DOMAIN:-gui/$(id -u)}"
APP="${HERMES_WRAPPER_APP:-/Applications/HermesGateway.app}"
LOG_DIR="${HERMES_WRAPPER_LOG_DIR:-$HOME/.hermes/logs}"
RESTART_LOG="$LOG_DIR/hermes-gateway-wrapper.restart.log"
PORT="${HERMES_DASHBOARD_PORT:-9119}"

usage() {
  cat <<EOF
Usage: $0 [--detach|--status|--help]

Restarts launchd service: $DOMAIN/$LABEL
Plist: $PLIST
App:   $APP
EOF
}

status() {
  echo "=== launchd ==="
  launchctl print "$DOMAIN/$LABEL" 2>&1 | sed -n '1,90p' || true
  echo
  echo "=== processes ==="
  ps -axo pid,ppid,stat,command | grep -Ei 'HermesGateway|hermes gateway run|web_server\.start_server|:9119|192\.168\.0\.109' | grep -v grep || true
  echo
  echo "=== optional dashboard listener :$PORT ==="
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>&1 || true
}

verify_prereqs() {
  if [[ ! -f "$PLIST" ]]; then
    echo "ERROR: plist not found: $PLIST" >&2
    exit 2
  fi
  if [[ ! -x "$APP/Contents/MacOS/HermesGateway" ]]; then
    echo "ERROR: wrapper executable not found/executable: $APP/Contents/MacOS/HermesGateway" >&2
    exit 2
  fi
  plutil -lint "$PLIST" >/dev/null
}

restart_foreground() {
  mkdir -p "$LOG_DIR"
  {
    echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') restarting $DOMAIN/$LABEL ====="
    echo "Plist: $PLIST"
    echo "App:   $APP"

    verify_prereqs

    echo
    echo "Booting out old service state..."
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    launchctl bootout "$DOMAIN" "$PLIST" 2>/dev/null || true

    echo "Bootstrapping service..."
    launchctl bootstrap "$DOMAIN" "$PLIST"
    launchctl kickstart -k "$DOMAIN/$LABEL"

    echo "Waiting for wrapper gateway process..."
    for _ in {1..30}; do
      if ps -axo ppid,command | awk -v p="$(pgrep -f '/Applications/HermesGateway.app/Contents/MacOS/HermesGateway' | head -n1)" '$1 == p && /hermes gateway run --replace/ { found=1 } END { exit(found ? 0 : 1) }'; then
        break
      fi
      sleep 1
    done

    echo
    status
    echo "===== restart complete ====="
  } 2>&1 | tee -a "$RESTART_LOG"
}

detach_restart() {
  mkdir -p "$LOG_DIR"
  verify_prereqs
  echo "Scheduling HermesGateway.app wrapper restart in background."
  echo "This chat/client may disconnect briefly. Log: $RESTART_LOG"
  # Use nohup + background intentionally: the caller may be a Hermes quick command
  # running under the very service being restarted.
  nohup /bin/bash -lc "sleep 2; exec '$0' --foreground" >>"$RESTART_LOG" 2>&1 &
}

case "${1:---foreground}" in
  --foreground)
    restart_foreground
    ;;
  --detach)
    detach_restart
    ;;
  --status)
    status
    ;;
  --help|-h)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
