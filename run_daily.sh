#!/usr/bin/env bash
# run_daily.sh — daily PM role scrape + Gmail sync + HTML build.
# Intended to be invoked by launchd via com.pmfarm.daily.plist.
#
# Usage (manual test):
#   bash run_daily.sh
#
# Install launchd agent (one-time):
#   cp com.pmfarm.daily.plist ~/Library/LaunchAgents/
#   launchctl load ~/Library/LaunchAgents/com.pmfarm.daily.plist

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HOME/.venv/pmfarm"
PYTHON="${VENV}/bin/python3"

# Bootstrap venv on first run if it doesn't exist yet.
if [ ! -x "$PYTHON" ]; then
  echo "  [setup] creating venv at $VENV"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --quiet google-api-python-client google-auth-oauthlib
fi
LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/pmfarm_$(date +%Y%m%d_%H%M%S).log"

{
  echo "=== pmfarm daily run $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

  cd "$SCRIPT_DIR"

  # 1. Sync applied companies from Gmail (requires prior --setup).
  #    Fails silently if token is missing — applied.csv still dedupes.
  "$PYTHON" pmfarm_gmail_sync.py 2>&1 || echo "  [warn] Gmail sync skipped (token missing?)"

  # 2. Scrape ATS APIs and write pm_roles.csv.
  python3 pmfarm.py --all-levels

  # 3. Build HTML dashboard from pm_roles.csv.
  python3 build_page.py

  # 4. Publish updated HTML to GitHub Pages.
  #    Only commits if pm_roles.html actually changed (idempotent).
  if ! git diff --quiet pm_roles.html 2>/dev/null; then
    git add pm_roles.html
    git commit -m "daily update $(date -u +%Y-%m-%d)"
    git push origin HEAD
    echo "  [ok] pm_roles.html pushed to GitHub"
  else
    echo "  [skip] pm_roles.html unchanged — nothing to push"
  fi

  echo "=== done $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
} | tee "$LOG"

# Keep only last 14 log files.
ls -t "$LOG_DIR"/pmfarm_*.log 2>/dev/null | tail -n +15 | xargs rm -f --
