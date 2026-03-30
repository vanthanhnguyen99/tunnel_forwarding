from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any

from .config import ROOT_DIR


LOGGER = logging.getLogger("tunnel_admin.docker_runtime")

SESSION_ID_FACTOR = 1_000_000


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decode_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return value.decode("utf-8", errors="replace").strip()


class DockerTunnelManager:
    def __init__(
        self,
        database: Any,
        status_callback: Any,
        docker_network_name: str,
        docker_network_subnet: str,
        docker_runner_image: str,
    ) -> None:
        self.database = database
        self.status_callback = status_callback
        self.docker_network_name = docker_network_name
        self.docker_network_subnet = docker_network_subnet
        self.docker_runner_image = docker_runner_image

    def shutdown(self) -> None:
        return None

    def start_endpoint(self, endpoint: dict[str, Any]) -> tuple[bool, str | None]:
        endpoint_id = int(endpoint["id"])
        if shutil.which("docker") is None:
            message = "Docker CLI is not available on this host"
            self.status_callback(endpoint_id, message)
            return False, message

        compose_path = self._compose_path(endpoint)
        if compose_path is None or not compose_path.exists():
            message = "Docker compose config has not been generated for this endpoint"
            self.status_callback(endpoint_id, message)
            return False, message

        network_ready, network_message = self._ensure_network_exists()
        if not network_ready:
            self.status_callback(endpoint_id, network_message)
            return False, network_message

        image_ready, image_message = self._ensure_runner_image()
        if not image_ready:
            self.status_callback(endpoint_id, image_message)
            return False, image_message

        result = self._run_compose(endpoint, "up", "-d", "--no-build", "--force-recreate", "--remove-orphans")
        if result.returncode != 0:
            message = _decode_text(result.stderr) or _decode_text(result.stdout) or "docker compose up failed"
            self.status_callback(endpoint_id, message)
            return False, message

        if not self.is_endpoint_running(endpoint_id):
            message = "Container did not reach running state after docker compose up"
            self.status_callback(endpoint_id, message)
            return False, message

        self.status_callback(endpoint_id, None)
        return True, None

    def stop_endpoint(
        self,
        endpoint_id: int,
        reason: str = "endpoint_stopped",
        silence_missing: bool = False,
    ) -> bool:
        del reason
        endpoint = self.database.get_endpoint(endpoint_id)
        if endpoint is None:
            return False

        compose_path = self._compose_path(endpoint)
        if compose_path is None or not compose_path.exists():
            return False if silence_missing else False

        result = self._run_compose(endpoint, "down", "--remove-orphans", "--timeout", "2")
        if result.returncode != 0 and not silence_missing:
            message = _decode_text(result.stderr) or _decode_text(result.stdout) or "docker compose down failed"
            self.status_callback(endpoint_id, message)
            return False

        self.status_callback(endpoint_id, None)
        return True

    def is_endpoint_running(self, endpoint_id: int) -> bool:
        endpoint = self.database.get_endpoint(endpoint_id)
        if endpoint is None:
            return False
        return self._read_compose_state(endpoint) == "running"

    def list_active_sessions(self, endpoint_id: int | None = None) -> list[dict[str, Any]]:
        endpoints = self._target_endpoints(endpoint_id)
        sessions: list[dict[str, Any]] = []
        for endpoint in endpoints:
            if not self.is_endpoint_running(int(endpoint["id"])):
                continue
            runtime_state = self._read_runtime_state(endpoint)
            for session in runtime_state.get("active_sessions", []):
                try:
                    local_session_id = int(session.get("local_session_id") or session.get("id"))
                except (TypeError, ValueError):
                    continue
                global_session_id = int(endpoint["id"]) * SESSION_ID_FACTOR + local_session_id
                sessions.append(
                    {
                        **session,
                        "id": global_session_id,
                        "worker_session_id": local_session_id,
                        "endpoint_id": int(endpoint["id"]),
                        "endpoint_name": endpoint["name"],
                    }
                )
        return sorted(sessions, key=lambda row: row.get("connected_at") or "", reverse=True)

    def disconnect_session(self, session_id: int, reason: str = "admin_disconnect") -> bool:
        del reason
        endpoint_id = int(session_id) // SESSION_ID_FACTOR
        local_session_id = int(session_id) % SESSION_ID_FACTOR
        if endpoint_id <= 0 or local_session_id <= 0:
            return False

        endpoint = self.database.get_endpoint(endpoint_id)
        if endpoint is None or not self.is_endpoint_running(endpoint_id):
            return False

        commands_dir = self._commands_dir(endpoint)
        commands_dir.mkdir(parents=True, exist_ok=True)
        command_path = commands_dir / f"{int(time.time() * 1000)}-disconnect-{local_session_id}.json"
        command_path.write_text(
            json.dumps(
                {
                    "action": "disconnect_session",
                    "session_id": local_session_id,
                    "requested_at": _utc_now_iso(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return True

    def collect_runtime_metrics(self) -> dict[str, Any]:
        per_endpoint: dict[int, dict[str, int]] = {}
        overall = {"active_connections": 0, "bytes_up": 0, "bytes_down": 0}

        for endpoint in self.database.list_endpoints():
            runtime_state = self._read_runtime_state(endpoint)
            metrics = runtime_state.get("metrics") or {}
            endpoint_metrics = {
                "active_connections": int(metrics.get("active_connections") or 0),
                "bytes_up": int(metrics.get("bytes_up") or 0),
                "bytes_down": int(metrics.get("bytes_down") or 0),
            }
            per_endpoint[int(endpoint["id"])] = endpoint_metrics
            overall["active_connections"] += endpoint_metrics["active_connections"]
            overall["bytes_up"] += endpoint_metrics["bytes_up"]
            overall["bytes_down"] += endpoint_metrics["bytes_down"]

        return {"overall": overall, "per_endpoint": per_endpoint}

    def get_endpoint_runtime_details(self, endpoint: dict[str, Any]) -> dict[str, Any]:
        runtime_state = self._read_runtime_state(endpoint)
        compose_state = self._read_compose_state(endpoint)
        return {
            "compose_state": compose_state,
            "status_message": runtime_state.get("status_message"),
            "metrics": runtime_state.get("metrics") or {},
        }

    def _target_endpoints(self, endpoint_id: int | None) -> list[dict[str, Any]]:
        if endpoint_id is None:
            return self.database.list_endpoints()
        endpoint = self.database.get_endpoint(endpoint_id)
        return [endpoint] if endpoint is not None else []

    def _compose_path(self, endpoint: dict[str, Any]) -> Path | None:
        candidate = str(endpoint.get("docker_compose_path") or "").strip()
        if not candidate:
            return None
        return Path(candidate).expanduser().resolve()

    def _runtime_state_path(self, endpoint: dict[str, Any]) -> Path:
        candidate = str(endpoint.get("docker_endpoint_config_path") or "").strip()
        if candidate:
            return Path(candidate).expanduser().resolve().with_name("runtime.json")
        compose_path = self._compose_path(endpoint)
        if compose_path is not None:
            return compose_path.with_name("runtime.json")
        endpoint_id = int(endpoint["id"])
        return Path.cwd() / "data" / "docker" / f"endpoint-{endpoint_id}" / "runtime.json"

    def _commands_dir(self, endpoint: dict[str, Any]) -> Path:
        return self._runtime_state_path(endpoint).with_name("commands")

    def _read_runtime_state(self, endpoint: dict[str, Any]) -> dict[str, Any]:
        runtime_path = self._runtime_state_path(endpoint)
        if not runtime_path.exists():
            return {}
        try:
            payload = json.loads(runtime_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOGGER.warning("Failed to read runtime state %s", runtime_path)
            return {}
        return payload if isinstance(payload, dict) else {}

    def _read_compose_state(self, endpoint: dict[str, Any]) -> str:
        result = self._run_compose(endpoint, "ps", "--format", "json")
        if result.returncode != 0:
            return "stopped"
        rows = self._parse_compose_ps_output(_decode_text(result.stdout))
        if not rows:
            return "stopped"
        state = str(rows[0].get("State") or rows[0].get("state") or "").strip().lower()
        status = str(rows[0].get("Status") or rows[0].get("status") or "").strip().lower()
        if state:
            return state
        if status.startswith("running"):
            return "running"
        return "stopped"

    def _run_compose(self, endpoint: dict[str, Any], *args: str) -> subprocess.CompletedProcess[str]:
        compose_path = self._compose_path(endpoint)
        if compose_path is None:
            return subprocess.CompletedProcess(args=["docker", "compose"], returncode=1, stdout="", stderr="compose path missing")
        command = ["docker", "compose", "-f", str(compose_path), *args]
        return subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

    def _ensure_network_exists(self) -> tuple[bool, str | None]:
        inspect = subprocess.run(
            ["docker", "network", "inspect", self.docker_network_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if inspect.returncode == 0:
            try:
                payload = json.loads(inspect.stdout)
            except json.JSONDecodeError:
                payload = []
            if isinstance(payload, list) and payload:
                config_rows = payload[0].get("IPAM", {}).get("Config", [])
                current_subnet = ""
                if config_rows and isinstance(config_rows[0], dict):
                    current_subnet = str(config_rows[0].get("Subnet") or "")
                if current_subnet and current_subnet != self.docker_network_subnet:
                    return (
                        False,
                        f"Docker network {self.docker_network_name} already exists with subnet {current_subnet}, expected {self.docker_network_subnet}",
                    )
            return True, None

        create = subprocess.run(
            [
                "docker",
                "network",
                "create",
                "--driver",
                "bridge",
                "--subnet",
                self.docker_network_subnet,
                self.docker_network_name,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if create.returncode != 0:
            message = _decode_text(create.stderr) or _decode_text(create.stdout) or "Failed to create Docker network"
            return False, message
        return True, None

    def _ensure_runner_image(self) -> tuple[bool, str | None]:
        inspect = subprocess.run(
            ["docker", "image", "inspect", self.docker_runner_image],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if inspect.returncode == 0:
            return True, None

        build = subprocess.run(
            [
                "docker",
                "build",
                "-t",
                self.docker_runner_image,
                "-f",
                str((ROOT_DIR / "Dockerfile.tunnel-runner").resolve()),
                str(ROOT_DIR),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if build.returncode != 0:
            message = (
                _decode_text(build.stderr)
                or _decode_text(build.stdout)
                or f"Failed to build Docker runner image {self.docker_runner_image}"
            )
            return False, self._format_runner_image_error(message)
        return True, None

    def _format_runner_image_error(self, message: str) -> str:
        normalized = message.lower()
        if "toomanyrequests" in normalized or "too many requests" in normalized or "429" in normalized:
            return (
                f"{message}\n"
                "Docker Hub rate limit hit while building the shared tunnel runner image. "
                "Run `docker login` on this server, or prebuild/push the image and point "
                "`APP_DOCKER_RUNNER_IMAGE` at that tag."
            )
        return message

    @staticmethod
    def _parse_compose_ps_output(output: str) -> list[dict[str, Any]]:
        text = output.strip()
        if not text:
            return []
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if isinstance(payload, dict):
            return [payload]

        rows: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows
