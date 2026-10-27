#!/usr/bin/env bash
# Deploy Bisect to a fresh Ubuntu VM (e.g. Nebius Compute) from your laptop.
#
#   scripts/deploy_vm.sh ubuntu@203.0.113.10            # first deploy or update
#   BRANCH=main scripts/deploy_vm.sh ubuntu@203.0.113.10
#
# Installs Docker if missing, checks out the repo, copies your local .env (API keys) over SSH,
# builds the sandbox image and starts the app on port 80. Safe to re-run.
set -euo pipefail

TARGET="${1:?usage: scripts/deploy_vm.sh user@host}"
BRANCH="${BRANCH:-dev}"
REPO="${REPO:-https://github.com/sja-thedude/bisect-agent.git}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

[ -f "$ROOT/.env" ] || { echo "missing $ROOT/.env (NEBIUS_API_KEY, TAVILY_API_KEY)"; exit 1; }

echo "==> preparing $TARGET"
ssh -o StrictHostKeyChecking=accept-new "$TARGET" bash -s -- "$REPO" "$BRANCH" <<'REMOTE'
set -euo pipefail
REPO="$1"; BRANCH="$2"
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
fi
sudo apt-get install -y -qq git >/dev/null
sudo mkdir -p /var/lib/bisect /opt/bisect
sudo chown "$USER" /opt/bisect
if [ -d /opt/bisect/.git ]; then
  git -C /opt/bisect fetch -q origin && git -C /opt/bisect checkout -q "$BRANCH" && git -C /opt/bisect reset -q --hard "origin/$BRANCH"
else
  git clone -q --branch "$BRANCH" "$REPO" /opt/bisect
fi
REMOTE

echo "==> copying .env"
scp -q "$ROOT/.env" "$TARGET:/opt/bisect/.env"
ssh "$TARGET" "chmod 600 /opt/bisect/.env"

echo "==> building and starting (first build takes a few minutes)"
ssh "$TARGET" bash -s <<'REMOTE'
set -euo pipefail
cd /opt/bisect
sudo docker build -q -t bisect-sandbox:py3.11 --build-arg PY=3.11 sandbox/ >/dev/null
sudo docker compose up -d --build
for i in $(seq 1 60); do
  curl -fsS localhost/api/health >/dev/null 2>&1 && break
  sleep 2
done
curl -fsS localhost/api/config | head -c 200; echo
REMOTE

HOST="${TARGET#*@}"
echo "==> live at http://$HOST/"
