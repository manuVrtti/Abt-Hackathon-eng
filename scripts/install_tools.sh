#!/usr/bin/env bash
# Install the optional analysis tools on the event server (Ubuntu 22.04/24.04).
# Every tool is optional: the engine records which ones ran and skips the rest
# for all teams alike (secrets and line counts fall back to built-in versions).
# Check the release pages for the current versions before the event.
set -euo pipefail

GITLEAKS_VERSION="${GITLEAKS_VERSION:-8.21.2}"
SCC_VERSION="${SCC_VERSION:-3.4.0}"
JPLAG_VERSION="${JPLAG_VERSION:-5.1.0}"
PREFIX="${PREFIX:-/usr/local/bin}"

sudo apt-get update
sudo apt-get install -y git docker.io openjdk-17-jre-headless nodejs npm postgresql curl tar

pip install --upgrade semgrep ruff
sudo npm install -g eslint

curl -sSL "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" \
  | sudo tar -xz -C "$PREFIX" gitleaks
curl -sSL "https://github.com/boyter/scc/releases/download/v${SCC_VERSION}/scc_Linux_x86_64.tar.gz" \
  | sudo tar -xz -C "$PREFIX" scc
sudo mkdir -p /opt/jplag
sudo curl -sSL -o /opt/jplag/jplag.jar \
  "https://github.com/jplag/JPlag/releases/download/v${JPLAG_VERSION}/jplag-${JPLAG_VERSION}-jar-with-dependencies.jar"

# Playwright for UI checks of the top teams.
pip install "playwright>=1.45" && python -m playwright install --with-deps chromium

# Sandbox network: containers on it cannot reach the internet.
sudo docker network inspect hackeval-internal >/dev/null 2>&1 || sudo docker network create --internal hackeval-internal

echo "Done. Set plagiarism.jplag_jar: /opt/jplag/jplag.jar in hackeval.yaml to enable JPlag confirmation."
