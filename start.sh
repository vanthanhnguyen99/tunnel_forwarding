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
DATA_DIR="${APP_DATA_DIR:-$ROOT_DIR/data}"
DOCKER_STATE_DIR="${APP_DOCKER_CONFIG_DIR:-$DATA_DIR/docker}"
PID_FILE="$RUNTIME_DIR/tunnel_forwarding.pid"
LOG_FILE="$RUNTIME_DIR/service.log"
APP_LOG_FILE="$RUNTIME_DIR/app.log"

mkdir -p "$RUNTIME_DIR" "$DATA_DIR"

stop_existing_service() {
  if [[ ! -f "$PID_FILE" ]]; then
    return 0
  fi

  EXISTING_PID="$(cat "$PID_FILE")"
  if [[ -z "$EXISTING_PID" ]]; then
    rm -f "$PID_FILE"
    return 0
  fi

  if ! kill -0 "$EXISTING_PID" 2>/dev/null; then
    rm -f "$PID_FILE"
    return 0
  fi

  echo "Stopping existing tunnel admin process: $EXISTING_PID"
  kill "$EXISTING_PID"

  for _ in {1..20}; do
    if ! kill -0 "$EXISTING_PID" 2>/dev/null; then
      rm -f "$PID_FILE"
      return 0
    fi
    sleep 0.5
  done

  echo "Process $EXISTING_PID did not stop gracefully. Sending SIGKILL."
  kill -9 "$EXISTING_PID"
  rm -f "$PID_FILE"
}

clean_previous_runtime() {
  rm -f "$PID_FILE" "$LOG_FILE" "$APP_LOG_FILE"

  if [[ -d "$DOCKER_STATE_DIR" ]]; then
    find "$DOCKER_STATE_DIR" -type f \( -name 'runtime.json' -o -name '*.tmp' \) -delete
    find "$DOCKER_STATE_DIR" -type f -path '*/commands/*.json' -delete
  fi

  find "$ROOT_DIR" -type d -name '__pycache__' -prune -exec rm -rf {} +
}

stop_existing_service
clean_previous_runtime

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
