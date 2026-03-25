#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT_DIR/.env"
  set +a
fi

RUNTIME_DIR="${APP_RUNTIME_DIR:-$ROOT_DIR/runtime}"
PID_FILE="$RUNTIME_DIR/tunnel_forwarding.pid"
LOG_FILE="$RUNTIME_DIR/service.log"

mkdir -p "$RUNTIME_DIR" "$ROOT_DIR/data"

if [[ -f "$PID_FILE" ]]; then
  EXISTING_PID="$(cat "$PID_FILE")"
  if [[ -n "$EXISTING_PID" ]] && kill -0 "$EXISTING_PID" 2>/dev/null; then
    echo "Service is already running with PID $EXISTING_PID"
    echo "Log file: $LOG_FILE"
    exit 0
  fi
  rm -f "$PID_FILE"
fi

if command -v setsid >/dev/null 2>&1; then
  setsid python3 -m tunnel_admin >>"$LOG_FILE" 2>&1 < /dev/null &
else
  nohup python3 -m tunnel_admin >>"$LOG_FILE" 2>&1 < /dev/null &
fi
SERVICE_PID="$!"
echo "$SERVICE_PID" >"$PID_FILE"

sleep 1
if kill -0 "$SERVICE_PID" 2>/dev/null; then
  echo "Tunnel admin started"
  echo "PID: $SERVICE_PID"
  echo "URL: http://${APP_HOST:-0.0.0.0}:${APP_PORT:-2020}"
  echo "Log file: $LOG_FILE"
  exit 0
fi

echo "Failed to start tunnel admin. Check $LOG_FILE"
rm -f "$PID_FILE"
exit 1
