"""Family logins: scrypt passwords, server-side sessions, personal upload tokens and a login rate limit."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request

from . import db

SESSION_COOKIE = "fa_session"
SESSION_DAYS = 90
MIN_PASSWORD = 10
_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=_N, r=_R, p=_P)
    return hmac.compare_digest(digest.hex(), digest_hex)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def has_users() -> bool:
    with db.connect() as conn:
        return db.row(conn, "SELECT 1 FROM users LIMIT 1") is not None


def create_user(name: str, username: str, password: str) -> dict:
    with db.connect() as conn:
        if db.row(conn, "SELECT 1 FROM users WHERE username = ?", username):
            raise HTTPException(409, "That username is taken")
        cur = conn.execute("INSERT INTO users (name, username, password_hash) VALUES (?, ?, ?)",
                           (name, username, hash_password(password)))
        return public_user(db.row(conn, "SELECT * FROM users WHERE id = ?", cur.lastrowid))


def public_user(u: dict) -> dict:
    return {"id": u["id"], "name": u["name"], "username": u["username"], "has_upload_token": bool(u["upload_token_hash"])}


def start_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    with db.connect() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (_now(),))
        conn.execute("INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                     (_sha256(token), user_id, expires))
    return token


def end_session(token: str) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_sha256(token),))


def end_other_sessions(user_id: int, keep_token: str) -> None:
    """After a password change: sign out every other browser of this user."""
    with db.connect() as conn:
        conn.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?", (user_id, _sha256(keep_token)))


def user_for_session(token: str) -> dict | None:
    if not token:
        return None
    with db.connect() as conn:
        return db.row(conn, "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
                            "WHERE s.token_hash = ? AND s.expires_at > ?", _sha256(token), _now())


def new_upload_token(user_id: int) -> str:
    token = "fa_" + secrets.token_urlsafe(32)
    with db.connect() as conn:
        conn.execute("UPDATE users SET upload_token_hash = ? WHERE id = ?", (_sha256(token), user_id))
    return token


def user_for_upload_token(token: str) -> dict | None:
    if not token:
        return None
    with db.connect() as conn:
        return db.row(conn, "SELECT * FROM users WHERE upload_token_hash = ?", _sha256(token))


# --- login rate limit ----------------------------------------------------------------------------
# In memory, so it resets on restart; fine for one server process.

_failures: dict[str, deque] = defaultdict(deque)
_lock = threading.Lock()
LOGIN_WINDOW_S = 15 * 60
MAX_FAILS_PER_USERNAME = 5
MAX_FAILS_PER_IP = 20


def client_ip(request: Request) -> str:
    # Render sits behind Cloudflare, which overwrites CF-Connecting-IP with the address that connected.
    value = (request.headers.get("cf-connecting-ip") or "").strip()
    return value or (request.client.host if request.client else "unknown")


def _recent(key: str, now: float) -> deque:
    hits = _failures[key]
    while hits and now - hits[0] > LOGIN_WINDOW_S:
        hits.popleft()
    return hits


def check_login_allowed(request: Request, username: str) -> None:
    now = time.monotonic()
    with _lock:
        if (len(_recent("u:" + username.lower(), now)) >= MAX_FAILS_PER_USERNAME
                or len(_recent("ip:" + client_ip(request), now)) >= MAX_FAILS_PER_IP):
            raise HTTPException(429, "Too many failed attempts. Please wait 15 minutes and try again.")


def record_login_failure(request: Request, username: str) -> None:
    now = time.monotonic()
    with _lock:
        _failures["u:" + username.lower()].append(now)
        _failures["ip:" + client_ip(request)].append(now)
        if len(_failures) > 10_000:  # drop keys with no recent failures
            for key in [k for k, hits in _failures.items() if not _recent(k, now)]:
                del _failures[key]


def reset_login_limits() -> None:
    with _lock:
        _failures.clear()
