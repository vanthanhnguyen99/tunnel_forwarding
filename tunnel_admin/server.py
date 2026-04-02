from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import mimetypes
from pathlib import Path
import queue
import re
import signal
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from .auth import AuthManager, hash_password, verify_password
from .config import Settings
from .docker_config import DockerConfigManager
from .docker_runtime import DockerTunnelManager
from .storage import Database


LOGGER = logging.getLogger("tunnel_admin.server")


class HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class EventBroker:
    def __init__(self) -> None:
        self._subscribers: set[queue.Queue[dict[str, Any]]] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        subscriber: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=200)
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def publish(self, event_name: str, data: dict[str, Any]) -> None:
        event = {"event": event_name, "data": data}
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                try:
                    subscriber.get_nowait()
                except queue.Empty:
                    pass
                try:
                    subscriber.put_nowait(event)
                except queue.Full:
                    pass


def _now_ts() -> int:
    return int(time.time())


def _sanitize_text(value: Any, *, max_length: int = 255) -> str:
    return str(value or "").strip()[:max_length]


def _sanitize_optional_path(value: Any) -> str | None:
    text = _sanitize_text(value, max_length=500)
    return text or None


def _parse_port(value: Any, field_name: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise HttpError(HTTPStatus.BAD_REQUEST, f"{field_name} must be an integer") from exc
    if not 1 <= port <= 65535:
        raise HttpError(HTTPStatus.BAD_REQUEST, f"{field_name} must be between 1 and 65535")
    return port


def _parse_service_port(value: Any, field_name: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise HttpError(HTTPStatus.BAD_REQUEST, f"{field_name} must be an integer") from exc
    if not 1 <= port <= 65535:
        raise HttpError(HTTPStatus.BAD_REQUEST, f"{field_name} must be between 1 and 65535")
    return port


def _parse_non_negative_int(value: Any, field_name: str) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError) as exc:
        raise HttpError(HTTPStatus.BAD_REQUEST, f"{field_name} must be an integer") from exc
    if number < 0:
        raise HttpError(HTTPStatus.BAD_REQUEST, f"{field_name} must be >= 0")
    return number


@dataclass(slots=True)
class OverviewSummary:
    total_endpoints: int
    active_endpoints: int
    total_active_sessions: int
    total_traffic_up: int
    total_traffic_down: int
    top_endpoints: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_endpoints": self.total_endpoints,
            "active_endpoints": self.active_endpoints,
            "total_active_sessions": self.total_active_sessions,
            "total_traffic_up": self.total_traffic_up,
            "total_traffic_down": self.total_traffic_down,
            "top_endpoints": self.top_endpoints,
        }


