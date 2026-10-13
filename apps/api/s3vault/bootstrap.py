"""Administrator bootstrap.

Creates the first administrator from configuration when no active admin
exists. Safe to run on every start: it is a no-op once an admin is present, so
an unset ``BOOTSTRAP_ADMIN_PASSWORD`` cannot silently reset an existing
account.
"""

from __future__ import annotations

import structlog

from .config import AppConfig
from .services.users import UserService

log = structlog.get_logger("s3vault.bootstrap")


def bootstrap_admin(config: AppConfig, users: UserService) -> str | None:
    """Create the initial administrator if configuration supplies one.

    Returns the new user id, or ``None`` when nothing was created.
    """
    username = config.security.bootstrap_admin_user
    email = config.security.bootstrap_admin_email
    password = config.security.bootstrap_admin_password

    if not (username and email and password):
        return None

    if users.has_any_admin():
        log.debug("bootstrap.skipped", reason="an administrator already exists")
        return None

    user = users.create_user(
        username=username,
        email=email,
        password=password,
        role="admin",
    )
    log.warning(
        "bootstrap.admin_created",
        username=user["username"],
        guidance="remove BOOTSTRAP_ADMIN_PASSWORD from the environment after first sign-in",
    )
    return str(user["id"])


def main() -> int:  # pragma: no cover - CLI entrypoint
    """Run bootstrap as a standalone command (used by compose init)."""
    from .app import Services
    from .config import load_config
    from .db import Migrator

    config = load_config()
    services = Services(config)
    try:
        Migrator(services.db).apply()
        user_id = bootstrap_admin(config, services.users)
        if user_id:
            print(f"administrator created: {user_id}")
        else:
            print("no administrator created (already present or not configured)")
        return 0
    finally:
        services.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())