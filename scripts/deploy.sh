#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Engine and Docker Compose v2 are required."
  exit 1
fi
if [ ! -f .env ]; then
  umask 077
  monitor_token=$(openssl rand -hex 32)
  printf 'MONITOR_API_TOKEN=%s\nMONITOR_PORT=8080\nASTRBOT_API_KEY=\nASTRBOT_BASE_URL=\n' "$monitor_token" > .env
  echo "Created .env with a random management token."
fi
docker compose up -d --build
echo "Service started. Inspect health with: docker compose ps"
echo "Management token is in .env. The service listens on 127.0.0.1:8080 by default."