class AppContext:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db = Database(str(settings.db_path))
        self.auth = AuthManager(settings.cookie_name, settings.auth_session_ttl)
        self.events = EventBroker()
        self.docker_config = DockerConfigManager(settings, self.db)
        self.engine = DockerTunnelManager(
            database=self.db,
            status_callback=self.db.update_endpoint_status_message,
            docker_network_name=settings.docker_network_name,
            docker_network_subnet=settings.docker_network_subnet,
            docker_runner_image=settings.docker_runner_image,
            apply_iptables_on_endpoint_start=settings.apply_iptables_on_endpoint_start,
            iptables_source_subnet=settings.iptables_source_subnet,
            iptables_input_interface=settings.iptables_input_interface,
            iptables_output_interface=settings.iptables_output_interface,
        )
        self._stop_event = threading.Event()
        self._metrics_thread = threading.Thread(target=self._metrics_loop, daemon=True)
        self._metric_totals_lock = threading.Lock()
        self._previous_metric_totals: dict[str, Any] = {
            "overall": {"bytes_up": 0, "bytes_down": 0},
            "per_endpoint": {},
        }

    def start(self) -> None:
        self.db.initialize()
        self._ensure_default_admin()
        endpoints = self.db.list_endpoints()
        for endpoint in endpoints:
            try:
                self.docker_config.sync_endpoint(endpoint)
            except Exception:
                LOGGER.exception("Failed to sync Docker config for endpoint %s", endpoint["name"])
        for endpoint in self.db.list_endpoints():
            endpoint_id = int(endpoint["id"])
            if endpoint["enabled"] and not self.engine.is_endpoint_running(endpoint_id):
                started, message = self.engine.start_endpoint(endpoint)
                if not started:
                    LOGGER.warning("Failed to auto-start endpoint %s: %s", endpoint["name"], message)
            elif not endpoint["enabled"] and self.engine.is_endpoint_running(endpoint_id):
                self.engine.stop_endpoint(endpoint_id, reason="endpoint_disabled", silence_missing=True)
        self._metrics_thread.start()

    def shutdown(self) -> None:
        self._stop_event.set()
        self.engine.shutdown()
        if self._metrics_thread.is_alive():
            self._metrics_thread.join(timeout=self.settings.shutdown_grace_seconds)
        self.db.close()

    def publish_event(self, event_name: str, data: dict[str, Any]) -> None:
        self.events.publish(event_name, data)

    def authenticate(self, username: str, password: str) -> dict[str, Any]:
        admin = self.db.get_admin_by_username(username)
        if admin is None or not verify_password(password, admin["password_hash"]):
            raise HttpError(HTTPStatus.UNAUTHORIZED, "Invalid username or password")
        self.db.touch_admin_login(int(admin["id"]))
        session = self.auth.create_session(int(admin["id"]), str(admin["username"]))
        return {"admin": admin, "session_token": session.token, "session": self.auth.session_to_dict(session)}

    def logout(self, token: str | None) -> None:
        self.auth.destroy_session(token)

    def list_endpoints(self) -> list[dict[str, Any]]:
        endpoints = self.db.list_endpoints()
        runtime_metrics = self.engine.collect_runtime_metrics()["per_endpoint"]

        result: list[dict[str, Any]] = []
        for endpoint in endpoints:
            endpoint_id = int(endpoint["id"])
            runtime = runtime_metrics.get(endpoint_id, {"active_connections": 0, "bytes_up": 0, "bytes_down": 0})
            runtime_details = self.engine.get_endpoint_runtime_details(endpoint)
            compose_state = str(runtime_details.get("compose_state") or "").lower()
            is_running = compose_state == "running"
            runtime_status = "running" if is_running else ("disabled" if not endpoint["enabled"] else "stopped")
            ssh_target = self._format_ssh_target(endpoint)
            container_listen = self._format_container_listen(endpoint)
            total_up = int(runtime["bytes_up"])
            total_down = int(runtime["bytes_down"])

            result.append(
                {
                    **endpoint,
                    "status_message": runtime_details.get("status_message") or endpoint.get("status_message"),
                    "runtime_status": runtime_status,
                    "listen": container_listen,
                    "forward_to": f"{endpoint['destination_host']}:{endpoint['destination_port']}",
                    "ssh_target": ssh_target,
                    "transport": f"{container_listen} -> "
                    f"{endpoint['destination_host']}:{endpoint['destination_port']} via {ssh_target}",
                    "container_bind": f"{endpoint['listen_host']}:{endpoint['listen_port']}",
                    "active_clients": int(runtime["active_connections"]),
                    "bytes_up_total": total_up,
                    "bytes_down_total": total_down,
                    "traffic_total": total_up + total_down,
                }
            )
        return result

    def get_endpoint(self, endpoint_id: int) -> dict[str, Any]:
        for endpoint in self.list_endpoints():
            if int(endpoint["id"]) == endpoint_id:
                return endpoint
        raise HttpError(HTTPStatus.NOT_FOUND, "Endpoint not found")

    def list_active_sessions(self, endpoint_id: int | None = None) -> list[dict[str, Any]]:
        return self.engine.list_active_sessions(endpoint_id=endpoint_id)

    def get_overview_summary(self) -> dict[str, Any]:
        endpoints = self.list_endpoints()
        total_traffic_up = sum(int(endpoint["bytes_up_total"]) for endpoint in endpoints)
        total_traffic_down = sum(int(endpoint["bytes_down_total"]) for endpoint in endpoints)
        active_endpoints = sum(1 for endpoint in endpoints if endpoint["runtime_status"] == "running")
        total_active_sessions = sum(int(endpoint["active_clients"]) for endpoint in endpoints)
        top_endpoints = sorted(
            (
                {
                    "id": endpoint["id"],
                    "name": endpoint["name"],
                    "active_clients": endpoint["active_clients"],
                    "traffic_total": endpoint["traffic_total"],
                    "runtime_status": endpoint["runtime_status"],
                }
                for endpoint in endpoints
            ),
            key=lambda row: (row["active_clients"], row["traffic_total"], row["name"]),
            reverse=True,
        )[:5]
        return OverviewSummary(
            total_endpoints=len(endpoints),
            active_endpoints=active_endpoints,
            total_active_sessions=total_active_sessions,
            total_traffic_up=total_traffic_up,
            total_traffic_down=total_traffic_down,
            top_endpoints=top_endpoints,
        ).to_dict()

    def list_timeseries(self, endpoint_id: int | None = None, window_seconds: int | None = None) -> list[dict[str, Any]]:
        effective_window = window_seconds or self.settings.metrics_window_seconds
        since_ts = _now_ts() - effective_window
        return self.db.list_metrics(since_ts=since_ts, endpoint_id=endpoint_id)

    def get_endpoint_metrics(self, endpoint_id: int) -> dict[str, Any]:
        endpoint = self.get_endpoint(endpoint_id)
        return {
            "endpoint": endpoint,
            "sessions": self.list_active_sessions(endpoint_id),
            "timeseries": self.list_timeseries(endpoint_id=endpoint_id),
        }

    def create_endpoint(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        clean_payload = self._validate_endpoint_payload(payload)
        endpoint = self.db.create_endpoint(clean_payload)
        endpoint = self.docker_config.sync_endpoint(endpoint)
        self.db.record_audit(actor=actor, action="endpoint.created", endpoint_id=int(endpoint["id"]), details=endpoint["name"])
        if endpoint["enabled"]:
            self.engine.start_endpoint(endpoint)
        endpoint_view = self.get_endpoint(int(endpoint["id"]))
        self.publish_event("endpoint.created", endpoint_view)
        if endpoint_view["runtime_status"] == "running":
            self.publish_event("endpoint.started", endpoint_view)
        return endpoint_view

    def update_endpoint(self, endpoint_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        current = self.db.get_endpoint(endpoint_id)
        if current is None:
            raise HttpError(HTTPStatus.NOT_FOUND, "Endpoint not found")
        clean_payload = self._validate_endpoint_payload(payload, current_endpoint_id=endpoint_id)
        self.db.update_endpoint(endpoint_id, clean_payload)
        updated = self.db.get_endpoint(endpoint_id)
        if updated is None:
            raise HttpError(HTTPStatus.NOT_FOUND, "Endpoint not found after update")
        updated = self.docker_config.sync_endpoint(updated)
        if updated["enabled"]:
            self.engine.start_endpoint(updated)
        else:
            self.engine.stop_endpoint(endpoint_id, reason="endpoint_stopped", silence_missing=True)
        self.db.record_audit(actor=actor, action="endpoint.updated", endpoint_id=endpoint_id, details=updated["name"])
        endpoint_view = self.get_endpoint(endpoint_id)
        self.publish_event("endpoint.updated", endpoint_view)
        if endpoint_view["runtime_status"] == "running":
            self.publish_event("endpoint.started", endpoint_view)
        else:
            self.publish_event("endpoint.stopped", endpoint_view)
        return endpoint_view

    def delete_endpoint(self, endpoint_id: int, actor: str) -> None:
        endpoint = self.db.get_endpoint(endpoint_id)
        if endpoint is None:
            raise HttpError(HTTPStatus.NOT_FOUND, "Endpoint not found")
        self.engine.stop_endpoint(endpoint_id, reason="endpoint_deleted", silence_missing=True)
        self.docker_config.delete_endpoint_artifacts(endpoint)
        self.db.record_audit(actor=actor, action="endpoint.deleted", endpoint_id=endpoint_id, details=endpoint["name"])
        self.db.delete_endpoint(endpoint_id)
        self.publish_event("endpoint.deleted", {"id": endpoint_id, "name": endpoint["name"]})

    def start_endpoint(self, endpoint_id: int, actor: str) -> dict[str, Any]:
        endpoint = self.db.get_endpoint(endpoint_id)
        if endpoint is None:
            raise HttpError(HTTPStatus.NOT_FOUND, "Endpoint not found")
        self.db.set_endpoint_enabled(endpoint_id, True)
        endpoint = self.db.get_endpoint(endpoint_id)
        if endpoint is None:
            raise HttpError(HTTPStatus.NOT_FOUND, "Endpoint not found after enable")
        self.engine.start_endpoint(endpoint)
        self.db.record_audit(actor=actor, action="endpoint.started", endpoint_id=endpoint_id, details=endpoint["name"])
        endpoint_view = self.get_endpoint(endpoint_id)
        self.publish_event("endpoint.started", endpoint_view)
        return endpoint_view

    def stop_endpoint(self, endpoint_id: int, actor: str) -> dict[str, Any]:
        endpoint = self.db.get_endpoint(endpoint_id)
        if endpoint is None:
            raise HttpError(HTTPStatus.NOT_FOUND, "Endpoint not found")
        self.engine.stop_endpoint(endpoint_id, reason="endpoint_stopped", silence_missing=True)
        self.db.set_endpoint_enabled(endpoint_id, False)
        self.db.update_endpoint_status_message(endpoint_id, None)
        self.db.record_audit(actor=actor, action="endpoint.stopped", endpoint_id=endpoint_id, details=endpoint["name"])
        endpoint_view = self.get_endpoint(endpoint_id)
        self.publish_event("endpoint.stopped", endpoint_view)
        return endpoint_view

    def disconnect_session(self, session_id: int, actor: str) -> bool:
        disconnected = self.engine.disconnect_session(session_id, reason="admin_disconnect")
        if disconnected:
            self.db.record_audit(actor=actor, action="session.disconnected", details=str(session_id))
        return disconnected

    def _ensure_default_admin(self) -> None:
        existing = self.db.get_admin_by_username(self.settings.admin_username)
        if existing is None:
            self.db.upsert_admin(
                username=self.settings.admin_username,
                password_hash=hash_password(self.settings.admin_password),
            )

    def _validate_endpoint_payload(
        self,
        payload: dict[str, Any],
        current_endpoint_id: int | None = None,
    ) -> dict[str, Any]:
        name = _sanitize_text(payload.get("name"), max_length=100)
        if not name:
            raise HttpError(HTTPStatus.BAD_REQUEST, "name is required")

        tunnel_type_raw = _sanitize_text(payload.get("tunnel_type") or payload.get("type") or "ssh_local_forward")
        tunnel_type = tunnel_type_raw.lower().replace(" ", "_")
        if tunnel_type not in {"ssh_local_forward", "ssh_local", "local_forward"}:
            raise HttpError(HTTPStatus.BAD_REQUEST, "Only SSH Local Forward tunnel type is supported in this MVP")
        tunnel_type = "ssh_local_forward"

        listen_host = _sanitize_text(payload.get("listen_host"), max_length=100)
        destination_host = _sanitize_text(payload.get("destination_host"), max_length=100)
        ssh_host = _sanitize_text(payload.get("ssh_host"), max_length=100)
        ssh_username = _sanitize_text(payload.get("ssh_username"), max_length=100)
        if not listen_host:
            raise HttpError(HTTPStatus.BAD_REQUEST, "listen_host is required")
        if not destination_host:
            raise HttpError(HTTPStatus.BAD_REQUEST, "destination_host is required")
        if not ssh_host:
            raise HttpError(HTTPStatus.BAD_REQUEST, "ssh_host is required")
        if not ssh_username:
            raise HttpError(HTTPStatus.BAD_REQUEST, "ssh_username is required")

        allowed_client_cidr = _sanitize_text(payload.get("allowed_client_cidr"), max_length=64) or None
        if allowed_client_cidr:
            import ipaddress

            try:
                ipaddress.ip_network(allowed_client_cidr, strict=False)
            except ValueError as exc:
                raise HttpError(HTTPStatus.BAD_REQUEST, "allowed_client_cidr is invalid") from exc

        tags_value = payload.get("tags", "")
        if isinstance(tags_value, list):
            tags = ", ".join(_sanitize_text(item, max_length=40) for item in tags_value if _sanitize_text(item))
        else:
            tags = _sanitize_text(tags_value, max_length=255)

        clean = {
            "name": name,
            "tunnel_type": tunnel_type,
            "listen_host": listen_host,
            "listen_port": _parse_port(payload.get("listen_port"), "listen_port"),
            "destination_host": destination_host,
            "destination_port": _parse_port(payload.get("destination_port"), "destination_port"),
            "ssh_host": ssh_host,
            "ssh_port": _parse_service_port(payload.get("ssh_port", 22), "ssh_port"),
            "ssh_username": ssh_username,
            "ssh_private_key_path": _sanitize_optional_path(payload.get("ssh_private_key_path")),
            "ssh_known_hosts_path": _sanitize_optional_path(payload.get("ssh_known_hosts_path")),
            "ssh_options": _sanitize_text(payload.get("ssh_options"), max_length=500),
            "description": _sanitize_text(payload.get("description"), max_length=1000),
            "allowed_client_cidr": allowed_client_cidr,
            "enabled": bool(payload.get("enabled")),
            "max_clients": _parse_non_negative_int(payload.get("max_clients", 0), "max_clients"),
            "idle_timeout": _parse_non_negative_int(payload.get("idle_timeout", 0), "idle_timeout"),
            "tags": tags,
            "status_message": None,
        }
        self._validate_ssh_paths(clean)
        self._assert_endpoint_uniqueness(clean, current_endpoint_id=current_endpoint_id)
        return clean

    def _assert_endpoint_uniqueness(
        self,
        candidate: dict[str, Any],
        current_endpoint_id: int | None = None,
    ) -> None:
        for endpoint in self.db.list_endpoints():
            endpoint_id = int(endpoint["id"])
            if current_endpoint_id is not None and endpoint_id == current_endpoint_id:
                continue

            if str(endpoint["name"]).lower() == str(candidate["name"]).lower():
                raise HttpError(HTTPStatus.BAD_REQUEST, "Endpoint name already exists")

    def _validate_ssh_paths(self, endpoint: dict[str, Any]) -> None:
        for field_name in ("ssh_private_key_path", "ssh_known_hosts_path"):
            candidate = endpoint.get(field_name)
            if not candidate:
                continue
            path = Path(str(candidate)).expanduser()
            if not path.exists():
                raise HttpError(HTTPStatus.BAD_REQUEST, f"{field_name} does not exist")
            if not path.is_file():
                raise HttpError(HTTPStatus.BAD_REQUEST, f"{field_name} must point to a file")

    @staticmethod
    def _format_ssh_target(endpoint: dict[str, Any]) -> str:
        ssh_host = _sanitize_text(endpoint.get("ssh_host"), max_length=100)
        ssh_username = _sanitize_text(endpoint.get("ssh_username"), max_length=100)
        ssh_port = endpoint.get("ssh_port") or 22
        if not ssh_host or not ssh_username:
            return "unconfigured"
        return f"{ssh_username}@{ssh_host}:{ssh_port}"

    @staticmethod
    def _format_container_listen(endpoint: dict[str, Any]) -> str:
        container_ip = _sanitize_text(endpoint.get("docker_nat_ip"), max_length=100)
        listen_port = endpoint.get("listen_port")
        if container_ip and listen_port:
            return f"{container_ip}:{listen_port}"
        if listen_port:
            return f"container:{listen_port}"
        return "pending"

    def _metrics_loop(self) -> None:
        retention_seconds = max(self.settings.metrics_window_seconds * 12, 86400)
        while not self._stop_event.wait(self.settings.metrics_interval_seconds):
            self.auth.purge_expired()
            runtime = self.engine.collect_runtime_metrics()
            endpoints = self.db.list_endpoints()
            ts = _now_ts()
            rows: list[dict[str, Any]] = []
            endpoint_rows: list[dict[str, Any]] = []

            with self._metric_totals_lock:
                previous_overall = self._previous_metric_totals["overall"]
                overall = runtime["overall"]
                overall_row = {
                    "ts": ts,
                    "endpoint_id": None,
                    "active_connections": int(overall["active_connections"]),
                    "bytes_up_per_sec": max(0, int(overall["bytes_up"]) - int(previous_overall["bytes_up"])),
                    "bytes_down_per_sec": max(
                        0,
                        int(overall["bytes_down"]) - int(previous_overall["bytes_down"]),
                    ),
                }
                rows.append(overall_row)
                self._previous_metric_totals["overall"] = {
                    "bytes_up": int(overall["bytes_up"]),
                    "bytes_down": int(overall["bytes_down"]),
                }

                for endpoint in endpoints:
                    endpoint_id = int(endpoint["id"])
                    endpoint_runtime = runtime["per_endpoint"].get(
                        endpoint_id,
                        {"active_connections": 0, "bytes_up": 0, "bytes_down": 0},
                    )
                    previous_endpoint = self._previous_metric_totals["per_endpoint"].get(
                        endpoint_id,
                        {"bytes_up": 0, "bytes_down": 0},
                    )
                    endpoint_row = {
                        "ts": ts,
                        "endpoint_id": endpoint_id,
                        "active_connections": int(endpoint_runtime["active_connections"]),
                        "bytes_up_per_sec": max(
                            0,
                            int(endpoint_runtime["bytes_up"]) - int(previous_endpoint["bytes_up"]),
                        ),
                        "bytes_down_per_sec": max(
                            0,
                            int(endpoint_runtime["bytes_down"]) - int(previous_endpoint["bytes_down"]),
                        ),
                    }
                    rows.append(endpoint_row)
                    endpoint_rows.append(endpoint_row)
                    self._previous_metric_totals["per_endpoint"][endpoint_id] = {
                        "bytes_up": int(endpoint_runtime["bytes_up"]),
                        "bytes_down": int(endpoint_runtime["bytes_down"]),
                    }

            self.db.insert_metrics(rows)
            self.db.prune_metrics(ts - retention_seconds)
            self.publish_event(
                "metrics.tick",
                {
                    "ts": ts,
                    "overview": self.get_overview_summary(),
                    "overall_point": overall_row,
                    "endpoint_points": endpoint_rows,
                },
            )


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], handler_cls: type[BaseHTTPRequestHandler], context: AppContext):
        super().__init__(server_address, handler_cls)
        self.context = context


