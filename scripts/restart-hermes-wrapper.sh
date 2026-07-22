#!/usr/bin/env bash
# Manage Pavel's custom HermesGateway.app LaunchAgent.
#
# Usage:
#   scripts/restart-hermes-wrapper.sh --foreground  # restart now
#   scripts/restart-hermes-wrapper.sh --detach      # delayed chat-safe restart
#   scripts/restart-hermes-wrapper.sh --stop        # stop and verify unloaded
#   scripts/restart-hermes-wrapper.sh --force-stop  # stop, then kill survivors
#   scripts/restart-hermes-wrapper.sh --enforce-exclusivity
#   scripts/restart-hermes-wrapper.sh --status      # read-only health/status
#   scripts/restart-hermes-wrapper.sh --assert-update-quiescence

set -euo pipefail

LABEL="${HERMES_WRAPPER_LABEL:-nz.diatche.hermes-gateway}"
PLIST="${HERMES_WRAPPER_PLIST:-$HOME/Library/LaunchAgents/${LABEL}.plist}"
DOMAIN="${HERMES_WRAPPER_DOMAIN:-gui/$(id -u)}"
APP="${HERMES_WRAPPER_APP:-/Applications/HermesGateway.app}"
ENTRYPOINT="${HERMES_WRAPPER_ENTRYPOINT:-$HOME/.hermes/local/bin/start-hermes-gateway-wrapper.sh}"
OFFICIAL_LABEL="${HERMES_OFFICIAL_GATEWAY_LABEL:-ai.hermes.gateway}"
LOG_DIR="${HERMES_WRAPPER_LOG_DIR:-$HOME/.hermes/logs}"
RESTART_LOG="$LOG_DIR/hermes-gateway-wrapper.restart.log"
PORT="${HERMES_DASHBOARD_PORT:-9119}"
REPO="${HERMES_REPO:-$HOME/.hermes/hermes-agent}"
MAINTENANCE_ACTIVE="${HERMES_MAINTENANCE_ACTIVE:-$HOME/.hermes/local/update/active.json}"
TIMEOUT_BUDGET_SCRIPT="${HERMES_WRAPPER_TIMEOUT_BUDGET_SCRIPT:-$REPO/scripts/hermes-wrapper-timeout-budget.py}"
TIMEOUT_STATE="${HERMES_WRAPPER_TIMEOUT_STATE:-$HOME/.hermes/local/run/hermes-gateway-wrapper-timeouts.json}"
PYTHON_BIN="${HERMES_WRAPPER_PYTHON:-$REPO/venv/bin/python}"

usage() {
  printf 'Usage: %s [--foreground|--detach|--stop|--force-stop|--enforce-exclusivity|--status|--assert-update-quiescence|--help]\n' "$0"
}

require_restart_permission() {
  if [[ "${HERMES_MAINTENANCE_START_ALLOWED:-0}" == "1" ]]; then
    return 0
  fi
  if [[ -e "$MAINTENANCE_ACTIVE" ]]; then
    echo "ERROR: maintenance owns gateway restart; refusing competing restart" >&2
    return 1
  fi
}

stop_wait_polls() {
  local expected_pid="$1" value
  if [[ ! -r "$TIMEOUT_STATE" ]]; then
    echo "ERROR: active wrapper timeout state is missing: $TIMEOUT_STATE" >&2
    return 1
  fi
  if ! value="$("$PYTHON_BIN" - "$TIMEOUT_STATE" "$expected_pid" <<'PY'
import json, math, sys
with open(sys.argv[1], encoding="utf-8") as stream:
    state = json.load(stream)
if state.get("pid") != int(sys.argv[2]):
    raise SystemExit("wrapper timeout state PID does not match active LaunchAgent")
value = state.get("controller_wait")
if isinstance(value, bool) or not isinstance(value, (int, float)):
    raise SystemExit("invalid controller_wait in wrapper timeout state")
if not math.isfinite(value) or value < 1 or value != math.ceil(value):
    raise SystemExit("invalid controller_wait in wrapper timeout state")
print(int(value))
PY
  )"; then
    echo "ERROR: active wrapper timeout state is invalid" >&2
    return 1
  fi
  [[ "$value" =~ ^[1-9][0-9]*$ ]] || return 1
  printf '%s\n' "$value"
}

wrapper_pid() {
  launchctl print "$DOMAIN/$LABEL" 2>/dev/null | awk '/^[[:space:]]*pid = [0-9]+/ { print $3; exit }'
}

wrapper_gateway_pids() {
  local parent="$1"
  ps -axo pid=,ppid=,command= | awk -v parent="$parent" \
    '$2 == parent && /hermes gateway run --replace/ { print $1 }'
}

