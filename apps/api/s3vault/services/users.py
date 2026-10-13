"""User accounts, opaque sessions and password resets."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from ..db import Database
from ..errors import (
    AuthError,
    ConflictError,
    NotFoundError,
    RateLimitedError,
    ValidationError,
)
from ..security.passwords import (
    PasswordHashError,
    hash_password,
    verify_and_maybe_rehash,
)
from ..security.tokens import (
    expires_in,
    hash_token,
    new_token,
    utcnow,
)

MAX_FAILED_LOGINS = 8
LOCKOUT_MINUTES = 15
RESET_TOKEN_TTL_S = 3600


@dataclass(frozen=True, slots=True)
class Actor:
    """The authenticated principal for a request."""

    user_id: str
    username: str
    role: str
    session_id: str | None = None
    credential_id: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class UserService:
    def __init__(self, db: Database, *, password_min_length: int = 12) -> None:
        self._db = db
        self._min_length = password_min_length

    # -- roles --------------------------------------------------------
    def _role_id(self, role: str) -> int:
        row = self._db.fetch_one("SELECT id FROM roles WHERE name = %s", (role,))
        if not row:
            raise ValidationError(f"unknown role: {role!r}")
        return int(row["id"])

    def role_of(self, user_id: str) -> str:
        row = self._db.fetch_one(
            """
            SELECT r.name FROM users u JOIN roles r ON r.id = u.role_id
            WHERE u.id = %s
            """,
            (user_id,),
        )
        if not row:
            raise NotFoundError("user not found")
        return str(row["name"])

    # -- creation ------------------------------------------------------
    def validate_password(self, password: str) -> None:
        if not isinstance(password, str) or len(password) < self._min_length:
            raise ValidationError(
                f"password must be at least {self._min_length} characters",
                details={"minimum_length": self._min_length},
            )
        if password.lower() in {"password", "changeme", "administrator"}:
            raise ValidationError("password is too easily guessed")

    def create_user(
        self,
        *,
        username: str,
        email: str,
        password: str,
        role: str = "owner",
        is_active: bool = True,
    ) -> dict[str, Any]:
        username = username.strip().lower()
        email = email.strip().lower()
        if not 3 <= len(username) <= 50:
            raise ValidationError("username must be 3-50 characters")
        if "@" not in email or "." not in email.split("@")[-1]:
            raise ValidationError("email is not a valid address")
        self.validate_password(password)

        if self._db.fetch_one("SELECT 1 FROM users WHERE username = %s", (username,)):
            raise ConflictError("username is already taken")
        if self._db.fetch_one("SELECT 1 FROM users WHERE email = %s", (email,)):
            raise ConflictError("email is already registered")

        row = self._db.fetch_one(
            """
            INSERT INTO users (username, email, password_hash, role_id, is_active)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id, username, email, role_id, is_active, created_at
            """,
            (username, email, hash_password(password), self._role_id(role), is_active),
        )
        assert row is not None
        row["role"] = role
        row.pop("role_id", None)
        return row

    def count(self) -> int:
        row = self._db.fetch_one("SELECT count(*) AS n FROM users")
        return int(row["n"]) if row else 0

    def has_any_admin(self) -> bool:
        row = self._db.fetch_one(
            """
            SELECT 1 FROM users u JOIN roles r ON r.id = u.role_id
            WHERE r.name = 'admin' AND u.is_active
            LIMIT 1
            """
        )
        return row is not None

    # -- listing / lookup ----------------------------------------------
    def list_users(self, *, limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        total_row = self._db.fetch_one("SELECT count(*) AS n FROM users")
        rows = self._db.fetch_all(
            """
            SELECT u.id, u.username, u.email, u.is_active, u.created_at,
                   u.last_login_at, r.name AS role
            FROM users u JOIN roles r ON r.id = u.role_id
            ORDER BY u.created_at ASC
            LIMIT %s OFFSET %s
            """,
            (limit, offset),
        )
        return rows, int(total_row["n"]) if total_row else 0

    def get(self, user_id: str) -> dict[str, Any]:
        row = self._db.fetch_one(
            """
            SELECT u.id, u.username, u.email, u.is_active, u.created_at,
                   u.last_login_at, u.password_changed_at, r.name AS role
            FROM users u JOIN roles r ON r.id = u.role_id
            WHERE u.id = %s
            """,
            (user_id,),
        )
        if not row:
            raise NotFoundError("user not found")
        return row

    # -- authentication -------------------------------------------------
    def authenticate(self, *, username: str, password: str, ip: str | None) -> dict[str, Any]:
        """Verify credentials, applying lockout on repeated failure."""
        row = self._db.fetch_one(
            """
            SELECT u.id, u.username, u.email, u.password_hash, u.is_active,
                   u.failed_logins, u.locked_until, r.name AS role
            FROM users u JOIN roles r ON r.id = u.role_id
            WHERE u.username = %s
            """,
            (username.strip().lower(),),
        )

        # Uniform failure path: do not disclose whether the account exists.
        if row is None:
            raise AuthError("invalid username or password")

        if row["locked_until"] is not None:
            locked_until = row["locked_until"]
            if isinstance(locked_until, dt.datetime):
                locked_until = locked_until.replace(tzinfo=dt.UTC)
            if locked_until > utcnow():
                raise RateLimitedError(
                    "account is temporarily locked after repeated failed sign-in attempts",
                    details={"retry_after_seconds": int((locked_until - utcnow()).total_seconds())},
                )

        if not row["is_active"]:
            raise AuthError("invalid username or password")

        try:
            ok, replacement = verify_and_maybe_rehash(password, str(row["password_hash"]))
        except PasswordHashError as exc:
            raise AuthError("invalid username or password") from exc

        if not ok:
            self._register_failure(row["id"], int(row["failed_logins"]))
            raise AuthError("invalid username or password")

        self._db.execute(
            """
            UPDATE users
               SET failed_logins = 0, locked_until = NULL, last_login_at = now()
             WHERE id = %s
            """,
            (row["id"],),
        )
        if replacement:
            # Opportunistic upgrade of password-hash parameters.
            self._db.execute(
                "UPDATE users SET password_hash = %s WHERE id = %s", (replacement, row["id"])
            )
        return {
            "id": row["id"],
            "username": row["username"],
            "email": row["email"],
            "role": row["role"],
            "ip": ip,
        }

    def _register_failure(self, user_id: str, current_failures: int) -> None:
        failures = current_failures + 1
        lock_until = None
        if failures >= MAX_FAILED_LOGINS:
            lock_until = expires_in(LOCKOUT_MINUTES * 60)
        self._db.execute(
            "UPDATE users SET failed_logins = %s, locked_until = %s WHERE id = %s",
            (failures, lock_until, user_id),
        )

    # -- sessions -------------------------------------------------------
    def create_session(
        self,
        *,
        user_id: str,
        ip: str | None,
        user_agent: str | None,
        session_ttl_s: int,
        idle_ttl_s: int,
    ) -> tuple[str, str, dt.datetime]:
        """Create a session; returns (session_token, csrf_token, expires_at)."""
        session_token = new_token()
        csrf_token = new_token()
        now = utcnow()
        absolute = now + dt.timedelta(seconds=session_ttl_s)
        idle = now + dt.timedelta(seconds=idle_ttl_s)

        self._db.execute(
            """
            INSERT INTO sessions
                (user_id, token_hash, csrf_token_hash, ip_address, user_agent,
                 absolute_expires_at, idle_expires_at, created_at, last_seen_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                user_id,
                hash_token(session_token),
                hash_token(csrf_token),
                _valid_ip(ip),
                (user_agent or "")[:512] or None,
                absolute,
                idle,
                now,
                now,
            ),
        )
        return session_token, csrf_token, absolute

    def load_session(self, session_token: str) -> dict[str, Any] | None:
        """Return the session and actor if valid and not expired, else ``None``."""
        row = self._db.fetch_one(
            """
            SELECT s.id AS session_id, s.csrf_token_hash, s.absolute_expires_at,
                   s.idle_expires_at, s.last_seen_at,
                   u.id AS user_id, u.username, u.is_active, r.name AS role
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            JOIN roles r ON r.id = u.role_id
            WHERE s.token_hash = %s AND s.revoked_at IS NULL
            """,
            (hash_token(session_token),),
        )
        if row is None:
            return None
        if not row["is_active"]:
            return None
        now = utcnow()
        absolute = _aware(row["absolute_expires_at"])
        idle = _aware(row["idle_expires_at"])
        if absolute <= now or idle <= now:
            return None
        return row

    def touch_session(self, session_id: str, idle_ttl_s: int) -> None:
        self._db.execute(
            "UPDATE sessions SET last_seen_at = now(), idle_expires_at = now() + (%s * interval '1 second') WHERE id = %s",
            (idle_ttl_s, session_id),
        )

    def revoke_session(self, session_token: str, *, reason: str = "user_request") -> bool:
        rows = self._db.execute(
            """
            UPDATE sessions SET revoked_at = now(), revoke_reason = %s
            WHERE token_hash = %s AND revoked_at IS NULL
            """,
            (reason, hash_token(session_token)),
        )
        return rows > 0

    def revoke_all_sessions(self, user_id: str, *, reason: str = "admin_revoked") -> int:
        return self._db.execute(
            """
            UPDATE sessions SET revoked_at = now(), revoke_reason = %s
            WHERE user_id = %s AND revoked_at IS NULL
            """,
            (reason, user_id),
        )

    def active_session_count(self, user_id: str) -> int:
        row = self._db.fetch_one(
            """
            SELECT count(*) AS n FROM sessions
            WHERE user_id = %s AND revoked_at IS NULL AND absolute_expires_at > now()
            """,
            (user_id,),
        )
        return int(row["n"]) if row else 0

    def purge_expired_sessions(self) -> int:
        return self._db.execute(
            "DELETE FROM sessions WHERE absolute_expires_at < now() OR idle_expires_at < now()"
        )

    # -- password reset --------------------------------------------------
    def issue_reset_token(self, email: str) -> str | None:
        """Return a reset token, or ``None`` when the email is unknown.

        Callers must respond identically either way so the endpoint cannot be
        used to enumerate accounts.
        """
        row = self._db.fetch_one(
            "SELECT id FROM users WHERE email = %s AND is_active", (email.strip().lower(),)
        )
        if row is None:
            return None
        token = new_token()
        self._db.execute(
            """
            INSERT INTO password_reset_tokens (user_id, token_hash, expires_at)
            VALUES (%s, %s, %s)
            """,
            (row["id"], hash_token(token), expires_in(RESET_TOKEN_TTL_S)),
        )
        return token

    def complete_reset(self, token: str, new_password: str) -> None:
        self.validate_password(new_password)
        row = self._db.fetch_one(
            """
            SELECT id, user_id FROM password_reset_tokens
            WHERE token_hash = %s AND used_at IS NULL AND expires_at > now()
            """,
            (hash_token(token),),
        )
        if row is None:
            raise AuthError("reset token is invalid or has expired")
        self._db.execute(
            "UPDATE password_reset_tokens SET used_at = now() WHERE id = %s", (row["id"],)
        )
        self._db.execute(
            """
            UPDATE users
               SET password_hash = %s, password_changed_at = now(),
                   failed_logins = 0, locked_until = NULL
             WHERE id = %s
            """,
            (hash_password(new_password), row["user_id"]),
        )
        # A password change invalidates every existing session.
        self.revoke_all_sessions(row["user_id"], reason="password_reset")

    # -- administration ----------------------------------------------------
    def set_active(self, user_id: str, active: bool) -> dict[str, Any]:
        if not active:
            self.revoke_all_sessions(user_id, reason="deactivated")
        self._db.execute("UPDATE users SET is_active = %s, updated_at = now() WHERE id = %s", (active, user_id))
        return self.get(user_id)

    def change_role(self, user_id: str, role: str) -> dict[str, Any]:
        self._role_id(role)
        self._db.execute(
            "UPDATE users SET role_id = %s, updated_at = now() WHERE id = %s",
            (self._role_id(role), user_id),
        )
        return self.get(user_id)

    def change_password(self, user_id: str, new_password: str) -> None:
        self.validate_password(new_password)
        self._db.execute(
            """
            UPDATE users SET password_hash = %s, password_changed_at = now(),
                             updated_at = now()
             WHERE id = %s
            """,
            (hash_password(new_password), user_id),
        )
        self.revoke_all_sessions(user_id, reason="password_changed")


def _aware(value: dt.datetime | None) -> dt.datetime:
    if value is None:
        return dt.datetime.min.replace(tzinfo=dt.UTC)
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def _valid_ip(value: str | None) -> str | None:
    """Normalise a client address for an ``INET`` column, or drop it.

    Test clients and some proxies present non-IP hostnames; storing an
    unparseable value would abort the write, so it is discarded instead.
    """
    if not value:
        return None
    import ipaddress

    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None