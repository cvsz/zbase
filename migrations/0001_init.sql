-- 0001_init.sql — application-owned metadata schema.
--
-- Scope: users, sessions, roles, credentials, bucket metadata, quotas,
-- usage snapshots, audit events, background jobs, app settings.
--
-- Object bytes and engine-owned object metadata are deliberately NOT stored
-- here. The storage engine remains the only source of truth for objects.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------- roles
CREATE TABLE roles (
    id          SMALLSERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO roles (name, description) VALUES
    ('owner',    'Full control of owned resources'),
    ('admin',    'Full control of all resources and settings'),
    ('operator', 'Read/write on buckets and objects, no credential issuance'),
    ('auditor',  'Read-only access to metrics and audit records')
ON CONFLICT (name) DO NOTHING;

-- --------------------------------------------------------------- users
CREATE TABLE users (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username       TEXT NOT NULL UNIQUE,
    email          TEXT NOT NULL UNIQUE,
    password_hash  TEXT NOT NULL,
    role_id        SMALLINT NOT NULL REFERENCES roles(id),
    is_active      BOOLEAN NOT NULL DEFAULT TRUE,
    failed_logins  INTEGER NOT NULL DEFAULT 0,
    locked_until   TIMESTAMPTZ,
    last_login_at  TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    password_changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT users_username_format CHECK (username ~ '^[a-z0-9][a-z0-9._-]{2,49}$')
);

CREATE INDEX users_role_idx ON users (role_id);
CREATE INDEX users_active_idx ON users (is_active) WHERE is_active;

-- Password reset tokens: only a hash is stored.
CREATE TABLE password_reset_tokens (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash  TEXT NOT NULL UNIQUE,
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX password_reset_user_idx ON password_reset_tokens (user_id);

-- ----------------------------------------------------------- sessions
-- Opaque, hashed session tokens. No JWT is stored server-side; the token
-- itself lives only in the client cookie.
CREATE TABLE sessions (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash        TEXT NOT NULL UNIQUE,
    csrf_token_hash   TEXT NOT NULL,
    ip_address        INET,
    user_agent        TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    absolute_expires_at TIMESTAMPTZ NOT NULL,
    idle_expires_at   TIMESTAMPTZ NOT NULL,
    revoked_at        TIMESTAMPTZ,
    revoke_reason     TEXT
);

CREATE INDEX sessions_user_idx ON sessions (user_id) WHERE revoked_at IS NULL;
CREATE INDEX sessions_idle_expiry_idx ON sessions (idle_expires_at) WHERE revoked_at IS NULL;

-- -------------------------------------------------------- credentials
-- S3 API credentials issued by S3Vault.
--
-- secret_ciphertext holds an AES-256-GCM envelope of the secret access key,
-- encrypted under KEK_BASE64 (held outside the database). The plaintext is
-- never stored and never returned by the API after creation.
CREATE TABLE credentials (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    access_key_id     TEXT NOT NULL UNIQUE,
    secret_ciphertext BYTEA NOT NULL,
    description       TEXT NOT NULL DEFAULT '',
    bucket_scope      TEXT[] NOT NULL DEFAULT '{}',
    prefix_scope      TEXT,
    permissions       TEXT[] NOT NULL,
    expires_at        TIMESTAMPTZ,
    revoked_at        TIMESTAMPTZ,
    last_used_at      TIMESTAMPTZ,
    created_by        UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    rotated_from      UUID REFERENCES credentials(id) ON DELETE SET NULL,
    CONSTRAINT credentials_perm_valid CHECK (
        permissions <@ ARRAY['read', 'write', 'readwrite']::TEXT[]
        AND cardinality(permissions) > 0
    )
);

CREATE INDEX credentials_user_idx ON credentials (user_id) WHERE revoked_at IS NULL;

-- ------------------------------------------------------------ buckets
-- Metadata mirror for engine buckets. The engine remains authoritative for
-- bucket existence; this table records ownership, policy and quota intent.
CREATE TABLE buckets (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name              TEXT NOT NULL UNIQUE,
    owner_id          UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_by        UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    versioning_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    cors_origins      TEXT[] NOT NULL DEFAULT '{}',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    engine_confirmed_at TIMESTAMPTZ,
    CONSTRAINT buckets_name_format CHECK (
        name ~ '^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$'
        AND name !~ '\.\.'
    )
);

CREATE INDEX buckets_owner_idx ON buckets (owner_id);

-- Engine-side lifecycle configuration, stored as JSON to stay forward
-- compatible with engine-specific schema evolution.
CREATE TABLE bucket_lifecycle (
    bucket_id    UUID PRIMARY KEY REFERENCES buckets(id) ON DELETE CASCADE,
    rules        JSONB NOT NULL DEFAULT '[]'::jsonb,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE bucket_policies (
    bucket_id  UUID PRIMARY KEY REFERENCES buckets(id) ON DELETE CASCADE,
    policy     JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------- quotas
CREATE TABLE quota_policies (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID REFERENCES users(id) ON DELETE CASCADE,
    bucket_id           UUID REFERENCES buckets(id) ON DELETE CASCADE,
    max_bytes           BIGINT NOT NULL DEFAULT -1,
    max_objects         BIGINT NOT NULL DEFAULT -1,
    max_object_bytes    BIGINT NOT NULL DEFAULT -1,
    enforcement_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT quota_target_xor CHECK ((user_id IS NULL) <> (bucket_id IS NULL)),
    CONSTRAINT quota_values_sane CHECK (
        (max_bytes = -1 OR max_bytes > 0)
        AND (max_objects = -1 OR max_objects > 0)
        AND (max_object_bytes = -1 OR max_object_bytes > 0)
    ),
    CONSTRAINT quota_user_unique UNIQUE NULLS NOT DISTINCT (user_id),
    CONSTRAINT quota_bucket_unique UNIQUE NULLS NOT DISTINCT (bucket_id)
);

-- Reservations are taken before an upload and settled afterwards, so that
-- concurrent uploads cannot both pass a bytes-remaining check.
CREATE TABLE quota_reservations (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    bucket_id    UUID NOT NULL REFERENCES buckets(id) ON DELETE CASCADE,
    bytes        BIGINT NOT NULL CHECK (bytes > 0),
    object_key   TEXT,
    state        TEXT NOT NULL DEFAULT 'held'
                 CHECK (state IN ('held', 'committed', 'released', 'expired')),
    expires_at   TIMESTAMPTZ NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled_at   TIMESTAMPTZ
);

CREATE INDEX quota_reservations_active_idx
    ON quota_reservations (user_id, bucket_id, state, expires_at);

-- -------------------------------------------------------- usage stats
CREATE TABLE usage_snapshots (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope          TEXT NOT NULL CHECK (scope IN ('global', 'user', 'bucket')),
    user_id        UUID REFERENCES users(id) ON DELETE CASCADE,
    bucket_id      UUID REFERENCES buckets(id) ON DELETE CASCADE,
    measured_at    TIMESTAMPTZ NOT NULL,
    storage_bytes  BIGINT NOT NULL DEFAULT 0,
    object_count   BIGINT NOT NULL DEFAULT 0,
    get_requests   BIGINT NOT NULL DEFAULT 0,
    put_requests   BIGINT NOT NULL DEFAULT 0,
    delete_requests BIGINT NOT NULL DEFAULT 0,
    measured_by    TEXT NOT NULL DEFAULT 'engine'
);

CREATE INDEX usage_scope_time_idx ON usage_snapshots (scope, measured_at DESC);
CREATE UNIQUE INDEX usage_bucket_time_uniq
    ON usage_snapshots (bucket_id, measured_at) WHERE scope = 'bucket';
CREATE UNIQUE INDEX usage_user_time_uniq
    ON usage_snapshots (user_id, measured_at) WHERE scope = 'user';
CREATE UNIQUE INDEX usage_global_time_uniq
    ON usage_snapshots (measured_at) WHERE scope = 'global';

-- ------------------------------------------------------ audit events
CREATE TABLE audit_events (
    id             BIGSERIAL PRIMARY KEY,
    occurred_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor_id       UUID REFERENCES users(id) ON DELETE SET NULL,
    actor_type     TEXT NOT NULL CHECK (actor_type IN ('user', 'credential', 'system')),
    actor_label    TEXT NOT NULL DEFAULT '',
    action         TEXT NOT NULL,
    resource_type  TEXT NOT NULL,
    resource_id    TEXT NOT NULL DEFAULT '',
    outcome        TEXT NOT NULL CHECK (outcome IN ('success', 'denied', 'failure')),
    request_id     TEXT NOT NULL DEFAULT '',
    ip_address     INET,
    user_agent     TEXT,
    detail         JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX audit_time_idx ON audit_events (occurred_at DESC);
CREATE INDEX audit_actor_idx ON audit_events (actor_id, occurred_at DESC);
CREATE INDEX audit_action_idx ON audit_events (action, occurred_at DESC);
CREATE INDEX audit_resource_idx ON audit_events (resource_type, resource_id);

-- ------------------------------------------------------ worker jobs
CREATE TABLE background_jobs (
    id            BIGSERIAL PRIMARY KEY,
    job_type      TEXT NOT NULL,
    payload       JSONB NOT NULL DEFAULT '{}'::jsonb,
    state         TEXT NOT NULL DEFAULT 'pending'
                  CHECK (state IN ('pending', 'running', 'done', 'failed')),
    run_after     TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts      INTEGER NOT NULL DEFAULT 0,
    max_attempts  INTEGER NOT NULL DEFAULT 5,
    last_error    TEXT,
    locked_at     TIMESTAMPTZ,
    locked_by     TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ
);

CREATE INDEX background_jobs_claim_idx
    ON background_jobs (job_type, state, run_after)
    WHERE state IN ('pending', 'running');

-- Backups are tracked so the console can show real backup status.
CREATE TABLE backup_records (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at    TIMESTAMPTZ,
    status         TEXT NOT NULL DEFAULT 'running'
                   CHECK (status IN ('running', 'ok', 'failed')),
    location       TEXT NOT NULL,
    bytes_total    BIGINT,
    checksum       TEXT,
    verified       BOOLEAN NOT NULL DEFAULT FALSE,
    detail         JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX backup_records_time_idx ON backup_records (started_at DESC);

-- ------------------------------------------------------- app settings
-- Secrets are never stored here. Only non-sensitive configuration and
-- masked indicators (e.g. "configured": true) are persisted.
CREATE TABLE application_settings (
    key         TEXT PRIMARY KEY,
    value       JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by  UUID REFERENCES users(id) ON DELETE SET NULL
);