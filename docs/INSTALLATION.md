# Installation

Two supported paths: Docker Compose on one host (recommended), or a hardened
systemd deployment. Both terminate TLS at a reverse proxy and keep the storage
engine and database off public interfaces.

## Requirements

| | Minimum | Recommended |
|---|---|---|
| CPU | 2 cores | 4+ cores |
| RAM | 4 GiB | 8+ GiB |
| Disk | 50 GiB SSD | 200+ GiB SSD |
| Docker | 26+ with Compose v2 | current |
| Public DNS | one hostname | — |

Tested on Debian 12 (bookworm) with Docker 29 and Compose v5.

## 1. Prepare the host

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2 openssl curl
sudo systemctl enable --now docker

# S3Vault runs as a non-root user inside the container; the host only needs Docker.
sudo groupadd -f s3vault && sudo usermod -aG docker "$USER"
```

Firewall: allow 80/tcp and 443/tcp (and 22/tcp from a restricted range). Nothing
else. The engine and database are never published by `docker-compose.yml`.

## 2. Get the code and configure it

```bash
sudo mkdir -p /opt/s3vault && sudo chown "$USER" /opt/s3vault
cd /opt/s3vault
git clone <your-repository-url> .
cp .env.example .env
```

Generate the secrets. Every one of these must be a real value:

```bash
python3 -c "import base64,os;print('KEK_BASE64='+base64.b64encode(os.urandom(32)).decode())"
openssl rand -base64 24          # POSTGRES_PASSWORD
openssl rand -hex 20             # STORAGE_ROOT_ACCESS_KEY
openssl rand -base64 40          # STORAGE_ROOT_SECRET_KEY
```

Edit `.env`:

```ini
PUBLIC_HOSTNAME=s3.example.com
POSTGRES_PASSWORD=<generated>
STORAGE_ROOT_ACCESS_KEY=<generated>
STORAGE_ROOT_SECRET_KEY=<generated>
KEK_BASE64=<generated>

# Optional: creates the first administrator on first start.
BOOTSTRAP_ADMIN_USER=admin
BOOTSTRAP_ADMIN_EMAIL=admin@example.com
BOOTSTRAP_ADMIN_PASSWORD=<a strong unique password>
```

The application refuses to start in production with a plaintext engine
endpoint, a placeholder hostname, a non-secure cookie, or an insecure secret.
That is deliberate — see [CONFIGURATION.md](CONFIGURATION.md).

## 3. Create the engine's identity

SeaweedFS reads its root credentials from the environment on first start and
stores them. The same values must be given to S3Vault, so set them once:

```bash
export STORAGE_ROOT_ACCESS_KEY=<generated>
export STORAGE_ROOT_SECRET_KEY=<generated>
```

They are supplied to both services from `.env`.

## 4. Obtain a TLS certificate

```bash
mkdir -p infra/proxy/certs
docker run --rm \
  -v "$PWD/infra/proxy/certs:/etc/letsencrypt" \
  -v "$PWD/certbot-webroot:/var/www/certbot" \
  certbot/certbot:4.1.0 certonly --webroot -w /var/www/certbot \
  -d s3.example.com --agree-tos -m admin@example.com --non-interactive
```

Copy the live certificate to the path nginx serves:

```bash
cp infra/proxy/certs/live/s3.example.com/fullchain.pem infra/proxy/certs/
cp infra/proxy/certs/live/s3.example.com/privkey.pem  infra/proxy/certs/
```

Enable automatic renewal:

```bash
docker compose --profile tls up -d certbot
```

## 5. Start

```bash
docker compose up -d --build
docker compose ps
```

Startup ordering is enforced with health checks and `condition: service_healthy`
rather than sleeps. The API waits for PostgreSQL; the proxy waits for the API.

## 6. Verify

```bash
scripts/verify-install.sh https://s3.example.com
```

This checks TLS, the HTTP→HTTPS redirect, HSTS, liveness, readiness,
unauthenticated access being refused, the console rendering, and that public
endpoints leak no secret material.

## 7. First sign-in

Open `https://s3.example.com` and sign in with the bootstrap administrator.
**Then remove the bootstrap password from `.env`** and restart, so the
credential is not sitting in a file:

```bash
sed -i '/^BOOTSTRAP_ADMIN_PASSWORD=/d' .env
docker compose up -d api
```

Bootstrap only acts when no administrator exists, so leaving it set is not a
way to reset an existing account — but it is still a plaintext credential on
disk.

## Hardened Linux deployment

Use when Docker is not acceptable.

### Services

| Service | Unit | Runs as |
|---|---|---|
| PostgreSQL 18 | `postgresql` | `postgres` |
| SeaweedFS | `s3vault-seaweedfs` | `s3vault` |
| API | `s3vault-api` | `s3vault` |
| Worker | `s3vault-worker` | `s3vault` |
| nginx | `nginx` | root (binds :443) |

### Layout

```
/opt/s3vault/app          code
/opt/s3vault/env          environment file, mode 0640 root:s3vault
/opt/s3vault/keystore     KEK_BASE64, mode 0600 root:s3vault
/var/lib/s3vault/engine   SeaweedFS volume
/var/lib/s3vault/pgdata   PostgreSQL data
/var/lib/s3vault/backups  backups
```

Keep the KEK outside the backup directory so a backup never contains both the
envelope and its key.

### Unit files

`/etc/systemd/system/s3vault-api.service`:

```ini
[Unit]
Description=S3Vault API
After=network-online.target postgresql.service s3vault-seaweedfs.service
Requires=postgresql.service s3vault-seaweedfs.service

[Service]
Type=exec
User=s3vault
Group=s3vault
WorkingDirectory=/opt/s3vault/app
EnvironmentFile=/opt/s3vault/env
ExecStart=/opt/s3vault/venv/bin/uvicorn s3vault.main:app \
  --app-dir /opt/s3vault/app/apps/api --host 127.0.0.1 --port 3000 \
  --no-access-log --proxy-headers --forwarded-allow-ips 127.0.0.1
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30

# The API needs no write access outside its own directories.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/s3vault/backups

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/s3vault-worker.service` is the same with
`ExecStart=/opt/s3vault/venv/bin/python /opt/s3vault/app/apps/worker/worker.py`.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now s3vault-api s3vault-worker
```

## Private network / no public DNS

For a LAN or homelab install, skip the proxy and reach the API directly:

```bash
PUBLIC_HOSTNAME=s3vault.lan
COOKIE_SECURE=false
TRUSTED_PROXY_HOPS=0
```

This serves plain HTTP and must not be exposed to an untrusted network. The
production profile will refuse to start with `COOKIE_SECURE=false`, so use
`S3VAULT_ENV=development` and treat it as a lab install.

## Upgrading

See [UPGRADES.md](UPGRADES.md).

## What is not covered

- Multi-node or high-availability deployment.
- Automated certificate issuance at first boot (certbot is a separate,
  explicit step).
- Kubernetes manifests. See [OPERATIONS.md](OPERATIONS.md).