#!/usr/bin/env bash
# Restart Pavel's custom HermesGateway.app LaunchAgent.
# Port 9119 is reserved for this wrapper: anything still listening there after
# launchd stop is terminated so the replacement can start cleanly.

set -euo pipefail

LABEL="${HERMES_WRAPPER_LABEL:-nz.diatche.hermes-gateway}"
PLIST="${HERMES_WRAPPER_PLIST:-$HOME/Library/LaunchAgents/${LABEL}.plist}"
DOMAIN="${HERMES_WRAPPER_DOMAIN:-gui/$(id -u)}"
APP="${HERMES_WRAPPER_APP:-/Applications/HermesGateway.app}"
ENTRYPOINT="${HERMES_WRAPPER_ENTRYPOINT:-$HOME/.hermes/local/bin/start-hermes-gateway-wrapper.sh}"
OFFICIAL_LABEL="${HERMES_OFFICIAL_GATEWAY_LABEL:-ai.hermes.gateway}"
PORT="${HERMES_DASHBOARD_PORT:-9119}"
REPO="${HERMES_REPO:-$HOME/.hermes/hermes-agent}"
LOG_DIR="${HERMES_WRAPPER_LOG_DIR:-$HOME/.hermes/logs}"
RESTART_LOG="$LOG_DIR/hermes-gateway-wrapper.restart.log"
MAINTENANCE_ACTIVE="${HERMES_MAINTENANCE_ACTIVE:-$HOME/.hermes/local/update/active.json}"
HEALTH_SCRIPT="${HERMES_WRAPPER_HEALTH_SCRIPT:-$HOME/.hermes/local/health/hermes_core_health.py}"
HEALTH_PYTHON="${HERMES_WRAPPER_HEALTH_PYTHON:-$REPO/venv/bin/python}"
START_WAIT="${HERMES_WRAPPER_START_WAIT:-30}"
PORT_TERM_WAIT="${HERMES_WRAPPER_PORT_TERM_WAIT:-2}"
PORT_KILL_WAIT="${HERMES_WRAPPER_PORT_KILL_WAIT:-2}"
KILL_BIN="${HERMES_WRAPPER_KILL_BIN:-/bin/kill}"

usage() {
  printf 'Usage: %s [--foreground|--detach|--stop|--force-stop|--status|--enforce-exclusivity|--assert-update-quiescence|--help]\n' "$0"
}

listener_pids() {
  lsof -nP -t -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | sort -nu
}

port_is_free() {
  [[ -z "$(listener_pids)" ]]
}

job_is_loaded() {
  launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1
}

require_restart_permission() {
  if [[ "${HERMES_MAINTENANCE_START_ALLOWED:-0}" != "1" && -e "$MAINTENANCE_ACTIVE" ]]; then
    echo "ERROR: maintenance owns gateway restart; refusing competing restart" >&2
    return 1
  fi
}

verify_prereqs() {
  [[ -f "$PLIST" ]] || { echo "ERROR: plist not found: $PLIST" >&2; return 2; }
  [[ -x "$APP/Contents/MacOS/HermesGateway" ]] || { echo "ERROR: wrapper executable missing: $APP/Contents/MacOS/HermesGateway" >&2; return 2; }
  [[ -x "$ENTRYPOINT" ]] || { echo "ERROR: wrapper entrypoint missing: $ENTRYPOINT" >&2; return 2; }
  [[ -x "$HEALTH_PYTHON" ]] || { echo "ERROR: health Python missing: $HEALTH_PYTHON" >&2; return 2; }
  [[ -f "$HEALTH_SCRIPT" ]] || { echo "ERROR: health script missing: $HEALTH_SCRIPT" >&2; return 2; }
  plutil -lint "$PLIST" >/dev/null
}

enforce_exclusivity() {
  local user_domain="user/$(id -u)"
  launchctl disable "$DOMAIN/$OFFICIAL_LABEL" >/dev/null 2>&1 || true
  launchctl disable "$user_domain/$OFFICIAL_LABEL" >/dev/null 2>&1 || true
  launchctl bootout "$DOMAIN/$OFFICIAL_LABEL" >/dev/null 2>&1 || true
  launchctl bootout "$user_domain/$OFFICIAL_LABEL" >/dev/null 2>&1 || true
}

wait_for_free_port() {
  local seconds="$1"
  for ((i = 0; i < seconds; i++)); do
    port_is_free && return 0
    sleep 1
  done
  port_is_free
}

clear_reserved_port() {
  local pids
  pids="$(listener_pids)"
  [[ -n "$pids" ]] || return 0

  echo "Port $PORT is still occupied; terminating listener(s): $pids" >&2
  for pid in $pids; do "$KILL_BIN" -TERM "$pid" 2>/dev/null || true; done
  wait_for_free_port "$PORT_TERM_WAIT" && return 0

  pids="$(listener_pids)"
  echo "Port $PORT is still occupied; killing listener(s): $pids" >&2
  for pid in $pids; do "$KILL_BIN" -KILL "$pid" 2>/dev/null || true; done
  if wait_for_free_port "$PORT_KILL_WAIT"; then
    return 0
  fi
  echo "ERROR: port $PORT remains occupied after forced cleanup" >&2
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2 || true
  return 1
}

