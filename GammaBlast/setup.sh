#!/usr/bin/env bash
# GammaBlast VM setup script.
# Run as root on the gammablast VM (GCP project: gammablast-20260615-rgk,
# zone: asia-south1-c, IP: 34.47.248.158).
#
# SAFETY: This script ONLY touches /opt/gammablast and the gammablast user.
# It does NOT touch /opt/blitztrader, blitztrader services, or BlitzTrader config.

set -euo pipefail

APP_DIR="/opt/gammablast"
APP_USER="gammablast"
REPO_URL="${GAMMABLAST_REPO_URL:-}"   # set in env or pass as arg

echo "=== GammaBlast setup ==="

# Verify we are NOT on a BlitzTrader VM
if systemctl list-units --all 2>/dev/null | grep -q "blitztrader.service"; then
    echo "ERROR: blitztrader.service detected. Do NOT run this on the BlitzTrader VM." >&2
    exit 1
fi

# System packages
apt-get update -qq
apt-get install -y python3 python3-venv python3-pip rclone git tzdata

# Create service user
if ! id "${APP_USER}" &>/dev/null; then
    useradd --system --shell /bin/bash --home "/home/${APP_USER}" --create-home "${APP_USER}"
fi

# App directory
mkdir -p "${APP_DIR}"
chown "${APP_USER}:${APP_USER}" "${APP_DIR}"

# Clone or pull
if [ -n "${REPO_URL}" ]; then
    if [ -d "${APP_DIR}/.git" ]; then
        sudo -u "${APP_USER}" git -C "${APP_DIR}" pull
    else
        sudo -u "${APP_USER}" git clone "${REPO_URL}" "${APP_DIR}"
    fi
fi

# Python venv
sudo -u "${APP_USER}" python3 -m venv "${APP_DIR}/venv"
sudo -u "${APP_USER}" "${APP_DIR}/venv/bin/pip" install -q --upgrade pip
sudo -u "${APP_USER}" "${APP_DIR}/venv/bin/pip" install -q -r "${APP_DIR}/requirements.txt"

# Runtime dirs
for d in journals logs data_exports candidate_signals wiki/daily_reviews wiki/promoted_rules; do
    mkdir -p "${APP_DIR}/${d}"
    chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}/${d}"
done

# Env file
if [ ! -f "${APP_DIR}/.env" ]; then
    cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
    chmod 600 "${APP_DIR}/.env"
    chown "${APP_USER}:${APP_USER}" "${APP_DIR}/.env"
    echo "Created ${APP_DIR}/.env — fill in credentials before starting."
fi

# Systemd units
for svc in gammablast gammablast-eod-backup gammablast-wiki-loop; do
    cp "${APP_DIR}/${svc}.service" "/etc/systemd/system/${svc}.service"
    if [ -f "${APP_DIR}/${svc}.timer" ]; then
        cp "${APP_DIR}/${svc}.timer" "/etc/systemd/system/${svc}.timer"
    fi
done

systemctl daemon-reload
systemctl enable gammablast.timer gammablast-eod-backup.timer gammablast-wiki-loop.timer

# Safety checks
if grep -r "/opt/blitztrader" "${APP_DIR}/scripts/" 2>/dev/null; then
    echo "ERROR: /opt/blitztrader found in GammaBlast scripts!" >&2
    exit 1
fi
if grep -r "blitztrader.service" "${APP_DIR}" --include="*.sh" --include="*.py" 2>/dev/null; then
    echo "ERROR: blitztrader.service reference in GammaBlast!" >&2
    exit 1
fi

echo ""
echo "=== GammaBlast setup complete ==="
echo "Next: edit ${APP_DIR}/.env with credentials, then:"
echo "  systemctl start gammablast.timer"
echo "  systemctl status gammablast.timer"
