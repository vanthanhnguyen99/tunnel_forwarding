from __future__ import annotations

import json
import logging
from pathlib import Path
import signal
import sys
import threading
from typing import Any

from .tunnel import TunnelEngine


LOGGER = logging.getLogger("tunnel_admin.worker")


class InMemorySessionStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_session_id = 1

    def create_session_record(
        self,
        endpoint_id: int,
        client_ip: str,
        client_port: int,
        upstream_ip: str,
        upstream_port: int,
        status: str = "active",
    ) -> int:
        del endpoint_id, client_ip, client_port, upstream_ip, upstream_port, status
        with self._lock:
            session_id = self._next_session_id
            self._next_session_id += 1
        return session_id

    def close_session_record(
        self,
        session_id: int,
        status: str,
        bytes_up: int,
        bytes_down: int,
        close_reason: str,
    ) -> None:
        del session_id, status, bytes_up, bytes_down, close_reason


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler()],
    )


def load_runtime_config(config_path: Path) -> tuple[dict[str, Any], float]:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Endpoint runtime config must be a JSON object")
    payload.setdefault("id", 1)
    payload.setdefault("enabled", True)
    connect_timeout_seconds = float(payload.pop("connect_timeout_seconds", 5.0))
    return payload, connect_timeout_seconds


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print("Usage: python -m tunnel_admin.worker /path/to/endpoint.json", file=sys.stderr)
        return 2

    configure_logging()
    config_path = Path(args[0]).expanduser().resolve()
    endpoint, connect_timeout_seconds = load_runtime_config(config_path)

    session_store = InMemorySessionStore()
    stop_event = threading.Event()

    def handle_event(event_name: str, data: dict[str, Any]) -> None:
        LOGGER.info("event=%s payload=%s", event_name, json.dumps(data, ensure_ascii=False, separators=(",", ":")))

    def handle_status(endpoint_id: int, message: str | None) -> None:
        if message:
            LOGGER.warning("endpoint=%s status=%s", endpoint_id, message)
        else:
            LOGGER.info("endpoint=%s status=ready", endpoint_id)

    engine = TunnelEngine(
        database=session_store,
        connect_timeout_seconds=connect_timeout_seconds,
        event_callback=handle_event,
        status_callback=handle_status,
    )

    started, message = engine.start_endpoint(endpoint)
    if not started:
        LOGGER.error("failed_to_start endpoint=%s reason=%s", endpoint.get("name"), message)
        return 1

    def request_shutdown(signum: int, _frame: Any) -> None:
        LOGGER.info("received signal=%s, stopping worker", signum)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, request_shutdown)

    try:
        stop_event.wait()
    finally:
        engine.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
