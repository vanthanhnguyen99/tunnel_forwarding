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
from typing import Any, BinaryIO, Callable


LOGGER = logging.getLogger("tunnel_admin.tunnel")


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


@dataclass(slots=True)
class SessionRuntime:
    session_id: int
    endpoint_id: int
    endpoint_name: str
    client_socket: socket.socket
    client_ip: str
    client_port: int
    destination_host: str
    destination_port: int
    ssh_target: str
    ssh_process: subprocess.Popen[bytes]
    ssh_stdin: BinaryIO
    ssh_stdout: BinaryIO
    ssh_stderr: BinaryIO
    connected_at: str
    idle_timeout: int
    stop_event: threading.Event = field(default_factory=threading.Event)
    close_reason: str = ""
    bytes_up: int = 0
    bytes_down: int = 0
    last_activity_at: float = field(default_factory=time.time)
    ssh_exit_code: int | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _stderr_lines: list[str] = field(default_factory=list)

    def snapshot(self, status: str = "active") -> dict[str, Any]:
        with self._lock:
            ssh_error = " | ".join(self._stderr_lines[-3:]) if self._stderr_lines else None
            return {
                "id": self.session_id,
                "endpoint_id": self.endpoint_id,
                "endpoint_name": self.endpoint_name,
                "client_ip": self.client_ip,
                "client_port": self.client_port,
                "upstream_ip": self.destination_host,
                "upstream_port": self.destination_port,
                "status": status,
                "bytes_up": self.bytes_up,
                "bytes_down": self.bytes_down,
                "connected_at": self.connected_at,
                "close_reason": self.close_reason or None,
                "ssh_target": self.ssh_target,
                "ssh_exit_code": self.ssh_exit_code,
                "ssh_error": ssh_error,
            }

    def request_stop(self, reason: str) -> None:
        with self._lock:
            if not self.close_reason:
                self.close_reason = reason
        self.stop_event.set()
        self._close_io()
        self._terminate_process()

    def run(self) -> dict[str, Any]:
        self.client_socket.settimeout(1.0)

        stderr_thread = threading.Thread(target=self._collect_stderr, daemon=True)
        upload_thread = threading.Thread(target=self._pump_client_to_ssh, daemon=True)
        download_thread = threading.Thread(target=self._pump_ssh_to_client, daemon=True)

        stderr_thread.start()
        upload_thread.start()
        download_thread.start()

        while upload_thread.is_alive() or download_thread.is_alive():
            upload_thread.join(timeout=0.25)
            download_thread.join(timeout=0.25)
            if self.stop_event.is_set():
                continue
            if self.idle_timeout and time.time() - self.last_activity_at > self.idle_timeout:
                self.request_stop("idle_timeout")
                break

        stderr_thread.join(timeout=0.5)
        self._terminate_process()
        self._close_io()

        exit_code = self.ssh_process.poll()
        with self._lock:
            self.ssh_exit_code = exit_code
            if not self.close_reason:
                if exit_code not in (None, 0):
                    self.close_reason = "ssh_failed"
                else:
                    self.close_reason = "closed"

        return self.snapshot(status="closed")

    def _collect_stderr(self) -> None:
        while True:
            try:
                chunk = self.ssh_stderr.readline()
            except OSError:
                break
            if not chunk:
                break
            line = _decode_output(chunk)
            if not line:
                continue
            with self._lock:
                self._stderr_lines.append(line)
                self._stderr_lines = self._stderr_lines[-10:]

    def _pump_client_to_ssh(self) -> None:
        while not self.stop_event.is_set():
            try:
                chunk = self.client_socket.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                self._mark_close_reason("client_unreachable")
                break

            if not chunk:
                self._mark_close_reason("client_closed")
                break

            try:
                self.ssh_stdin.write(chunk)
                self.ssh_stdin.flush()
            except OSError:
                self._mark_close_reason("ssh_pipe_broken")
                break

            self._mark_bytes("bytes_up", len(chunk))

        self.stop_event.set()

    def _pump_ssh_to_client(self) -> None:
        while not self.stop_event.is_set():
            try:
                chunk = self.ssh_stdout.read(65536)
            except OSError:
                self._mark_close_reason("ssh_pipe_broken")
                break

            if not chunk:
                self._mark_close_reason("ssh_channel_closed")
                break

            try:
                self.client_socket.sendall(chunk)
            except OSError:
                self._mark_close_reason("client_unreachable")
                break

            self._mark_bytes("bytes_down", len(chunk))

        self.stop_event.set()

    def _mark_bytes(self, attr_name: str, amount: int) -> None:
        with self._lock:
            setattr(self, attr_name, getattr(self, attr_name) + amount)
            self.last_activity_at = time.time()

    def _mark_close_reason(self, reason: str) -> None:
        with self._lock:
            if not self.close_reason:
                self.close_reason = reason

    def _close_io(self) -> None:
        try:
            self.client_socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.client_socket.close()
        except OSError:
            pass

        for pipe_handle in (self.ssh_stdin, self.ssh_stdout, self.ssh_stderr):
            try:
                pipe_handle.close()
            except OSError:
                pass

    def _terminate_process(self) -> None:
        if self.ssh_process.poll() is not None:
            return
        try:
            self.ssh_process.terminate()
            self.ssh_process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            self.ssh_process.kill()
            self.ssh_process.wait(timeout=1.0)
        except OSError:
            pass


