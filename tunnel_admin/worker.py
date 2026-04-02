from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import signal
import shutil
import sys
import threading
from typing import Any

from .tunnel import TunnelEngine


LOGGER = logging.getLogger("tunnel_admin.worker")

SSH_HOME_MOUNT = Path("/run/tunnel-secrets/ssh-home")
SSH_RUNTIME_DIR = Path("/root/.ssh")
SSH_RUNTIME_PRIVATE_KEY = SSH_RUNTIME_DIR / "tunnel_identity"
SSH_RUNTIME_KNOWN_HOSTS = SSH_RUNTIME_DIR / "known_hosts"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


class RuntimeStateMirror:
    def __init__(
        self,
        endpoint: dict[str, Any],
        engine: TunnelEngine | None,
        state_file: Path,
        commands_dir: Path,
    ) -> None:
        self.endpoint = endpoint
        self.engine = engine
        self.state_file = state_file
        self.commands_dir = commands_dir
        self._phase = "starting"
        self._status_message: str | None = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.commands_dir.mkdir(parents=True, exist_ok=True)
        self.write_snapshot()
        self._thread.start()

    def request_stop(self) -> None:
        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=1.5)

    def set_phase(self, phase: str) -> None:
        with self._lock:
            self._phase = phase

    def set_status_message(self, message: str | None) -> None:
        with self._lock:
            self._status_message = message

    def write_snapshot(self) -> None:
        payload = self._build_snapshot()
        tmp_path = self.state_file.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(self.state_file)

    def _loop(self) -> None:
        while not self._stop_event.wait(1.0):
            self._process_commands()
            self.write_snapshot()

    def _process_commands(self) -> None:
        if self.engine is None:
            return
        if not self.commands_dir.exists():
            return
        for command_path in sorted(self.commands_dir.glob("*.json")):
            try:
                payload = json.loads(command_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
            action = str(payload.get("action") or "").strip().lower()
            if action == "disconnect_session":
                try:
                    session_id = int(payload.get("session_id"))
                except (TypeError, ValueError):
                    session_id = 0
                if session_id > 0:
                    self.engine.disconnect_session(session_id, reason="admin_disconnect")
            try:
                command_path.unlink()
            except OSError:
                LOGGER.warning("failed to remove command file %s", command_path)

    def _build_snapshot(self) -> dict[str, Any]:
        if self.engine is None:
            endpoint_metrics = {"active_connections": 0, "bytes_up": 0, "bytes_down": 0}
            sessions: list[dict[str, Any]] = []
        else:
            runtime = self.engine.collect_runtime_metrics()
            endpoint_metrics = runtime["per_endpoint"].get(
                int(self.endpoint["id"]),
                {"active_connections": 0, "bytes_up": 0, "bytes_down": 0},
            )
            sessions = []
            for session in self.engine.list_active_sessions(endpoint_id=int(self.endpoint["id"])):
                local_session_id = int(session["id"])
                sessions.append(
                    {
                        **session,
                        "id": local_session_id,
                        "local_session_id": local_session_id,
                    }
                )
        with self._lock:
            phase = self._phase
            status_message = self._status_message
        return {
            "endpoint_id": int(self.endpoint["id"]),
            "endpoint_name": self.endpoint["name"],
            "phase": phase,
            "status_message": status_message,
            "updated_at": utc_now_iso(),
            "metrics": {
                "active_connections": int(endpoint_metrics["active_connections"]),
                "bytes_up": int(endpoint_metrics["bytes_up"]),
                "bytes_down": int(endpoint_metrics["bytes_down"]),
            },
            "active_sessions": sessions,
        }


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


def _chmod_if_exists(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except FileNotFoundError:
        return


def _reset_runtime_ssh_dir(target_dir: Path) -> None:
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_dir.chmod(0o700)


def _copy_tree_with_secure_permissions(source_dir: Path, target_dir: Path) -> None:
    for source_path in sorted(source_dir.rglob("*")):
        relative_path = source_path.relative_to(source_dir)
        target_path = target_dir / relative_path

        if source_path.is_dir():
            target_path.mkdir(parents=True, exist_ok=True)
            target_path.chmod(0o700)
            continue

        if not source_path.is_file():
            continue

        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)
        target_path.chmod(0o600)


def _stage_runtime_ssh_material(endpoint: dict[str, Any]) -> dict[str, Any]:
    staged_endpoint = dict(endpoint)
    _reset_runtime_ssh_dir(SSH_RUNTIME_DIR)

    if SSH_HOME_MOUNT.exists() and SSH_HOME_MOUNT.is_dir():
        _copy_tree_with_secure_permissions(SSH_HOME_MOUNT, SSH_RUNTIME_DIR)

    explicit_key = str(endpoint.get("ssh_private_key_path") or "").strip()
    if explicit_key:
        source_key = Path(explicit_key).expanduser().resolve()
        SSH_RUNTIME_PRIVATE_KEY.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_key, SSH_RUNTIME_PRIVATE_KEY)
        SSH_RUNTIME_PRIVATE_KEY.chmod(0o600)
        staged_endpoint["ssh_private_key_path"] = str(SSH_RUNTIME_PRIVATE_KEY)

    explicit_known_hosts = str(endpoint.get("ssh_known_hosts_path") or "").strip()
    if explicit_known_hosts:
        source_known_hosts = Path(explicit_known_hosts).expanduser().resolve()
        SSH_RUNTIME_KNOWN_HOSTS.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_known_hosts, SSH_RUNTIME_KNOWN_HOSTS)
        SSH_RUNTIME_KNOWN_HOSTS.chmod(0o600)
        staged_endpoint["ssh_known_hosts_path"] = str(SSH_RUNTIME_KNOWN_HOSTS)

    _chmod_if_exists(SSH_RUNTIME_DIR / "config", 0o600)
    _chmod_if_exists(SSH_RUNTIME_DIR / "known_hosts", 0o600)
    _chmod_if_exists(SSH_RUNTIME_DIR / "authorized_keys", 0o600)

    for candidate in SSH_RUNTIME_DIR.iterdir():
        if candidate.is_dir():
            candidate.chmod(0o700)
            continue
        if not candidate.is_file():
            continue
        if candidate.suffix == ".pub":
            candidate.chmod(0o644)
            continue
        if candidate.name in {"known_hosts", "known_hosts.old"}:
            candidate.chmod(0o600)
            continue
        candidate.chmod(0o600)

    return staged_endpoint


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print("Usage: python -m tunnel_admin.worker /path/to/endpoint.json", file=sys.stderr)
        return 2

    configure_logging()
    config_path = Path(args[0]).expanduser().resolve()
    endpoint, connect_timeout_seconds = load_runtime_config(config_path)
    endpoint = _stage_runtime_ssh_material(endpoint)
    state_file = Path(
        os.getenv("TUNNEL_RUNTIME_STATE_FILE") or str(config_path.with_name("runtime.json"))
    ).expanduser().resolve()
    commands_dir = Path(
        os.getenv("TUNNEL_COMMANDS_DIR") or str(config_path.with_name("commands"))
    ).expanduser().resolve()

    session_store = InMemorySessionStore()
    stop_event = threading.Event()
    mirror = RuntimeStateMirror(endpoint, None, state_file, commands_dir)

    def handle_event(event_name: str, data: dict[str, Any]) -> None:
        LOGGER.info("event=%s payload=%s", event_name, json.dumps(data, ensure_ascii=False, separators=(",", ":")))
        mirror.write_snapshot()

    def handle_status(endpoint_id: int, message: str | None) -> None:
        mirror.set_status_message(message)
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
    mirror.engine = engine

    started, message = engine.start_endpoint(endpoint)
    if not started:
        mirror.set_phase("error")
        mirror.set_status_message(message)
        mirror.write_snapshot()
        LOGGER.error("failed_to_start endpoint=%s reason=%s", endpoint.get("name"), message)
        return 1
    mirror.set_phase("running")
    mirror.set_status_message(None)
    mirror.start()

    def request_shutdown(signum: int, _frame: Any) -> None:
        LOGGER.info("received signal=%s, stopping worker", signum)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, request_shutdown)

    try:
        stop_event.wait()
    finally:
        mirror.set_phase("stopping")
        engine.shutdown()
        mirror.set_phase("stopped")
        mirror.set_status_message(None)
        mirror.write_snapshot()
        mirror.request_stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
