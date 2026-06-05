#!/bin/bash
# vm_setup_and_verify.sh
# Run as: sudo -u blitztrader bash /opt/blitztrader/scripts/vm_setup_and_verify.sh
# Or:     bash /opt/blitztrader/scripts/vm_setup_and_verify.sh
#
# Does:
#   1. Show current rclone remotes and .env upload config
#   2. Git pull latest code from current branch
#   3. Ensure RCLONE_REMOTE is set in .env (auto-detects from rclone listremotes)
#   4. Show today's data exports and ATM option files
#   5. Run eod_backup dry-run for today
#   6. Reload systemd and restart eod-backup timer
#   7. Final status summary

set -euo pipefail
REPO=/opt/blitztrader
ENV_FILE=$REPO/.env
RCLONE_CONF=/home/blitztrader/.config/rclone/rclone.conf
DATE=$(date +%Y%m%d)

echo "================================================================"
echo "  BlitzTrader VM Setup & Verify — $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "================================================================"

# ── 1. Disk space ─────────────────────────────────────────────────────
echo ""
echo "── Disk space ────────────────────────────────────────────────"
df -h /opt /home 2>/dev/null || df -h /
du -sh $REPO/data_exports 2>/dev/null && echo "  (data_exports total)"
du -sh $REPO/runtime/data_exports 2>/dev/null && echo "  (runtime/data_exports total)" || true

# ── 2. Current .env upload config ─────────────────────────────────────
echo ""
echo "── Current .env upload config ───────────────────────────────"
grep -E "RCLONE|DRIVE|RUNTIME_STORAGE" $ENV_FILE 2>/dev/null || echo "  (none of those vars found in .env)"

# ── 3. rclone remotes ─────────────────────────────────────────────────
echo ""
echo "── rclone remotes ───────────────────────────────────────────"
RCLONE_ARGS=""
if [ -f "$RCLONE_CONF" ]; then
    RCLONE_ARGS="--config $RCLONE_CONF"
fi
rclone $RCLONE_ARGS listremotes 2>/dev/null || echo "  rclone not found or no config"
REMOTES=$(rclone $RCLONE_ARGS listremotes 2>/dev/null | tr -d ':' | head -1)
echo "  First remote detected: '${REMOTES:-none}'"

# ── 4. Auto-fix .env: RCLONE_REMOTE ───────────────────────────────────
echo ""
echo "── Auto-fixing .env ─────────────────────────────────────────"
if grep -q "^RCLONE_REMOTE=" $ENV_FILE 2>/dev/null; then
    echo "  RCLONE_REMOTE already set: $(grep '^RCLONE_REMOTE=' $ENV_FILE)"
elif [ -n "${REMOTES:-}" ]; then
    echo "  Adding RCLONE_REMOTE=$REMOTES to .env"
    echo "RCLONE_REMOTE=$REMOTES" >> $ENV_FILE
    echo "  Done."
else
    echo "  WARNING: No rclone remote detected — skipping auto-set."
    echo "  Run: rclone config  (to add a Google Drive remote)"
fi

# Ensure RCLONE_FOLDER is set
if ! grep -q "^RCLONE_FOLDER=" $ENV_FILE 2>/dev/null; then
    echo "  Adding RCLONE_FOLDER=BlitzTrader to .env"
    echo "RCLONE_FOLDER=BlitzTrader" >> $ENV_FILE
fi

echo "  Final upload config in .env:"
grep -E "RCLONE|DRIVE|RUNTIME_STORAGE" $ENV_FILE 2>/dev/null || echo "    (still none)"

# ── 5. Git pull latest code ────────────────────────────────────────────
echo ""
echo "── Git pull ─────────────────────────────────────────────────"
cd $REPO
git fetch origin 2>&1 | head -5
BRANCH=$(git rev-parse --abbrev-ref HEAD)
echo "  Branch: $BRANCH"
git pull origin "$BRANCH" 2>&1 | tail -5
echo "  Latest commit: $(git log --oneline -1)"

# ── 6. Today's data exports ───────────────────────────────────────────
echo ""
echo "── Today's data exports ($DATE) ─────────────────────────────"
for EXPORTS_DIR in "$REPO/data_exports/$DATE" "$REPO/runtime/data_exports/$DATE"; do
    if [ -d "$EXPORTS_DIR" ]; then
        echo "  Found: $EXPORTS_DIR"
        find "$EXPORTS_DIR" -type f | sort | head -30 | sed 's/^/    /'
        echo "  Total files: $(find "$EXPORTS_DIR" -type f | wc -l)"
        du -sh "$EXPORTS_DIR"
    fi
