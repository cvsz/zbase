# syntax=docker/dockerfile:1.7
# S3Vault application image.
#
# Pinned to an explicit interpreter minor version so the interpreter used in CI
# is the interpreter used in production.

FROM python:3.14-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# Dependencies first so the layer caches across source changes.
COPY requirements.txt ./
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install -r requirements.txt

COPY apps ./apps
COPY migrations ./migrations

# Byte-compile to catch syntax errors at build time rather than at first request.
RUN /opt/venv/bin/python -m compileall -q apps

# ---------------------------------------------------------------------------
FROM python:3.14-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    S3VAULT_ENV=production

# curl is used by the compose healthcheck; nothing else is added.
RUN apt-get update \
 && apt-get install --no-install-recommends -y curl \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --from=builder /build/apps /app/apps
COPY --from=builder /build/migrations /app/migrations

# Run as a non-root user with no shell.
RUN groupadd --system --gid 1001 s3vault \
 && useradd --system --uid 1001 --gid s3vault --no-create-home --shell /usr/sbin/nologin s3vault \
 && mkdir -p /app/data /app/logs \
 && chown -R s3vault:s3vault /app
USER s3vault:s3vault

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:3000/healthz || exit 1

# exec form so uvicorn receives SIGTERM and the app can shut down gracefully.
CMD ["uvicorn", "s3vault.main:app", \
     "--app-dir", "/app/apps/api", \
     "--host", "0.0.0.0", \
     "--port", "3000", \
     "--no-access-log", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*"]