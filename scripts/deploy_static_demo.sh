#!/usr/bin/env bash
# Build the static demo (verified runs, replayed in the browser, no backend) and publish it
# to Cloudflare Pages. Run from the repo root on your machine:
#
#   scripts/deploy_static_demo.sh                       # project "bisect-agent"
#   LIVE_URL=http://<vm-ip> scripts/deploy_static_demo.sh   # also link the form to the live app
#
# Needs: Node 20+, `wrangler login` done once, and CLOUDFLARE_ACCOUNT_ID if you have several accounts.
set -euo pipefail
cd "$(dirname "$0")/.."
PROJECT="${PROJECT:-bisect-agent}"
python3 scripts/build_static_demo.py
( cd web && npm ci --silent && NEXT_PUBLIC_STATIC_DEMO=1 NEXT_PUBLIC_LIVE_URL="${LIVE_URL:-}" npx next build >/dev/null )
npx --yes wrangler pages project create "$PROJECT" --production-branch main >/dev/null 2>&1 || true  # no-op if it exists
npx --yes wrangler pages deploy web/out --project-name "$PROJECT" --branch main --commit-dirty=true
