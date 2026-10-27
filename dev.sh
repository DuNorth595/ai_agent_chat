#!/bin/bash
# Start (or restart) the ai_agent_chat DEV instance on port 8771.
#
# The recurring failure: the bridge was started twice without the old
# one being stopped first, so the new process died with
#   ERROR: [Errno 48] error while attempting to bind on address
#   ('0.0.0.0', 8771): address already in use
# ...while the OLD process kept serving stale code. That is the worst
# version of the bug: the service looks healthy but is running code
# from before the fix.
#
# This script makes the "stop first, verify the port is free" part
# automatic, and prints the PID it actually ended up with.
#
# Usage:
#   ./dev.sh start     # restart dev on 8771
#   ./dev.sh stop
#   ./dev.sh status
#   ./dev.sh log       # tail the dev log

set -uo pipefail

# Derive the dev tree from this script's own location, so the path
# works wherever the repo is checked out. A hardcoded absolute path
# would leak the operator's home directory into a public repo.
DEV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="/tmp/voice-bridge-dev"
PORT=8771

mkdir -p "$LOG_DIR"

kill_dev() {
  # Match on the absolute path so we never touch PROD
  # (~/voice-bridge/agent_voice_ai.py). The prod instance is launchd-
  # supervised and must never be killed from here.
  local pids
  pids=$(lsof -nP -iTCP:${PORT} -sTCP:LISTEN -t 2>/dev/null)
  if [ -n "$pids" ]; then
    for pid in $pids; do
      # Verify this pid really is the dev tree before killing it.
      local cwd
      cwd=$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | grep '^n' | tail -1 | cut -c2-)
      if [ "$cwd" = "$DEV_DIR" ]; then
        kill "$pid" 2>/dev/null
        echo "  stopped dev pid $pid"
      else
        echo "  REFUSING to kill pid $pid — cwd is '$cwd', not '$DEV_DIR'"
      fi
    done
  fi
  # Give the port a moment to release.
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if ! lsof -nP -iTCP:${PORT} -sTCP:LISTEN -t >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
  done
  echo "  WARNING: port ${PORT} still held after 5s"
  lsof -nP -iTCP:${PORT} -sTCP:LISTEN | sed 's/^/    /'
  return 1
}

start_dev() {
  echo "  stopping any existing dev instance..."
  kill_dev || { echo "  ABORT — port still held"; return 1; }

  echo "  starting dev on :${PORT}..."
  cd "$DEV_DIR" || { echo "  ABORT — no $DEV_DIR"; return 1; }
  # PYTHONPATH must be cleared. Hermes' shell environment injects the
  # gateway venv's PYTHON 3.14 site-packages ahead of this venv's 3.11
  # packages, so a 3.14 numpy shadows the right one and the bridge
  # dies with "No module named 'numpy._core._multiarray_umath'".
  # The launchd prod job has no PYTHONPATH, so prod never sees this.
  nohup env -u PYTHONPATH .venv-iris/bin/python agent_voice_ai.py \
    >> "$LOG_DIR/bridge.log" 2>> "$LOG_DIR/bridge.err" &
  local newpid=$!

  # Wait for it to bind and answer.
  for _ in $(seq 1 40); do
    sleep 0.5
    if curl -sk --max-time 3 "https://127.0.0.1:${PORT}/api/health" 2>/dev/null | grep -q '"status"'; then
      echo "  ✓ dev up — pid $newpid, port ${PORT}"
      curl -sk "https://127.0.0.1:${PORT}/api/health" \
        | python3 -c 'import json,sys; d=json.load(sys.stdin); print("    status:",d.get("status"),"v"+str(d.get("build")),"channel="+str(d.get("channel")))' 2>/dev/null
      return 0
    fi
    # If it died, surface the reason instead of pretending.
    if ! kill -0 "$newpid" 2>/dev/null; then
      echo "  ✗ dev DIED on startup — last log lines:"
      tail -8 "$LOG_DIR/bridge.err" | sed 's/^/      /'
      return 1
    fi
  done
  echo "  ✗ dev did not become ready in 20s"
  tail -8 "$LOG_DIR/bridge.err" | sed 's/^/      /'
  return 1
}

case "${1:-start}" in
  start) start_dev ;;
  stop)  kill_dev ;;
  restart) start_dev ;;
  status)
    echo "  listeners:"
    lsof -nP -iTCP:8770 -iTCP:8771 -sTCP:LISTEN 2>/dev/null | awk 'NR>1{print "    pid "$2"  "$9}'
    for p in 8770 8771; do
      printf "    :%s -> " "$p"
      curl -sk --max-time 3 "https://127.0.0.1:${p}/api/health" 2>/dev/null \
        | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("status"),"v"+str(d.get("build")),"channel="+str(d.get("channel")))' 2>/dev/null || echo "DOWN"
    done
    ;;
  log) tail -f "$LOG_DIR/bridge.err" ;;
  *) echo "usage: $0 {start|stop|restart|status|log}"; exit 1 ;;
esac
