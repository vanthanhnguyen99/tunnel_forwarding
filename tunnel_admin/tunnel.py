from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import logging
import shlex
import shutil
import socket
import subprocess
import threading
import time
from typing import Any, Callable


LOGGER = logging.getLogger("tunnel_admin.tunnel")
WILDCARD_HOSTS = {"0.0.0.0", "::", "*", ""}
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _endpoint_log_prefix(endpoint: dict[str, Any]) -> str:
    ssh_target = "unconfigured"
    if endpoint.get("ssh_host") and endpoint.get("ssh_username"):
        ssh_target = f"{endpoint['ssh_username']}@{endpoint['ssh_host']}:{endpoint.get('ssh_port') or 22}"
    return (
        f"[endpoint={endpoint['name']} listen={endpoint['listen_host']}:{endpoint['listen_port']} "
        f"dst={endpoint['destination_host']}:{endpoint['destination_port']} via={ssh_target}]"
    )


def _decode_output(raw: bytes | str | None) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    return raw.decode("utf-8", errors="replace").strip()


def _split_host_port(address: str) -> tuple[str, int]:
    text = address.strip()
    if text.startswith("[") and "]:" in text:
        host, _, port = text[1:].rpartition("]:")
        return host, int(port)
    host, _, port = text.rpartition(":")
    return host, int(port)


@dataclass(slots=True)
class EndpointRuntime:
    endpoint_id: int
    endpoint: dict[str, Any]
    process: subprocess.Popen[bytes]
    stop_requested: bool = False
    stderr_lines: list[str] = field(default_factory=list)
    stderr_lock: threading.Lock = field(default_factory=threading.Lock)
    stderr_thread: threading.Thread | None = None
    wait_thread: threading.Thread | None = None

    def append_stderr(self, line: str) -> None:
        if not line:
            return
        with self.stderr_lock:
            self.stderr_lines.append(line)
            self.stderr_lines = self.stderr_lines[-10:]

    def stderr_summary(self) -> str | None:
        with self.stderr_lock:
            if not self.stderr_lines:
                return None
            return " | ".join(self.stderr_lines[-3:])


@dataclass(slots=True)
class SessionRuntime:
    session_id: int
    endpoint_id: int
    endpoint_name: str
    client_ip: str
    client_port: int
    local_ip: str
    local_port: int
    destination_host: str
    destination_port: int
    ssh_target: str
    connected_at: str
    bytes_up: int = 0
    bytes_down: int = 0
    close_reason: str = ""

    def update_counters(self, *, bytes_received: int, bytes_sent: int) -> None:
        self.bytes_up = max(0, int(bytes_received))
        self.bytes_down = max(0, int(bytes_sent))

    def snapshot(self, status: str = "active") -> dict[str, Any]:
        return {
            "id": self.session_id,
            "endpoint_id": self.endpoint_id,
            "endpoint_name": self.endpoint_name,
            "client_ip": self.client_ip,
            "client_port": self.client_port,
            "local_ip": self.local_ip,
            "local_port": self.local_port,
            "upstream_ip": self.destination_host,
            "upstream_port": self.destination_port,
            "status": status,
            "bytes_up": self.bytes_up,
            "bytes_down": self.bytes_down,
            "connected_at": self.connected_at,
            "close_reason": self.close_reason or None,
            "ssh_target": self.ssh_target,
        }


