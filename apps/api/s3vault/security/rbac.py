"""Role-based access control.

Roles are seeded by migration 0001 and looked up from the ``roles`` table.
Permissions are expressed as capabilities so route dependencies can ask for a
single capability instead of hard-coding role names.
"""

from __future__ import annotations

from enum import StrEnum


class Capability(StrEnum):
    BUCKET_READ = "bucket:read"
    BUCKET_WRITE = "bucket:write"
    BUCKET_DELETE = "bucket:delete"
    OBJECT_READ = "object:read"
    OBJECT_WRITE = "object:write"
    OBJECT_DELETE = "object:delete"
    CREDENTIAL_ISSUE = "credential:issue"
    USER_READ = "user:read"
    USER_WRITE = "user:write"
    AUDIT_READ = "audit:read"
    USAGE_READ = "usage:read"
    SETTINGS_READ = "settings:read"
    SETTINGS_WRITE = "settings:write"
    BACKUP_MANAGE = "backup:manage"
    ENGINE_ADMIN = "engine:admin"


#: Least-privilege grants. ``admin`` is the only role with engine access.
ROLE_CAPABILITIES: dict[str, frozenset[Capability]] = {
    "admin": frozenset(Capability),
    "owner": frozenset(
        {
            Capability.BUCKET_READ,
            Capability.BUCKET_WRITE,
            Capability.BUCKET_DELETE,
            Capability.OBJECT_READ,
            Capability.OBJECT_WRITE,
            Capability.OBJECT_DELETE,
            Capability.CREDENTIAL_ISSUE,
            Capability.USAGE_READ,
            Capability.AUDIT_READ,
        }
    ),
    "operator": frozenset(
        {
            Capability.BUCKET_READ,
            Capability.OBJECT_READ,
            Capability.OBJECT_WRITE,
            Capability.OBJECT_DELETE,
            Capability.USAGE_READ,
        }
    ),
    "auditor": frozenset(
        {
            Capability.BUCKET_READ,
            Capability.OBJECT_READ,
            Capability.USAGE_READ,
            Capability.AUDIT_READ,
            Capability.SETTINGS_READ,
        }
    ),
}

#: S3 credential permission strings -> required capabilities.
CREDENTIAL_PERMISSION_CAPABILITIES: dict[str, tuple[Capability, ...]] = {
    "read": (Capability.OBJECT_READ,),
    "write": (Capability.OBJECT_WRITE,),
    "readwrite": (Capability.OBJECT_READ, Capability.OBJECT_WRITE),
}


def capabilities_for_role(role: str) -> frozenset[Capability]:
    try:
        return ROLE_CAPABILITIES[role]
    except KeyError as exc:  # pragma: no cover - guarded by DB constraint
        raise ValueError(f"unknown role: {role!r}") from exc


def has_capability(role: str, capability: Capability) -> bool:
    return capability in capabilities_for_role(role)


def can_use_credential_permission(
    role: str, granted: frozenset[str], needed: str
) -> bool:
    """Check whether a role holding ``granted`` credential permissions may exercise ``needed``.

    ``readwrite`` implies ``read`` and ``write``.
    """
    granted_set = set(granted)
    if "readwrite" in granted_set:
        granted_set |= {"read", "write"}
    return needed in granted_set


def is_admin(role: str) -> bool:
    return role == "admin"