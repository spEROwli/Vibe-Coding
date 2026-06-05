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

# Tee stdout+stderr to log without a pipe subshell — set -e stays effective.
exec > >(tee "$LOG") 2>&1

echo "=== pmfarm daily run $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

cd "$SCRIPT_DIR"

# 1. Sync applied companies from Gmail (optional).
#    Only runs if the OAuth token exists — set up with pmfarm_gmail_sync.py --setup.
if [ -f "$HOME/.config/pmfarm/gmail_token.json" ]; then
  "$PYTHON" pmfarm_gmail_sync.py || echo "  [warn] Gmail sync failed — check token"
fi

# 2. Refresh company list once a week so new companies surface fresh roles.
CACHE="$SCRIPT_DIR/verified_companies.json"
if [ ! -f "$CACHE" ] || [ "$(( $(date +%s) - $(stat -f %m "$CACHE" 2>/dev/null || stat -c %Y "$CACHE" 2>/dev/null || echo 0) ))" -gt 518400 ]; then
  echo "  [discover] refreshing company list…"
  "$PYTHON" discover.py || echo "  [warn] discover.py failed — using existing cache"
fi

# 3. Scrape ATS APIs and write pm_roles.csv (IC-level only, no --all-levels).
"$PYTHON" pmfarm.py

# 4. Build HTML dashboard from pm_roles.csv.
"$PYTHON" build_page.py

# 4. Publish updated HTML to GitHub Pages.
#    Only commits if pm_roles.html actually changed (idempotent).
#    Pull --rebase BEFORE committing so local never diverges from remote, and
#    force a non-interactive merge so the script can never trap you in vim.
if ! git diff --quiet pm_roles.html 2>/dev/null; then
  BRANCH="$(git rev-parse --abbrev-ref HEAD)"
  # Sync with remote first (autostash protects the uncommitted pm_roles.html).
  GIT_EDITOR=true git pull --rebase --autostash origin "$BRANCH" \
    || echo "  [warn] pull --rebase failed — pushing local state anyway"
  git add pm_roles.html
  git commit -m "daily update $(date -u +%Y-%m-%d)"
  git push origin "HEAD:$BRANCH"
  echo "  [ok] pm_roles.html pushed to $BRANCH"
else
  echo "  [skip] pm_roles.html unchanged — nothing to push"
fi

echo "=== done $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

# Keep only last 14 log files.
ls -t "$LOG_DIR"/pmfarm_*.log 2>/dev/null | tail -n +15 | xargs rm -f --
