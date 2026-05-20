#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# install_redis_vps.sh — Uzum Seller Hub
#
# One-shot installer that stands up Redis on an Ubuntu 22.04+ VPS for use
# as the shared cache / revoke blocklist store (Step 1 of
# project_scaling_roadmap_20k.md).
#
# READ BEFORE RUNNING:
#   1. You MUST replace the placeholder password below (REDIS_PASSWORD)
#      before executing on a real host. Grep for "CHANGE_ME" and set a
#      strong value (openssl rand -base64 32).
#   2. Script is idempotent: safe to re-run — config backups append a
#      timestamp, systemctl enable/restart is a no-op if already correct.
#   3. Redis binds only to loopback (127.0.0.1 / ::1). If the app and
#      Redis live on different boxes, edit the bind line after review
#      and open the firewall deliberately.
#
# After this finishes, the exact REDIS_URL to put into the app's .env is
# printed at the end.
# ──────────────────────────────────────────────────────────────────────
set -euo pipefail

# ──────────────────────────────────────────────────────────────────────
# CHANGE_ME: replace before running in production.
REDIS_PASSWORD="CHANGE_ME_STRONG_PASSWORD"
# ──────────────────────────────────────────────────────────────────────

REDIS_CONF="/etc/redis/redis.conf"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"

if [[ "${REDIS_PASSWORD}" == "CHANGE_ME_STRONG_PASSWORD" ]]; then
    echo "────────────────────────────────────────────────────────────" >&2
    echo "ERROR: edit this script and set REDIS_PASSWORD before running." >&2
    echo "Suggested: openssl rand -base64 32" >&2
    echo "────────────────────────────────────────────────────────────" >&2
    exit 1
fi

if [[ "${EUID}" -ne 0 ]]; then
    echo "ERROR: run as root (sudo)." >&2
    exit 1
fi

echo "==> Installing redis-server via apt"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y redis-server

if [[ ! -f "${REDIS_CONF}" ]]; then
    echo "ERROR: ${REDIS_CONF} missing after install — aborting." >&2
    exit 1
fi

echo "==> Backing up ${REDIS_CONF} → ${REDIS_CONF}.bak.${TIMESTAMP}"
cp -a "${REDIS_CONF}" "${REDIS_CONF}.bak.${TIMESTAMP}"
# Keep a stable .bak copy too for the "first backup" conventional name.
if [[ ! -f "${REDIS_CONF}.bak" ]]; then
    cp -a "${REDIS_CONF}" "${REDIS_CONF}.bak"
fi

# Helper: ensure a directive exists with exactly the desired value. Replaces
# any existing (uncommented or commented) line, otherwise appends. Idempotent.
set_directive() {
    local key="$1"
    local value="$2"
    local file="$3"

    if grep -Eq "^[[:space:]]*#?[[:space:]]*${key}[[:space:]]" "${file}"; then
        sed -i -E "s|^[[:space:]]*#?[[:space:]]*${key}[[:space:]].*|${key} ${value}|" "${file}"
    else
        printf '\n%s %s\n' "${key}" "${value}" >> "${file}"
    fi
}

echo "==> Writing hardened directives into ${REDIS_CONF}"
# Listen on loopback only — do NOT expose Redis to the internet.
set_directive "bind" "127.0.0.1 ::1" "${REDIS_CONF}"
# Require a password for every command.
set_directive "requirepass" "${REDIS_PASSWORD}" "${REDIS_CONF}"
# 512mb cap — we store subscription ctx (~hundreds of bytes/user) and a
# revoke set; for 20k users this is overkill. Raise if you add larger keys.
set_directive "maxmemory" "512mb" "${REDIS_CONF}"
# LRU across all keys — we use Redis as a cache, so evict stale entries
# rather than error on OOM.
set_directive "maxmemory-policy" "allkeys-lru" "${REDIS_CONF}"
# Disable AOF: this is a cache, not a store of record. Losing the set on
# reboot only forces a cold re-cache from Postgres, no data loss.
set_directive "appendonly" "no" "${REDIS_CONF}"
# Disable RDB snapshots too for the same reason (less disk I/O).
set_directive "save" '""' "${REDIS_CONF}"

echo "==> Enabling + restarting redis-server"
systemctl enable redis-server
systemctl restart redis-server

echo "==> Waiting for redis to come up"
for _ in 1 2 3 4 5; do
    if redis-cli -a "${REDIS_PASSWORD}" --no-auth-warning ping 2>/dev/null | grep -q PONG; then
        break
    fi
    sleep 1
done

echo "==> Sanity check: AUTH + PING"
if ! redis-cli -a "${REDIS_PASSWORD}" --no-auth-warning ping | grep -q PONG; then
    echo "ERROR: redis-cli ping did not return PONG. Check journalctl -u redis-server." >&2
    exit 1
fi

# Escape any characters that are unsafe in a URL. The password contains
# base64 slashes/plus signs fine; shell escape is enough for the printout.
PASSWORD_URLENC="$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "${REDIS_PASSWORD}")"

cat <<EOF

────────────────────────────────────────────────────────────────────
Redis is up and password-protected. Next steps:

1. Put the following line in the app's .env (same box as Gunicorn):

     REDIS_URL=redis://:${PASSWORD_URLENC}@127.0.0.1:6379/0

2. If the app runs on a DIFFERENT box, edit /etc/redis/redis.conf so
   'bind' includes the VPS's private IP and open port 6379 only to
   that peer (ufw allow from <app_ip> to any port 6379). Loopback-only
   is the default here.

3. Verify from the app box:  redis-cli -h 127.0.0.1 -a '<password>' ping

Config backup saved at: ${REDIS_CONF}.bak.${TIMESTAMP}
────────────────────────────────────────────────────────────────────
EOF
