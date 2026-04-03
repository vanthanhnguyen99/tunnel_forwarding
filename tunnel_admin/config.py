from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import os
import secrets


ROOT_DIR = Path(__file__).resolve().parent.parent


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Invalid integer for {name}: {value}") from exc


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean for {name}: {value}")


def _load_or_create_secret(secret_file: Path) -> str:
    if secret_file.exists():
        return secret_file.read_text(encoding="utf-8").strip()

    secret = secrets.token_hex(32)
    secret_file.write_text(secret, encoding="utf-8")
    return secret


def _default_docker_runner_image() -> str:
    digest = hashlib.sha256()
    tracked_paths = [ROOT_DIR / "Dockerfile.tunnel-runner", *sorted((ROOT_DIR / "tunnel_admin").rglob("*.py"))]
    for path in tracked_paths:
        digest.update(path.relative_to(ROOT_DIR).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"tunnel-forwarding-runner:{digest.hexdigest()[:12]}"


@dataclass(slots=True)
class Settings:
    host: str
    port: int
    data_dir: Path
    runtime_dir: Path
    static_dir: Path
    docker_configs_dir: Path
    db_path: Path
    secret: str
    cookie_name: str
    auth_session_ttl: int
    admin_username: str
    admin_password: str
    metrics_interval_seconds: int
    metrics_window_seconds: int
    connect_timeout_seconds: float
    shutdown_grace_seconds: float
    docker_network_name: str
    docker_network_subnet: str
    docker_runner_image: str
    apply_iptables_on_endpoint_start: bool
    iptables_source_subnet: str
    iptables_input_interface: str
    iptables_output_interface: str
    iptables_use_sudo: bool

    @classmethod
    def load(cls) -> "Settings":
        data_dir = Path(os.getenv("APP_DATA_DIR", ROOT_DIR / "data")).resolve()
        runtime_dir = Path(os.getenv("APP_RUNTIME_DIR", ROOT_DIR / "runtime")).resolve()
        static_dir = Path(os.getenv("APP_STATIC_DIR", ROOT_DIR / "tunnel_admin" / "static")).resolve()
        docker_configs_dir = Path(os.getenv("APP_DOCKER_CONFIG_DIR", data_dir / "docker")).resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        runtime_dir.mkdir(parents=True, exist_ok=True)
        docker_configs_dir.mkdir(parents=True, exist_ok=True)

        secret_file = data_dir / ".app_secret"
        secret = os.getenv("APP_SECRET") or _load_or_create_secret(secret_file)

        return cls(
            host=os.getenv("APP_HOST", "0.0.0.0"),
            port=_env_int("APP_PORT", 2020),
            data_dir=data_dir,
            runtime_dir=runtime_dir,
            static_dir=static_dir,
            docker_configs_dir=docker_configs_dir,
            db_path=Path(os.getenv("APP_DB_PATH", data_dir / "tunnel_admin.db")).resolve(),
            secret=secret,
            cookie_name=os.getenv("AUTH_COOKIE_NAME", "tunnel_admin_session"),
            auth_session_ttl=_env_int("AUTH_SESSION_TTL_SECONDS", 86400),
            admin_username=os.getenv("ADMIN_DEFAULT_USER", "admin"),
            admin_password=os.getenv("ADMIN_DEFAULT_PASS", "admin123"),
            metrics_interval_seconds=max(1, _env_int("METRICS_INTERVAL_SECONDS", 1)),
            metrics_window_seconds=max(30, _env_int("METRICS_WINDOW_SECONDS", 300)),
            connect_timeout_seconds=float(os.getenv("UPSTREAM_CONNECT_TIMEOUT_SECONDS", "5")),
            shutdown_grace_seconds=float(os.getenv("SHUTDOWN_GRACE_SECONDS", "5")),
            docker_network_name=os.getenv("APP_DOCKER_NETWORK_NAME", "tunnel_nat"),
            docker_network_subnet=os.getenv("APP_DOCKER_NETWORK_SUBNET", "172.20.0.0/16"),
            docker_runner_image=os.getenv("APP_DOCKER_RUNNER_IMAGE", _default_docker_runner_image()),
            apply_iptables_on_endpoint_start=_env_bool("APP_APPLY_IPTABLES_ON_ENDPOINT_START", True),
            iptables_source_subnet=os.getenv("APP_IPTABLES_SOURCE_SUBNET", "172.31.250.0/24"),
            iptables_input_interface=os.getenv("APP_IPTABLES_INPUT_INTERFACE", "tun0"),
            iptables_output_interface=os.getenv("APP_IPTABLES_OUTPUT_INTERFACE", "eth1"),
            iptables_use_sudo=_env_bool("APP_IPTABLES_USE_SUDO", False),
        )
