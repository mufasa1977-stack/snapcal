#!/usr/bin/env bash
# SnapCal ONE-DOOR deploy — born 2026-07-19 after a silent failure: a ship chain committed
# locally, hit the Render deploy hook, and Render rebuilt OLD code because the commit was
# never PUSHED (Render builds from GitHub, not this disk). This script makes that class
# impossible: it refuses to call the deploy hook until origin/main == HEAD.
#
# Usage: bash deploy.sh              (gate + push-verify + deploy + wait-live)
#        bash deploy.sh --skip-gate  (redeploy only — e.g. env change, no code diff)
set -euo pipefail
cd "$(dirname "$0")"
BASE="https://snapcal-api-lgla.onrender.com"
HOOK="https://api.render.com/deploy/srv-d8smnra8qa3s73bge1cg?key=K3WyaAzFmVg"

if [[ "${1:-}" != "--skip-gate" ]]; then
  echo "== regression gate =="
  python regression_gate.py || { echo "GATE FAILED — NOT DEPLOYING"; exit 1; }
fi

echo "== push-verify (the 2026-07-19 lesson) =="
if [[ -n "$(git status --porcelain -- app.py static/ templates/ 2>/dev/null)" ]]; then
  echo "UNCOMMITTED changes in shipped paths — commit first:"; git status --short -- app.py static/ templates/; exit 1
fi
git push origin main
git fetch origin -q
LOCAL=$(git rev-parse HEAD); REMOTE=$(git rev-parse origin/main)
if [[ "$LOCAL" != "$REMOTE" ]]; then
  echo "PUSH-VERIFY FAILED: HEAD=$LOCAL but origin/main=$REMOTE — Render would build stale code."; exit 1
fi
echo "push-verified: origin/main == HEAD ($(git rev-parse --short HEAD))"

echo "== deploy =="
curl -s "$HOOK" && echo

echo "== wait for live (/api/version app_mtime must advance) =="
BEFORE=$(curl -s "$BASE/api/version" | python -c "import json,sys; print(json.load(sys.stdin).get('app_mtime',''))" 2>/dev/null || echo "")
for i in $(seq 1 40); do
  sleep 15
  NOW=$(curl -s "$BASE/api/version" | python -c "import json,sys; print(json.load(sys.stdin).get('app_mtime',''))" 2>/dev/null || echo "")
  if [[ -n "$NOW" && "$NOW" != "$BEFORE" ]]; then echo "LIVE after ~$((i*15))s (app_mtime $BEFORE -> $NOW)"; exit 0; fi
done
echo "TIMED OUT waiting for live after 10min — check the Render dashboard"; exit 1
