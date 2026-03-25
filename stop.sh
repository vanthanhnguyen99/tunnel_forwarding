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

if [[ ! -f "$PID_FILE" ]]; then
  echo "No PID file found. Service may already be stopped."
  exit 0
fi

SERVICE_PID="$(cat "$PID_FILE")"
if [[ -z "$SERVICE_PID" ]]; then
  rm -f "$PID_FILE"
  echo "PID file was empty. Cleaned it up."
  exit 0
fi

if ! kill -0 "$SERVICE_PID" 2>/dev/null; then
  rm -f "$PID_FILE"
  echo "Process $SERVICE_PID is not running. Removed stale PID file."
  exit 0
fi

kill "$SERVICE_PID"

for _ in {1..20}; do
  if ! kill -0 "$SERVICE_PID" 2>/dev/null; then
    rm -f "$PID_FILE"
    echo "Tunnel admin stopped"
    exit 0
  fi
  sleep 0.5
done

echo "Process $SERVICE_PID did not stop gracefully. Sending SIGKILL."
kill -9 "$SERVICE_PID"
rm -f "$PID_FILE"
echo "Tunnel admin stopped"
