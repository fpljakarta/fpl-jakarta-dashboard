#!/usr/bin/env bash
#
# Start a refresh from outside GitHub.
#
# This is what the external trigger does on a schedule -- see "The external
# trigger" in the README. It is kept here so the token can be proved before it
# is pasted into a cron service, and so the exact request is written down
# somewhere rather than living only in that service's settings.
#
#     GITHUB_TOKEN=github_pat_... ./scripts/ping-refresh.sh
#
# A success prints "queued" and exits 0. Anything else prints what GitHub said
# and exits 1, which is also what a cron service will alert on.
#
set -euo pipefail

REPO="${REPO:-fpljakarta/fpl-jakarta-dashboard}"
WORKFLOW="${WORKFLOW:-refresh-live.yml}"
REF="${REF:-main}"

if [ -z "${GITHUB_TOKEN:-}" ]; then
  echo "No GITHUB_TOKEN. Make a fine-grained token with Actions: Read and write" >&2
  echo "on ${REPO}, then run:" >&2
  echo "    GITHUB_TOKEN=... $0" >&2
  exit 1
fi

url="https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches"

# --write-out puts the status on its own last line so the body, if any, stays
# readable above it.
response=$(curl -sS -X POST "$url" \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer ${GITHUB_TOKEN}" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  -H "Content-Type: application/json" \
  -H "User-Agent: fpl-jakarta-refresh-ping" \
  -d "{\"ref\":\"${REF}\"}" \
  --write-out $'\n%{http_code}')

code=$(printf '%s' "$response" | tail -n1)
body=$(printf '%s' "$response" | sed '$d')

if [ "$code" = "204" ]; then
  echo "queued: ${WORKFLOW} on ${REF}"
  echo "watch it at https://github.com/${REPO}/actions/workflows/${WORKFLOW}"
  exit 0
fi

echo "GitHub answered ${code}, not 204." >&2
[ -n "$body" ] && echo "$body" >&2
case "$code" in
  401) echo "-> the token is wrong or expired." >&2 ;;
  403) echo "-> the token lacks Actions: Read and write on ${REPO}." >&2 ;;
  404) echo "-> wrong repository, or the token cannot see it, or there is no" >&2
       echo "   ${WORKFLOW} on ${REF}." >&2 ;;
  422) echo "-> ${REF} is not a branch this workflow can run on." >&2 ;;
esac
exit 1
