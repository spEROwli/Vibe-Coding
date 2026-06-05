#!/usr/bin/env bash
# PM Farm — double-click from Desktop or the Vibe-Coding folder to run.

# Find the repo whether this file lives in the repo, on the Desktop,
# or anywhere else (checks common clone locations).
_self="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
REPO=""
for _candidate in \
  "$_self" \
  "$HOME/Vibe-Coding" \
  "$HOME/Documents/Vibe-Coding" \
  "$HOME/projects/Vibe-Coding" \
  "$HOME/Desktop/Vibe-Coding"; do
  if [[ -f "$_candidate/run_daily.sh" ]]; then
    REPO="$_candidate"
    break
  fi
done

if [[ -z "$REPO" ]]; then
  osascript -e 'display alert "PM Farm" message "Cannot find the Vibe-Coding folder. Make sure the repo is cloned to your home directory." as critical'
  exit 1
fi

printf '\033]0;PM Farm — refreshing roles…\007'
echo ""
echo "╔══════════════════════════════════════════╗"
echo "║         PM Farm — refreshing roles       ║"
echo "╚══════════════════════════════════════════╝"
echo ""

cd "$REPO"
bash run_daily.sh

echo ""
echo "✓ Done. Opening triage page…"
open "$REPO/pm_roles.html"

osascript -e 'display notification "Triage page is ready — go apply." with title "PM Farm ✓" sound name "Glass"' 2>/dev/null || true
