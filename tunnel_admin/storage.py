from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
import threading
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, db_path: str) -> None:
        self._connection = sqlite3.connect(db_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS admins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_login_at TEXT
        );

        CREATE TABLE IF NOT EXISTS endpoints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            tunnel_type TEXT NOT NULL,
            listen_host TEXT NOT NULL,
            listen_port INTEGER NOT NULL,
            destination_host TEXT NOT NULL,
            destination_port INTEGER NOT NULL,
            ssh_host TEXT NOT NULL DEFAULT '',
            ssh_port INTEGER NOT NULL DEFAULT 22,
            ssh_username TEXT NOT NULL DEFAULT '',
            ssh_private_key_path TEXT,
            ssh_known_hosts_path TEXT,
            ssh_options TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            allowed_client_cidr TEXT,
            enabled INTEGER NOT NULL DEFAULT 0,
            max_clients INTEGER NOT NULL DEFAULT 0,
            idle_timeout INTEGER NOT NULL DEFAULT 0,
            tags TEXT NOT NULL DEFAULT '',
            status_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            endpoint_id INTEGER NOT NULL,
            client_ip TEXT NOT NULL,
            client_port INTEGER NOT NULL,
            upstream_ip TEXT,
            upstream_port INTEGER,
            status TEXT NOT NULL,
            bytes_up INTEGER NOT NULL DEFAULT 0,
            bytes_down INTEGER NOT NULL DEFAULT 0,
            connected_at TEXT NOT NULL,
            closed_at TEXT,
            close_reason TEXT,
            FOREIGN KEY(endpoint_id) REFERENCES endpoints(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS metrics_timeseries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            endpoint_id INTEGER,
            active_connections INTEGER NOT NULL,
            bytes_up_per_sec INTEGER NOT NULL,
            bytes_down_per_sec INTEGER NOT NULL,
            FOREIGN KEY(endpoint_id) REFERENCES endpoints(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            endpoint_id INTEGER,
            details TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(endpoint_id) REFERENCES endpoints(id) ON DELETE SET NULL
        );

        CREATE INDEX IF NOT EXISTS idx_sessions_endpoint_status ON sessions (endpoint_id, status);
        CREATE INDEX IF NOT EXISTS idx_sessions_connected_at ON sessions (connected_at);
        CREATE INDEX IF NOT EXISTS idx_metrics_endpoint_ts ON metrics_timeseries (endpoint_id, ts);
        """
        with self._lock, self._connection:
            self._connection.executescript(schema)
            self._ensure_endpoint_columns()

    def _ensure_endpoint_columns(self) -> None:
        existing = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(endpoints)").fetchall()
        }
        expected_columns = {
            "ssh_host": "TEXT NOT NULL DEFAULT ''",
            "ssh_port": "INTEGER NOT NULL DEFAULT 22",
            "ssh_username": "TEXT NOT NULL DEFAULT ''",
            "ssh_private_key_path": "TEXT",
            "ssh_known_hosts_path": "TEXT",
            "ssh_options": "TEXT NOT NULL DEFAULT ''",
        }
        for column_name, definition in expected_columns.items():
            if column_name in existing:
                continue
            self._connection.execute(
                f"ALTER TABLE endpoints ADD COLUMN {column_name} {definition}"
            )

    def _row_to_dict(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        if "enabled" in data:
            data["enabled"] = bool(data["enabled"])
        return data

    def get_admin_by_username(self, username: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM admins WHERE username = ?",
                (username,),
            ).fetchone()
        return self._row_to_dict(row)

    def upsert_admin(self, username: str, password_hash: str) -> dict[str, Any]:
        now = utc_now_iso()
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT id FROM admins WHERE username = ?",
                (username,),
            ).fetchone()
            if row:
                self._connection.execute(
                    "UPDATE admins SET password_hash = ? WHERE id = ?",
                    (password_hash, row["id"]),
                )
                admin_id = row["id"]
            else:
                cursor = self._connection.execute(
                    """
                    INSERT INTO admins (username, password_hash, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (username, password_hash, now),
                )
                admin_id = int(cursor.lastrowid)
        admin = self.get_admin_by_id(admin_id)
        if admin is None:
            raise RuntimeError("Failed to load admin after upsert")
        return admin

    def touch_admin_login(self, admin_id: int) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE admins SET last_login_at = ? WHERE id = ?",
                (utc_now_iso(), admin_id),
            )

    def get_admin_by_id(self, admin_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM admins WHERE id = ?",
                (admin_id,),
            ).fetchone()
        return self._row_to_dict(row)

    def list_endpoints(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM endpoints ORDER BY name COLLATE NOCASE ASC"
            ).fetchall()
        return [self._row_to_dict(row) for row in rows if row is not None]

    def get_endpoint(self, endpoint_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM endpoints WHERE id = ?",
                (endpoint_id,),
            ).fetchone()
        return self._row_to_dict(row)

    def create_endpoint(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_now_iso()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO endpoints (
                    name, tunnel_type, listen_host, listen_port, destination_host,
                    destination_port, ssh_host, ssh_port, ssh_username,
                    ssh_private_key_path, ssh_known_hosts_path, ssh_options,
                    description, allowed_client_cidr, enabled,
                    max_clients, idle_timeout, tags, status_message, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["name"],
                    payload["tunnel_type"],
                    payload["listen_host"],
                    payload["listen_port"],
                    payload["destination_host"],
                    payload["destination_port"],
                    payload["ssh_host"],
                    payload["ssh_port"],
                    payload["ssh_username"],
                    payload["ssh_private_key_path"],
                    payload["ssh_known_hosts_path"],
                    payload["ssh_options"],
                    payload["description"],
                    payload["allowed_client_cidr"],
                    int(bool(payload["enabled"])),
                    payload["max_clients"],
                    payload["idle_timeout"],
                    payload["tags"],
                    payload.get("status_message"),
                    now,
                    now,
                ),
            )
            endpoint_id = int(cursor.lastrowid)
        endpoint = self.get_endpoint(endpoint_id)
        if endpoint is None:
            raise RuntimeError("Failed to load endpoint after create")
        return endpoint

    def update_endpoint(self, endpoint_id: int, payload: dict[str, Any]) -> dict[str, Any] | None:
        now = utc_now_iso()
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE endpoints
                SET name = ?,
                    tunnel_type = ?,
                    listen_host = ?,
                    listen_port = ?,
                    destination_host = ?,
                    destination_port = ?,
                    ssh_host = ?,
                    ssh_port = ?,
                    ssh_username = ?,
                    ssh_private_key_path = ?,
                    ssh_known_hosts_path = ?,
                    ssh_options = ?,
                    description = ?,
                    allowed_client_cidr = ?,
                    enabled = ?,
                    max_clients = ?,
                    idle_timeout = ?,
                    tags = ?,
                    status_message = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    payload["name"],
                    payload["tunnel_type"],
                    payload["listen_host"],
                    payload["listen_port"],
                    payload["destination_host"],
                    payload["destination_port"],
                    payload["ssh_host"],
                    payload["ssh_port"],
                    payload["ssh_username"],
                    payload["ssh_private_key_path"],
                    payload["ssh_known_hosts_path"],
                    payload["ssh_options"],
                    payload["description"],
                    payload["allowed_client_cidr"],
                    int(bool(payload["enabled"])),
                    payload["max_clients"],
                    payload["idle_timeout"],
                    payload["tags"],
                    payload.get("status_message"),
                    now,
                    endpoint_id,
                ),
            )
        return self.get_endpoint(endpoint_id)

    def delete_endpoint(self, endpoint_id: int) -> None:
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM endpoints WHERE id = ?", (endpoint_id,))

    def set_endpoint_enabled(self, endpoint_id: int, enabled: bool) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE endpoints SET enabled = ?, updated_at = ? WHERE id = ?",
                (int(enabled), utc_now_iso(), endpoint_id),
            )

    def update_endpoint_status_message(self, endpoint_id: int, message: str | None) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE endpoints SET status_message = ?, updated_at = ? WHERE id = ?",
                (message, utc_now_iso(), endpoint_id),
            )

    def create_session_record(
        self,
        endpoint_id: int,
        client_ip: str,
        client_port: int,
        upstream_ip: str,
        upstream_port: int,
        status: str = "active",
    ) -> int:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO sessions (
                    endpoint_id, client_ip, client_port, upstream_ip, upstream_port,
                    status, connected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    endpoint_id,
                    client_ip,
                    client_port,
                    upstream_ip,
                    upstream_port,
                    status,
                    utc_now_iso(),
                ),
            )
            return int(cursor.lastrowid)

    def close_session_record(
        self,
        session_id: int,
        status: str,
        bytes_up: int,
        bytes_down: int,
        close_reason: str,
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE sessions
                SET status = ?,
                    bytes_up = ?,
                    bytes_down = ?,
                    close_reason = ?,
                    closed_at = ?
                WHERE id = ?
                """,
                (status, bytes_up, bytes_down, close_reason, utc_now_iso(), session_id),
            )

    def list_recent_sessions(
        self,
        endpoint_id: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if endpoint_id is None:
            query = "SELECT * FROM sessions ORDER BY connected_at DESC LIMIT ?"
            params = (limit,)
        else:
            query = """
                SELECT * FROM sessions
                WHERE endpoint_id = ?
                ORDER BY connected_at DESC
                LIMIT ?
            """
            params = (endpoint_id, limit)
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def traffic_totals_by_endpoint(self) -> dict[int, dict[str, int]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT endpoint_id,
                       COALESCE(SUM(bytes_up), 0) AS bytes_up,
                       COALESCE(SUM(bytes_down), 0) AS bytes_down
                FROM sessions
                GROUP BY endpoint_id
                """
            ).fetchall()
        totals: dict[int, dict[str, int]] = {}
        for row in rows:
            totals[int(row["endpoint_id"])] = {
                "bytes_up": int(row["bytes_up"]),
                "bytes_down": int(row["bytes_down"]),
            }
        return totals

    def insert_metrics(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        payload = [
            (
                row["ts"],
                row.get("endpoint_id"),
                row["active_connections"],
                row["bytes_up_per_sec"],
                row["bytes_down_per_sec"],
            )
            for row in rows
        ]
        with self._lock, self._connection:
            self._connection.executemany(
                """
                INSERT INTO metrics_timeseries (
                    ts, endpoint_id, active_connections, bytes_up_per_sec, bytes_down_per_sec
                ) VALUES (?, ?, ?, ?, ?)
                """,
                payload,
            )

    def list_metrics(
        self,
        since_ts: int,
        endpoint_id: int | None = None,
    ) -> list[dict[str, Any]]:
        if endpoint_id is None:
            query = """
                SELECT ts, endpoint_id, active_connections, bytes_up_per_sec, bytes_down_per_sec
                FROM metrics_timeseries
                WHERE ts >= ? AND endpoint_id IS NULL
                ORDER BY ts ASC
            """
            params = (since_ts,)
        else:
            query = """
                SELECT ts, endpoint_id, active_connections, bytes_up_per_sec, bytes_down_per_sec
                FROM metrics_timeseries
                WHERE ts >= ? AND endpoint_id = ?
                ORDER BY ts ASC
            """
            params = (since_ts, endpoint_id)
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def prune_metrics(self, older_than_ts: int) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM metrics_timeseries WHERE ts < ?",
                (older_than_ts,),
            )

    def record_audit(
        self,
        actor: str,
        action: str,
        endpoint_id: int | None = None,
        details: str = "",
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO audit_logs (actor, action, endpoint_id, details, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (actor, action, endpoint_id, details, utc_now_iso()),
            )

    def list_audit_logs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