wrapper_dashboard_pids() {
  local parent="$1"
  ps -axo pid=,ppid=,command= | awk -v parent="$parent" -v port="$PORT" \
    '$2 == parent && /hermes dashboard/ && $0 ~ "--port " port "([[:space:]]|$)" { print $1 }'
}

descendant_pids() {
  local parent="$1" child
  while read -r child; do
    [[ -n "$child" ]] || continue
    printf '%s\n' "$child"
    descendant_pids "$child"
  done < <(pgrep -P "$parent" 2>/dev/null || true)
}

is_descendant_of() {
  local pid="$1" ancestor="$2" parent
  while [[ "$pid" =~ ^[0-9]+$ ]] && (( pid > 1 )); do
    [[ "$pid" == "$ancestor" ]] && return 0
    parent="$(ps -p "$pid" -o ppid= 2>/dev/null | tr -d ' ' || true)"
    [[ -n "$parent" && "$parent" != "$pid" ]] || return 1
    pid="$parent"
  done
  return 1
}

wrapper_child_running() {
  local root_pid wrapper_command gateway_pids gateway_count gateway_command
  root_pid="$(wrapper_pid)"
  [[ -n "$root_pid" ]] || return 1
  wrapper_command="$(ps -p "$root_pid" -o command= 2>/dev/null || true)"
  [[ "$wrapper_command" == *"$APP/Contents/MacOS/HermesGateway"* ]] || return 1
  gateway_pids="$(wrapper_gateway_pids "$root_pid")"
  gateway_count="$(printf '%s\n' "$gateway_pids" | awk 'NF { count++ } END { print count+0 }')"
  [[ "$gateway_count" == "1" ]] || return 1
  gateway_command="$(ps -p "$gateway_pids" -o command= 2>/dev/null || true)"
  [[ "$gateway_command" == *"$REPO/venv/bin/hermes gateway run --replace"* ]]
}

official_disabled() {
  launchctl print-disabled "$1" 2>/dev/null \
    | grep -Eq "[\"']?$OFFICIAL_LABEL[\"']?[[:space:]]*=>[[:space:]]*disabled"
}

official_unloaded() {
  ! launchctl print "$1/$OFFICIAL_LABEL" >/dev/null 2>&1
}

wrapper_ready() {
  local root dashboards owners all_wrapper_pids
  local owner_count dashboard_count
  wrapper_child_running || return 1
  root="$(wrapper_pid)"
  owners="$(lsof -nP -t -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | sort -u)"
  owner_count="$(printf '%s\n' "$owners" | awk 'NF { count++ } END { print count+0 }')"
  [[ "$owner_count" == "1" ]] || return 1
  dashboards="$(wrapper_dashboard_pids "$root")"
  dashboard_count="$(printf '%s\n' "$dashboards" | awk 'NF { count++ } END { print count+0 }')"
  [[ "$dashboard_count" == "1" ]] || return 1
  [[ "$owners" == "$dashboards" ]] || return 1

  all_wrapper_pids="$(wrapper_app_pids | sort -nu)"
  [[ "$all_wrapper_pids" == "$root" ]]
}

wrapper_signature() {
  local root gateway dashboard owner
  wrapper_ready || return 1
  root="$(wrapper_pid)"
  gateway="$(wrapper_gateway_pids "$root")"
  dashboard="$(wrapper_dashboard_pids "$root")"
  owner="$(lsof -nP -t -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | sort -u)"
  printf '%s:%s:%s:%s\n' "$root" "$gateway" "$dashboard" "$owner"
}

status() {
  local healthy=0 wrapper_state user_domain="user/$(id -u)"
  echo "=== supervisor policy ==="
  for scope in "$DOMAIN" "$user_domain"; do
    if official_disabled "$scope"; then
      echo "OK: official gateway is persistently disabled: $scope/$OFFICIAL_LABEL"
    else
      echo "ERROR: official gateway is not persistently disabled: $scope/$OFFICIAL_LABEL"
      healthy=1
    fi
    if official_unloaded "$scope"; then
      echo "OK: competing default gateway is not loaded: $scope/$OFFICIAL_LABEL"
    else
      echo "ERROR: competing default gateway is loaded: $scope/$OFFICIAL_LABEL"
      healthy=1
    fi
  done
  marker="$HOME/.hermes/gateway/supervisor_repair_needed"
  if [[ -f "$marker" ]]; then
    echo "ERROR: persistent-disable repair marker exists: $marker"
    healthy=1
  fi
  echo
  echo "=== wrapper launchd ==="
  if wrapper_state="$(launchctl print "$DOMAIN/$LABEL" 2>&1)"; then
    printf '%s\n' "$wrapper_state" | sed -n '1,90p'
  else
    printf '%s\n' "$wrapper_state"
    echo "ERROR: custom wrapper is not loaded: $DOMAIN/$LABEL"
    healthy=1
  fi
  echo
  echo "=== processes ==="
  ps -axo pid,ppid,stat,command | grep -Ei 'HermesGateway|hermes gateway run|web_server\.start_server|:9119|192\.168\.0\.109' | grep -v grep || true
  if wrapper_ready; then
    echo "OK: Hermes gateway is wrapper-owned and :$PORT is listening"
  else
    echo "ERROR: wrapper-owned gateway/listener is not ready"
    healthy=1
  fi
  echo
  echo "=== dashboard listener :$PORT ==="
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>&1 || true
  return "$healthy"
}

