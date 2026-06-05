#!/usr/bin/env bash
# setup_desktop_icon.sh — run ONCE after pulling the repo.
# Creates a double-clickable "PM Farm" icon on your Desktop.
#
# Usage (from repo root):
#   bash setup_desktop_icon.sh

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ICON="$HOME/Desktop/PM Farm.command"

cat > "$ICON" << SCRIPT
#!/usr/bin/env bash
# PM Farm — scrape live ATS APIs, build triage page, open it.
# Created by setup_desktop_icon.sh from: ${REPO}

REPO="${REPO}"

# Keep the Terminal window title readable.
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

# macOS notification so you know it finished even if you switched away.
osascript -e 'display notification "Triage page is ready — go apply." with title "PM Farm ✓" sound name "Glass"' 2>/dev/null || true
SCRIPT

chmod +x "$ICON"

echo ""
echo "✓ Icon created → $ICON"
echo ""
echo "  Double-click \"PM Farm.command\" on your Desktop to refresh roles."
echo "  (macOS will ask to allow Terminal the first time — click Allow.)"
echo ""
