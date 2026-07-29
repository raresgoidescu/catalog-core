#!/usr/bin/env bash
#
# Posts a compact embed to Discord about this workflow run.
#
# Usage: discord-notify.sh <status: started|success|failure|cancelled> <details-text>
#
# Requires env: DISCORD_WEBHOOK_URL, plus the usual GITHUB_* context vars
# GitHub Actions sets automatically (GITHUB_REPOSITORY, GITHUB_RUN_ID,
# GITHUB_WORKFLOW, GITHUB_SERVER_URL).

set -euo pipefail

STATUS="$1"
DETAILS="${2:-}"

if [ -z "${DISCORD_WEBHOOK_URL:-}" ]; then
  echo "DISCORD_WEBHOOK_URL not set -- skipping Discord notification."
  exit 0
fi

RUN_URL="${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"

case "$STATUS" in
  started)   COLOR=3447003;  TITLE="🚀 ${GITHUB_WORKFLOW} started" ;;
  success)   COLOR=3066993;  TITLE="✅ ${GITHUB_WORKFLOW} passed" ;;
  failure)   COLOR=15158332; TITLE="❌ ${GITHUB_WORKFLOW} failed" ;;
  cancelled) COLOR=9807270;  TITLE="⚪ ${GITHUB_WORKFLOW} cancelled" ;;
  *)         COLOR=9807270;  TITLE="${GITHUB_WORKFLOW}: ${STATUS}" ;;
esac

# Discord embed description has a 4096 char cap; keep it well under that.
DETAILS_TRUNCATED=$(printf '%s' "$DETAILS" | cut -c1-1500)

payload=$(jq -n \
  --arg title "$TITLE" \
  --arg desc "$DETAILS_TRUNCATED" \
  --arg url "$RUN_URL" \
  --argjson color "$COLOR" \
  '{
    embeds: [{
      title: $title,
      description: $desc,
      url: $url,
      color: $color
    }]
  }')

curl -sS -X POST -H "Content-Type: application/json" \
  -d "$payload" \
  "$DISCORD_WEBHOOK_URL" \
  --fail-with-body \
  || echo "::warning::Discord notification failed to send (non-fatal)."