stop_foreground() {
  launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || \
    launchctl bootout "$DOMAIN" "$PLIST" >/dev/null 2>&1 || true
  clear_reserved_port
  if job_is_loaded; then
    echo "Stopped listener on port $PORT; $DOMAIN/$LABEL remains loaded"
  else
    echo "Stopped: $DOMAIN/$LABEL; port $PORT is free"
  fi
}

wait_for_ready_port() {
  for ((i = 0; i < START_WAIT; i++)); do
    if job_is_loaded && ! port_is_free; then
      return 0
    fi
    sleep 1
  done
  echo "ERROR: wrapper did not open port $PORT within ${START_WAIT}s" >&2
  return 1
}

run_health_check() {
  "$HEALTH_PYTHON" "$HEALTH_SCRIPT" --check-only --no-state --json
}

status() {
  local failed=0
  if job_is_loaded; then
    echo "OK: $DOMAIN/$LABEL is loaded"
  else
    echo "ERROR: $DOMAIN/$LABEL is not loaded"
    failed=1
  fi
  if port_is_free; then
    echo "ERROR: port $PORT is not listening"
    failed=1
  else
    echo "OK: port $PORT is listening"
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true
  fi
  return "$failed"
}

restart_foreground() {
  require_restart_permission
  verify_prereqs
  mkdir -p "$LOG_DIR"
  {
    echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') restarting $DOMAIN/$LABEL ====="
    stop_foreground
    enforce_exclusivity
    if job_is_loaded; then
      launchctl kickstart -k "$DOMAIN/$LABEL"
    else
      launchctl bootstrap "$DOMAIN" "$PLIST"
      launchctl kickstart "$DOMAIN/$LABEL"
    fi
    wait_for_ready_port
    run_health_check
    echo "===== restart complete: port $PORT is healthy ====="
  } 2>&1 | tee -a "$RESTART_LOG"
}

detach_restart() {
  local worker_label command
  require_restart_permission
  verify_prereqs
  mkdir -p "$LOG_DIR"
  worker_label="${LABEL}.restart.$(date +%s).$$"
  printf -v command 'sleep 2; exec %q --foreground' "$0"
  echo "Scheduling HermesGateway.app restart through launchd. Log: $RESTART_LOG"
  launchctl submit -l "$worker_label" -- /bin/bash -lc "$command"
}

loaded_official_gateway_labels() {
  launchctl list 2>/dev/null | awk '$3 ~ /^ai[.]hermes[.]gateway($|-)/ { print $3 }'
}

wrapper_app_pids() {
  ps -axo pid=,command= | awk -v wrapper="$APP/Contents/MacOS/HermesGateway" '
    {
      command = $0
      sub(/^[[:space:]]*[0-9]+[[:space:]]+/, "", command)
      if (command == wrapper || index(command, wrapper " ") == 1) print $1
    }'
}

hermes_runtime_pids() {
  local python_bin="${HERMES_WRAPPER_PYTHON:-$REPO/venv/bin/python}" output
  if [[ ! -x "$python_bin" ]]; then
    echo "matcher-unavailable"
    return 0
  fi
  if ! output="$(
    ps -axo pid=,ppid=,command= | PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}" \
      "$python_bin" -c '
import os
import sys
from gateway.status import looks_like_dashboard_runtime_command_line, looks_like_gateway_runtime_command_line
records = []
parents = {}
for line in sys.stdin:
    parts = line.strip().split(None, 2)
    if len(parts) != 3:
        continue
    try:
        pid, parent = int(parts[0]), int(parts[1])
    except ValueError:
        continue
    records.append((pid, parts[2]))
    parents[pid] = parent
excluded = {os.getpid()}
pid = os.getppid()
while pid > 1 and pid not in excluded:
    excluded.add(pid)
    pid = parents.get(pid, 0)
for pid, command in records:
    if pid not in excluded and (
        looks_like_gateway_runtime_command_line(command)
        or looks_like_dashboard_runtime_command_line(command)
    ):
        print(pid)
'
  )"; then
    echo "matcher-unavailable"
    return 0
  fi
  printf '%s\n' "$output" | awk 'NF'
}

listener_pids() {
  lsof -nP -t -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | sort -nu
}

assert_update_quiescence() {
  local stable=0
  for _ in {1..40}; do
    if ! launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 \
      && [[ -z "$(wrapper_app_pids)" ]] \
      && [[ -z "$(hermes_runtime_pids)" ]] \
      && [[ -z "$(listener_pids)" ]] \
      && [[ -z "$(loaded_official_gateway_labels)" ]]; then
      stable=$((stable + 1))
      if (( stable >= 12 )); then
        echo "Update quiescence verified"
        return 0
      fi
    else
      stable=0
    fi
    sleep 0.25
  done
  echo "ERROR: Hermes update quiescence could not be established" >&2
  loaded_official_gateway_labels >&2 || true
  wrapper_app_pids >&2 || true
  hermes_runtime_pids >&2 || true
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2 || true
  return 1
}

case "${1:---foreground}" in
  --foreground) restart_foreground ;;
  --detach) detach_restart ;;
  --stop|--force-stop) stop_foreground ;;
  --status) status ;;
  --enforce-exclusivity) enforce_exclusivity ;;
  --assert-update-quiescence) assert_update_quiescence ;;
  --help|-h) usage ;;
  *) usage >&2; exit 2 ;;
esac