verify_prereqs() {
  [[ -f "$PLIST" ]] || { echo "ERROR: plist not found: $PLIST" >&2; exit 2; }
  [[ -x "$APP/Contents/MacOS/HermesGateway" ]] || { echo "ERROR: wrapper executable missing: $APP/Contents/MacOS/HermesGateway" >&2; exit 2; }
  [[ -x "$ENTRYPOINT" ]] || { echo "ERROR: wrapper entrypoint missing: $ENTRYPOINT" >&2; exit 2; }
  [[ -x "$TIMEOUT_BUDGET_SCRIPT" ]] || { echo "ERROR: timeout budget resolver missing: $TIMEOUT_BUDGET_SCRIPT" >&2; exit 2; }
  plutil -lint "$PLIST" >/dev/null
}

enforce_exclusivity() {
  local user_domain="user/$(id -u)"
  launchctl disable "$DOMAIN/$OFFICIAL_LABEL" >/dev/null 2>&1 || true
  launchctl disable "$user_domain/$OFFICIAL_LABEL" >/dev/null 2>&1 || true
  launchctl bootout "$DOMAIN/$OFFICIAL_LABEL" >/dev/null 2>&1 || true
  launchctl bootout "$user_domain/$OFFICIAL_LABEL" >/dev/null 2>&1 || true
  for _ in {1..10}; do
    if official_disabled "$DOMAIN" && official_disabled "$user_domain" \
      && official_unloaded "$DOMAIN" && official_unloaded "$user_domain"; then
      echo "Official gateway is persistently disabled and unloaded in GUI and user domains"
      return 0
    fi
    sleep 0.4
  done
  echo "ERROR: official gateway disable/unload invariant failed" >&2
  return 1
}

stop_foreground() {
  local old_wrapper old_children current_children pid all_gone wait_polls poll deadline bootout_error
  bootout_error="$(mktemp -t hermes-wrapper-bootout.XXXXXX)"
  trap 'rm -f "$bootout_error"' RETURN
  old_wrapper="$(wrapper_pid || true)"
  if [[ -z "$old_wrapper" ]]; then
    if ! launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 \
      && [[ -z "$(wrapper_app_pids)" ]] \
      && [[ -z "$(listener_pids)" ]]; then
      echo "Stopped and quiescent: $DOMAIN/$LABEL"
      return 0
    fi
    echo "ERROR: loaded wrapper has no verifiable PID" >&2
    return 1
  fi
  if ! wait_polls="$(stop_wait_polls "$old_wrapper")"; then
    return 1
  fi
  deadline=$((SECONDS + wait_polls))
  old_children=""
  if [[ -n "$old_wrapper" ]]; then
    old_children="$(descendant_pids "$old_wrapper" || true)"
  fi
  launchctl bootout "$DOMAIN/$LABEL" 2>"$bootout_error" &
  local bootout_pid=$!
  while kill -0 "$bootout_pid" 2>/dev/null && (( SECONDS < deadline )); do sleep 0.1; done
  if kill -0 "$bootout_pid" 2>/dev/null; then
    kill -TERM "$bootout_pid" 2>/dev/null || true
    sleep 0.2
    kill -KILL "$bootout_pid" 2>/dev/null || true
    wait "$bootout_pid" 2>/dev/null || true
    echo "ERROR: launchctl bootout exceeded coordinated stop deadline" >&2
    cat "$bootout_error" >&2 2>/dev/null || true
    return 1
  fi
  wait "$bootout_pid" 2>/dev/null || true
  for ((poll = 0; SECONDS < deadline; poll++)); do
    if [[ -n "$old_wrapper" ]] && kill -0 "$old_wrapper" 2>/dev/null; then
      current_children="$(descendant_pids "$old_wrapper" || true)"
      old_children="$(printf '%s\n%s\n' "$old_children" "$current_children" | awk 'NF && !seen[$0]++')"
    fi
    all_gone=0
    for pid in $old_wrapper $old_children; do
      if kill -0 "$pid" 2>/dev/null; then
        all_gone=1
      fi
    done
    if ! launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 \
      && (( all_gone == 0 )) \
      && ! lsof -nP -t -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
      echo "Stopped and quiescent: $DOMAIN/$LABEL"
      return 0
    fi
    sleep 1
  done
  echo "ERROR: wrapper-owned process topology remains after bootout" >&2
  cat "$bootout_error" >&2 2>/dev/null || true
  ps -axo pid,ppid,stat,command | grep -Ei 'HermesGateway|hermes gateway run' | grep -v grep >&2 || true
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2 || true
  return 1
}

