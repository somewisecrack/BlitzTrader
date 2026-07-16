#!/usr/bin/env bash
#
# deploy_gammablast_v2.sh — install the cheap-ticket v2 promoted rules onto the
# GammaBlast box and restart the service so the NEXT session uses them.
#
# This must be run ON THE GAMMABLAST VM (it needs the local /opt/gammablast tree
# and systemctl). It is NOT runnable from the BlitzTrader repo host — that host
# has no access to the box.
#
# Safe by default: it runs a DRY RUN and changes nothing unless you pass --go.
#
#   ./deploy_gammablast_v2.sh            # dry run: show what would happen
#   ./deploy_gammablast_v2.sh --go       # actually copy rules + restart service
#
# ── VERIFY THESE THREE PATHS FOR YOUR BOX BEFORE RUNNING WITH --go ──────────────
GAMMABLAST_HOME="${GAMMABLAST_HOME:-/opt/gammablast}"      # base dir (from startup log)
RULES_DIR="${RULES_DIR:-$GAMMABLAST_HOME/promoted_rules}"  # dir the rule loader reads
SERVICE_NAME="${SERVICE_NAME:-gammablast}"                 # systemd unit name
# ───────────────────────────────────────────────────────────────────────────────

set -euo pipefail

GO=0
[[ "${1:-}" == "--go" ]] && GO=1

# Rule files live next to this script's repo checkout, under wiki/promoted_filters/.
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/wiki/promoted_filters"
NIFTY="$SRC_DIR/gammablast_cheap_ticket_v2_NIFTY.json"
SENSEX="$SRC_DIR/gammablast_cheap_ticket_v2_SENSEX.json"

echo "== GammaBlast cheap-ticket v2 deploy =="
echo "  source rules : $SRC_DIR"
echo "  target dir   : $RULES_DIR"
echo "  service      : $SERVICE_NAME"
echo "  mode         : $([[ $GO -eq 1 ]] && echo LIVE || echo 'DRY RUN (pass --go to apply)')"
echo

for f in "$NIFTY" "$SENSEX"; do
    [[ -f "$f" ]] || { echo "ERROR: missing rule file: $f" >&2; exit 1; }
done

if [[ $GO -eq 0 ]]; then
    echo "Would copy:"
    echo "  $NIFTY  -> $RULES_DIR/"
    echo "  $SENSEX -> $RULES_DIR/"
    echo "Would restart: systemctl restart $SERVICE_NAME"
    echo
    echo "Dry run only — nothing changed."
    exit 0
fi

mkdir -p "$RULES_DIR"
cp -v "$NIFTY" "$SENSEX" "$RULES_DIR/"
echo "Restarting $SERVICE_NAME ..."
systemctl restart "$SERVICE_NAME"
echo "Done. Verify the next startup log shows the new keys, e.g.:"
echo "  Promoted rule: ENTRY_CUTOFF = 15:25 (scope=NIFTY)"
echo "  Promoted rule: RECORDER_END = 15:30 (scope=NIFTY)"
echo
echo "IMPORTANT: the 15:30 window only takes full effect if the ladder recorder"
echo "and main scan loop actually honour RECORDER_END / ENTRY_CUTOFF. If those EOD"
echo "times are hard-coded (the recorder shut off at 15:15:04 on 16 Jul), they need"
echo "a source change in the GammaBlast engine — this script cannot do that."
echo "Verify: next session's gamma-ladder JSONL should end ~15:30, not ~15:15."
