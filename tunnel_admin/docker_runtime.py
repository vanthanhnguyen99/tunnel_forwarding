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
STARTUP_READY_TIMEOUT_SECONDS = 20.0
STARTUP_POLL_INTERVAL_SECONDS = 0.25


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
        docker_use_sudo: bool,
        apply_iptables_on_endpoint_start: bool,
        iptables_source_subnet: str,
        iptables_input_interface: str,
        iptables_output_interface: str,
        iptables_use_sudo: bool,
    ) -> None:
        self.database = database
        self.status_callback = status_callback
        self.docker_network_name = docker_network_name
        self.docker_network_subnet = docker_network_subnet
        self.docker_runner_image = docker_runner_image
        self.docker_use_sudo = docker_use_sudo
        self.apply_iptables_on_endpoint_start = apply_iptables_on_endpoint_start
        self.iptables_source_subnet = iptables_source_subnet
        self.iptables_input_interface = iptables_input_interface
        self.iptables_output_interface = iptables_output_interface
        self.iptables_use_sudo = iptables_use_sudo

    def shutdown(self) -> None:
        return None

    def start_endpoint(self, endpoint: dict[str, Any]) -> tuple[bool, str | None]:
        endpoint_id = int(endpoint["id"])
        if shutil.which("docker") is None:
            message = "Docker CLI is not available on this host"
            self.status_callback(endpoint_id, message)
            return False, message
        if self.docker_use_sudo and shutil.which("sudo") is None:
            message = "sudo CLI is not available on this host"
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
            message = self._augment_permission_message(message, tool="docker")
            self.status_callback(endpoint_id, message)
            return False, message

        ready, ready_message = self._wait_for_endpoint_ready(endpoint)
        if not ready:
            self._run_compose(endpoint, "down", "--remove-orphans", "--timeout", "2")
            self.status_callback(endpoint_id, ready_message)
            return False, ready_message

        iptables_ready, iptables_message = self._apply_iptables_rules()
        if not iptables_ready:
            self._run_compose(endpoint, "down", "--remove-orphans", "--timeout", "2")
            self.status_callback(endpoint_id, iptables_message)
            return False, iptables_message

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
            message = self._augment_permission_message(message, tool="docker")
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

    def _wait_for_endpoint_ready(self, endpoint: dict[str, Any]) -> tuple[bool, str | None]:
        deadline = time.monotonic() + STARTUP_READY_TIMEOUT_SECONDS
        last_phase = ""
        last_status_message = ""

        while time.monotonic() < deadline:
            compose_state = self._read_compose_state(endpoint)
            runtime_state = self._read_runtime_state(endpoint)
            phase = str(runtime_state.get("phase") or "").strip().lower()
            status_message = str(runtime_state.get("status_message") or "").strip()

            if phase:
                last_phase = phase
            if status_message:
                last_status_message = status_message

            if phase == "running":
                return True, None
            if phase == "error":
                return False, status_message or "Endpoint worker failed during startup"
            if compose_state == "stopped":
                if status_message:
                    return False, status_message
                if phase and phase != "running":
                    return False, f"Endpoint worker stopped during startup with phase={phase}"
                return False, "Container exited before endpoint runtime became ready"

            time.sleep(STARTUP_POLL_INTERVAL_SECONDS)

        if last_status_message:
            return False, last_status_message
        if last_phase and last_phase != "running":
            return False, f"Timed out waiting for endpoint runtime readiness; last phase={last_phase}"
        return False, "Timed out waiting for endpoint runtime readiness"

    def _run_compose(self, endpoint: dict[str, Any], *args: str) -> subprocess.CompletedProcess[str]:
        compose_path = self._compose_path(endpoint)
        if compose_path is None:
            return subprocess.CompletedProcess(args=["docker", "compose"], returncode=1, stdout="", stderr="compose path missing")
        command = [*self._docker_prefix(), "docker", "compose", "-f", str(compose_path), *args]
        return subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

    def _ensure_network_exists(self) -> tuple[bool, str | None]:
        inspect = subprocess.run(
            [*self._docker_prefix(), "docker", "network", "inspect", self.docker_network_name],
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
                *self._docker_prefix(),
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
            message = self._augment_permission_message(message, tool="docker")
            return False, message
        return True, None

    def _ensure_runner_image(self) -> tuple[bool, str | None]:
        inspect = subprocess.run(
            [*self._docker_prefix(), "docker", "image", "inspect", self.docker_runner_image],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if inspect.returncode == 0:
            return True, None

        build = subprocess.run(
            [
                *self._docker_prefix(),
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
            message = self._augment_permission_message(message, tool="docker")
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

    def _docker_prefix(self) -> list[str]:
        return ["sudo", "-n"] if self.docker_use_sudo else []

    def _augment_permission_message(self, message: str, *, tool: str) -> str:
        normalized = message.lower()
        if "permission denied" not in normalized and "must be root" not in normalized:
            return message
        if tool == "docker":
            if self.docker_use_sudo:
                return (
                    f"{message}. Configure passwordless sudo for docker, "
                    "or run the admin process as root."
                )
            return (
                f"{message}. Run the admin process as root, add the service user to the docker group, "
                "or set `APP_DOCKER_USE_SUDO=1` and allow passwordless sudo for docker."
            )
        if self.iptables_use_sudo:
            return (
                f"{message}. Configure passwordless sudo for iptables, "
                "or run the admin process as root."
            )
        return (
            f"{message}. Run the admin process as root, or set "
            "`APP_IPTABLES_USE_SUDO=1` and allow passwordless sudo for iptables."
        )

    def _apply_iptables_rules(self) -> tuple[bool, str | None]:
        if not self.apply_iptables_on_endpoint_start:
            return True, None
        if shutil.which("iptables") is None:
            return False, "iptables CLI is not available on this host"
        if self.iptables_use_sudo and shutil.which("sudo") is None:
            return False, "sudo CLI is not available on this host"

        prefix = ["sudo", "-n"] if self.iptables_use_sudo else []

        commands = [
            [*prefix, "iptables", "-w", "-F"],
            [*prefix, "iptables", "-w", "-t", "nat", "-F"],
            [*prefix, "iptables", "-w", "-A", "FORWARD", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
            [
                *prefix,
                "iptables",
                "-w",
                "-A",
                "FORWARD",
                "-i",
                self.iptables_input_interface,
                "-o",
                self.iptables_output_interface,
                "-s",
                self.iptables_source_subnet,
                "-d",
                self.docker_network_subnet,
                "-j",
                "ACCEPT",
            ],
            [*prefix, "iptables", "-w", "-A", "FORWARD", "-i", self.iptables_input_interface, "-j", "DROP"],
            [
                *prefix,
                "iptables",
                "-w",
                "-t",
                "nat",
                "-A",
                "POSTROUTING",
                "-s",
                self.iptables_source_subnet,
                "-d",
                self.docker_network_subnet,
                "-o",
                self.iptables_output_interface,
                "-j",
                "MASQUERADE",
            ],
        ]
        for command in commands:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if result.returncode == 0:
                continue
            message = _decode_text(result.stderr) or _decode_text(result.stdout) or "iptables command failed"
            message = self._augment_permission_message(message, tool="iptables")
            return False, f"Failed to apply iptables rule `{ ' '.join(command) }`: {message}"
        return True, None

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