force_stop_foreground() {
  if stop_foreground; then
    return 0
  fi
  echo "Force-stopping surviving wrapper processes" >&2
  local pid pids
  pids="$(printf '%s\n%s\n' "$(wrapper_app_pids)" "$(listener_pids)" | awk 'NF && !seen[$0]++')"
  for pid in $pids; do kill -TERM "$pid" 2>/dev/null || true; done
  sleep 1
  for pid in $pids; do kill -KILL "$pid" 2>/dev/null || true; done
  launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
  launchctl bootout "$DOMAIN" "$PLIST" >/dev/null 2>&1 || true
  for _ in {1..10}; do
    if ! launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 \
      && [[ -z "$(wrapper_app_pids)" ]] \
      && [[ -z "$(listener_pids)" ]]; then
      echo "Force-stopped and quiescent: $DOMAIN/$LABEL"
      return 0
    fi
    sleep 0.2
  done
  echo "ERROR: wrapper processes survived forced stop" >&2
  return 1
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

from gateway.status import (
    looks_like_dashboard_runtime_command_line,
    looks_like_gateway_runtime_command_line,
)


records = []
parents = {}
for line in sys.stdin:
    stripped = line.strip()
    if not stripped:
        continue
    parts = stripped.split(None, 2)
    if len(parts) != 3:
        raise SystemExit(2)
    pid_text, parent_text, command = parts
    try:
        pid = int(pid_text)
        parent = int(parent_text)
    except ValueError:
        raise SystemExit(2)
    records.append((pid, command))
    parents[pid] = parent

excluded = {os.getpid()}
pid = os.getppid()
while pid > 1 and pid not in excluded:
    excluded.add(pid)
    pid = parents.get(pid, 0)

for pid, command in records:
    if pid in excluded:
        continue
    if looks_like_gateway_runtime_command_line(
        command
    ) or looks_like_dashboard_runtime_command_line(command):
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

restart_foreground() {
  require_restart_permission
  mkdir -p "$LOG_DIR"
  {
    echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') restarting $DOMAIN/$LABEL ====="
    verify_prereqs
    stop_foreground
    enforce_exclusivity
    launchctl bootstrap "$DOMAIN" "$PLIST"
    launchctl kickstart "$DOMAIN/$LABEL"
    echo "Waiting for wrapper-owned gateway process..."
    stable=0
    stable_signature=""
    for _ in {1..30}; do
      if signature="$(wrapper_signature)"; then
        if [[ "$signature" == "$stable_signature" ]]; then
          stable=$((stable + 1))
        else
          stable_signature="$signature"
          stable=1
        fi
        if (( stable >= 3 )); then
          echo "===== restart complete after stable readiness ====="
          return 0
        fi
      else
        stable=0
        stable_signature=""
      fi
      sleep 1
    done
    echo "ERROR: wrapper loaded but gateway child did not become ready" >&2
    return 1
  } 2>&1 | tee -a "$RESTART_LOG"
}

detach_restart() {
  require_restart_permission
  mkdir -p "$LOG_DIR"
  verify_prereqs
  echo "Scheduling HermesGateway.app wrapper restart in background."
  echo "This chat/client may disconnect briefly. Log: $RESTART_LOG"
  nohup /bin/bash -lc "sleep 2; exec '$0' --foreground" >>"$RESTART_LOG" 2>&1 &
}

case "${1:---foreground}" in
  --foreground) restart_foreground ;;
  --detach) detach_restart ;;
  --stop) stop_foreground ;;
  --force-stop) force_stop_foreground ;;
  --enforce-exclusivity) enforce_exclusivity ;;
  --assert-update-quiescence) assert_update_quiescence ;;
  --status) status ;;
  --help|-h) usage ;;
  *) usage >&2; exit 2 ;;
esac
