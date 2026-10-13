"""PostgreSQL access layer: pooled connections and a forward-only migration runner.

Only application metadata lives here. Object bytes and object metadata that the
storage engine owns are never mirrored into PostgreSQL.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import AppConfig
from .errors import AppError

def _default_migrations_dir() -> Path:
    """Locate the repository ``migrations/`` directory.

    Walks up from this file so the package works from a source checkout, an
    editable install, or a container copy without hard-coded depths.
    ``S3VAULT_MIGRATIONS_DIR`` overrides the search.
    """
    override = os.environ.get("S3VAULT_MIGRATIONS_DIR")
    if override:
        return Path(override).expanduser().resolve()
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "migrations"
        if candidate.is_dir():
            return candidate
    # Fall back to the documented layout (apps/api/s3vault/db.py -> repo root).
    return here.parents[3] / "migrations"


MIGRATIONS_DIR = _default_migrations_dir()
_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


class Database:
    """Thin wrapper over a psycopg connection pool."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._pool = ConnectionPool(
            conninfo=config.db.dsn,
            min_size=config.db.pool_min,
            max_size=config.db.pool_max,
            kwargs={
                "row_factory": dict_row,
                "autocommit": False,
                "connect_timeout": config.db.connect_timeout_s,
                "application_name": "s3vault",
                "options": f"-c statement_timeout={config.db.statement_timeout_ms}",
            },
            open=True,
            timeout=config.db.connect_timeout_s * 2,
        )

    # -- lifecycle --------------------------------------------------
    def close(self) -> None:
        self._pool.close()

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection[Any]]:
        """Borrow a connection from the pool; commits on success."""
        with self._pool.connection() as conn:
            yield conn
            conn.commit()

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection[Any]]:
        """Explicit transaction; rolls back on exception."""
        with self._pool.connection() as conn:
            with conn.transaction():
                yield conn

    # -- queries ----------------------------------------------------
    def fetch_one(self, query: str, params: Sequence[Any] | None = None) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(query, params).fetchone()
        return dict(row) if row else None

    def fetch_all(self, query: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def execute(self, query: str, params: Sequence[Any] | None = None) -> int:
        with self.connection() as conn:
            cur = conn.execute(query, params)
        return cur.rowcount

    def execute_many(self, query: str, rows: Sequence[Sequence[Any]]) -> None:
        if not rows:
            return
        with self.connection() as conn:
            conn.execute(query, rows)

    # -- health -----------------------------------------------------
    def ping(self) -> bool:
        try:
            with self.connection() as conn:
                conn.execute("SELECT 1 AS ok")
            return True
        except psycopg.Error:
            return False


class Migrator:
    """Applies numbered ``.sql`` files once, tracked in ``schema_migrations``.

    Files are applied in lexicographic order. Each file runs inside one
    transaction together with its bookkeeping row, so a failure leaves the
    database on the last fully applied version.
    """

    def __init__(self, db: Database, directory: Path | None = None) -> None:
        self._db = db
        self._dir = directory or MIGRATIONS_DIR
        self._ensure_tracking_table()

    def _ensure_tracking_table(self) -> None:
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version     TEXT PRIMARY KEY,
                checksum    TEXT NOT NULL,
                applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )

    def _files(self) -> list[Path]:
        if not self._dir.is_dir():
            raise AppError(f"migrations directory not found: {self._dir}")
        files = sorted(p for p in self._dir.glob("*.sql") if p.is_file())
        if not files:
            raise AppError(f"no .sql migration files in {self._dir}")
        return files

    def applied(self) -> set[str]:
        rows = self._db.fetch_all("SELECT version FROM schema_migrations")
        return {str(r["version"]) for r in rows}

    def pending(self) -> list[Path]:
        done = self.applied()
        return [p for p in self._files() if p.name not in done]

    def apply(self, *, dry_run: bool = False) -> list[str]:
        """Apply pending migrations. Returns the versions applied (or that would be)."""
        applied: list[str] = []
        done = self.applied()
        for path in self._files():
            version = path.name
            body = path.read_text(encoding="utf-8")
            checksum = hashlib.sha256(body.encode("utf-8")).hexdigest()
            if version in done:
                existing = self._db.fetch_one(
                    "SELECT checksum FROM schema_migrations WHERE version = %s", (version,)
                )
                if existing and existing["checksum"] != checksum:
                    raise AppError(
                        f"migration {version} was modified after being applied "
                        "(checksum mismatch); create a new migration instead"
                    )
                continue
            if not dry_run:
                with self._db.transaction() as conn:
                    conn.execute(body)
                    conn.execute(
                        "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                        (version, checksum),
                    )
            applied.append(version)
        return applied

    # -- identifier safety -----------------------------------------
    @staticmethod
    def safe_identifier(name: str) -> sql.Identifier:
        """Guard against identifier injection when composing SQL identifiers."""
        if not _IDENT_RE.fullmatch(name):
            raise AppError(f"unsafe SQL identifier: {name!r}")
        return sql.Identifier(name)