done

# ATM option data specifically
echo ""
echo "── ATM option data ──────────────────────────────────────────"
ATM_FOUND=0
for EXPORTS_DIR in "$REPO/data_exports/$DATE/atm_options" "$REPO/runtime/data_exports/$DATE/atm_options"; do
    if [ -d "$EXPORTS_DIR" ]; then
        ATM_FOUND=1
        echo "  ATM data found at: $EXPORTS_DIR"
        find "$EXPORTS_DIR" -type f | sort | sed 's/^/    /'
        # Show first and last row of first OHLCV file
        FIRST_OHLCV=$(find "$EXPORTS_DIR" -name "*_ohlcv.jsonl" | head -1)
        if [ -n "$FIRST_OHLCV" ]; then
            echo "  --- First OHLCV row ($FIRST_OHLCV): ---"
            head -1 "$FIRST_OHLCV" | python3 -m json.tool 2>/dev/null || head -1 "$FIRST_OHLCV"
            echo "  --- Last OHLCV row: ---"
            tail -1 "$FIRST_OHLCV" | python3 -m json.tool 2>/dev/null || tail -1 "$FIRST_OHLCV"
        fi
    fi
done
if [ $ATM_FOUND -eq 0 ]; then
    echo "  No ATM option data found for today."
    echo "  (ATM recorder was not wired into main loop before today's code push)"
    echo "  Will be active from next session."
fi

# ── 7. eod_backup dry-run ─────────────────────────────────────────────
echo ""
echo "── eod_backup dry-run ───────────────────────────────────────"
cd $REPO
$REPO/venv/bin/python scripts/eod_backup.py --dry-run --force 2>&1 || echo "  dry-run failed (check above)"

# ── 8. rclone test upload ─────────────────────────────────────────────
echo ""
echo "── rclone test upload ───────────────────────────────────────"
REMOTE_VAL=$(grep '^RCLONE_REMOTE=' $ENV_FILE 2>/dev/null | cut -d= -f2 | tr -d '"' | tr -d "'" || echo "")
FOLDER_VAL=$(grep '^RCLONE_FOLDER=' $ENV_FILE 2>/dev/null | cut -d= -f2 | tr -d '"' | tr -d "'" || echo "BlitzTrader")
if [ -n "$REMOTE_VAL" ]; then
    echo "  Testing: rclone lsd $REMOTE_VAL:$FOLDER_VAL"
    rclone $RCLONE_ARGS lsd "$REMOTE_VAL:$FOLDER_VAL" 2>&1 || echo "  Folder not yet on Drive (will be created on first upload)"
else
    echo "  RCLONE_REMOTE not set — skipping test"
fi

# ── 9. Systemd reload + restart eod timer ─────────────────────────────
echo ""
echo "── Systemd ──────────────────────────────────────────────────"
sudo systemctl daemon-reload 2>/dev/null && echo "  daemon-reload OK" || echo "  daemon-reload failed (run with sudo?)"
sudo systemctl enable blitztrader-eod-backup.timer 2>/dev/null && echo "  eod-backup timer enabled" || true
sudo systemctl restart blitztrader-eod-backup.timer 2>/dev/null && echo "  eod-backup timer restarted" || echo "  timer restart failed"
sudo systemctl status blitztrader-eod-backup.timer --no-pager 2>/dev/null | grep -E "Active|Next|Last" || true

# blitztrader.service — don't restart if trading; just show status
sudo systemctl status blitztrader.service --no-pager 2>/dev/null | grep -E "Active|Main PID" || true

# ── 10. Today's service logs (export/upload lines) ────────────────────
echo ""
echo "── Today's relevant service logs ────────────────────────────"
journalctl -u blitztrader.service --since today --no-pager 2>/dev/null \
    | grep -iE "export|upload|rclone|drive|atm|ATM|eod|backup|error" | tail -20 \
    || echo "  No matching log lines"

echo ""
echo "================================================================"
echo "  Done. Check output above for any warnings or errors."
echo "================================================================"
