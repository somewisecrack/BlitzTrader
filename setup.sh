#!/bin/bash
# setup.sh — GCP VM provisioning for BlitzTrader
# Usage: sudo bash setup.sh
set -euo pipefail

echo "=== BlitzTrader GCP VM Setup ==="

# ──────────────────────────────────────────────────────────
#   SYSTEM
# ──────────────────────────────────────────────────────────

echo "[1/8] Setting timezone..."
timedatectl set-timezone Asia/Kolkata

echo "[2/8] Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq python3.11 python3.11-venv python3-pip git rclone

# ──────────────────────────────────────────────────────────
#   USER & DIRECTORY
# ──────────────────────────────────────────────────────────

echo "[3/8] Creating blitztrader user..."
id -u blitztrader &>/dev/null || useradd -r -m -s /bin/bash blitztrader

echo "[4/8] Setting up directory..."
APP_DIR="/opt/blitztrader"
mkdir -p "$APP_DIR"/{journals,logs,strategies,data_exports}

# Copy project files (exclude local .env so Secret Manager is always used)
cp -r ./* "$APP_DIR/"
rm -f "$APP_DIR/.env"
chown -R blitztrader:blitztrader "$APP_DIR"

# ──────────────────────────────────────────────────────────
#   PYTHON ENVIRONMENT
# ──────────────────────────────────────────────────────────

echo "[5/8] Creating Python virtual environment..."
su - blitztrader -c "python3.11 -m venv $APP_DIR/venv"
su - blitztrader -c "$APP_DIR/venv/bin/pip install --quiet -r $APP_DIR/requirements.txt"

# ──────────────────────────────────────────────────────────
#   SECRETS (from GCP Secret Manager)
# ──────────────────────────────────────────────────────────

echo "[6/8] Loading secrets from GCP Secret Manager..."

# Install gcloud if not present
if ! command -v gcloud &>/dev/null; then
    echo "  gcloud not found, skipping secret loading."
    echo "  Please manually create $APP_DIR/.env"
else
    PROJECT_ID=$(gcloud config get-value project 2>/dev/null)

    declare -a SECRETS=(
        SHOONYA_USER_ID
        SHOONYA_PASSWORD
        SHOONYA_TOTP_SECRET
        SHOONYA_API_KEY
        SHOONYA_VENDOR_CODE
        SHOONYA_IMEI
        TELEGRAM_BOT_TOKEN
        TELEGRAM_AUTHORIZED_USER_ID
        GEMINI_API_KEY
        GOOGLE_DRIVE_UPLOAD_DIR
        RCLONE_REMOTE
        RCLONE_FOLDER
    )

    ENV_FILE="$APP_DIR/.env"
    
    if [ -s "$ENV_FILE" ] && grep -q "SHOONYA_USER_ID" "$ENV_FILE"; then
        echo "  ✓ Existing .env file found with credentials. Skipping Secret Manager."
    else
        > "$ENV_FILE"
        for SECRET in "${SECRETS[@]}"; do
            VALUE=$(gcloud secrets versions access latest --secret="$SECRET" --project="$PROJECT_ID" 2>/dev/null || echo "")
            if [ -n "$VALUE" ]; then
                echo "$SECRET=$VALUE" >> "$ENV_FILE"
                echo "  ✓ $SECRET loaded"
            else
                echo "  ✗ $SECRET not found in Secret Manager"
                echo "$SECRET=" >> "$ENV_FILE"
            fi
        done
    fi

    chmod 600 "$ENV_FILE"
    chown blitztrader:blitztrader "$ENV_FILE"
fi

# ──────────────────────────────────────────────────────────
#   SYSTEMD
# ──────────────────────────────────────────────────────────

echo "[7/8] Installing systemd service..."
cp "$APP_DIR/blitztrader.service" /etc/systemd/system/
cp "$APP_DIR/blitztrader.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable blitztrader.timer
systemctl start blitztrader.timer

# ──────────────────────────────────────────────────────────
#   KILL TIMER (3:25 PM safety net)
# ──────────────────────────────────────────────────────────

echo "[8/8] Installing safety cron job and log rotation..."
# Forcefully kill at 3:25 PM IST as safety net (weekdays only)
(crontab -l 2>/dev/null; echo "25 15 * * 1-5 systemctl stop blitztrader.service") | sort -u | crontab -

# Log rotation — prevent disk fill from runaway log sessions
cat > /etc/logrotate.d/blitztrader << 'EOF'
/opt/blitztrader/logs/*.log {
    daily
    rotate 14
    compress
    missingok
    notifempty
    copytruncate
}
EOF

echo ""
echo "=== Setup Complete ==="
echo ""
echo "  App directory: $APP_DIR"
echo "  Service:       blitztrader.service"
echo "  Timer:         blitztrader.timer (9:00 AM IST weekdays only)"
echo "  Safety kill:   cron at 3:25 PM IST"
echo ""
echo "  Manual start:  sudo systemctl start blitztrader"
echo "  Check status:  sudo systemctl status blitztrader"
echo "  View logs:     tail -f $APP_DIR/logs/blitztrader_*.log"
echo ""
echo "  ⚠️  Verify .env file has all credentials before first run"
echo ""