class EndpointListener:
    def __init__(
        self,
        endpoint: dict[str, Any],
        on_client: Callable[[dict[str, Any], socket.socket, tuple[Any, ...]], None],
    ) -> None:
        self.endpoint = endpoint
        self.on_client = on_client
        self.socket: socket.socket | None = None
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()

    def start(self) -> tuple[bool, str | None]:
        bind_host = self.endpoint["listen_host"]
        bind_port = int(self.endpoint["listen_port"])
        last_error: str | None = None

        try:
            infos = socket.getaddrinfo(
                bind_host,
                bind_port,
                socket.AF_UNSPEC,
                socket.SOCK_STREAM,
                0,
                socket.AI_PASSIVE,
            )
        except OSError as exc:
            return False, f"Resolve listen host failed: {exc}"

        for family, socktype, proto, _, sockaddr in infos:
            candidate: socket.socket | None = None
            try:
                candidate = socket.socket(family, socktype, proto)
                candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                candidate.bind(sockaddr)
                candidate.listen()
                candidate.settimeout(1.0)
                self.socket = candidate
                break
            except OSError as exc:
                last_error = str(exc)
                if candidate is not None:
                    try:
                        candidate.close()
                    except OSError:
                        pass

        if self.socket is None:
            return False, last_error or "Unknown bind error"

        self.thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.thread.start()
        LOGGER.info("%s listener started", _endpoint_log_prefix(self.endpoint))
        return True, None

    def _accept_loop(self) -> None:
        assert self.socket is not None
        while not self.stop_event.is_set():
            try:
                client_socket, client_addr = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                if not self.stop_event.is_set():
                    LOGGER.exception("%s accept loop crashed", _endpoint_log_prefix(self.endpoint))
                break
            threading.Thread(
                target=self.on_client,
                args=(self.endpoint, client_socket, client_addr),
                daemon=True,
            ).start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.socket is not None:
            try:
                self.socket.close()
            except OSError:
                pass
        if self.thread is not None:
            self.thread.join(timeout=1.5)
        LOGGER.info("%s listener stopped", _endpoint_log_prefix(self.endpoint))


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
        self._listeners: dict[int, EndpointListener] = {}
        self._sessions: dict[int, SessionRuntime] = {}
        self._lock = threading.Lock()

    def shutdown(self) -> None:
        endpoint_ids = list(self._listeners.keys())
        for endpoint_id in endpoint_ids:
            self.stop_endpoint(endpoint_id, reason="service_shutdown")

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

        listener = EndpointListener(endpoint, self._handle_client)
        started, message = listener.start()
        if not started:
            self.status_callback(endpoint_id, message)
            return False, message

        with self._lock:
            self._listeners[endpoint_id] = listener
        self.status_callback(endpoint_id, None)
        return True, None

    def stop_endpoint(
        self,
        endpoint_id: int,
        reason: str = "endpoint_stopped",
        silence_missing: bool = False,
    ) -> bool:
        with self._lock:
            listener = self._listeners.pop(endpoint_id, None)
            sessions = [session for session in self._sessions.values() if session.endpoint_id == endpoint_id]

        if listener is None and not sessions:
            return False if silence_missing else False

        for session in sessions:
            session.request_stop(reason)
        if listener is not None:
            listener.stop()
        return True

    def is_endpoint_running(self, endpoint_id: int) -> bool:
        with self._lock:
            return endpoint_id in self._listeners

    def running_endpoint_ids(self) -> set[int]:
        with self._lock:
            return set(self._listeners.keys())

    def list_active_sessions(self, endpoint_id: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            sessions = list(self._sessions.values())
        snapshots = []
        for session in sessions:
            if endpoint_id is not None and session.endpoint_id != endpoint_id:
                continue
            snapshots.append(session.snapshot(status="active"))
        return sorted(snapshots, key=lambda row: row["connected_at"], reverse=True)

    def disconnect_session(self, session_id: int, reason: str = "admin_disconnect") -> bool:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            return False
        session.request_stop(reason)
        return True

    def collect_runtime_metrics(self) -> dict[str, Any]:
        with self._lock:
            sessions = list(self._sessions.values())
            endpoint_ids = set(self._listeners.keys())
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

    def _handle_client(
        self,
        endpoint: dict[str, Any],
        client_socket: socket.socket,
        client_addr: tuple[Any, ...],
    ) -> None:
        endpoint_id = int(endpoint["id"])
        client_ip = str(client_addr[0])
        client_port = int(client_addr[1])

        if not self._client_allowed(endpoint, client_ip):
            LOGGER.warning(
                "%s rejected client %s:%s due to CIDR allowlist",
                _endpoint_log_prefix(endpoint),
                client_ip,
                client_port,
            )
            try:
                client_socket.close()
            except OSError:
                pass
            return

        if self._max_clients_reached(endpoint_id, int(endpoint["max_clients"])):
            LOGGER.warning(
                "%s rejected client %s:%s due to max_clients",
                _endpoint_log_prefix(endpoint),
                client_ip,
                client_port,
            )
            try:
                client_socket.close()
            except OSError:
                pass
            return

        try:
            ssh_process = self._spawn_ssh_process(endpoint)
        except OSError as exc:
            LOGGER.warning(
                "%s failed to launch ssh process for client %s:%s: %s",
                _endpoint_log_prefix(endpoint),
                client_ip,
                client_port,
                exc,
            )
            self.status_callback(endpoint_id, f"SSH launch failed: {exc}")
            try:
                client_socket.close()
            except OSError:
                pass
            return

        if ssh_process.stdin is None or ssh_process.stdout is None or ssh_process.stderr is None:
            self.status_callback(endpoint_id, "SSH process pipes were not created")
            try:
                client_socket.close()
            except OSError:
                pass
            self._terminate_process(ssh_process)
            return

        session_id = self.database.create_session_record(
            endpoint_id=endpoint_id,
            client_ip=client_ip,
            client_port=client_port,
            upstream_ip=str(endpoint["destination_host"]),
            upstream_port=int(endpoint["destination_port"]),
        )
        session = SessionRuntime(
            session_id=session_id,
            endpoint_id=endpoint_id,
            endpoint_name=str(endpoint["name"]),
            client_socket=client_socket,
            client_ip=client_ip,
            client_port=client_port,
            destination_host=str(endpoint["destination_host"]),
            destination_port=int(endpoint["destination_port"]),
            ssh_target=f"{endpoint['ssh_username']}@{endpoint['ssh_host']}:{endpoint.get('ssh_port') or 22}",
            ssh_process=ssh_process,
            ssh_stdin=ssh_process.stdin,
            ssh_stdout=ssh_process.stdout,
            ssh_stderr=ssh_process.stderr,
            connected_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            idle_timeout=int(endpoint["idle_timeout"]),
        )

        with self._lock:
            self._sessions[session_id] = session
        self.event_callback("session.opened", session.snapshot(status="active"))
        LOGGER.info(
            "%s session opened id=%s client=%s:%s",
            _endpoint_log_prefix(endpoint),
            session_id,
            client_ip,
            client_port,
        )

        closed_snapshot = session.run()
        with self._lock:
            self._sessions.pop(session_id, None)

        if closed_snapshot.get("ssh_error"):
            self.status_callback(endpoint_id, str(closed_snapshot["ssh_error"]))

        final_status = "closed"
        if closed_snapshot["close_reason"] in {
            "ssh_failed",
            "ssh_pipe_broken",
            "client_unreachable",
        }:
            final_status = "error"

        self.database.close_session_record(
            session_id=session_id,
            status=final_status,
            bytes_up=int(closed_snapshot["bytes_up"]),
            bytes_down=int(closed_snapshot["bytes_down"]),
            close_reason=str(closed_snapshot["close_reason"] or "closed"),
        )
        self.event_callback("session.closed", closed_snapshot)
        LOGGER.info(
            "%s session closed id=%s reason=%s up=%s down=%s ssh_exit=%s",
            _endpoint_log_prefix(endpoint),
            session_id,
            closed_snapshot["close_reason"],
            closed_snapshot["bytes_up"],
            closed_snapshot["bytes_down"],
            closed_snapshot.get("ssh_exit_code"),
        )

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
            *self._build_ssh_options(endpoint),
            self._build_ssh_target(endpoint),
            "true",
        ]

    def _spawn_ssh_process(self, endpoint: dict[str, Any]) -> subprocess.Popen[bytes]:
        command = [
            "ssh",
            *self._build_ssh_options(endpoint),
            "-W",
            f"{endpoint['destination_host']}:{endpoint['destination_port']}",
            self._build_ssh_target(endpoint),
        ]
        return subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            close_fds=True,
            start_new_session=True,
        )

    def _build_ssh_target(self, endpoint: dict[str, Any]) -> str:
        return f"{endpoint['ssh_username']}@{endpoint['ssh_host']}"

    def _build_ssh_options(self, endpoint: dict[str, Any]) -> list[str]:
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

    def _client_allowed(self, endpoint: dict[str, Any], client_ip: str) -> bool:
        cidr = endpoint.get("allowed_client_cidr")
        if not cidr:
            return True
        try:
            network = ipaddress.ip_network(str(cidr), strict=False)
            return ipaddress.ip_address(client_ip) in network
        except ValueError:
            return False

    def _max_clients_reached(self, endpoint_id: int, max_clients: int) -> bool:
        if max_clients <= 0:
            return False
        with self._lock:
            current = sum(1 for session in self._sessions.values() if session.endpoint_id == endpoint_id)
        return current >= max_clients

    @staticmethod
    def _terminate_process(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1.0)
        except OSError:
            pass
