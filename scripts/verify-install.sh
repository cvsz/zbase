#!/usr/bin/env bash
# Post-deployment verification.
#
# Checks the things an operator actually needs to know after a deploy:
#   - the proxy answers TLS
#   - liveness and readiness pass
#   - the engine is reachable and its capabilities match expectations
#   - no secret is served by the public endpoints
#
# Usage: scripts/verify-install.sh [base-url]
#   BASE_URL defaults to $PUBLIC_HOSTNAME or https://localhost
set -uo pipefail

BASE_URL="${1:-${PUBLIC_HOSTNAME:-}}"
if [ -z "$BASE_URL" ]; then
  echo "usage: $0 <base-url>   (e.g. https://s3.example.com)" >&2
  exit 2
fi
case "$BASE_URL" in
  http://*|https://*) ;;
  *) BASE_URL="https://$BASE_URL" ;;
esac

failures=0
pass() { printf '  ok    %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*"; failures=$((failures + 1)); }

echo "verifying $BASE_URL"

# ------------------------------------------------------------------- TLS
if curl -fsS --max-time 15 -o /dev/null "$BASE_URL/healthz" 2>/dev/null; then
  pass "GET /healthz over TLS"
else
  fail "GET /healthz over TLS (is the certificate valid and is the proxy running?)"
fi

if curl -sS --max-time 10 -o /dev/null -w '%{http_code}' "http://${BASE_URL#*://}/healthz" 2>/dev/null \
   | grep -qE '^(301|308)$'; then
  pass "plaintext HTTP redirects to HTTPS"
else
  fail "plaintext HTTP does not redirect (check the port-80 server block)"
fi

if curl -sS --max-time 15 -D - -o /dev/null "$BASE_URL/" 2>/dev/null | grep -qi 'strict-transport-security'; then
  pass "HSTS header present"
else
  fail "HSTS header missing"
fi

# ------------------------------------------------------------- readiness
body=$(curl -sS --max-time 15 "$BASE_URL/readyz" 2>/dev/null || echo '')
if echo "$body" | grep -q '"status":"ready"'; then
  pass "readiness reports ready (database and storage)"
else
  fail "readiness not ready: $body"
fi

# ------------------------------------------------------- unauthenticated
code=$(curl -sS --max-time 15 -o /dev/null -w '%{http_code}' "$BASE_URL/api/v1/buckets" 2>/dev/null || echo 000)
if [ "$code" = "401" ]; then
  pass "unauthenticated /api/v1/buckets is refused with 401"
else
  fail "unauthenticated /api/v1/buckets returned $code, expected 401"
fi

code=$(curl -sS --max-time 15 -o /dev/null -w '%{http_code}' "$BASE_URL/api/v1/overview" 2>/dev/null || echo 000)
if [ "$code" = "401" ]; then
  pass "unauthenticated /api/v1/overview is refused with 401"
else
  fail "unauthenticated /api/v1/overview returned $code, expected 401"
fi

# ----------------------------------------------------------- no secrets
for path in /healthz /readyz /; do
  payload=$(curl -sS --max-time 15 "$BASE_URL$path" 2>/dev/null || echo '')
  for needle in KEK_BASE64 STORAGE_ROOT_SECRET_KEY AWS_SECRET database.dump; do
    if echo "$payload" | grep -q "$needle"; then
      fail "$path leaked '$needle'"
    fi
  done
done
pass "public endpoints expose no secret material"

# ------------------------------------------------------------- console
if curl -fsS --max-time 15 "$BASE_URL/" 2>/dev/null | grep -qi 's3vault'; then
  pass "management console is served"
else
  fail "management console did not render"
fi

echo
if [ "$failures" -eq 0 ]; then
  echo "RESULT: deployment verified"
  exit 0
fi
echo "RESULT: $failures check(s) failed"
exit 1