class RequestHandler(BaseHTTPRequestHandler):
    server: AppServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def log_message(self, format: str, *args: Any) -> None:
        LOGGER.info("%s - %s", self.address_string(), format % args)

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path.startswith("/api/"):
                self._handle_api(method, path, query)
            else:
                self._serve_static(path)
        except HttpError as exc:
            self._send_json(int(exc.status), {"error": exc.message})
        except BrokenPipeError:
            LOGGER.info("Client disconnected while sending response")
        except Exception:
            LOGGER.exception("Unhandled request error")
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Internal server error"})

    def _handle_api(self, method: str, path: str, query: dict[str, list[str]]) -> None:
        if path == "/api/login" and method == "POST":
            self._handle_login()
            return

        if path == "/api/me" and method == "GET":
            session = self._require_session()
            self._send_json(HTTPStatus.OK, {"authenticated": True, "user": {"username": session.username}})
            return

        if path == "/api/logout" and method == "POST":
            token = self._read_session_token()
            self.server.context.logout(token)
            self._send_json(
                HTTPStatus.OK,
                {"ok": True},
                extra_headers={"Set-Cookie": self.server.context.auth.clear_cookie_header()},
            )
            return

        if path == "/api/events" and method == "GET":
            self._require_session()
            self._handle_event_stream()
            return

        session = self._require_session()
        actor = session.username

        if path == "/api/endpoints" and method == "GET":
            self._send_json(HTTPStatus.OK, {"items": self.server.context.list_endpoints()})
            return

        if path == "/api/endpoints" and method == "POST":
            payload = self._read_json_body()
            endpoint = self.server.context.create_endpoint(payload, actor=actor)
            self._send_json(HTTPStatus.CREATED, {"item": endpoint})
            return

        if path == "/api/sessions" and method == "GET":
            self._send_json(HTTPStatus.OK, {"items": self.server.context.list_active_sessions()})
            return

        if path == "/api/metrics/overview" and method == "GET":
            self._send_json(HTTPStatus.OK, {"item": self.server.context.get_overview_summary()})
            return

        if path == "/api/metrics/timeseries" and method == "GET":
            endpoint_id = int(query["endpoint_id"][0]) if "endpoint_id" in query and query["endpoint_id"] else None
            window = int(query["window"][0]) if "window" in query and query["window"] else None
            self._send_json(
                HTTPStatus.OK,
                {"items": self.server.context.list_timeseries(endpoint_id=endpoint_id, window_seconds=window)},
            )
            return

        endpoint_match = re.fullmatch(r"/api/endpoints/(\d+)", path)
        endpoint_start_match = re.fullmatch(r"/api/endpoints/(\d+)/start", path)
        endpoint_stop_match = re.fullmatch(r"/api/endpoints/(\d+)/stop", path)
        endpoint_sessions_match = re.fullmatch(r"/api/endpoints/(\d+)/sessions", path)
        endpoint_metrics_match = re.fullmatch(r"/api/endpoints/(\d+)/metrics", path)
        session_disconnect_match = re.fullmatch(r"/api/sessions/(\d+)/disconnect", path)

        if endpoint_match:
            endpoint_id = int(endpoint_match.group(1))
            if method == "GET":
                self._send_json(HTTPStatus.OK, {"item": self.server.context.get_endpoint(endpoint_id)})
                return
            if method == "PUT":
                payload = self._read_json_body()
                endpoint = self.server.context.update_endpoint(endpoint_id, payload, actor=actor)
                self._send_json(HTTPStatus.OK, {"item": endpoint})
                return
            if method == "DELETE":
                self.server.context.delete_endpoint(endpoint_id, actor=actor)
                self._send_json(HTTPStatus.OK, {"ok": True})
                return

        if endpoint_start_match and method == "POST":
            endpoint_id = int(endpoint_start_match.group(1))
            endpoint = self.server.context.start_endpoint(endpoint_id, actor=actor)
            self._send_json(HTTPStatus.OK, {"item": endpoint})
            return

        if endpoint_stop_match and method == "POST":
            endpoint_id = int(endpoint_stop_match.group(1))
            endpoint = self.server.context.stop_endpoint(endpoint_id, actor=actor)
            self._send_json(HTTPStatus.OK, {"item": endpoint})
            return

        if endpoint_sessions_match and method == "GET":
            endpoint_id = int(endpoint_sessions_match.group(1))
            self._send_json(HTTPStatus.OK, {"items": self.server.context.list_active_sessions(endpoint_id)})
            return

        if endpoint_metrics_match and method == "GET":
            endpoint_id = int(endpoint_metrics_match.group(1))
            self._send_json(HTTPStatus.OK, {"item": self.server.context.get_endpoint_metrics(endpoint_id)})
            return

        if session_disconnect_match and method == "POST":
            session_id = int(session_disconnect_match.group(1))
            disconnected = self.server.context.disconnect_session(session_id, actor=actor)
            if not disconnected:
                raise HttpError(HTTPStatus.NOT_FOUND, "Session not found")
            self._send_json(HTTPStatus.OK, {"ok": True})
            return

        raise HttpError(HTTPStatus.NOT_FOUND, "Route not found")

    def _serve_static(self, path: str) -> None:
        if path in {"/", "/index.html"}:
            relative_path = "index.html"
        elif path.startswith("/static/"):
            relative_path = path.removeprefix("/static/")
        else:
            relative_path = "index.html"

        target = (self.server.context.settings.static_dir / relative_path).resolve()
        static_root = self.server.context.settings.static_dir.resolve()
        if static_root not in target.parents and target != static_root:
            raise HttpError(HTTPStatus.FORBIDDEN, "Invalid static path")
        if not target.exists() or not target.is_file():
            raise HttpError(HTTPStatus.NOT_FOUND, "Static file not found")

        content_type, _ = mimetypes.guess_type(str(target))
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _handle_login(self) -> None:
        payload = self._read_json_body()
        username = _sanitize_text(payload.get("username"), max_length=100)
        password = str(payload.get("password") or "")
        if not username or not password:
            raise HttpError(HTTPStatus.BAD_REQUEST, "username and password are required")
        auth_result = self.server.context.authenticate(username, password)
        self._send_json(
            HTTPStatus.OK,
            {"user": {"username": auth_result["admin"]["username"]}},
            extra_headers={
                "Set-Cookie": self.server.context.auth.cookie_header(auth_result["session_token"])
            },
        )

    def _handle_event_stream(self) -> None:
        subscriber = self.server.context.events.subscribe()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        try:
            self._write_sse("connected", {"ok": True, "ts": _now_ts()})
            while True:
                try:
                    event = subscriber.get(timeout=10)
                except queue.Empty:
                    self._write_sse("heartbeat", {"ts": _now_ts()})
                    continue
                self._write_sse(event["event"], event["data"])
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.server.context.events.unsubscribe(subscriber)

    def _write_sse(self, event_name: str, data: dict[str, Any]) -> None:
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        message = f"event: {event_name}\ndata: {payload}\n\n".encode("utf-8")
        self.wfile.write(message)
        self.wfile.flush()

    def _read_json_body(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0") or "0")
        if content_length <= 0:
            return {}
        raw = self.rfile.read(content_length)
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise HttpError(HTTPStatus.BAD_REQUEST, "Invalid JSON body") from exc

    def _read_session_token(self) -> str | None:
        cookie_header = self.headers.get("Cookie")
        if not cookie_header:
            return None
        cookie = SimpleCookie()
        cookie.load(cookie_header)
        morsel = cookie.get(self.server.context.auth.cookie_name)
        return morsel.value if morsel is not None else None

    def _require_session(self):
        token = self._read_session_token()
        session = self.server.context.auth.get_session(token)
        if session is None:
            raise HttpError(HTTPStatus.UNAUTHORIZED, "Authentication required")
        return session

    def _send_json(
        self,
        status: int,
        payload: dict[str, Any],
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for header_name, header_value in extra_headers.items():
                self.send_header(header_name, header_value)
        self.end_headers()
        self.wfile.write(body)


def configure_logging(runtime_dir: Path) -> None:
    log_path = runtime_dir / "app.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def run() -> None:
    settings = Settings.load()
    configure_logging(settings.runtime_dir)
    context = AppContext(settings)
    context.start()
    server = AppServer((settings.host, settings.port), RequestHandler, context)

    stop_requested = threading.Event()

    def _request_shutdown(signum: int, _frame: Any) -> None:
        LOGGER.info("Received signal %s, shutting down", signum)
        if stop_requested.is_set():
            return
        stop_requested.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _request_shutdown)

    LOGGER.info("Tunnel admin listening on http://%s:%s", settings.host, settings.port)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        context.shutdown()
        LOGGER.info("Tunnel admin stopped")