class TunnelEngine:
    def __init__(
        self,
        database: Any,
        connect_timeout_seconds: float,
        event_callback: Callable[[str, dict[str, Any]], None],
        status_callback: Callable[[int, str | None], None],
    ) -> None:
        self.database = database
        self.connect_timeout_seconds = connect_timeout_seconds
        self.event_callback = event_callback
        self.status_callback = status_callback
        self._endpoint_runtimes: dict[int, EndpointRuntime] = {}
        self._sessions_by_key: dict[tuple[Any, ...], SessionRuntime] = {}
        self._sessions_by_id: dict[int, SessionRuntime] = {}
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._last_refresh_at = 0.0

    def shutdown(self) -> None:
        endpoint_ids = list(self._endpoint_runtimes.keys())
        for endpoint_id in endpoint_ids:
            self.stop_endpoint(endpoint_id, reason="service_shutdown", silence_missing=True)

    def start_endpoint(self, endpoint: dict[str, Any]) -> tuple[bool, str | None]:
        endpoint_id = int(endpoint["id"])
        self.stop_endpoint(endpoint_id, reason="endpoint_reconfigured", silence_missing=True)

        if shutil.which("ssh") is None:
            message = "OpenSSH client binary 'ssh' is not available on this host"
            self.status_callback(endpoint_id, message)
            return False, message

        validation_error = self._validate_endpoint(endpoint)
        if validation_error:
            self.status_callback(endpoint_id, validation_error)
            return False, validation_error

        probe_ok, probe_message = self._probe_endpoint(endpoint)
        if not probe_ok:
            self.status_callback(endpoint_id, probe_message)
            return False, probe_message

        try:
            process = self._spawn_tunnel_process(endpoint)
        except OSError as exc:
            message = f"SSH tunnel failed to launch: {exc}"
            self.status_callback(endpoint_id, message)
            return False, message

        time.sleep(0.35)
        if process.poll() is not None:
            stderr_message = _decode_output(process.stderr.read() if process.stderr is not None else "")
            message = stderr_message or f"SSH tunnel exited immediately with code {process.returncode}"
            self.status_callback(endpoint_id, message)
            return False, message

        runtime = EndpointRuntime(
            endpoint_id=endpoint_id,
            endpoint=endpoint,
            process=process,
        )
        runtime.stderr_thread = threading.Thread(
            target=self._drain_endpoint_stderr,
            args=(runtime,),
            daemon=True,
        )
        runtime.wait_thread = threading.Thread(
            target=self._watch_endpoint_process,
            args=(runtime,),
            daemon=True,
        )
        runtime.stderr_thread.start()
        runtime.wait_thread.start()

        with self._lock:
            self._endpoint_runtimes[endpoint_id] = runtime
        self.status_callback(endpoint_id, None)
        LOGGER.info("%s ssh tunnel started", _endpoint_log_prefix(endpoint))
        return True, None

    def stop_endpoint(
        self,
        endpoint_id: int,
        reason: str = "endpoint_stopped",
        silence_missing: bool = False,
    ) -> bool:
        with self._lock:
            runtime = self._endpoint_runtimes.pop(endpoint_id, None)

        if runtime is None:
            self._close_sessions_for_endpoint(endpoint_id, reason)
            return False if silence_missing else False

        runtime.stop_requested = True
        self._terminate_process(runtime.process)
        self._close_sessions_for_endpoint(endpoint_id, reason)
        LOGGER.info("%s ssh tunnel stopped", _endpoint_log_prefix(runtime.endpoint))
        return True

    def is_endpoint_running(self, endpoint_id: int) -> bool:
        with self._lock:
            return endpoint_id in self._endpoint_runtimes

    def running_endpoint_ids(self) -> set[int]:
        with self._lock:
            return set(self._endpoint_runtimes.keys())

    def list_active_sessions(self, endpoint_id: int | None = None) -> list[dict[str, Any]]:
        self._refresh_runtime_sessions(force=True)
        with self._lock:
            sessions = list(self._sessions_by_id.values())
        snapshots = []
        for session in sessions:
            if endpoint_id is not None and session.endpoint_id != endpoint_id:
                continue
            snapshots.append(session.snapshot(status="active"))
        return sorted(snapshots, key=lambda row: row["connected_at"], reverse=True)

    def disconnect_session(self, session_id: int, reason: str = "admin_disconnect") -> bool:
        self._refresh_runtime_sessions(force=True)
        with self._lock:
            session = self._sessions_by_id.get(session_id)
        if session is None:
            return False

        command = [
            "ss",
            "-K",
            "src",
            session.local_ip,
            "sport",
            "=",
            str(session.local_port),
            "dst",
            session.client_ip,
            "dport",
            "=",
            str(session.client_port),
        ]
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=2.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False

        if result.returncode != 0 and "No such file" not in (result.stderr or ""):
            return False

        self._finalize_session(session, reason)
        return True

    def collect_runtime_metrics(self) -> dict[str, Any]:
        self._refresh_runtime_sessions(force=True)
        with self._lock:
            sessions = list(self._sessions_by_id.values())
            endpoint_ids = set(self._endpoint_runtimes.keys())
        per_endpoint: dict[int, dict[str, int]] = {
            endpoint_id: {"active_connections": 0, "bytes_up": 0, "bytes_down": 0}
            for endpoint_id in endpoint_ids
        }
        overall = {"active_connections": 0, "bytes_up": 0, "bytes_down": 0}

        for session in sessions:
            snapshot = session.snapshot(status="active")
            metrics = per_endpoint.setdefault(
                session.endpoint_id,
                {"active_connections": 0, "bytes_up": 0, "bytes_down": 0},
            )
            metrics["active_connections"] += 1
            metrics["bytes_up"] += int(snapshot["bytes_up"])
            metrics["bytes_down"] += int(snapshot["bytes_down"])
            overall["active_connections"] += 1
            overall["bytes_up"] += int(snapshot["bytes_up"])
            overall["bytes_down"] += int(snapshot["bytes_down"])

        return {"overall": overall, "per_endpoint": per_endpoint}

    def _refresh_runtime_sessions(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_refresh_at < 0.5:
            return

        with self._refresh_lock:
            now = time.monotonic()
            if not force and now - self._last_refresh_at < 0.5:
                return

            with self._lock:
                runtimes = {endpoint_id: runtime for endpoint_id, runtime in self._endpoint_runtimes.items()}

            if not runtimes:
                self._close_all_sessions("endpoint_stopped")
                self._last_refresh_at = now
                return

            ss_entries = self._read_ss_established_entries()
            current_keys: set[tuple[Any, ...]] = set()

            for entry in ss_entries:
                endpoint = self._match_endpoint(runtimes, entry["local_ip"], entry["local_port"])
                if endpoint is None:
                    continue

                key = (
                    int(endpoint["id"]),
                    entry["client_ip"],
                    entry["client_port"],
                    entry["local_ip"],
                    entry["local_port"],
                )
                current_keys.add(key)

                with self._lock:
                    session = self._sessions_by_key.get(key)

                if session is None:
                    session_id = self.database.create_session_record(
                        endpoint_id=int(endpoint["id"]),
                        client_ip=str(entry["client_ip"]),
                        client_port=int(entry["client_port"]),
                        upstream_ip=str(endpoint["destination_host"]),
                        upstream_port=int(endpoint["destination_port"]),
                    )
                    session = SessionRuntime(
                        session_id=session_id,
                        endpoint_id=int(endpoint["id"]),
                        endpoint_name=str(endpoint["name"]),
                        client_ip=str(entry["client_ip"]),
                        client_port=int(entry["client_port"]),
                        local_ip=str(entry["local_ip"]),
                        local_port=int(entry["local_port"]),
                        destination_host=str(endpoint["destination_host"]),
                        destination_port=int(endpoint["destination_port"]),
                        ssh_target=f"{endpoint['ssh_username']}@{endpoint['ssh_host']}:{endpoint.get('ssh_port') or 22}",
                        connected_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    )
                    session.update_counters(
                        bytes_received=int(entry["bytes_received"]),
                        bytes_sent=int(entry["bytes_sent"]),
                    )
                    with self._lock:
                        self._sessions_by_key[key] = session
                        self._sessions_by_id[session.session_id] = session
                    self.event_callback("session.opened", session.snapshot(status="active"))
                else:
                    session.update_counters(
                        bytes_received=int(entry["bytes_received"]),
                        bytes_sent=int(entry["bytes_sent"]),
                    )

            with self._lock:
                stale_keys = [
                    key
                    for key, session in self._sessions_by_key.items()
                    if session.endpoint_id in runtimes and key not in current_keys
                ]

            for key in stale_keys:
                with self._lock:
                    session = self._sessions_by_key.get(key)
                if session is not None:
                    self._finalize_session(session, "closed")

            self._last_refresh_at = now

    def _read_ss_established_entries(self) -> list[dict[str, Any]]:
        try:
            result = subprocess.run(
                ["ss", "-tinH", "state", "established"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=2.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            if stderr and "netlink" not in stderr.lower():
                LOGGER.warning("ss query failed: %s", stderr)
            return []

        return self._parse_ss_output(result.stdout)

    def _parse_ss_output(self, output: str) -> list[dict[str, Any]]:
        blocks: list[tuple[str, str]] = []
        header_line: str | None = None
        for raw_line in output.splitlines():
            if not raw_line.strip():
                continue
            if raw_line[:1].isspace():
                if header_line is not None:
                    blocks.append((header_line, raw_line.strip()))
                    header_line = None
            else:
                if header_line is not None:
                    blocks.append((header_line, ""))
                header_line = raw_line.strip()
        if header_line is not None:
            blocks.append((header_line, ""))

        entries: list[dict[str, Any]] = []
        for header, stats in blocks:
            tokens = header.split()
            if len(tokens) < 2:
                continue
            try:
                local_ip, local_port = _split_host_port(tokens[-2])
                client_ip, client_port = _split_host_port(tokens[-1])
            except (ValueError, IndexError):
                continue

            bytes_sent = 0
            bytes_received = 0
            for stat_token in stats.split():
                if stat_token.startswith("bytes_sent:"):
                    try:
                        bytes_sent = int(stat_token.split(":", 1)[1])
                    except ValueError:
                        bytes_sent = 0
                elif stat_token.startswith("bytes_received:"):
                    try:
                        bytes_received = int(stat_token.split(":", 1)[1])
                    except ValueError:
                        bytes_received = 0

            entries.append(
                {
                    "local_ip": local_ip,
                    "local_port": local_port,
                    "client_ip": client_ip,
                    "client_port": client_port,
                    "bytes_sent": bytes_sent,
                    "bytes_received": bytes_received,
                }
            )
        return entries

    def _match_endpoint(
        self,
        runtimes: dict[int, EndpointRuntime],
        local_ip: str,
        local_port: int,
    ) -> dict[str, Any] | None:
        for runtime in runtimes.values():
            endpoint = runtime.endpoint
            if int(endpoint["listen_port"]) != int(local_port):
                continue
            listen_host = str(endpoint["listen_host"]).strip().lower()
            candidate_ip = str(local_ip).strip().lower()
            if listen_host in WILDCARD_HOSTS:
                return endpoint
            if listen_host in LOOPBACK_HOSTS and candidate_ip in {"127.0.0.1", "::1"}:
                return endpoint
            if listen_host == candidate_ip:
                return endpoint
        return None

    def _close_all_sessions(self, reason: str) -> None:
        with self._lock:
            sessions = list(self._sessions_by_id.values())
        for session in sessions:
            self._finalize_session(session, reason)

    def _close_sessions_for_endpoint(self, endpoint_id: int, reason: str) -> None:
        with self._lock:
            sessions = [session for session in self._sessions_by_id.values() if session.endpoint_id == endpoint_id]
        for session in sessions:
            self._finalize_session(session, reason)

    def _finalize_session(self, session: SessionRuntime, reason: str) -> None:
        session.close_reason = reason
        key = (
            session.endpoint_id,
            session.client_ip,
            session.client_port,
            session.local_ip,
            session.local_port,
        )
        with self._lock:
            self._sessions_by_key.pop(key, None)
            self._sessions_by_id.pop(session.session_id, None)
        self.database.close_session_record(
            session_id=session.session_id,
            status="closed",
            bytes_up=int(session.bytes_up),
            bytes_down=int(session.bytes_down),
            close_reason=reason,
        )
        self.event_callback("session.closed", session.snapshot(status="closed"))

    def _validate_endpoint(self, endpoint: dict[str, Any]) -> str | None:
        if str(endpoint.get("tunnel_type") or "") != "ssh_local_forward":
            return "Legacy direct-forward endpoints are no longer supported; recreate as SSH Local Forward"
        if not endpoint.get("ssh_host"):
            return "ssh_host is required"
        if not endpoint.get("ssh_username"):
            return "ssh_username is required"
        return None

    def _probe_endpoint(self, endpoint: dict[str, Any]) -> tuple[bool, str | None]:
        command = self._build_ssh_probe_command(endpoint)
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=max(5.0, self.connect_timeout_seconds + 3.0),
                check=False,
            )
        except subprocess.TimeoutExpired:
            return False, "SSH probe timed out"
        except OSError as exc:
            return False, f"SSH probe failed to launch: {exc}"

        if result.returncode == 0:
            return True, None

        stderr_message = _decode_output(result.stderr) or _decode_output(result.stdout)
        if stderr_message:
            return False, f"SSH probe failed: {stderr_message}"
        return False, f"SSH probe failed with exit code {result.returncode}"

    def _build_ssh_probe_command(self, endpoint: dict[str, Any]) -> list[str]:
        return [
            "ssh",
            *self._build_ssh_common_options(endpoint),
            self._build_ssh_target(endpoint),
            "true",
        ]

    def _spawn_tunnel_process(self, endpoint: dict[str, Any]) -> subprocess.Popen[bytes]:
        listen_spec = (
            f"{endpoint['listen_host']}:{endpoint['listen_port']}:"
            f"{endpoint['destination_host']}:{endpoint['destination_port']}"
        )
        command = [
            "ssh",
            *self._build_ssh_common_options(endpoint),
            "-N",
            "-L",
            listen_spec,
            self._build_ssh_target(endpoint),
        ]
        if str(endpoint["listen_host"]).strip().lower() not in LOOPBACK_HOSTS:
            command.insert(1, "-g")

        return subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            close_fds=True,
            start_new_session=True,
        )

    def _build_ssh_target(self, endpoint: dict[str, Any]) -> str:
        return f"{endpoint['ssh_username']}@{endpoint['ssh_host']}"

    def _build_ssh_common_options(self, endpoint: dict[str, Any]) -> list[str]:
        options = [
            "-T",
            "-p",
            str(endpoint.get("ssh_port") or 22),
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={int(max(1.0, self.connect_timeout_seconds))}",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=2",
            "-o",
            "TCPKeepAlive=yes",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "LogLevel=ERROR",
            "-o",
            "StrictHostKeyChecking=accept-new",
        ]
        if endpoint.get("ssh_private_key_path"):
            options.extend(["-i", str(endpoint["ssh_private_key_path"])])
        if endpoint.get("ssh_known_hosts_path"):
            options.extend(["-o", f"UserKnownHostsFile={endpoint['ssh_known_hosts_path']}"])
        if endpoint.get("ssh_options"):
            options.extend(shlex.split(str(endpoint["ssh_options"])))
        return options

    def _drain_endpoint_stderr(self, runtime: EndpointRuntime) -> None:
        if runtime.process.stderr is None:
            return
        while True:
            try:
                chunk = runtime.process.stderr.readline()
            except OSError:
                break
            if not chunk:
                break
            runtime.append_stderr(_decode_output(chunk))

    def _watch_endpoint_process(self, runtime: EndpointRuntime) -> None:
        return_code = runtime.process.wait()
        if runtime.stop_requested:
            return

        with self._lock:
            current = self._endpoint_runtimes.get(runtime.endpoint_id)
            if current is runtime:
                self._endpoint_runtimes.pop(runtime.endpoint_id, None)

        message = runtime.stderr_summary() or f"SSH tunnel exited with code {return_code}"
        self.status_callback(runtime.endpoint_id, message)
        self._close_sessions_for_endpoint(runtime.endpoint_id, "ssh_tunnel_exited")
        self.event_callback(
            "endpoint.stopped",
            {
                "id": runtime.endpoint_id,
                "name": runtime.endpoint["name"],
                "runtime_status": "stopped",
                "status_message": message,
            },
        )

    @staticmethod
    def _terminate_process(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1.0)
        except OSError:
            pass
