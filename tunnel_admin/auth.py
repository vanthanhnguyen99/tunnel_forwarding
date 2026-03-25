from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import secrets
import threading
from typing import Any


PBKDF2_ITERATIONS = 260000


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iterations, salt_hex, digest_hex = encoded.split("$", 3)
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt_hex),
        int(iterations),
    )
    return hmac.compare_digest(digest.hex(), digest_hex)


@dataclass(slots=True)
class AuthSession:
    token: str
    user_id: int
    username: str
    expires_at: datetime

    def is_expired(self) -> bool:
        return utc_now() >= self.expires_at


class AuthManager:
    def __init__(self, cookie_name: str, session_ttl_seconds: int) -> None:
        self.cookie_name = cookie_name
        self.session_ttl_seconds = session_ttl_seconds
        self._sessions: dict[str, AuthSession] = {}
        self._lock = threading.Lock()

    def create_session(self, user_id: int, username: str) -> AuthSession:
        token = secrets.token_urlsafe(32)
        session = AuthSession(
            token=token,
            user_id=user_id,
            username=username,
            expires_at=utc_now() + timedelta(seconds=self.session_ttl_seconds),
        )
        with self._lock:
            self._sessions[token] = session
        return session

    def get_session(self, token: str | None) -> AuthSession | None:
        if not token:
            return None
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                return None
            if session.is_expired():
                self._sessions.pop(token, None)
                return None
            return session

    def destroy_session(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)

    def purge_expired(self) -> None:
        with self._lock:
            expired = [token for token, session in self._sessions.items() if session.is_expired()]
            for token in expired:
                self._sessions.pop(token, None)

    def cookie_header(self, token: str, secure: bool = False) -> str:
        max_age = self.session_ttl_seconds
        secure_suffix = "; Secure" if secure else ""
        return (
            f"{self.cookie_name}={token}; Path=/; HttpOnly; SameSite=Lax; "
            f"Max-Age={max_age}{secure_suffix}"
        )

    def clear_cookie_header(self) -> str:
        return f"{self.cookie_name}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"

    @staticmethod
    def session_to_dict(session: AuthSession) -> dict[str, Any]:
        return {
            "user_id": session.user_id,
            "username": session.username,
            "expires_at": session.expires_at.isoformat(),
        }
