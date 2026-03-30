from __future__ import annotations

from dataclasses import dataclass
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


def _load_or_create_secret(secret_file: Path) -> str:
    if secret_file.exists():
        return secret_file.read_text(encoding="utf-8").strip()

    secret = secrets.token_hex(32)
    secret_file.write_text(secret, encoding="utf-8")
    return secret


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
        )
