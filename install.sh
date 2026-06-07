#!/usr/bin/env bash
# install.sh — run ONCE on your Mac. After this you never touch the terminal:
#   • a Desktop icon ("PM Farm") refreshes roles on double-click
#   • a launchd agent auto-runs every morning at 7:30am
#   • does a first scrape right now so you have a page immediately
#
# Usage (from the repo folder):
#   bash install.sh
#
# Re-running is safe — it overwrites the icon and reloads the agent.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_NAME="$(whoami)"
PLIST_SRC="$REPO/com.pmfarm.daily.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.pmfarm.daily.plist"
ICON="$HOME/Desktop/PM Farm.command"

echo ""
echo "╔════════════════════════════════════════════════╗"
echo "║   PM Farm — one-time setup                     ║"
echo "╚════════════════════════════════════════════════╝"
echo "  repo: $REPO"
echo ""

# ── 1. Desktop icon (double-click to refresh on demand) ───────────────────────
echo "1/3  Creating Desktop icon…"
cat > "$ICON" << SCRIPT
#!/usr/bin/env bash
# PM Farm — scrape live ATS APIs, build triage page, open it.
REPO="${REPO}"
echo -e "\033]0;PM Farm — scraping roles…\007"
cd "\$REPO"
echo ""
echo "╔══════════════════════════════════════════╗"
echo "║         PM Farm — refreshing roles       ║"
echo "╚══════════════════════════════════════════╝"
echo ""
bash run_daily.sh
echo ""
echo "✓ Done. Opening triage page…"
open pm_roles.html
osascript -e 'display notification "Triage page is ready — go apply." with title "PM Farm ✓" sound name "Glass"' 2>/dev/null || true
SCRIPT
chmod +x "$ICON"
echo "     ✓ $ICON"

# ── 2. launchd agent (auto-run daily at 7:30am) ───────────────────────────────
echo "2/3  Installing daily auto-run (7:30am)…"
mkdir -p "$HOME/Library/LaunchAgents" "$REPO/logs"
# Generate the plist with the real username/path filled in (no manual editing).
sed -e "s#/Users/YOUR_USERNAME/Vibe-Coding#${REPO}#g" \
    "$PLIST_SRC" > "$PLIST_DST"
# Reload cleanly whether or not it was already loaded.
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load  "$PLIST_DST"
echo "     ✓ agent loaded → $PLIST_DST"

# ── 3. First run now, so there's a page to open immediately ───────────────────
echo "3/3  Running first scrape now (this takes ~30s)…"
echo ""
cd "$REPO"
bash run_daily.sh
echo ""
echo "✓ Setup complete. Opening your triage page…"
open pm_roles.html

cat << DONE

────────────────────────────────────────────────────────────
You're done. From now on, with zero terminal use:

  • The page refreshes automatically every morning at 7:30am.
  • Double-click "PM Farm" on your Desktop to refresh anytime.
  • The latest page is always at: $REPO/pm_roles.html

(macOS may ask to allow Terminal the first time the icon runs —
 click Allow.)
────────────────────────────────────────────────────────────
DONE
